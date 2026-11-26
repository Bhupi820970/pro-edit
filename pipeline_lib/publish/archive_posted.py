"""Move already-posted videos out of the `vc-ready` stock into the draft release `vc-posted-archive`.

The posting ledger (ledger/posted.json on main) is the source of truth. For every vc-ready video whose id
is in the ledger, its .mp4, .preview.mp4 and .mp4.json are copied to the archive release first, the
copy is verified (same names + sizes), and only then are they removed from vc-ready. Nothing is ever
deleted without a verified archive copy.

    python -m pipeline_lib.publish.archive_posted status            # stock per platform (posted / unposted)
    python -m pipeline_lib.publish.archive_posted move [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

from pipeline_lib.util import log, now_local

STOCK_TAG = "vc-ready"
ARCHIVE_TAG = "vc-posted-archive"
PREFIX = {"yo-": "youtube", "in-": "instagram"}
SUFFIXES = (".mp4", ".preview.mp4", ".mp4.json")


def _gh(args: list[str], timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
    from pipeline_lib.util import run
    r = run(["gh", *args], timeout=timeout, check=False)   # retried when the command is a safe read/upload
    if check and r.returncode != 0:
        raise RuntimeError(f"gh {args[0]} {args[1] if len(args) > 1 else ''} failed: {r.stderr.strip()[:200]}")
    return r


def assets(repo: str, tag: str) -> dict[str, int]:
    r = _gh(["release", "view", tag, "--repo", repo, "--json", "assets"], timeout=60, check=False)
    if r.returncode != 0:
        return {}
    return {a["name"]: int(a.get("size") or 0) for a in (json.loads(r.stdout) or {}).get("assets") or []}


def video_ids(names) -> dict[str, str]:
    """{video id: platform} for the main .mp4 files in a release asset list."""
    out = {}
    for n in names:
        if not n.endswith(".mp4") or n.endswith(".preview.mp4"):
            continue
        vid = n[:-4]
        out[vid] = next((p for pre, p in PREFIX.items() if vid.startswith(pre)), "other")
    return out


def posted_rows(repo: str) -> list[dict] | None:
    from pipeline_lib.publish.ledger import fetch_remote
    d = fetch_remote(repo)
    return None if d is None else [r for r in d.get("posted") or [] if r.get("status") == "posted"]


def stock_status(repo: str, names=None, rows=None) -> dict:
    names = assets(repo, STOCK_TAG) if names is None else names
    rows = posted_rows(repo) if rows is None else rows
    posted = {(r.get("id"), r.get("platform")) for r in rows or []}
    out = {}
    for vid, plat in sorted(video_ids(names).items()):
        p = out.setdefault(plat, {"in_stock": 0, "posted_still_in_stock": 0, "unposted": 0, "unposted_ids": []})
        p["in_stock"] += 1
        if (vid, plat) in posted:
            p["posted_still_in_stock"] += 1
        else:
            p["unposted"] += 1
            p["unposted_ids"].append(vid)
    for plat in ("youtube", "instagram"):
        out.setdefault(plat, {"in_stock": 0, "posted_still_in_stock": 0, "unposted": 0, "unposted_ids": []})
        out[plat]["posted_total"] = sum(1 for r in rows or [] if r.get("platform") == plat)
    return {"updated": now_local().isoformat(), "ledger_readable": rows is not None, "platforms": out}


def ensure_archive(repo: str) -> None:
    if _gh(["release", "view", ARCHIVE_TAG, "--repo", repo], timeout=60, check=False).returncode == 0:
        return
    _gh(["release", "create", ARCHIVE_TAG, "--repo", repo, "--draft", "--title", "Vice Cartel posted archive",
         "--notes", "DRAFT: videos that were already posted, moved out of vc-ready stock. Never publish."],
        timeout=60)


def move(repo: str, dry_run: bool = False) -> list[str]:
    rows = posted_rows(repo)
    if rows is None:
        log("archive: ledger on main unreadable -> nothing moved")
        return []
    posted = {(r.get("id"), r.get("platform")) for r in rows}
    stock = assets(repo, STOCK_TAG)
    todo = [vid for vid, plat in video_ids(stock).items() if (vid, plat) in posted]
    if not todo:
        log("archive: no posted video left in vc-ready")
        return []
    if dry_run:
        log("archive (dry run) would move:", ", ".join(sorted(todo)))
        return sorted(todo)
    ensure_archive(repo)
    moved = []
    for vid in sorted(todo):
        names = [vid + s for s in SUFFIXES if vid + s in stock]
        with tempfile.TemporaryDirectory(prefix="vcarch-") as td:
            for n in names:
                _gh(["release", "download", STOCK_TAG, "--repo", repo, "--dir", td, "--pattern", n, "--clobber"],
                    timeout=900)
            if any((Path(td) / n).stat().st_size != stock[n] for n in names):
                log(f"archive: {vid} download size mismatch -> kept in vc-ready")
                continue
            _gh(["release", "upload", ARCHIVE_TAG, "--repo", repo, "--clobber", *[str(Path(td) / n) for n in names]],
                timeout=1800)
        arch = assets(repo, ARCHIVE_TAG)
        if any(arch.get(n) != stock[n] for n in names):
            log(f"archive: {vid} archive copy not verified -> kept in vc-ready")
            continue
        for n in names:
            _gh(["release", "delete-asset", STOCK_TAG, n, "--repo", repo, "-y"], timeout=120)
        moved.append(vid)
        log(f"archive: moved {vid} ({len(names)} files) vc-ready -> {ARCHIVE_TAG}")
    if moved:
        prune_manifest(repo, set(moved))
    return moved


def prune_manifest(repo: str, ids: set[str]) -> None:
    """Drop moved videos from the vc-ready manifest.json (the files are in the archive now)."""
    with tempfile.TemporaryDirectory(prefix="vcman-") as td:
        r = _gh(["release", "download", STOCK_TAG, "--repo", repo, "--dir", td, "--pattern", "manifest.json",
                 "--clobber"], timeout=120, check=False)
        mp = Path(td) / "manifest.json"
        if r.returncode != 0 or not mp.exists():
            return
        man = json.loads(mp.read_text())
        man["videos"] = [v for v in man.get("videos") or [] if v.get("id") not in ids]
        man["updated"] = now_local().isoformat()
        mp.write_text(json.dumps(man, indent=1, ensure_ascii=False))
        _gh(["release", "upload", STOCK_TAG, "--repo", repo, "--clobber", str(mp)], timeout=120, check=False)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["status", "move"])
    ap.add_argument("--repo", default="Bhupi820970/pro-edit")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "move":
        print(json.dumps({"moved": move(a.repo, a.dry_run)}))
    print(json.dumps(stock_status(a.repo), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
