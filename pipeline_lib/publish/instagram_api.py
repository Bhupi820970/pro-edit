"""Instagram Graph API — publish Reels to @benreelr via graph.facebook.com.

Env: IG_ACCESS_TOKEN (secret, masked via secrets.py), IG_USER_ID.
Optional: IG_GRAPH_VERSION (default from config/publish.json or v26.0).

Flow:
1. Verify account identity (GET /{IG_USER_ID}?fields=username,id).
2. Check rolling 24h quota (GET /{IG_USER_ID}/content_publishing_limit?fields=config,quota_usage).
3. Create Reel container (POST /{IG_USER_ID}/media with media_type=REELS, video_url, caption, share_to_feed=true).
4. Poll container status (GET /{container_id}?fields=status_code,status) with backoff until FINISHED.
5. Publish container (POST /{IG_USER_ID}/media_publish with creation_id={container_id}).
6. Best-effort permalink lookup (GET /{media_id}?fields=id,permalink,shortcode).

Retries never double-publish: before retrying media_publish (or after an uncertain
publish error), container status_code and recent IG media are checked first.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import cfg as cfgmod
from . import secrets as S

GRAPH_HOST = "https://graph.facebook.com"
DEFAULT_VERSION = "v26.0"
DEFAULT_POLL_INTERVALS = (5, 10, 15, 20)
_TRANSIENT_HTTP = {429, 500, 502, 503, 504}
_TRANSIENT_META_CODES = {1, 2, 4, 17}


class InstagramApiError(RuntimeError):
    def __init__(self, message: str, *, http_status: int | None = None,
                 code: int | None = None, subcode: int | None = None,
                 transient: bool = False):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.subcode = subcode
        self.transient = transient or (http_status in _TRANSIENT_HTTP) or (code in _TRANSIENT_META_CODES)


def graph_version(version: str | None = None) -> str:
    raw = (
        version
        or os.environ.get("IG_GRAPH_VERSION")
        or cfgmod.load().get("instagram_graph_version")
        or DEFAULT_VERSION
    )
    v = str(raw).strip()
    if not v:
        return DEFAULT_VERSION
    return v if v.startswith("v") else f"v{v}"


def _credentials(token: str | None = None, ig_user_id: str | None = None) -> tuple[str, str]:
    tok = (token or S.env("IG_ACCESS_TOKEN") or "").strip()
    if tok:
        S.remember(tok)
    uid = (ig_user_id or os.environ.get("IG_USER_ID") or "").strip()
    if not tok or not uid:
        missing = [n for n, v in (("IG_ACCESS_TOKEN", tok), ("IG_USER_ID", uid)) if not v]
        raise InstagramApiError("missing secrets/env: " + ", ".join(missing))
    return tok, uid


def _request(method: str, path: str, token: str, *, params: dict | None = None, data: dict | None = None,
             version: str | None = None, timeout: int = 60) -> dict:
    """Graph call. GETs get 3 tries with backoff on transient/5xx errors; POSTs are not repeated here
    (publish has its own recovery that first checks whether the reel already went live)."""
    if method.upper() != "GET":
        return _request_once(method, path, token, params=params, data=data, version=version, timeout=timeout)
    from pipeline_lib.netretry import retry

    def soft(e):
        return not (getattr(e, "transient", False) or (getattr(e, "http_status", 0) or 0) >= 500)
    return retry(lambda: _request_once(method, path, token, params=params, data=data, version=version,
                                       timeout=timeout),
                 label=f"IG GET {path.split('/')[-1][:20]}", give_up=soft, log=lambda m: print(S.redact(m), flush=True))


def _request_once(method: str, path: str, token: str, *,
             params: dict | None = None, data: dict | None = None,
             version: str | None = None, timeout: int = 60) -> dict:
    ver = graph_version(version)
    url = f"{GRAPH_HOST}/{ver}/{path.lstrip('/')}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        code, subcode, msg = None, None, raw
        try:
            err = (json.loads(raw) or {}).get("error") or {}
            if isinstance(err, dict):
                code = err.get("code")
                subcode = err.get("error_subcode")
                msg = err.get("message") or raw
        except Exception:
            pass
        code_part = ""
        if code is not None:
            code_part = f" (code {code}/{subcode})" if subcode is not None else f" (code {code})"
        raise InstagramApiError(
            f"IG Graph HTTP {e.code}{code_part}: {S.redact(str(msg))[:350]}",
            http_status=e.code,
            code=code if isinstance(code, int) else None,
            subcode=subcode if isinstance(subcode, int) else None,
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        raise InstagramApiError(
            f"IG Graph {method} {path}: {S.redact(str(e))[:300]}",
            transient=True,
        ) from None


def _norm_user(s: str | None) -> str:
    return (s or "").strip().lstrip("@").lower()


def get_account(token: str | None = None, ig_user_id: str | None = None,
                version: str | None = None) -> dict:
    tok, uid = _credentials(token, ig_user_id)
    d = _request("GET", uid, tok, params={"fields": "username,id"}, version=version)
    return {
        "id": str(d.get("id") or uid),
        "username": d.get("username") or "",
    }


def assert_account(expected_username: str = "benreelr", token: str | None = None,
                   ig_user_id: str | None = None, version: str | None = None) -> dict:
    acct = get_account(token=token, ig_user_id=ig_user_id, version=version)
    want = _norm_user(expected_username)
    got = _norm_user(acct.get("username"))
    if want and got != want:
        raise InstagramApiError(
            f"IG account is @{got or '?'}, expected @{want}. Refusing to publish."
        )
    return acct


def get_publishing_limit(token: str | None = None, ig_user_id: str | None = None,
                         version: str | None = None) -> dict:
    tok, uid = _credentials(token, ig_user_id)
    d = _request(
        "GET",
        f"{uid}/content_publishing_limit",
        tok,
        params={"fields": "config,quota_usage"},
        version=version,
    )
    rows = d.get("data") or []
    row = rows[0] if rows and isinstance(rows[0], dict) else {}
    cfg = row.get("config") if isinstance(row.get("config"), dict) else {}
    usage = row.get("quota_usage")
    total = cfg.get("quota_total")
    duration = cfg.get("quota_duration")
    return {
        "quota_usage": int(usage) if usage is not None else 0,
        "quota_total": int(total) if total is not None else None,
        "quota_duration": int(duration) if duration is not None else None,
        "raw": d,
    }


def ensure_publishing_quota(token: str | None = None, ig_user_id: str | None = None,
                            version: str | None = None) -> dict:
    lim = get_publishing_limit(token=token, ig_user_id=ig_user_id, version=version)
    total = lim.get("quota_total")
    cap = total if total is not None else 50
    usage = lim.get("quota_usage") or 0
    if usage >= cap:
        raise InstagramApiError(
            f"IG publishing limit reached: {usage}/{cap} in rolling 24h",
            code=9,
            subcode=2207042,
        )
    return lim


def check_token(expected_username: str = "benreelr", token: str | None = None,
                ig_user_id: str | None = None, version: str | None = None) -> dict:
    """Read-only token verification: GET username/id + content_publishing_limit."""
    ver = graph_version(version)
    acct = assert_account(
        expected_username=expected_username,
        token=token,
        ig_user_id=ig_user_id,
        version=ver,
    )
    lim = get_publishing_limit(token=token, ig_user_id=ig_user_id, version=ver)
    return {
        "ok": True,
        "id": acct["id"],
        "username": acct["username"],
        "quota_usage": lim["quota_usage"],
        "quota_total": lim["quota_total"],
        "quota_duration": lim["quota_duration"],
        "graph_version": ver,
    }


def create_reel_container(video_url: str, caption: str, *, share_to_feed: bool = True,
                          token: str | None = None, ig_user_id: str | None = None,
                          version: str | None = None) -> str:
    tok, uid = _credentials(token, ig_user_id)
    payload = {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": (caption or "")[:2200],
        "share_to_feed": "true" if share_to_feed else "false",
    }
    d = _request("POST", f"{uid}/media", tok, data=payload, version=version, timeout=90)
    cid = str(d.get("id") or "").strip()
    if not cid:
        raise InstagramApiError(f"create_reel_container returned no container id: {S.redact(str(d))[:200]}")
    return cid


def get_container_status(container_id: str, token: str | None = None,
                         ig_user_id: str | None = None, version: str | None = None) -> dict:
    tok, _ = _credentials(token, ig_user_id)
    d = _request(
        "GET",
        container_id,
        tok,
        params={"fields": "status_code,status"},
        version=version,
    )
    return {
        "id": str(d.get("id") or container_id),
        "status_code": str(d.get("status_code") or "").upper(),
        "status": d.get("status"),
    }


def wait_for_container(container_id: str, *, timeout_s: int = 600,
                       poll_intervals: tuple[int, ...] = DEFAULT_POLL_INTERVALS,
                       token: str | None = None, ig_user_id: str | None = None,
                       version: str | None = None, log=None) -> dict:
    deadline = time.time() + max(1, timeout_s)
    idx = 0
    consecutive_errors = 0
    last_code = "UNKNOWN"
    while True:
        try:
            st = get_container_status(container_id, token=token, ig_user_id=ig_user_id, version=version)
            consecutive_errors = 0
        except InstagramApiError as e:
            consecutive_errors += 1
            if not e.transient or consecutive_errors > 5 or time.time() >= deadline:
                raise
            if log:
                log(f"ig container {container_id} status poll transient error ({consecutive_errors}): {e}")
            st = {"id": container_id, "status_code": last_code, "status": None}

        code = st.get("status_code") or "UNKNOWN"
        last_code = code
        if code in {"FINISHED", "PUBLISHED"}:
            return st
        if code in {"ERROR", "EXPIRED"}:
            detail = S.redact(str(st.get("status") or "container failed"))[:300]
            raise InstagramApiError(f"IG container {container_id} {code}: {detail}")

        remaining = deadline - time.time()
        if remaining <= 0:
            raise InstagramApiError(
                f"IG container {container_id} timed out after {timeout_s}s (last status_code={last_code})"
            )
        delay = poll_intervals[min(idx, len(poll_intervals) - 1)]
        idx += 1
        time.sleep(min(delay, max(0.0, remaining)))


def list_recent_media(limit: int = 10, token: str | None = None,
                      ig_user_id: str | None = None, version: str | None = None) -> list[dict]:
    tok, uid = _credentials(token, ig_user_id)
    d = _request(
        "GET",
        f"{uid}/media",
        tok,
        params={"fields": "id,caption,permalink,timestamp,media_type,media_product_type", "limit": str(limit)},
        version=version,
    )
    rows = d.get("data") or []
    return [r for r in rows if isinstance(r, dict)]


def get_media(media_id: str, token: str | None = None,
              ig_user_id: str | None = None, version: str | None = None) -> dict:
    tok, _ = _credentials(token, ig_user_id)
    return _request(
        "GET",
        media_id,
        tok,
        params={"fields": "id,permalink,shortcode,timestamp"},
        version=version,
    )


def _parse_ts(raw: str | None) -> float | None:
    if not raw:
        return None
    s = str(raw).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _norm_caption(s: str | None) -> str:
    return " ".join((s or "").split())


def _find_published_match(caption: str, since_ts: float, *,
                          token: str | None = None, ig_user_id: str | None = None,
                          version: str | None = None) -> dict | None:
    try:
        rows = list_recent_media(limit=10, token=token, ig_user_id=ig_user_id, version=version)
    except Exception:
        return None
    want = _norm_caption(caption)
    if not want:
        return None
    prefix = want[:100]
    for row in rows:
        ts = _parse_ts(row.get("timestamp"))
        if ts is not None and ts < (since_ts - 120):
            continue
        got = _norm_caption(row.get("caption"))
        if got and (got == want or got.startswith(prefix) or want.startswith(got[:100])):
            return row
    return None


def publish_container(container_id: str, token: str | None = None,
                      ig_user_id: str | None = None, version: str | None = None,
                      log=None) -> dict:
    tok, uid = _credentials(token, ig_user_id)
    d = _request(
        "POST",
        f"{uid}/media_publish",
        tok,
        data={"creation_id": container_id},
        version=version,
        timeout=90,
    )
    mid = str(d.get("id") or "").strip()
    if not mid:
        raise InstagramApiError(f"media_publish returned no media id: {S.redact(str(d))[:200]}")
    for warn_key in ("config_issue", "caption_add_on_attachment_status"):
        if d.get(warn_key) and log:
            log(f"IG media_publish {mid} partial notice ({warn_key}): {d.get(warn_key)}")
    return d


def _recover_published(container_id: str, caption: str, started_at: float, *,
                       token: str | None, ig_user_id: str | None, version: str | None,
                       acct: dict, lim: dict, uid: str, log=None) -> dict | None:
    st_code = None
    try:
        st = get_container_status(container_id, token=token, ig_user_id=ig_user_id, version=version)
        st_code = st.get("status_code")
    except Exception:
        pass
    match = _find_published_match(caption, started_at, token=token, ig_user_id=ig_user_id, version=version)
    if st_code == "PUBLISHED" or match is not None:
        media_id = str((match or {}).get("id") or "") or None
        url = (match or {}).get("permalink")
        if media_id and not url:
            try:
                url = get_media(media_id, token=token, ig_user_id=ig_user_id, version=version).get("permalink")
            except Exception:
                pass
        if log:
            log(f"IG container {container_id} already published (media_id={media_id}); skipping duplicate publish")
        return {
            "ig_user_id": uid,
            "username": acct.get("username"),
            "container_id": container_id,
            "media_id": media_id,
            "url": url,
            "quota_usage": lim.get("quota_usage"),
            "quota_total": lim.get("quota_total"),
            "already_published": True,
        }
    return None


def publish_reel(video_url: str, caption: str, *,
                 expected_username: str = "benreelr",
                 share_to_feed: bool = True,
                 timeout_s: int = 600,
                 retry_once: bool = True,
                 token: str | None = None,
                 ig_user_id: str | None = None,
                 version: str | None = None,
                 log=None) -> dict:
    tok, uid = _credentials(token, ig_user_id)
    ver = graph_version(version)
    acct = assert_account(expected_username=expected_username, token=tok, ig_user_id=uid, version=ver)
    lim = ensure_publishing_quota(token=tok, ig_user_id=uid, version=ver)
    started_at = time.time()

    def _make_and_wait() -> tuple[str, dict]:
        cid = create_reel_container(
            video_url,
            caption,
            share_to_feed=share_to_feed,
            token=tok,
            ig_user_id=uid,
            version=ver,
        )
        if log:
            log(f"IG container created: {cid}")
        st = wait_for_container(
            cid,
            timeout_s=timeout_s,
            token=tok,
            ig_user_id=uid,
            version=ver,
            log=log,
        )
        return cid, st

    try:
        cid, st = _make_and_wait()
    except InstagramApiError as e:
        if not retry_once:
            raise
        if log:
            log(f"IG container stage failed ({e}); retrying once")
        time.sleep(5)
        cid, st = _make_and_wait()

    if st.get("status_code") == "PUBLISHED":
        rec = _recover_published(
            cid, caption, started_at,
            token=tok, ig_user_id=uid, version=ver,
            acct=acct, lim=lim, uid=uid, log=log,
        )
        if rec:
            return rec

    pub = None
    try:
        pub = publish_container(cid, token=tok, ig_user_id=uid, version=ver, log=log)
    except InstagramApiError as first_err:
        recovered = _recover_published(
            cid, caption, started_at,
            token=tok, ig_user_id=uid, version=ver,
            acct=acct, lim=lim, uid=uid, log=log,
        )
        if recovered:
            return recovered
        if not retry_once:
            raise
        if first_err.code == 9 or first_err.subcode == 2207042:
            raise
        if log:
            log(f"IG media_publish failed ({first_err}); verifying status before retrying once")
        time.sleep(5)
        recovered = _recover_published(
            cid, caption, started_at,
            token=tok, ig_user_id=uid, version=ver,
            acct=acct, lim=lim, uid=uid, log=log,
        )
        if recovered:
            return recovered
        st2 = get_container_status(cid, token=tok, ig_user_id=uid, version=ver)
        if st2.get("status_code") in {"ERROR", "EXPIRED"}:
            raise InstagramApiError(f"IG container {cid} {st2.get('status_code')} before retry: {st2.get('status')}")
        try:
            pub = publish_container(cid, token=tok, ig_user_id=uid, version=ver, log=log)
        except InstagramApiError:
            recovered = _recover_published(
                cid, caption, started_at,
                token=tok, ig_user_id=uid, version=ver,
                acct=acct, lim=lim, uid=uid, log=log,
            )
            if recovered:
                return recovered
            raise

    media_id = str((pub or {}).get("id") or "")
    url = None
    if media_id:
        try:
            url = get_media(media_id, token=tok, ig_user_id=uid, version=ver).get("permalink")
        except Exception as e:
            if log:
                log(f"IG permalink lookup skipped for {media_id}: {e}")
    return {
        "ig_user_id": uid,
        "username": acct.get("username"),
        "container_id": cid,
        "media_id": media_id or None,
        "url": url,
        "quota_usage": lim.get("quota_usage"),
        "quota_total": lim.get("quota_total"),
        "already_published": False,
    }
