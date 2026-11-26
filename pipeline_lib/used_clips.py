"""Persistent "used clips" no-repeat ledger (plain JSON data, never executed).

Two copies, merged on read:
  * ledger/used_clips.json            committed to git (ids/URLs/video ids only, never media)
  * <box_fetch dir>/used_clips.json   box-local persistent copy (survives the cleanup)

Written by box/cleanup_posted.py (before it deletes anything) from the source ids of videos that
are already posted. Read by box_fetch.py (do not re-download / count) and sourcing.py (do not
pick again). Complements the 3-day `sources_used` cooldown in state.json, which forgets.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .util import REPO

LEDGER = REPO / "ledger" / "used_clips.json"
BOX_COPY = Path(os.environ.get("VICE_BOX_FETCH_DIR") or (Path(os.environ.get("VICE_BOX_ROOT") or Path(__file__).resolve().parents[2]) / "box_fetch")) / "used_clips.json"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


def _read(path: Path) -> list[dict]:
    try:
        d = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return []
    items = d.get("used") if isinstance(d, dict) else None
    return [i for i in (items or []) if isinstance(i, dict) and ID_RE.match(str(i.get("id") or ""))]


def load_entries(paths=None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for p in (paths or (LEDGER, BOX_COPY)):
        for it in _read(p):
            cur = out.setdefault(it["id"], {"id": it["id"], "videos": []})
            for k in ("url", "category", "first_used"):
                if it.get(k) and not cur.get(k):
                    cur[k] = it[k]
            for v in it.get("videos") or []:
                if isinstance(v, str) and v not in cur["videos"]:
                    cur["videos"].append(v)
    return out


def used_ids(paths=None) -> set[str]:
    """Source ids (11-char YouTube ids, without any _cN chunk suffix) that must never be used again."""
    return set(load_entries(paths))


def is_used(sid: str, used: set[str]) -> bool:
    base = re.sub(r"_c\d+$", "", sid or "")
    return base in used or sid in used


def write_ledger(entries: dict[str, dict], paths=None, now_iso: str = "") -> None:
    doc = {"version": 1, "updated": now_iso,
           "note": "source clips already used by posted videos; ids/URLs only, no media",
           "used": [entries[k] for k in sorted(entries)]}
    for p in (paths or (LEDGER, BOX_COPY)):
        p = Path(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
        os.replace(tmp, p)
