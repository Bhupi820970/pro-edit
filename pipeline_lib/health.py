"""One GitHub issue (label `health`) only when something is wrong; closed automatically on recovery.

Alerts: an IST upload slot missed (>60 min past, nothing posted on a platform), ready stock 0 on a
platform, the last 2 finished produce runs failed, a YouTube/IG token expiring within 7 days (or broken).
Issue text holds counts, ids and secret NAMES only, never values. Nothing reads issue content back.

    python -m pipeline_lib.health check [--apply]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import urllib.parse
import urllib.request

from .util import log, now_local, run

REPO = os.environ.get("GITHUB_REPOSITORY", "Bhupi820970/pro-edit")
LABEL = "health"
TITLE = "Vice Cartel health alert"
WARN_DAYS = 7


def missed_slots(led: dict, slots: list[str], now=None, grace_min: int = 60) -> list[str]:
    """Today's slots that ended more than grace_min ago with no live post on a platform."""
    now = now or now_local()
    day = now.strftime("%Y-%m-%d")
    have = {(str(r.get("slot")), r.get("platform")) for r in led.get("posted") or []
            if r.get("status") not in ("failed", "abandoned", "posting")}
    out = []
    for raw in slots:
        hh, mm = map(int, raw.split(":"))
        t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if (now - t).total_seconds() / 60 < grace_min:
            continue
        key = f"{day}T{raw}"
        miss = [p for p in ("youtube", "instagram") if (key, p) not in have]
        if miss:
            out.append(f"{raw} IST ({', '.join(miss)})")
    return out[-3:]


def produce_failed_twice(runs: list[dict]) -> bool:
    done = [r for r in runs if r.get("status") == "completed" and r.get("conclusion") not in ("cancelled", "skipped")]
    return len(done) >= 2 and all(r.get("conclusion") in ("failure", "timed_out") for r in done[:2])


def ig_token_days_left() -> float | None:
    """Days until the IG token expires (None = never / unknown). Uses the token on itself (debug_token)."""
    tok = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    if not tok:
        return None
    from .publish import secrets as S
    S.remember(tok)
    url = "https://graph.facebook.com/debug_token?" + urllib.parse.urlencode({"input_token": tok, "access_token": tok})
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            d = (json.loads(r.read().decode()) or {}).get("data") or {}
    except Exception:
        return None
    if d.get("is_valid") is False:
        return 0.0
    exp = int(d.get("expires_at") or 0)
    return None if exp == 0 else (exp - now_local().timestamp()) / 86400


def yt_token_ok() -> bool | None:
    if not os.environ.get("YT_REFRESH_TOKEN"):
        return None
    try:
        from .publish import youtube
        youtube.access_token_from_env()
        return True
    except Exception as e:
        return "invalid_grant" not in str(e) and None    # invalid_grant = revoked/expired refresh token


def problems(counts: dict, led: dict, slots: list[str], runs: list[dict], ig_days, yt_ok, now=None) -> list[str]:
    out = []
    for m in missed_slots(led, slots, now):
        out.append(f"Upload slot missed: {m}")
    for p, n in counts.items():
        if n == 0:
            out.append(f"Ready stock is 0 on {p}")
    if produce_failed_twice(runs):
        out.append("The last 2 produce runs failed")
    if ig_days is not None and ig_days <= WARN_DAYS:
        out.append(f"Instagram token expires in {max(ig_days, 0):.1f} days: renew secret IG_ACCESS_TOKEN"
                   + (" (auto-refresh needs secrets IG_APP_ID, IG_APP_SECRET, GH_SECRETS_PAT)"
                      if not os.environ.get("GH_SECRETS_PAT") else ""))
    if yt_ok is False:
        out.append("YouTube refresh token no longer works: re-authorise and update secret YT_REFRESH_TOKEN")
    return out


def refresh_ig_token(days_left) -> str:
    """Exchange the long-lived token for a new one and store it as the IG_ACCESS_TOKEN secret.
    Needs IG_APP_ID + IG_APP_SECRET (Graph refresh) and GH_SECRETS_PAT (a token allowed to write secrets);
    the workflow token cannot write secrets. The token is passed on stdin, never printed or logged."""
    if days_left is None or days_left > WARN_DAYS:
        return "not needed"
    need = [n for n in ("IG_APP_ID", "IG_APP_SECRET", "GH_SECRETS_PAT") if not os.environ.get(n)]
    if need:
        return "skipped: missing secrets " + ", ".join(need)
    from .publish import secrets as S
    q = urllib.parse.urlencode({"grant_type": "fb_exchange_token", "client_id": os.environ["IG_APP_ID"],
                                "client_secret": os.environ["IG_APP_SECRET"],
                                "fb_exchange_token": os.environ["IG_ACCESS_TOKEN"]})
    try:
        with urllib.request.urlopen("https://graph.facebook.com/oauth/access_token?" + q, timeout=30) as r:
            new = (json.loads(r.read().decode()) or {}).get("access_token") or ""
    except Exception as e:
        return "refresh failed: " + S.redact(str(e))[:120]
    if not new:
        return "refresh returned no token"
    S.remember(new)
    import subprocess
    env = {**os.environ, "GH_TOKEN": os.environ["GH_SECRETS_PAT"]}
    r = subprocess.run(["gh", "secret", "set", "IG_ACCESS_TOKEN", "--repo", REPO], input=new, text=True,
                       capture_output=True, env=env, timeout=60)
    return "refreshed" if r.returncode == 0 else "secret update failed: " + S.redact(r.stderr)[:120]


def apply_issue(probs: list[str]) -> str:
    r = run(["gh", "issue", "list", "--repo", REPO, "--label", LABEL, "--state", "open", "--json", "number",
             "-L", "5"], timeout=60, check=False)
    try:
        open_ = [i["number"] for i in json.loads(r.stdout)] if r.returncode == 0 else []
    except Exception:
        open_ = []
    if not probs:
        for n in open_:
            run(["gh", "issue", "close", str(n), "--repo", REPO, "--comment",
                 f"Recovered at {now_local().strftime('%Y-%m-%d %H:%M')} IST: all checks OK."], timeout=60, check=False)
        return f"ok (closed {open_})" if open_ else "ok (silent)"
    body = "\n".join([f"Updated {now_local().strftime('%Y-%m-%d %H:%M')} IST by the watchdog workflow.", ""]
                     + [f"- {p}" for p in probs] + ["", "Closes automatically when everything is OK again."])
    if open_:
        run(["gh", "issue", "edit", str(open_[0]), "--repo", REPO, "--body", body], timeout=60, check=False)
        return f"updated #{open_[0]}"
    run(["gh", "label", "create", LABEL, "--repo", REPO, "--color", "B60205", "--force"], timeout=60, check=False)
    r = run(["gh", "issue", "create", "--repo", REPO, "--title", TITLE, "--label", LABEL, "--body", body],
            timeout=60, check=False)
    return "opened " + (r.stdout or "").strip()[-60:]


def check(apply: bool) -> dict:
    from .autoproduce import produce_runs, stock
    from .publish import cfg as cfgmod, ledger
    counts = stock()
    led = ledger.fetch_remote(REPO) or ledger.load(None)
    days = ig_token_days_left()
    probs = problems(counts, led, cfgmod.load().get("slots_ist") or [], produce_runs(10), days, yt_token_ok())
    res = {"problems": probs, "stock": counts,
           "ig_token_days_left": None if days is None else round(days, 1),
           "ig_refresh": refresh_ig_token(days) if apply else "dry"}
    if apply:
        res["issue"] = apply_issue(probs)
    log("health:", json.dumps(res))
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if out:
        with open(out, "a") as f:
            f.write("### Health\n\n```\n" + json.dumps(res, indent=1) + "\n```\n")
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check"])
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    check(a.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
