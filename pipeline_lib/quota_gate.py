"""Gemini quota gate: is every key AND every backup model out of quota right now?

Reads the per-platform cool-down files the produce runs save on branch pipeline-state
(`youtube/gemini_quota.json`, `instagram/gemini_quota.json`: key labels + timestamps only, never keys)
and answers without calling Gemini (no quota spent). Used:
- by produce.yml's plan job: blocked -> the run ends in about a minute with a clear notice (no 2 h of
  attempts that cannot produce anything);
- by the watchdog (autoproduce): blocked -> no dispatch until the daily reset (~13:30 IST).

    python -m pipeline_lib.quota_gate check     # writes blocked=/until= to $GITHUB_OUTPUT
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime

from .util import log, now_local, run

REPO = os.environ.get("GITHUB_REPOSITORY", "Bhupi820970/pro-edit")
PLATFORMS = ("youtube", "instagram")


def key_labels_from_env() -> list[str]:
    labs = [f"key{i}" for i in range(1, 21) if os.environ.get(f"GEMINI_API_KEY_{i}")]
    if not labs and os.environ.get("GEMINI_API_KEY"):
        labs = ["key"]
    return labs


def _merge(docs: list[dict], now: float) -> tuple[dict[str, float], dict[str, float]]:
    dead: dict[str, float] = {}
    invalid: dict[str, float] = {}
    for d in docs:
        for name, dst in (("dead_until", dead), ("invalid", invalid)):
            for lab, ts in ((d or {}).get(name) or {}).items():
                try:
                    t = float(ts)
                except (TypeError, ValueError):
                    continue
                if t > now:
                    dst[lab] = max(dst.get(lab, 0.0), t)
    return dead, invalid


def blocked_until(docs: list[dict], keys: list[str] | None, models: list[str],
                  now: float | None = None) -> float | None:
    """Epoch when the first (key, model) pair is usable again if EVERY pair is blocked now, else None.
    keys=None -> the key labels named in the files (watchdog has no key env)."""
    now = time.time() if now is None else now
    dead, invalid = _merge(docs, now)
    if keys is None:
        keys = sorted({lab.split("|", 1)[0] for lab in list(dead) + list(invalid)})
    if not keys or not models:
        return None
    first_free = None
    for k in keys:
        for m in models:
            t = max(invalid.get(k, 0.0), dead.get(k, 0.0), dead.get(f"{k}|{m}", 0.0))
            if t <= now:
                return None            # at least one pair can be tried
            first_free = t if first_free is None else min(first_free, t)
    return first_free


def fetch_docs(repo: str = REPO) -> list[dict]:
    docs = []
    for p in PLATFORMS:
        r = run(["gh", "api", "-H", "Accept: application/vnd.github.raw",
                 f"repos/{repo}/contents/{p}/gemini_quota.json?ref=pipeline-state"], timeout=30, check=False)
        try:
            d = json.loads(r.stdout) if r.returncode == 0 else None
        except ValueError:
            d = None
        if isinstance(d, dict):
            docs.append(d)
    return docs


def check(keys: list[str] | None = None, docs: list[dict] | None = None) -> tuple[bool, str]:
    from .gemini import DEFAULT_MODELS
    docs = fetch_docs() if docs is None else docs
    until = blocked_until(docs, keys, list(DEFAULT_MODELS))
    if until is None:
        return False, ""
    return True, datetime.fromtimestamp(until, now_local().tzinfo).strftime("%Y-%m-%d %H:%M IST")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] != "check":
        print(__doc__)
        return 2
    keys = key_labels_from_env() or None
    blocked, until = check(keys)
    msg = (f"Gemini quota is out on every key and every backup model until {until}; "
           "no produce attempts this run (they cannot vet clips or pass QA) - the watchdog waits for the reset"
           if blocked else "Gemini quota available")
    log("quota gate:", msg)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"blocked={'true' if blocked else 'false'}\nuntil={until}\n")
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a") as f:
            f.write(f"### Gemini quota gate\n\n{msg}\n")
    if blocked:
        print(f"::warning title=Gemini quota out::{msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
