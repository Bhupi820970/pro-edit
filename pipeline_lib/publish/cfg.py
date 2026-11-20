"""Load config/publish.json."""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_lib.util import REPO

PATH = REPO / "config" / "publish.json"
DEFAULTS = {
    "timezone": "Asia/Kolkata",
    "slots_ist": ["08:00", "11:30", "15:00", "18:30", "21:30"],
    "stock_release": "vc-ready",
    "stock_warn_below": 3,
    "youtube_channel": "@vicecartelgta",
    "youtube_category_id": "20",
    "instagram_username": "benreelr",
    "retry_once": True,
    "transfer_prefix": "vc-xfer-",
    "protected_releases": ["vc-ready", "sources", "songs", "test-renders"],
}


def load() -> dict:
    data = json.loads(PATH.read_text()) if PATH.is_file() else {}
    out = {**DEFAULTS, **data}
    out["slots_ist"] = list(out.get("slots_ist") or DEFAULTS["slots_ist"])
    out["protected_releases"] = list(out.get("protected_releases") or DEFAULTS["protected_releases"])
    out["stock_warn_below"] = int(out.get("stock_warn_below") or 3)
    return out
