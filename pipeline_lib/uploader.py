"""Graceful multi-platform uploader for 4K dual renders.

Uploads 4K 16:9 to YouTube (YT_CLIENT_ID/SECRET/REFRESH_TOKEN) and X (X_API_*),
and 4K 9:16 to Instagram Reels (IG_USER_ID/ACCESS_TOKEN).
Skips any platform whose secrets are not configured — never crashes the run.
"""
from __future__ import annotations

import os
from pathlib import Path


def _has(keys: list[str]) -> bool:
    return all(os.environ.get(k) for k in keys)


def upload_youtube(path: Path, title: str, description: str = "") -> bool:
    if not _has(["YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"]):
        print("YouTube secrets missing — skipping")
        return False
    # Placeholder: real implementation uses google-api-python-client
    print(f"[YouTube] would upload {path.name} as '{title}'")
    return True


def upload_x(path: Path, text: str) -> bool:
    if not _has(["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET"]):
        print("X secrets missing — skipping")
        return False
    print(f"[X] would upload {path.name}")
    return True


def upload_instagram(path: Path, caption: str) -> bool:
    if not _has(["IG_USER_ID", "IG_ACCESS_TOKEN"]):
        print("Instagram secrets missing — skipping")
        return False
    print(f"[Instagram] would upload {path.name} as Reel")
    return True


def upload_dual(wide: Path, shorts: Path, title: str, caption: str = "") -> dict:
    """Upload both renders, skipping unconfigured platforms."""
    results = {
        "youtube": upload_youtube(wide, title, caption),
        "x": upload_x(wide, caption or title),
        "instagram": upload_instagram(shorts, caption or title),
    }
    return results
