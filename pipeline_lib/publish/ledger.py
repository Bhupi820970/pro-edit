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


# ---------------------------------------------------------------- race guard (two upload runs, one slot)
def merge(a: dict, b: dict | None) -> dict:
    """Union of two ledgers keyed by video id (fields merged, nothing dropped)."""
    out: dict = {}
    out.update({k: v for k, v in (a or {}).items() if k != "posted"})
    rows: dict[str, dict] = {}
    order: list[str] = []
    for src in ((b or {}).get("posted") or [], (a or {}).get("posted") or []):
        for e in src:
            if not isinstance(e, dict) or not e.get("id"):
                continue
            k = str(e["id"])
            if k not in rows:
                order.append(k)
                rows[k] = dict(e)
            else:
                rows[k] = {**rows[k], **e}
    out["version"] = (a or {}).get("version", 1)
    out["posted"] = sorted((rows[k] for k in order), key=lambda e: str(e.get("posted_at") or ""))
    return out


def slot_taken(d: dict, slot: str | None, platform: str) -> dict | None:
    """The row that already used this IST slot on this platform, if any."""
    if not slot:
        return None
    for e in d.get("posted") or []:
        if str(e.get("slot") or "") == slot and e.get("platform") == platform:
            return e
    return None


def fetch_remote(repo: str, ref: str = "main") -> dict | None:
    """Current ledger on GitHub main (plain gh api call, argument list). None if unreadable."""
    import json
    import subprocess
    try:
        r = subprocess.run(["gh", "api", "-H", "Accept: application/vnd.github.raw",
                            f"repos/{repo}/contents/ledger/posted.json?ref={ref}"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return None
        d = json.loads(r.stdout)
        return d if isinstance(d, dict) and isinstance(d.get("posted"), list) else None
    except Exception:
        return None


def _main(argv=None) -> int:
    """python -m pipeline_lib.publish.ledger merge-file --ours ledger/posted.json --theirs other.json"""
    import argparse
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["merge-file"])
    ap.add_argument("--ours", required=True)
    ap.add_argument("--theirs", required=True)
    a = ap.parse_args(argv)
    ours = load(Path(a.ours))
    try:
        theirs = json.loads(Path(a.theirs).read_text())
    except Exception:
        theirs = None
    m = merge(ours, theirs)
    m["updated"] = ours.get("updated")
    save_json(Path(a.ours), m)
    print(f"ledger merged: {len(m['posted'])} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
