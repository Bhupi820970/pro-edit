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


def due_slot(conf: dict, led: dict | None = None, now=None) -> str | None:
    """Earliest IST slot today that has already started and is not in the ledger.

    Used so a frequent GitHub cron (or repository_dispatch) can catch up missed
    slots without posting more than once per slot. Returns 'YYYY-MM-DDTHH:MM' or None.
    """
    now = now or now_local()
    day = now.strftime("%Y-%m-%d")
    slotted, yt_n, ig_n = set(), 0, 0
    for e in (led or {}).get("posted") or []:
        if not isinstance(e, dict):
            continue
        sl = str(e.get("slot") or "")
        if sl.startswith(day):
            slotted.add(sl)
            continue
        ts = str(e.get("posted_at") or e.get("updated_at") or "")
        if ts.startswith(day):
            if e.get("platform") == "youtube":
                yt_n += 1
            elif e.get("platform") == "instagram":
                ig_n += 1
    used = len(slotted) + max(yt_n, ig_n)
    elapsed = []
    for raw in conf.get("slots_ist") or []:
        try:
            hh, mm = map(int, str(raw).split(":"))
        except (TypeError, ValueError):
            continue
        t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now >= t:
            elapsed.append(t.strftime("%Y-%m-%dT%H:%M"))
    # already-consumed slots (named or anonymous) map to the earliest elapsed ones
    remaining = [k for k in elapsed if k not in slotted]
    skip = max(0, used - len(slotted))
    remaining = remaining[skip:]
    if not remaining:
        return None
    if conf.get("catch_up_missed"):
        return remaining[0]
    # Default: only the CURRENT slot (latest one already started) is due. A slot missed earlier in the
    # day (no stock / GitHub dropped the cron) is skipped instead of burst-posting several videos in a
    # row, so fresh stock is kept for the next slot (e.g. 21:30) at the normal spacing.
    return remaining[-1] if remaining[-1] == elapsed[-1] else None
