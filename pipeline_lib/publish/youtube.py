"""YouTube Data API v3 OAuth refresh-token flow + resumable upload.

Env: YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN (never printed).
The access token is masked with ::add-mask:: as soon as it is issued.
Target channel is checked against config (default @vicecartelgta) before any upload.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from . import secrets as S

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_INIT = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels?part=snippet,status&mine=true"
CHUNK = 8 * 1024 * 1024  # 8 MiB, multiple of 256 KiB as required by Google


class YouTubeError(RuntimeError):
    pass


def _open(req: urllib.request.Request, timeout: int = 120):
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise YouTubeError(f"HTTP {e.code}: {S.redact(body)[:400]}") from None


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with _open(req, timeout=60) as r:
        payload = json.loads(r.read().decode())
    token = payload.get("access_token")
    if not token:
        raise YouTubeError("token endpoint returned no access_token")
    S.remember(token)
    return token


def access_token_from_env() -> str:
    cid, secret, refresh = S.env("YT_CLIENT_ID"), S.env("YT_CLIENT_SECRET"), S.env("YT_REFRESH_TOKEN")
    if not (cid and secret and refresh):
        missing = [n for n, v in (("YT_CLIENT_ID", cid), ("YT_CLIENT_SECRET", secret),
                                  ("YT_REFRESH_TOKEN", refresh)) if not v]
        raise YouTubeError("missing secrets: " + ", ".join(missing))
    return refresh_access_token(cid, secret, refresh)


def _auth_headers(token: str, extra: dict | None = None) -> dict:
    h = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    if extra:
        h.update(extra)
    return h


def channel_identity(token: str) -> dict:
    req = urllib.request.Request(CHANNELS_URL, headers=_auth_headers(token))
    with _open(req, timeout=60) as r:
        data = json.loads(r.read().decode())
    items = data.get("items") or []
    if not items:
        raise YouTubeError("YouTube auth returned no channel (token may lack youtube.upload scope)")
    sn = items[0].get("snippet") or {}
    handle = (sn.get("customUrl") or "").strip()
    return {
        "id": items[0].get("id"),
        "title": sn.get("title") or "",
        "handle": handle if handle.startswith("@") else (f"@{handle}" if handle else ""),
        "custom_url": handle,
    }


def assert_channel(token: str, expected: str = "@vicecartelgta") -> dict:
    ident = channel_identity(token)
    want = (expected or "@vicecartelgta").strip().lstrip("@").lower()
    got = (ident.get("handle") or ident.get("custom_url") or "").strip().lstrip("@").lower()
    if not got:
        raise YouTubeError(
            f"could not verify YouTube channel handle (title={ident.get('title')!r}); "
            f"refusing to upload. Expected @{want}."
        )
    if got != want:
        raise YouTubeError(f"YouTube auth is for @{got}, expected @{want}. Refusing to upload.")
    return ident


def _init_resumable(token: str, meta: dict, size: int, content_type: str = "video/mp4") -> str:
    body = json.dumps(meta).encode()
    req = urllib.request.Request(
        UPLOAD_INIT, data=body, method="POST",
        headers=_auth_headers(token, {
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": content_type,
        }),
    )
    with _open(req, timeout=60) as r:
        loc = r.headers.get("Location") or r.headers.get("location")
    if not loc:
        raise YouTubeError("resumable upload: no Location header")
    return loc


def _put_chunk(url: str, data: bytes, start: int, total: int, content_type: str = "video/mp4") -> dict | None:
    end = start + len(data) - 1
    req = urllib.request.Request(
        url, data=data, method="PUT",
        headers={
            "Content-Type": content_type,
            "Content-Length": str(len(data)),
            "Content-Range": f"bytes {start}-{end}/{total}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            raw = r.read()
            if r.status in (200, 201) and raw:
                return json.loads(raw.decode())
            return None
    except urllib.error.HTTPError as e:
        if e.code == 308:  # Resume Incomplete — keep sending the next chunk
            return None
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise YouTubeError(f"upload chunk HTTP {e.code}: {S.redact(body)[:400]}") from None


def resumable_upload(token: str, path: str, title: str, description: str, tags: list[str],
                     category_id: str = "20", privacy: str = "public") -> dict:
    size = os.path.getsize(path)
    if size <= 0:
        raise YouTubeError("video file is empty")
    meta = {
        "snippet": {
            "title": (title or "Vice Cartel")[:100],
            "description": (description or "")[:5000],
            "tags": [t[:30] for t in (tags or [])][:15],
            "categoryId": str(category_id or "20"),
        },
        "status": {
            "privacyStatus": privacy or "public",
            "selfDeclaredMadeForKids": False,
            "embeddable": True,
        },
    }
    session = _init_resumable(token, meta, size)
    sent = 0
    result = None
    with open(path, "rb") as f:
        while sent < size:
            chunk = f.read(min(CHUNK, size - sent))
            if not chunk:
                break
            result = _put_chunk(session, chunk, sent, size, "video/mp4")
            sent += len(chunk)
    if not result or not result.get("id"):
        # some implementations return the resource only on the last chunk; if we
        # somehow missed it, fail clearly rather than inventing an id
        raise YouTubeError("upload finished but YouTube returned no video id")
    vid = result["id"]
    return {
        "id": vid,
        "url": f"https://www.youtube.com/watch?v={vid}",
        "title": (result.get("snippet") or {}).get("title") or title,
    }


def upload_video(path: str, title: str, description: str, tags: list[str],
                 expected_channel: str = "@vicecartelgta",
                 category_id: str = "20", privacy: str = "public") -> dict:
    token = access_token_from_env()
    assert_channel(token, expected_channel)
    return resumable_upload(token, path, title, description, tags, category_id, privacy)
