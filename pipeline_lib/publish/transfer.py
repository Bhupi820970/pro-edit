"""Short-lived PUBLIC GitHub release so Buffer can fetch an Instagram video.

`vc-ready` stays draft (private assets). Buffer needs a direct URL with no auth,
so we copy one file to a published release named vc-xfer-<run>-<hex>, with a
random filename, wait until Buffer reports the post as sent, then delete only
that transfer release. Protected tags (vc-ready, sources, songs, test-renders)
are never deleted.

This is a brief public window of one unguessable URL, not a published stock
library. Callers must still never publish the ready/source/song releases.
"""
from __future__ import annotations

import secrets
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

from pipeline_lib.util import run

from . import cfg as cfgmod


class TransferError(RuntimeError):
    pass


def _protected() -> set[str]:
    c = cfgmod.load()
    return set(c.get("protected_releases") or []) | {"vc-ready", "sources", "songs", "test-renders"}


def _prefix() -> str:
    return str(cfgmod.load().get("transfer_prefix") or "vc-xfer-")


def is_transfer_tag(tag: str) -> bool:
    return bool(tag) and tag.startswith(_prefix()) and tag not in _protected()


def public_url(repo: str, tag: str, filename: str) -> str:
    return f"https://github.com/{repo}/releases/download/{tag}/{filename}"


def url_is_public(url: str, timeout: int = 30) -> bool:
    """True if an unauthenticated request can reach the file (2xx/3xx or 206)."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "pro-edit-xfer"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 400
    except urllib.error.HTTPError as e:
        # some hosts reject HEAD but allow GET range
        if e.code in {403, 404, 405}:
            try:
                greq = urllib.request.Request(
                    url, method="GET",
                    headers={"User-Agent": "pro-edit-xfer", "Range": "bytes=0-16"},
                )
                with urllib.request.urlopen(greq, timeout=timeout) as r:
                    return r.status in {200, 206}
            except Exception:
                return False
        return False
    except Exception:
        return False


def create(repo: str, src: Path, run_id: str = "local") -> dict:
    """Publish a one-file transfer release. Returns {tag, filename, url}."""
    src = Path(src)
    if not src.is_file():
        raise TransferError(f"missing video: {src}")
    prefix = _prefix()
    tag = f"{prefix}{run_id}-{secrets.token_hex(6)}"
    if not is_transfer_tag(tag):
        raise TransferError(f"refusing to create non-transfer tag {tag}")
    filename = secrets.token_hex(16) + src.suffix.lower()
    tmp = src.parent / filename
    if tmp.resolve() != src.resolve():
        shutil.copy2(src, tmp)
    notes = (
        "Temporary public copy so Buffer can fetch an Instagram post. "
        "Deleted automatically after the post is sent. Not ready stock."
    )
    run(["gh", "release", "create", tag, str(tmp), "--repo", repo,
         "--title", "tmp buffer transfer", "--notes", notes, "--latest=false"],
        timeout=600)
    if tmp.resolve() != src.resolve():
        try:
            tmp.unlink()
        except OSError:
            pass
    url = public_url(repo, tag, filename)
    # GitHub CDN can lag a few seconds after create
    for i in range(8):
        if url_is_public(url):
            break
        time.sleep(2 + i)
    else:
        delete(repo, tag)
        raise TransferError(f"transfer URL is not publicly reachable: {url}")
    return {"tag": tag, "filename": filename, "url": url}


def delete(repo: str, tag: str) -> None:
    if not is_transfer_tag(tag):
        raise TransferError(f"refusing to delete non-transfer release {tag!r}")
    if tag in _protected():
        raise TransferError(f"refusing to delete protected release {tag!r}")
    run(["gh", "release", "delete", tag, "--repo", repo, "--yes"], check=False, timeout=60)


def cleanup_stale(repo: str, keep_tags: set[str] | None = None, max_age_hours: float = 24.0) -> list[str]:
    """Delete leftover vc-xfer-* releases older than max_age_hours, except keep_tags."""
    import json
    from datetime import datetime, timedelta, timezone
    r = run(["gh", "release", "list", "--repo", repo, "--limit", "50",
             "--json", "tagName,createdAt,isDraft"], check=False, timeout=60)
    if r.returncode != 0 or not r.stdout.strip():
        return []
    try:
        rows = json.loads(r.stdout)
    except Exception:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    keep = keep_tags or set()
    removed = []
    for row in rows:
        tag = row.get("tagName") or ""
        if tag in keep or not is_transfer_tag(tag):
            continue
        created = row.get("createdAt") or ""
        try:
            ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except Exception:
            continue
        if ts > cutoff:
            continue
        delete(repo, tag)
        removed.append(tag)
    return removed
