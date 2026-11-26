"""Keep ~7 unposted videos per platform: dispatch produce.yml (3-4 videos) when a platform drops below 3.

Runs in the watchdog workflow (schedule + box kick) and after each upload. It never stacks runs:
- no dispatch while any produce run is queued / in progress (plus produce.yml's `produce-auto` concurrency group)
- cooldown 60 min after the last auto run, 3 h after two auto runs in a row produced nothing
- no dispatch while Gemini is known to be out on every key and every backup model (until the daily reset;
  status.json or the saved gemini_quota.json cool-downs, see quota_gate.py)
Instagram has priority (it empties first): while it is low, the auto run is Instagram-only.

    python -m pipeline_lib.autoproduce check [--dispatch]
    python -m pipeline_lib.autoproduce count --platform instagram     # videos this auto job should make
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os

from .util import log, now_local, run

REPO = os.environ.get("GITHUB_REPOSITORY", "Bhupi820970/pro-edit")
TARGET, LOW, MIN_N, MAX_N = 7, 3, 3, 4
AUTO_TITLE = "produce (auto buffer refill)"
COOLDOWN_MIN, BACKOFF_MIN = 60, 180


def _ts(raw: str) -> dt.datetime:
    return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))


def produce_runs(limit: int = 20) -> list[dict]:
    r = run(["gh", "run", "list", "--repo", REPO, "--workflow", "produce.yml", "-L", str(limit), "--json",
             "databaseId,status,conclusion,createdAt,displayTitle,event"], timeout=60, check=False)
    try:
        return json.loads(r.stdout) if r.returncode == 0 else []
    except Exception:
        return []


def gemini_out() -> tuple[bool, str]:
    """(True, reset time) if the last produce runs saw every key AND every backup model out of quota."""
    r = run(["gh", "api", "-H", "Accept: application/vnd.github.raw",
             f"repos/{REPO}/contents/status.json?ref=pipeline-state"], timeout=30, check=False)
    try:
        st = json.loads(r.stdout)
    except Exception:
        return False, ""
    outs = []
    for p in (st.get("platforms") or {}).values():
        g = ((p or {}).get("last_run") or {}).get("gemini") or {}
        if not g:
            continue
        reset = g.get("reset_at") or ""
        try:
            future = dt.datetime.strptime(reset.replace(" IST", ""), "%Y-%m-%d %H:%M").replace(
                tzinfo=now_local().tzinfo) > now_local()
        except Exception:
            future = False
        outs.append((bool(g.get("exhausted")) and future, reset))
    if outs and all(o for o, _ in outs):
        return True, outs[0][1]
    # the saved per key+model cool-downs are the source of truth (also when a run was quota-gated or
    # cancelled before it wrote status.json)
    try:
        from .quota_gate import check as gate_check
        return gate_check()
    except Exception:
        return False, ""


def stock() -> dict[str, int]:
    from .publish.archive_posted import stock_status
    st = stock_status(REPO)
    return {p: v["unposted"] for p, v in st["platforms"].items() if p in ("youtube", "instagram")}


def decide(counts: dict[str, int], runs: list[dict], quota_out: tuple[bool, str], now=None) -> dict:
    now = now or now_local()
    low = [p for p in ("instagram", "youtube") if counts.get(p, 0) < LOW]   # Instagram first
    if not low:
        return {"dispatch": False, "reason": f"stock ok {counts}"}
    if any(r.get("status") in ("queued", "in_progress", "waiting", "pending", "requested") for r in runs):
        return {"dispatch": False, "reason": "a produce run is already queued/running"}
    if quota_out[0]:
        return {"dispatch": False, "reason": f"Gemini out on all keys and backup models until {quota_out[1]}"}
    auto = [r for r in runs if r.get("displayTitle") == AUTO_TITLE]
    if auto:
        age = (now - _ts(auto[0]["createdAt"]).astimezone(now.tzinfo)).total_seconds() / 60
        if age < COOLDOWN_MIN:
            return {"dispatch": False, "reason": f"cooldown: last auto run {age:.0f} min ago"}
        bad = [r for r in auto[:2] if r.get("conclusion") in ("failure", "timed_out", "startup_failure")]
        if len(bad) == 2 and age < BACKOFF_MIN:
            return {"dispatch": False, "reason": f"back-off: last 2 auto runs failed ({age:.0f} min ago)"}
    # Both low -> one run, both jobs in parallel (matrix lists Instagram first; YouTube makes the
    # smaller batch, see count_for). Only one low -> just that platform.
    platform = "both" if len(low) == 2 else low[0]
    return {"dispatch": True, "platform": platform, "low": low, "counts": counts,
            "reason": f"stock below {LOW}: {counts}"}


def count_for(platform: str, counts: dict[str, int] | None = None) -> int:
    counts = counts if counts is not None else stock()
    need = TARGET - counts.get(platform, 0)
    n = max(MIN_N, min(MAX_N, need))
    if platform == "youtube" and counts.get("instagram", TARGET) < LOW:
        n = MIN_N                       # Instagram priority: YouTube makes the smaller batch
    return max(0, n) if need > 0 else 0


def check(dispatch: bool) -> dict:
    counts = stock()
    d = decide(counts, produce_runs(), gemini_out())
    log("auto-produce:", json.dumps(d))
    if d["dispatch"] and dispatch:
        r = run(["gh", "workflow", "run", "produce.yml", "--repo", REPO, "--ref", "main",
                 "-f", f"platform={d['platform']}", "-f", "mode=produce", "-f", "count=4", "-f", "auto=true",
                 "-f", "max_minutes=60"], timeout=60, check=False)
        d["dispatched"] = r.returncode == 0
        log("auto-produce dispatch:", "ok" if d["dispatched"] else (r.stderr or "")[-200:])
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if out:
        with open(out, "a") as f:
            f.write(f"### Auto-produce\n\n`{json.dumps(d)}`\n")
    return d


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "count"])
    ap.add_argument("--dispatch", action="store_true")
    ap.add_argument("--platform", default="instagram")
    a = ap.parse_args(argv)
    if a.cmd == "count":
        print(count_for(a.platform))
        return 0
    check(a.dispatch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
