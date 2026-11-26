"""What is actually live on @vicecartelgta (YouTube) and @benreelr (Instagram).

Used (a) before every post, so a video/title that is already live is never posted again, and
(b) by the owner-only maintenance workflow to list recent uploads and remove a verified duplicate.
Tokens come from the environment only and are never printed (secrets.redact on every message).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import instagram_api, ledger, youtube
from . import secrets as S
from ..netretry import retry

API = "https://www.googleapis.com/youtube/v3"
# originals that must never be deleted, whatever the inputs say
PROTECT = {"4DJCHbfbcbk", "DgNKzWdhHL9", "18033167072922205"}


def _yt_get(token: str, path: str, params: dict) -> dict:
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "Accept": "application/json"})

    def go():
        with youtube._open(req, timeout=60) as r:
            return json.loads(r.read().decode())
    return retry(go, label=f"youtube GET {path}")


def yt_recent_uploads(token: str, n: int = 25) -> list[dict]:
    ch = _yt_get(token, "channels", {"part": "contentDetails", "mine": "true"})
    pl = (((ch.get("items") or [{}])[0].get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
    if not pl:
        return []
    d = _yt_get(token, "playlistItems", {"part": "snippet,contentDetails", "playlistId": pl, "maxResults": str(n)})
    out = []
    for it in d.get("items") or []:
        sn = it.get("snippet") or {}
        out.append({"id": (it.get("contentDetails") or {}).get("videoId") or (sn.get("resourceId") or {}).get("videoId"),
                    "title": sn.get("title") or "", "published": sn.get("publishedAt")})
    return out


def ig_recent_media(n: int = 25) -> list[dict]:
    rows = retry(lambda: instagram_api.list_recent_media(limit=n), label="instagram GET media")
    out = []
    for r in rows:
        link = r.get("permalink") or ""
        m = re.search(r"/(?:reel|p)/([\w-]+)", link)
        out.append({"id": r.get("id"), "shortcode": m.group(1) if m else None, "permalink": link,
                    "caption_head": " ".join((r.get("caption") or "").split())[:120], "timestamp": r.get("timestamp")})
    return out


def vc_tag(video_id: str) -> str:
    """Hidden YouTube tag that identifies our render (tags are not shown on the Short)."""
    return "vc-" + re.sub(r"[^a-z0-9-]", "", video_id.lower())[:40]


def yt_tags(token: str, ids: list[str]) -> dict[str, list[str]]:
    ids = [i for i in ids if i][:50]
    if not ids:
        return {}
    d = _yt_get(token, "videos", {"part": "snippet", "id": ",".join(ids)})
    return {v.get("id"): (v.get("snippet") or {}).get("tags") or [] for v in d.get("items") or []}


def yt_live_copy(token: str, video_id: str, titles: list[str], n: int = 25) -> dict | None:
    """A recent upload of this render: same hidden tag, or (older uploads without tag) the same title."""
    ups = yt_recent_uploads(token, n)
    tags = yt_tags(token, [u["id"] for u in ups])
    want = vc_tag(video_id)
    for u in ups:
        if want in (tags.get(u["id"]) or []):
            return u
    wt = {_norm(t) for t in titles if t}
    return next((u for u in ups if _norm(u["title"]) in wt), None)


def ig_live_copy(credit: str, footage: str, days: float = 3, n: int = 25) -> dict | None:
    """A recent reel carrying this render's song + footage credit lines (deterministic per render)."""
    if not credit:
        return None
    rows = retry(lambda: instagram_api.list_recent_media(limit=n), label="instagram GET media")
    cut = time.time() - days * 86400
    for r in rows:
        ts = instagram_api._parse_ts(r.get("timestamp"))
        if ts is not None and ts < cut:
            continue
        cap = r.get("caption") or ""
        if credit in cap and (not footage or footage in cap):
            return {"id": r.get("id"), "permalink": r.get("permalink"), "timestamp": r.get("timestamp")}
    return None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def find_live(platform: str, title: str, items: list[dict] | None = None, token: str | None = None) -> dict | None:
    """A recent live post whose title (YouTube) / caption start (Instagram) equals this title."""
    want = _norm(title)
    if not want:
        return None
    if platform == "youtube":
        items = items if items is not None else yt_recent_uploads(token or youtube.access_token_from_env())
        return next((v for v in items if _norm(v.get("title")) == want), None)
    items = items if items is not None else ig_recent_media()
    return next((m for m in items if _norm(m.get("caption_head")).startswith(want)), None)


def ledger_ids(d: dict) -> set[str]:
    ids = set()
    for r in d.get("posted") or []:
        for k in ("youtube_id", "ig_media_id", "ig_container_id"):
            if r.get(k):
                ids.add(str(r[k]))
        m = re.search(r"/(?:reel|p)/([\w-]+)", r.get("instagram_url") or "")
        if m:
            ids.add(m.group(1))
    return ids


def _summary(lines: list[str]) -> None:
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        Path(p).open("a").write("\n".join(lines) + "\n")
    print("\n".join(lines), flush=True)


def delete_duplicate(platform: str, media_id: str, must_contain: str, led: dict) -> dict:
    """Delete one duplicate post, only after every guard passes. Never deletes a ledger (original) post."""
    protected = PROTECT | ledger_ids(led)
    if not media_id or media_id in protected:
        return {"platform": platform, "id": media_id, "deleted": False, "reason": "protected original / in ledger"}
    want = _norm(must_contain)
    if platform == "youtube":
        tok = youtube.access_token_from_env()
        items = yt_recent_uploads(tok, 50)
        it = next((v for v in items if v["id"] == media_id), None)
        if not it or want not in _norm(it["title"]):
            return {"platform": platform, "id": media_id, "deleted": False, "reason": "not a recent upload with that title"}
        if not any(v["id"] in protected and _norm(v["title"]) == _norm(it["title"]) for v in items):
            return {"platform": platform, "id": media_id, "deleted": False, "reason": "original copy not found live; keeping"}
        req = urllib.request.Request(f"{API}/videos?id={urllib.parse.quote(media_id)}", method="DELETE",
                                     headers={"Authorization": "Bearer " + tok})
        try:
            with youtube._open(req, timeout=60):
                pass
            return {"platform": platform, "id": media_id, "title": it["title"], "deleted": True}
        except Exception as e:
            return {"platform": platform, "id": media_id, "deleted": False, "reason": S.redact(str(e))[:200]}
    items = ig_recent_media(50)
    it = next((m for m in items if media_id in (m["id"], m["shortcode"])), None)
    if not it or not want or want not in _norm(it["caption_head"]):
        return {"platform": platform, "id": media_id, "deleted": False, "reason": "not a recent post with that caption"}
    if it["id"] in protected or it["shortcode"] in protected:
        return {"platform": platform, "id": media_id, "deleted": False, "reason": "protected original"}
    tok, _ = instagram_api._credentials()
    try:   # the IG Graph API may not allow deleting published media; we do not work around a refusal
        instagram_api._request("DELETE", it["id"], tok)
        return {"platform": platform, "id": it["id"], "permalink": it["permalink"], "deleted": True}
    except Exception as e:
        return {"platform": platform, "id": it["id"], "permalink": it["permalink"], "deleted": False,
                "reason": S.redact(str(e))[:200]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["list", "delete"])
    ap.add_argument("--platform", choices=["youtube", "instagram"])
    ap.add_argument("--id", default="")
    ap.add_argument("--must-contain", default="")
    a = ap.parse_args(argv)
    led = ledger.load(None)
    lid = ledger_ids(led)
    if a.cmd == "list":
        lines = ["## Live posts (recent)", "", "### YouTube @vicecartelgta", ""]
        try:
            for v in yt_recent_uploads(youtube.access_token_from_env(), 25):
                lines.append(f"- `{v['id']}` {v['published']} {'LEDGER' if v['id'] in lid else 'NOT-IN-LEDGER'} | {v['title']}")
        except Exception as e:
            lines.append(f"- YouTube list failed: {S.redact(str(e))[:200]}")
        lines += ["", "### Instagram @benreelr", ""]
        try:
            for m in ig_recent_media(25):
                tag = "LEDGER" if (m["id"] in lid or m["shortcode"] in lid) else "NOT-IN-LEDGER"
                lines.append(f"- `{m['id']}` {m['shortcode']} {m['timestamp']} {tag} | {m['caption_head'][:80]} | {m['permalink']}")
        except Exception as e:
            lines.append(f"- Instagram list failed: {S.redact(str(e))[:200]}")
        _summary(lines)
        return 0
    res = delete_duplicate(a.platform, a.id.strip(), a.must_contain, led)
    _summary(["## Duplicate removal", "", "```", json.dumps(res, indent=1), "```"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
