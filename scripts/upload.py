#!/usr/bin/env python3
"""Vice Cartel auto-upload CLI (GitHub Actions).

  python scripts/upload.py --mode test          # 1 YouTube + 1 Instagram, ignores SCHEDULE_ENABLED
  python scripts/upload.py --mode slot          # same posts, used by the IST slot cron
  python scripts/upload.py --mode test --dry-run

Secrets (env only, never printed): YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN,
BUFFER_API_KEY. Optional Gemini keys GEMINI_API_KEY_1..13 for captions.
Stock is the draft release `vc-ready`.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from pipeline_lib.publish.runner import run_pass  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=["test", "slot"], default="test")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "Bhupi820970/pro-edit"))
    ap.add_argument("--stock-dir", default=os.environ.get("VC_STOCK_DIR") or "")
    ap.add_argument("--work-dir", default="")
    args = ap.parse_args(argv)
    dry = args.dry_run or os.environ.get("DRY_RUN") == "1"
    out = run_pass(
        mode=args.mode,
        dry_run=dry,
        repo=args.repo,
        stock_dir=Path(args.stock_dir) if args.stock_dir else None,
        work_dir=Path(args.work_dir) if args.work_dir else None,
    )
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
