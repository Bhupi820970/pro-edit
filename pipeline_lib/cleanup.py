"""Safe disk cleanup after a video is marked posted, plus an age-based sweep.

On a successful YouTube / Instagram post (`pipeline.py mark --state posted`):
  * delete that video's local source clips, work/temp files and the preview
  * keep the final render for `final_keep_hours` (default 24h), then the sweep
    removes it unless the video has a keep flag (`mark --keep` or a sibling
    `.keep` file)

Never deletes code, the songs library, approved samples, backup-buffer videos
(ready / approved / still in progress), or any path that is not inside an
allowlisted data directory after resolve(). No shell; no rm -rf on paths
outside the allowlist.
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .util import REPO, home, log, now_local

# Operational files that live in data dirs and must never be removed.
SKIP_NAMES = {
    "manifest.json", "box_heartbeat.json", "fetch_queue.json",
    "state.json", "status.json", "ALL6_DONE",
}

PENDING_STATES = frozenset({"ready", "approved", "qa_failed"})
DONE_STATES = frozenset({"posted", "rejected"})


def resolve_root(path: Path) -> Path:
    """Resolve an existing path, or join the name onto the resolved parent."""
    path = Path(path)
    if path.exists():
        return path.resolve()
    parts: list[str] = []
    cur = path
    while not cur.exists() and cur != cur.parent:
        if cur.name in ("", ".", ".."):
            break
        parts.append(cur.name)
        cur = cur.parent
    base = cur.resolve()
    for name in reversed(parts):
        if name in ("", ".", ".."):
            raise ValueError(f"refusing unsafe path: {path}")
        base = base / name
    return base


def is_inside(path: Path, root: Path) -> bool:
    """True if `path` (resolved) is `root` or a descendant. Does not follow a final symlink."""
    try:
        target = resolve_root(root)
        # locate the path itself without following a trailing symlink
        if path.exists() or path.is_symlink():
            loc = path.parent.resolve() / path.name
        else:
            loc = resolve_root(path)
        return loc == target or target in loc.parents
    except (OSError, RuntimeError, ValueError):
        return False


def is_inside_resolved(path: Path, root: Path) -> bool:
    """True if the fully resolved target of `path` is inside `root`."""
    try:
        target = resolve_root(root)
        resolved = path.resolve()
        return resolved == target or target in resolved.parents
    except (OSError, RuntimeError, ValueError):
        return False


@dataclass
class CleanupConfig:
    home: Path
    allow: tuple[Path, ...]
    protected: tuple[Path, ...]
    box_root: Path = Path("/workspace/gta6")
    final_keep: dt.timedelta = dt.timedelta(hours=24)
    temp_max_age: dt.timedelta = dt.timedelta(days=3)

    def __post_init__(self):
        self.home = resolve_root(self.home)
        self.box_root = resolve_root(self.box_root)
        self.allow = tuple(resolve_root(p) for p in self.allow)
        self.protected = tuple(resolve_root(p) for p in self.protected)

    @classmethod
    def from_env(cls, pipeline_home: Path | None = None, cfg: dict | None = None) -> "CleanupConfig":
        h = resolve_root(Path(pipeline_home) if pipeline_home is not None else home())
        box = Path("/workspace/gta6")
        clean = (cfg or {}).get("cleanup") or {}
        allow = [
            h / "work", h / "sources", h / "prev", h / "ready",
        ]
        protected = [
            REPO / "song",
            h / "songs",
            box / "demo_src" / "songs",
            box / "out" / "samples",
        ]
        # Extra box-local data dirs only for a real pipeline home, never a test tmp.
        prod = False
        try:
            prod = resolve_root(h) == resolve_root(box / "pipeline") or str(resolve_root(h)).startswith("/mnt/ph/")
        except (OSError, ValueError):
            prod = False
        if prod:
            allow.extend([
                REPO / "dl", REPO / "cache", REPO / "sources",
                box / "work", box / "demo_src" / "work",
            ])
        hours = float(clean.get("final_keep_hours", 24))
        days = float(clean.get("temp_max_days", 3))
        return cls(
            home=h, allow=tuple(allow), protected=tuple(protected), box_root=box,
            final_keep=dt.timedelta(hours=hours), temp_max_age=dt.timedelta(days=days),
        )


@dataclass
class CleanupResult:
    deleted: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    freed_bytes: int = 0

    @property
    def freed_mb(self) -> float:
        return round(self.freed_bytes / 1e6, 1)

    def merge(self, other: "CleanupResult") -> "CleanupResult":
        self.deleted.extend(other.deleted)
        self.skipped.extend(other.skipped)
        self.freed_bytes += other.freed_bytes
        return self


def _is_allowlist_root(path: Path, cfg: CleanupConfig) -> bool:
    loc = path.parent.resolve() / path.name if (path.exists() or path.is_symlink()) else resolve_root(path)
    return any(loc == root for root in cfg.allow)


def _is_protected(path: Path, cfg: CleanupConfig) -> bool:
    loc = path.parent.resolve() / path.name if (path.exists() or path.is_symlink()) else resolve_root(path)
    if path.name in SKIP_NAMES or path.name.endswith(".lock") or path.name.endswith(".keep"):
        return True
    for root in cfg.protected:
        if is_inside(path, root):
            return True
        try:
            if path.exists() and not path.is_symlink() and is_inside_resolved(path, root):
                return True
        except OSError:
            pass
    # Approved GTA / gaming demo samples under out/demos, even if that dir is allowlisted.
    demos = cfg.box_root / "out" / "demos"
    if is_inside(path, demos):
        name = loc.name.lower()
        if name.startswith("gta") or name.startswith("gaming"):
            return True
    return False


def _allowed_here(path: Path, cfg: CleanupConfig) -> bool:
    """The path's own location (symlink, not target) sits inside an allowlisted dir."""
    if _is_allowlist_root(path, cfg):
        return False
    return any(is_inside(path, root) for root in cfg.allow)


def _target_allowed(path: Path, cfg: CleanupConfig) -> bool:
    """The resolved target sits inside an allowlisted dir (not the root itself)."""
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return False
    if any(resolved == root for root in cfg.allow):
        return False
    return any(is_inside_resolved(path, root) for root in cfg.allow)


def _iter_files(root: Path):
    """Yield files under root without following directory symlinks."""
    if not root.exists() or root.is_symlink():
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
        for name in filenames:
            yield Path(dirpath) / name


def _reserved_paths(reserved: Iterable[Path]) -> set[Path]:
    out: set[Path] = set()
    for p in reserved:
        try:
            p = Path(p)
            if p.exists() or p.is_symlink():
                out.add(p.parent.resolve() / p.name)
                if not p.is_symlink():
                    out.add(p.resolve())
                else:
                    try:
                        out.add(p.resolve())
                    except OSError:
                        pass
        except (OSError, RuntimeError):
            continue
    return out


def safe_delete(path: Path, cfg: CleanupConfig, result: CleanupResult,
                reserved: set[Path] | None = None) -> bool:
    """Unlink a file if and only if it lives inside the allowlist and is not protected.

    Symlinks: the link is removed when the link itself is allowlisted. The target
    is removed only when it also resolves inside the allowlist and is not reserved.
    """
    path = Path(path)
    reserved = reserved or set()
    try:
        if not path.exists() and not path.is_symlink():
            return False
        loc = path.parent.resolve() / path.name
        if loc in reserved:
            result.skipped.append((str(path), "reserved"))
            return False
        if not _allowed_here(path, cfg) or _is_protected(path, cfg):
            result.skipped.append((str(path), "not-allowed-or-protected"))
            return False
        is_link = path.is_symlink()
        target = None
        if is_link:
            try:
                target = path.resolve()
            except OSError:
                target = None
        size = 0 if is_link else path.stat().st_size
        path.unlink()
        result.deleted.append(str(loc))
        result.freed_bytes += size
        if (is_link and target is not None and target.exists() and not target.is_dir()
                and _target_allowed(target, cfg) and not _is_protected(target, cfg)):
            tloc = target.parent.resolve() / target.name
            if tloc not in reserved and target.resolve() not in reserved:
                tsize = target.stat().st_size
                target.unlink()
                result.deleted.append(str(tloc))
                result.freed_bytes += tsize
        return True
    except OSError as exc:
        result.skipped.append((str(path), str(exc)))
        return False


def prune_empty_dirs(root: Path, cfg: CleanupConfig, result: CleanupResult):
    """Remove empty directories under an allowlisted root, never the root itself."""
    if not root.exists() or root.is_symlink():
        return
    if not _is_allowlist_root(root, cfg) and not _allowed_here(root, cfg):
        if not any(is_inside(root, a) or resolve_root(root) == a for a in cfg.allow):
            return
    for dirpath, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        p = Path(dirpath)
        if _is_allowlist_root(p, cfg) or _is_protected(p, cfg):
            continue
        if not _allowed_here(p, cfg):
            continue
        try:
            if not any(p.iterdir()):
                p.rmdir()
                result.deleted.append(str(p) + "/")
        except OSError:
            continue


def source_ids(video: dict) -> set[str]:
    ids: set[str] = set()
    for s in video.get("sources") or []:
        vals = []
        if isinstance(s, dict):
            vals = [s.get("id"), s.get("src_id")]
        elif s:
            vals = [s]
        for v in vals:
            if not v:
                continue
            name = str(v).strip()
            if not name or name in (".", "..") or "/" in name or "\\" in name:
                continue
            ids.add(name)
    return ids


def _stem_matches(path: Path, ids: set[str]) -> bool:
    name, stem = path.name, path.stem
    # Path.stem of "id.tx.json" is "id.tx"
    for i in ids:
        if len(i) < 3:
            if stem == i or name == i:
                return True
            continue
        if name == i or name.startswith(i + ".") or stem == i:
            return True
        if stem.startswith(i + "_c") or stem.startswith(i + "."):
            return True
        # chunks: {id}_c0.mp4 already covered; also id.tx.json
        if name.startswith(i + ".") or name.startswith(i + "_"):
            return True
    return False


def source_files_for_video(video: dict, cfg: CleanupConfig) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()

    def add(p: Path):
        try:
            loc = p.parent.resolve() / p.name if (p.exists() or p.is_symlink()) else p
        except OSError:
            loc = p
        if loc in seen:
            return
        seen.add(loc)
        found.append(p)

    for s in video.get("sources") or []:
        if isinstance(s, dict) and s.get("path"):
            p = Path(s["path"])
            if p.exists() or p.is_symlink():
                add(p)
    ids = source_ids(video)
    if not ids:
        return found
    for root in cfg.allow:
        if not root.exists():
            continue
        for p in _iter_files(root):
            if _stem_matches(p, ids):
                add(p)
    return found


def video_outputs(video: dict) -> list[Path]:
    out: list[Path] = []
    for key in ("path", "preview"):
        raw = video.get(key)
        if raw:
            p = Path(raw)
            out.append(p)
            out.append(Path(str(p) + ".json"))
            if p.suffix:
                out.append(p.with_name(p.name + ".json"))
    return out


def has_keep_flag(video: dict) -> bool:
    if video.get("keep"):
        return True
    raw = video.get("path")
    if not raw:
        return False
    p = Path(raw)
    return p.with_suffix(".keep").exists() or Path(str(p) + ".keep").exists()


def pending_videos(state) -> list[dict]:
    return [v for v in (state.d.get("videos") or [])
            if v.get("state") not in DONE_STATES]


def reserved_for_state(state, cfg: CleanupConfig, extra: Sequence[Path] = ()) -> set[Path]:
    paths: list[Path] = list(extra)
    for v in pending_videos(state):
        paths.extend(video_outputs(v))
        paths.extend(source_files_for_video(v, cfg))
    return _reserved_paths(paths)


def work_dir_for(video: dict, cfg: CleanupConfig) -> Path | None:
    vid = str(video.get("id") or "")
    if not vid or "/" in vid or "\\" in vid or vid in (".", ".."):
        return None
    return cfg.home / "work" / vid


def cleanup_posted(video: dict, state, cfg: CleanupConfig | None = None,
                   keep: bool | None = None, now: dt.datetime | None = None) -> CleanupResult:
    """Delete source clips + temp/work/preview for a posted video. Final stays 24h unless swept."""
    del now  # reserved for API symmetry with sweep
    cfg = cfg or CleanupConfig.from_env()
    result = CleanupResult()
    if keep:
        video["keep"] = True
    reserved = reserved_for_state(state, cfg)
    # work dir for this video (already usually removed after produce)
    work = work_dir_for(video, cfg)
    if work and work.exists():
        for p in list(_iter_files(work)):
            safe_delete(p, cfg, result, reserved)
        prune_empty_dirs(work, cfg, result)
    for p in source_files_for_video(video, cfg):
        safe_delete(p, cfg, result, reserved)
    # preview is a temp; the final render is kept for final_keep hours
    preview = video.get("preview")
    if preview:
        pp = Path(preview)
        final = Path(video["path"]) if video.get("path") else None
        if not final or pp.resolve() != final.resolve():
            safe_delete(pp, cfg, result, reserved)
            sidecar = Path(str(pp) + ".json")
            if sidecar.exists():
                safe_delete(sidecar, cfg, result, reserved)
    log(f"cleanup {video.get('id')}: freed {result.freed_mb} MB ({len(result.deleted)} files)")
    return result


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _posted_at(video: dict) -> dt.datetime | None:
    raw = video.get("posted_at") or video.get("created")
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def sweep(state, cfg: CleanupConfig | None = None, now: dt.datetime | None = None) -> CleanupResult:
    """Remove posted finals older than 24h (unless keep) and stale work/temp older than 3 days."""
    cfg = cfg or CleanupConfig.from_env()
    now = now or now_local()
    result = CleanupResult()
    reserved = reserved_for_state(state, cfg)

    for v in state.d.get("videos") or []:
        if v.get("state") != "posted":
            continue
        if has_keep_flag(v):
            continue
        when = _posted_at(v)
        if when is None:
            continue
        if when.tzinfo is None and now.tzinfo is not None:
            when = when.replace(tzinfo=now.tzinfo)
        if now - when < cfg.final_keep:
            continue
        for p in video_outputs(v):
            if p.exists() or p.is_symlink():
                safe_delete(p, cfg, result, reserved)

    cutoff = (now - cfg.temp_max_age).timestamp()
    work_roots = []
    for root in cfg.allow:
        if root.name == "work" or root.parts[-2:] == ("demo_src", "work"):
            work_roots.append(root)
        # also treat home/work explicitly
    # de-dupe
    seen_roots: set[Path] = set()
    for root in work_roots:
        key = resolve_root(root)
        if key in seen_roots or not root.exists():
            continue
        seen_roots.add(key)
        for p in list(_iter_files(root)):
            if _mtime(p) > cutoff:
                result.skipped.append((str(p), "too-new"))
                continue
            safe_delete(p, cfg, result, reserved)
        prune_empty_dirs(root, cfg, result)

    log(f"cleanup sweep: freed {result.freed_mb} MB ({len(result.deleted)} files)")
    return result


def after_mark(video: dict, state, keep: bool = False, cfg: CleanupConfig | None = None,
               now: dt.datetime | None = None) -> CleanupResult:
    """Hook for `pipeline.py mark --state posted`."""
    cfg = cfg or CleanupConfig.from_env()
    result = CleanupResult()
    if keep:
        video["keep"] = True
    if video.get("state") == "posted":
        result.merge(cleanup_posted(video, state, cfg=cfg, keep=keep, now=now))
        result.merge(sweep(state, cfg=cfg, now=now))
    return result

