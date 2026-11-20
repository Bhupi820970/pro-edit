"""Posted-video ledger committed on main. A video id is never uploaded twice."""
from __future__ import annotations

from pathlib import Path

from pipeline_lib.util import REPO, load_json, now_local, save_json

LEDGER_PATH = REPO / "ledger" / "posted.json"


def load(path: Path | None = None) -> dict:
    p = path or LEDGER_PATH
    d = load_json(p, {}) or {}
    d.setdefault("version", 1)
    d.setdefault("updated", None)
    d.setdefault("posted", [])
    if not isinstance(d["posted"], list):
        d["posted"] = []
    return d


def save(d: dict, path: Path | None = None) -> None:
    d = dict(d)
    d["updated"] = now_local().isoformat()
    save_json(path or LEDGER_PATH, d)


def posted_ids(d: dict | None = None, path: Path | None = None) -> set[str]:
    d = d if d is not None else load(path)
    return {str(e.get("id")) for e in d.get("posted") or [] if e.get("id")}


def entry_for(d: dict, video_id: str) -> dict | None:
    for e in d.get("posted") or []:
        if e.get("id") == video_id:
            return e
    return None


def is_posted(d: dict, video_id: str, platform: str | None = None) -> bool:
    e = entry_for(d, video_id)
    if not e:
        return False
    if platform is None:
        return True
    return e.get("platform") == platform or bool(e.get(platform))


def record(d: dict, entry: dict) -> dict:
    """Insert or merge a ledger row keyed by video id. Never drops earlier fields."""
    if not entry.get("id"):
        raise ValueError("ledger entry needs id")
    now = now_local().isoformat()
    entry = {**entry, "updated_at": now}
    entry.setdefault("posted_at", now)
    rows = list(d.get("posted") or [])
    for i, e in enumerate(rows):
        if e.get("id") == entry["id"]:
            rows[i] = {**e, **entry}
            d["posted"] = rows
            return rows[i]
    rows.append(entry)
    d["posted"] = rows
    return entry
