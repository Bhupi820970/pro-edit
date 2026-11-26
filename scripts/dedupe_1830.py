#!/usr/bin/env python3
"""One-shot cleanup for the 2026-11-26 18:30 IST double upload (run 45825420513).

Deletes ONLY the newer YouTube copy GoCHmaSRViw (not in the ledger).
Keeps 4DJCHbfbcbk and Instagram reel DgNKzWdhHL9 (media 18033167072922205).
If a second "Somebody\'s Watching Me" IG item is live, try to delete it via Graph API.
Never prints tokens. Owner-only workflow.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from pipeline_lib.publish import instagram_api as ig  # noqa: E402
from pipeline_lib.publish import secrets as S  # noqa: E402
from pipeline_lib.publish import youtube as yt  # noqa: E402
import urllib.parse
import urllib.request

KEEP_YT = "4DJCHbfbcbk"
DUP_YT = "GoCHmaSRViw"
KEEP_IG_SHORT = "DgNKzWdhHL9"
KEEP_IG_MEDIA = "18033167072922205"
KEEP_IG_CONTAINER = "18190763965423233"
DUP_IG_CONTAINER = "18190764157423233"  # created by the second run; media id may differ
TITLE_NEEDLE = "somebody's watching me"


def log(msg: str) -> None:
    print(S.redact(str(msg)), flush=True)


def _yt_get(token: str, url: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    with yt._open(req, timeout=60) as r:
        return json.loads(r.read().decode())


def list_youtube(token: str, n: int = 15) -> list[dict]:
    # uploads playlist of the authorized channel
    ch = _yt_get(token, "https://www.googleapis.com/youtube/v3/channels?part=contentDetails,snippet&mine=true")
    items = ch.get("items") or []
    if not items:
        raise yt.YouTubeError("no channel for this token")
    uploads = ((items[0].get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
    title = ((items[0].get("snippet") or {}).get("title") or "")
    handle = ((items[0].get("snippet") or {}).get("customUrl") or "")
    log(f"YT channel {title} {handle} uploads={uploads}")
    if not uploads:
        raise yt.YouTubeError("channel has no uploads playlist")
    q = urllib.parse.urlencode({
        "part": "snippet,contentDetails",
        "playlistId": uploads,
        "maxResults": str(n),
    })
    data = _yt_get(token, f"https://www.googleapis.com/youtube/v3/playlistItems?{q}")
    out = []
    for it in data.get("items") or []:
        sn = it.get("snippet") or {}
        vid = ((it.get("contentDetails") or {}).get("videoId")
               or (sn.get("resourceId") or {}).get("videoId"))
        out.append({"id": vid, "title": sn.get("title"), "publishedAt": sn.get("publishedAt")})
    return out


def delete_youtube(token: str, video_id: str) -> None:
    if video_id != DUP_YT:
        raise RuntimeError(f"refusing to delete YouTube id {video_id} (not the allowlisted duplicate)")
    url = "https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode({"id": video_id})
    req = urllib.request.Request(url, method="DELETE",
                                 headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    with yt._open(req, timeout=60) as r:
        body = r.read().decode("utf-8", "replace")
        log(f"YT DELETE {video_id} HTTP {r.status} {body[:80] or 'ok'}")


def delete_ig(media_id: str) -> None:
    if media_id in {KEEP_IG_MEDIA, KEEP_IG_CONTAINER} or media_id == KEEP_IG_SHORT:
        raise RuntimeError(f"refusing to delete kept IG id {media_id}")
    d = ig._request("DELETE", media_id, ig._credentials()[0])
    log(f"IG DELETE {media_id} -> {S.redact(json.dumps(d)[:200])}")


def main() -> int:
    apply = os.environ.get("DEDUPE_APPLY") == "1"
    log(f"dedupe_1830 apply={apply}")
    report = {"youtube": [], "instagram": [], "deleted": [], "browser": [], "errors": []}

    # --- YouTube
    token = yt.access_token_from_env()
    ident = yt.channel_identity(token)
    log(f"YT auth channel {ident}")
    yt.assert_channel(token, "@vicecartelgta")
    recent = list_youtube(token, 15)
    report["youtube"] = recent
    for v in recent:
        log(f"YT {v['id']} {v.get('publishedAt')} {v.get('title')}")

    ids = {v["id"] for v in recent}
    if KEEP_YT not in ids:
        report["errors"].append(f"kept original {KEEP_YT} not in recent uploads — abort YT delete")
        log(report["errors"][-1])
        apply_yt = False
    else:
        apply_yt = True
    if DUP_YT in ids:
        dup = next(v for v in recent if v["id"] == DUP_YT)
        if "Get Lucky" not in (dup.get("title") or ""):
            report["errors"].append(f"{DUP_YT} title unexpected: {dup.get('title')}")
            apply_yt = False
        elif apply and apply_yt:
            try:
                delete_youtube(token, DUP_YT)
                report["deleted"].append({"platform": "youtube", "id": DUP_YT})
                log(f"deleted YT duplicate {DUP_YT}")
            except Exception as e:
                report["errors"].append(f"YT delete failed: {type(e).__name__}: {S.redact(str(e)[:300])}")
                report["browser"].append(f"YouTube Studio: delete https://www.youtube.com/watch?v={DUP_YT} only, keep {KEEP_YT}")
                log(report["errors"][-1])
        else:
            log(f"would delete YT {DUP_YT}")
    else:
        log(f"YT duplicate {DUP_YT} not in recent list (already gone?)")

    # --- Instagram
    try:
        ident_ig = ig.assert_account("benreelr")
        log(f"IG auth {ident_ig}")
        media = ig.list_recent_media(limit=15)
    except Exception as e:
        report["errors"].append(f"IG list failed: {type(e).__name__}: {S.redact(str(e)[:300])}")
        report["browser"].append(
            "Instagram: open @benreelr and delete the newer 'Somebody\'s Watching Me' reel "
            f"if it is not https://www.instagram.com/reel/{KEEP_IG_SHORT}/"
        )
        log(report["errors"][-1])
        media = []
    report["instagram"] = [
        {"id": m.get("id"), "permalink": m.get("permalink"), "timestamp": m.get("timestamp"),
         "caption": (m.get("caption") or "")[:80], "type": m.get("media_product_type") or m.get("media_type")}
        for m in media
    ]
    for m in report["instagram"]:
        log(f"IG {m['id']} {m.get('timestamp')} {m.get('permalink')} {m.get('caption')!r}")

    extras = []
    for m in media:
        mid = str(m.get("id") or "")
        perm = str(m.get("permalink") or "")
        cap = (m.get("caption") or "").lower()
        if mid == KEEP_IG_MEDIA or KEEP_IG_SHORT in perm:
            continue
        if TITLE_NEEDLE in cap or mid == DUP_IG_CONTAINER:
            extras.append(m)
    if not extras:
        # second copy may have the same caption; also flag any media published after the original
        # around 18:35 IST whose permalink is not the kept reel — only if caption matches or is empty
        log("no extra Somebody's Watching Me item obvious in the IG list")
    for m in extras:
        mid = m.get("id")
        if apply:
            try:
                delete_ig(mid)
                report["deleted"].append({"platform": "instagram", "id": mid, "permalink": m.get("permalink")})
                log(f"deleted IG duplicate {mid}")
            except Exception as e:
                report["errors"].append(f"IG delete failed {mid}: {type(e).__name__}: {S.redact(str(e)[:300])}")
                report["browser"].append(
                    f"Instagram: delete media {mid} ({m.get('permalink')}), keep "
                    f"https://www.instagram.com/reel/{KEEP_IG_SHORT}/"
                )
                log(report["errors"][-1])
        else:
            log(f"would delete IG {mid} {m.get('permalink')}")

    out = Path(os.environ.get("GITHUB_STEP_SUMMARY") or "/tmp/dedupe_1830_summary.md")
    lines = ["## 18:30 IST duplicate cleanup", "",
             f"- keep YT `{KEEP_YT}`  IG `{KEEP_IG_SHORT}` (`{KEEP_IG_MEDIA}`)",
             f"- apply={apply}", ""]
    if report["deleted"]:
        lines.append("Deleted:")
        for d in report["deleted"]:
            lines.append(f"- {d}")
    if report["browser"]:
        lines.append("Needs browser:")
        for b in report["browser"]:
            lines.append(f"- {b}")
    if report["errors"]:
        lines.append("Errors:")
        for b in report["errors"]:
            lines.append(f"- {b}")
    Path(out).open("a").write("\n".join(lines) + "\n")
    print(json.dumps({k: report[k] for k in ("deleted", "browser", "errors")}, ensure_ascii=False), flush=True)
    # non-zero if we needed a delete that did not happen
    if DUP_YT in {v["id"] for v in recent} and not any(d.get("id") == DUP_YT for d in report["deleted"]) and apply:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
