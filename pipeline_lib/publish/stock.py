"""Ready-stock helpers for the draft GitHub release `vc-ready`."""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_lib.util import load_json, now_local, run, save_json

MANIFEST_NAME = "manifest.json"
STOCK_TAG = "vc-ready"


def video_record(v: dict) -> dict:
    vid = Path(v.get("path") or "")
    preview = Path(v.get("preview") or "")
    rec = {
        "id": v["id"],
        "platform": v.get("platform"),
        "category": v.get("category"),
        "file": vid.name or f"{v['id']}.mp4",
        "sidecar": (vid.name + ".json") if vid.name else f"{v['id']}.mp4.json",
        "preview": f"{vid.stem}.preview{preview.suffix}" if preview.suffix or v.get("preview") else None,
        "title": v.get("title"),
        "hook": v.get("hook"),
        "description": v.get("description"),
        "hashtags": v.get("hashtags") or [],
        "created": v.get("created"),
        "duration": v.get("duration"),
        "size_mb": v.get("size_mb"),
        "song": v.get("song"),
        "song_credit": v.get("song_credit"),
        "sources": v.get("sources") or [],
    }
    return rec


def upsert_manifest(path: Path, records: list[dict]) -> dict:
    man = load_json(path, {"videos": []}) or {"videos": []}
    by = {x["id"]: x for x in (man.get("videos") or []) if x.get("id")}
    for r in records:
        if not r.get("id"):
            continue
        by[r["id"]] = {**by.get(r["id"], {}), **r}
    man["videos"] = sorted(by.values(), key=lambda x: x.get("created") or x.get("id") or "")
    man["updated"] = now_local().isoformat()
    man["tag"] = STOCK_TAG
    save_json(path, man)
    return man


def ensure_release(repo: str, tag: str = STOCK_TAG) -> None:
    r = run(["gh", "release", "view", tag, "--repo", repo], check=False, timeout=60)
    if r.returncode == 0:
        return
    notes = (
        "DRAFT: Vice Cartel ready stock (unposted renders). Keep this release "
        "draft so footage stays private. The upload workflow reads from here. "
        "Never publish this release."
    )
    run(["gh", "release", "create", tag, "--repo", repo, "--draft",
         "--title", "Vice Cartel ready stock", "--notes", notes], timeout=60)


def item_from_sidecar(path: Path) -> dict | None:
    d = load_json(path, None)
    if not isinstance(d, dict) or not d.get("id"):
        return None
    mp4 = path.with_suffix("") if path.name.endswith(".mp4.json") else path.with_suffix(".mp4")
    # sidecar is named {id}.mp4.json → with_suffix('') would drop only .json giving {id}.mp4
    if path.name.endswith(".mp4.json"):
        mp4 = path.with_name(path.name[: -len(".json")])
    d = dict(d)
    if mp4.exists():
        d["path"] = str(mp4)
    d.setdefault("sidecar", str(path))
    return d


def load_stock_dir(stock_dir: Path) -> list[dict]:
    items, seen = [], set()
    for p in sorted(stock_dir.glob("*.mp4.json")):
        it = item_from_sidecar(p)
        if it and it["id"] not in seen:
            items.append(it)
            seen.add(it["id"])
    man = load_json(stock_dir / MANIFEST_NAME, None)
    if man:
        for rec in man.get("videos") or []:
            if rec.get("id") in seen:
                # fill missing fields from manifest
                for it in items:
                    if it["id"] == rec["id"]:
                        for k, v in rec.items():
                            it.setdefault(k, v)
                        break
                continue
            mp4 = stock_dir / rec["file"] if rec.get("file") else None
            rec = dict(rec)
            if mp4 and mp4.exists():
                rec["path"] = str(mp4)
            side = stock_dir / rec["sidecar"] if rec.get("sidecar") else None
            if side and side.exists():
                extra = load_json(side, {}) or {}
                rec = {**extra, **rec}
                rec.setdefault("sidecar", str(side))
            items.append(rec)
            seen.add(rec["id"])
    items.sort(key=lambda x: x.get("created") or x.get("id") or "")
    return items


def unposted_for(items: list[dict], platform: str, posted_ids: set[str]) -> list[dict]:
    out = []
    for it in items:
        if it.get("id") in posted_ids:
            continue
        if (it.get("platform") or "") != platform:
            continue
        if it.get("state") in {"qa_failed", "rejected", "posted"}:
            continue
        out.append(it)
    return out


def counts(items: list[dict], posted_ids: set[str]) -> dict[str, int]:
    return {
        "youtube": len(unposted_for(items, "youtube", posted_ids)),
        "instagram": len(unposted_for(items, "instagram", posted_ids)),
    }


def pick_next(items: list[dict], platform: str, posted_ids: set[str]) -> dict | None:
    ready = unposted_for(items, platform, posted_ids)
    return ready[0] if ready else None


def list_release_assets(repo: str, tag: str = STOCK_TAG) -> list[dict]:
    r = run(["gh", "release", "view", tag, "--repo", repo, "--json", "assets,isDraft,tagName"],
            check=False, timeout=60)
    if r.returncode != 0:
        return []
    try:
        return (json.loads(r.stdout) or {}).get("assets") or []
    except Exception:
        return []


def download_assets(repo: str, tag: str, dest: Path, names: list[str]) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name in names:
        run(["gh", "release", "download", tag, "--repo", repo, "--dir", str(dest),
             "--pattern", name, "--clobber"], check=False, timeout=600)
