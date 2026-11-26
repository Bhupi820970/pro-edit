#!/usr/bin/env python3
"""Vice Cartel box disk cleanup — plain data-only script, no AI, runs nothing from repo/queue/issues.

Every 2 days (crontab, see box/cleanup_posted.sh):
  1. read ledger/posted.json from pro/main (git show) -> videos that are really posted
  2. map them to their source clip ids via pipeline-state {youtube,instagram}/state.json (videos[].sources)
  3. record each source id/URL in the used-clips ledger: ledger/used_clips.json (committed as JSON data
     only via the GitHub contents API) AND the box copy <box_fetch>/used_clips.json
  4. delete from the box disk: those source clips (+ their .wav/.tx.json/chunks), renders/previews of the
     posted videos, and temp/work leftovers older than 3 days
Never deletes: the code repo, song libraries, approved samples, manifest/heartbeat files, backup stock
that is not posted yet, clips an unposted ready video still needs, or anything modified in the last 6 h.
Usage: cleanup_posted.py [--dry-run] [--no-commit] [--no-fetch]
All subprocess calls are argument lists (no shell). Output contains paths/sizes only, never secrets.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from pipeline_lib.util import run, log, now_local  # noqa: E402
from pipeline_lib import used_clips as UC  # noqa: E402
from pipeline_lib.cleanup import CleanupConfig, CleanupResult, safe_delete, prune_empty_dirs  # noqa: E402

REPO_SLUG = "Bhupi820970/pro-edit"
REMOTE = "pro"
BOX = Path("/workspace/gta6")
BOX_FETCH = Path(os.environ.get("VICE_BOX_FETCH_DIR", str(BOX / "box_fetch")))
PIPE_HOME = BOX / "pipeline"
PLATFORMS = ("youtube", "instagram")
MIN_AGE_H = 6          # never touch a file newer than this (an in-progress run may be using it)
TEMP_AGE_D = 3         # temp/work leftovers older than this
KEEP_STATES = {"posted", "rejected", "qa_failed"}   # any other state = live/backup stock -> protected
MEDIA_EXT = {".mkv", ".mp4", ".webm", ".mov", ".wav", ".m4a", ".mp3", ".opus", ".part", ".ytdl", ".jpg", ".png"}
ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


def git_show(ref_path: str):
    r = run(["git", "-C", str(HERE), "show", ref_path], check=False, timeout=60)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def collect(now: dt.datetime):
    """-> (new ledger entries {id: entry}, protected ids from unposted stock, posted video ids)."""
    posted_doc = git_show(f"{REMOTE}/main:ledger/posted.json") or {}
    posted = {p["id"]: p for p in posted_doc.get("posted", [])
              if isinstance(p, dict) and p.get("status") == "posted" and p.get("id")}
    entries: dict[str, dict] = {}
    protected: set[str] = set()
    for plat in PLATFORMS:
        st = git_show(f"{REMOTE}/pipeline-state:{plat}/state.json") or {}
        for v in st.get("videos") or []:
            vid = str(v.get("id") or "")
            srcs = [s for s in (v.get("sources") or []) if isinstance(s, dict)]
            ids = {str(s.get("src_id") or s.get("id") or "") for s in srcs}
            if vid in posted:
                p = posted[vid]
                for s in srcs:
                    sid = str(s.get("src_id") or s.get("id") or "")
                    sid = re.sub(r"_c\d+$", "", sid)
                    if not ID_RE.match(sid):
                        continue
                    e = entries.setdefault(sid, {"id": sid, "videos": []})
                    if s.get("url") and not e.get("url"):
                        e["url"] = str(s["url"])
                    e.setdefault("category", v.get("category"))
                    e.setdefault("first_used", p.get("posted_at") or p.get("updated_at") or now.isoformat())
                    if vid not in e["videos"]:
                        e["videos"].append(vid)
            elif v.get("state") not in KEEP_STATES:      # ready / approved / in progress: keep its clips
                protected |= {re.sub(r"_c\d+$", "", i) for i in ids if i}
    return entries, protected, set(posted)


def id_pattern(ids: set[str]):
    if not ids:
        return None
    alt = "|".join(re.escape(i) for i in sorted(ids, key=len, reverse=True))
    return re.compile(rf"^(?:{alt})(?:_c\d+)?(?:\..+)?$")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-commit", action="store_true")
    ap.add_argument("--no-fetch", action="store_true")
    a = ap.parse_args()
    now = now_local()
    lock = HERE / ".pipeline.lock"
    if lock.exists():
        log("cleanup: pipeline lock present (a local run is active) -> skip")
        return 0
    if not a.no_fetch:
        run(["git", "-C", str(HERE), "fetch", "-q", REMOTE, "main", "pipeline-state"], check=False, timeout=180)

    new, protected_ids, posted_ids = collect(now)
    if not posted_ids:
        log("cleanup: no posted videos read from pro/main -> nothing deleted")
        return 1
    ledger = UC.load_entries()
    before = len(ledger)
    for sid, e in new.items():
        cur = ledger.setdefault(sid, {"id": sid, "videos": []})
        for k in ("url", "category", "first_used"):
            if e.get(k) and not cur.get(k):
                cur[k] = e[k]
        for v in e["videos"]:
            if v not in cur["videos"]:
                cur["videos"].append(v)
    log(f"used-clips ledger: {before} -> {len(ledger)} source ids ({len(new)} from {len(posted_ids)} posted videos)")
    # 1) ledger first (a failed write aborts before anything is deleted)
    if not a.dry_run:
        UC.write_ledger(ledger, now_iso=now.isoformat())
        if not a.no_commit:
            commit_ledger()
    # 2) delete
    allow = [HERE / "sources", HERE / "dl", HERE / "cache", HERE / "prev", BOX_FETCH,
             PIPE_HOME / "work", PIPE_HOME / "prev", PIPE_HOME / "ready", BOX / "work"]
    protected = [HERE / "song", BOX_FETCH / "songs", BOX / "demo_src" / "songs", BOX / "out" / "samples",
                 PIPE_HOME / "songs"]
    cfg = CleanupConfig(home=PIPE_HOME, allow=tuple(allow), protected=tuple(protected), box_root=BOX)
    res = CleanupResult()
    free_before = disk_free()
    posted_pat = id_pattern(set(ledger) - protected_ids)          # source clips of posted videos
    render_pat = id_pattern(posted_ids)                           # their renders / previews
    cutoff_new = time.time() - MIN_AGE_H * 3600
    cutoff_tmp = time.time() - TEMP_AGE_D * 86400
    todo: list[tuple[Path, str]] = []
    for root in allow:
        if not root.exists() or root.is_symlink():
            continue
        for dp, dns, fns in os.walk(root, followlinks=False):
            dns[:] = [d for d in dns if not (Path(dp) / d).is_symlink()]
            for fn in fns:
                p = Path(dp) / fn
                try:
                    mt = p.lstat().st_mtime
                except OSError:
                    continue
                if mt > cutoff_new or fn == "used_clips.json":
                    continue
                rel = p.relative_to(root).parts
                if posted_pat and posted_pat.match(fn):
                    todo.append((p, "posted source clip"))
                elif render_pat and render_pat.match(fn):
                    todo.append((p, "posted render/preview"))
                elif mt < cutoff_tmp and (
                        root.name in ("work", "prev", "ready")
                        or (root == HERE / "cache" and (len(rel) > 1 and rel[0] in ("cuts", "fr", "sheets", "elprobe")
                                                          or (len(rel) == 1 and p.suffix.lower() in MEDIA_EXT)))
                        or (root == BOX_FETCH and p.suffix.lower() in (".part", ".ytdl"))):
                    todo.append((p, "temp/work leftover"))
    by_reason: dict[str, int] = {}
    for p, why in todo:
        try:
            size = p.stat().st_size
        except OSError:
            continue
        by_reason[why] = by_reason.get(why, 0) + size
        if a.dry_run:
            res.deleted.append(str(p)); res.freed_bytes += size
        else:
            safe_delete(p, cfg, res)
    if not a.dry_run:
        for root in allow:
            if root not in (BOX_FETCH, HERE / "sources", HERE / "dl", HERE / "cache"):
                prune_empty_dirs(root, cfg, res)
    tag = "DRY-RUN would free" if a.dry_run else "freed"
    log(f"cleanup {tag} {res.freed_bytes/1e9:.3f} GB in {len(res.deleted)} files "
        f"({', '.join(f'{k}: {v/1e9:.3f} GB' for k, v in by_reason.items()) or 'nothing matched'}); "
        f"skipped {len(res.skipped)}; disk free {free_before:.1f} -> {disk_free():.1f} GB")
    for p in res.deleted[:8]:
        log("  -", p)
    return 0


def disk_free() -> float:
    st = os.statvfs(str(BOX))
    return st.f_bavail * st.f_frsize / 1e9


def commit_ledger():
    """Commit ledger/used_clips.json (JSON data only) to main via the GitHub contents API."""
    path = "ledger/used_clips.json"
    data = UC.LEDGER.read_bytes()
    cur = run(["gh", "api", f"repos/{REPO_SLUG}/contents/{path}?ref=main", "--jq", ".sha"], check=False, timeout=60)
    args = ["gh", "api", "-X", "PUT", f"repos/{REPO_SLUG}/contents/{path}",
            "-f", "message=ledger: used source clips (box cleanup, data only)",
            "-f", f"content={base64.b64encode(data).decode()}", "-f", "branch=main"]
    sha = cur.stdout.strip() if cur.returncode == 0 else ""
    if re.fullmatch(r"[0-9a-f]{40}", sha):
        args += ["-f", f"sha={sha}"]
    r = run(args, check=False, timeout=120)
    log("ledger commit:", "ok" if r.returncode == 0 else f"FAILED rc={r.returncode} (box copy is still written)")
    if r.returncode == 0:
        run(["git", "-C", str(HERE), "fetch", "-q", REMOTE, "main"], check=False, timeout=120)


if __name__ == "__main__":
    sys.exit(main())
