"""Orchestrate one upload pass: 1 YouTube + 1 Instagram, ledger, retry-once, stock warn."""
from __future__ import annotations

import os
import time
import traceback
from pathlib import Path

from pipeline_lib.util import now_local

from . import buffer_ig, captions, cfg as cfgmod, instagram_api, ledger, stock, transfer, youtube
from . import secrets as S


class UploadSkip(Exception):
    pass


def _log(msg: str) -> None:
    print(f"[upload {now_local().strftime('%H:%M:%S')}] {S.redact(str(msg))}", flush=True)


def _retry(fn, once: bool, label: str):
    try:
        return fn()
    except Exception as e:
        if not once:
            raise
        _log(f"{label}: first attempt failed ({S.redact(str(e)[:300])}); retrying once")
        time.sleep(5)
        return fn()


def _gemini():
    try:
        from pipeline_lib.gemini import Gemini
        return Gemini(log=lambda *a: _log(" ".join(str(x) for x in a)))
    except Exception as e:
        _log(f"Gemini unavailable, using template captions: {type(e).__name__}")
        return None


def _ensure_path(item: dict, repo: str, tag: str, dest: Path) -> Path:
    p = Path(item["path"]) if item.get("path") else None
    if p and p.is_file():
        return p
    name = item.get("file") or (item.get("id") + ".mp4")
    stock.download_assets(repo, tag, dest, [name, (item.get("sidecar") or "")])
    got = dest / name
    if not got.is_file():
        raise UploadSkip(f"{item.get('id')}: ready file {name} not in {tag}")
    item["path"] = str(got)
    return got


def _write_summary(results: list[dict], counts: dict, low: bool, warn_at: int) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = ["## Vice Cartel upload", ""]
    for r in results:
        plat = r.get("platform")
        if r.get("ok"):
            lines.append(f"- **{plat}**: posted {r.get('url') or r.get('id')} (`{r.get('video_id')}`)")
        else:
            lines.append(f"- **{plat}**: {r.get('status')}: {r.get('error') or 'n/a'}")
    lines += [
        "",
        f"Ready stock (unposted): YouTube {counts.get('youtube', 0)}, Instagram {counts.get('instagram', 0)}.",
    ]
    if low:
        lines.append(f"WARNING: ready stock is under {warn_at} for at least one platform.")
    Path(path).open("a").write("\n".join(lines) + "\n")


def _outputs(results: list[dict], counts: dict, low: bool) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    by = {r["platform"]: r for r in results}
    yt = by.get("youtube") or {}
    ig = by.get("instagram") or {}
    with open(path, "a") as f:
        f.write(f"youtube={yt.get('url') or yt.get('status') or ''}\n")
        f.write(f"instagram={ig.get('url') or ig.get('status') or ''}\n")
        f.write(f"stock_youtube={counts.get('youtube', 0)}\n")
        f.write(f"stock_instagram={counts.get('instagram', 0)}\n")
        f.write(f"low_stock={'true' if low else 'false'}\n")


def _poll_buffer(org_id: str, channel_id: str, post_id: str, timeout_s: int = 480) -> dict:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        last = buffer_ig.get_post(org_id, channel_id, post_id)
        st = (last or {}).get("status") or ""
        if st in {"sent", "error"} or (last or {}).get("externalLink"):
            return last or {}
        time.sleep(15)
    return last or {}


def _upload_youtube(item: dict, text: dict, conf: dict, dry: bool) -> dict:
    if dry:
        _log(f"DRY_RUN youtube {item['id']}: {text['title']!r}")
        return {"platform": "youtube", "video_id": item["id"], "ok": True, "status": "dry_run",
                "title": text["title"]}
    def go():
        return youtube.upload_video(
            item["path"], text["title"], text["description"], text["tags"],
            expected_channel=conf.get("youtube_channel") or "@vicecartelgta",
            category_id=str(conf.get("youtube_category_id") or "20"),
            privacy="public",
        )
    info = _retry(go, bool(conf.get("retry_once", True)), f"youtube {item['id']}")
    return {
        "platform": "youtube", "video_id": item["id"], "ok": True, "status": "posted",
        "youtube_id": info["id"], "url": info["url"], "title": text["title"],
    }


def _buffer_fallback_enabled(conf: dict) -> bool:
    env_flag = (os.environ.get("IG_BUFFER_FALLBACK") or "").strip().lower()
    if env_flag in {"1", "true", "yes"}:
        return True
    if env_flag in {"0", "false", "no"}:
        return False
    return bool(conf.get("instagram_buffer_fallback", False))


def _upload_instagram_buffer(item: dict, text: dict, conf: dict, repo: str) -> dict:
    run_id = os.environ.get("GITHUB_RUN_ID") or "local"
    xfer = None

    def go():
        nonlocal xfer
        if xfer:
            try:
                transfer.delete(repo, xfer["tag"])
            except Exception:
                pass
            xfer = None
        xfer = transfer.create(repo, Path(item["path"]), run_id=run_id)
        ch = buffer_ig.find_instagram_channel(conf.get("instagram_username") or "benreelr")
        post = buffer_ig.create_video_post(ch["id"], text["ig_caption"], xfer["url"], share_now=True)
        return ch, post

    try:
        ch, post = _retry(go, bool(conf.get("retry_once", True)), f"instagram {item['id']}")
    except Exception:
        if xfer:
            try:
                transfer.delete(repo, xfer["tag"])
            except Exception:
                pass
        raise
    # Wait until Buffer has fetched + sent, then drop the public copy.
    final = _poll_buffer(ch["organizationId"], ch["id"], post["id"])
    status = final.get("status") or post.get("status") or "unknown"
    url = final.get("externalLink") or post.get("externalLink")
    err = (final.get("error") or {}).get("message")
    if status == "error":
        # leave transfer for inspection; runner will still record failure
        raise buffer_ig.BufferError(f"Buffer post {post['id']} error: {S.redact(err or 'unknown')}")
    if status == "sent" or url:
        try:
            transfer.delete(repo, xfer["tag"])
            xfer = None
        except Exception as e:
            _log(f"could not delete transfer {xfer['tag']}: {e}")
    else:
        _log(f"Buffer post {post['id']} still {status}; leaving transfer {xfer['tag']} up")
    return {
        "platform": "instagram", "video_id": item["id"], "ok": True,
        "status": "posted" if (status == "sent" or url) else "pending",
        "buffer_post_id": post["id"], "url": url, "title": text["title"],
        "transfer_tag": None if xfer is None else xfer["tag"],
        "buffer_status": status,
    }


def _upload_instagram_graph(item: dict, text: dict, conf: dict, repo: str) -> dict:
    run_id = os.environ.get("GITHUB_RUN_ID") or "local"
    retry_once = bool(conf.get("retry_once", True))
    xfer = _retry(
        lambda: transfer.create(repo, Path(item["path"]), run_id=run_id),
        retry_once,
        f"instagram transfer {item['id']}",
    )
    try:
        res = instagram_api.publish_reel(
            xfer["url"],
            text["ig_caption"],
            expected_username=conf.get("instagram_username") or "benreelr",
            share_to_feed=True,
            retry_once=retry_once,
            version=conf.get("instagram_graph_version") or None,
            log=_log,
        )
    finally:
        try:
            transfer.delete(repo, xfer["tag"])
        except Exception as e:
            _log(f"could not delete transfer {xfer['tag']}: {e}")
    return {
        "platform": "instagram",
        "video_id": item["id"],
        "ok": True,
        "status": "posted",
        "ig_media_id": res.get("media_id"),
        "ig_container_id": res.get("container_id"),
        "url": res.get("url"),
        "title": text["title"],
        "transfer_tag": None,
    }


def _upload_instagram(item: dict, text: dict, conf: dict, dry: bool, repo: str) -> dict:
    if dry:
        _log(f"DRY_RUN instagram {item['id']}: {text['title']!r}")
        return {"platform": "instagram", "video_id": item["id"], "ok": True, "status": "dry_run",
                "title": text["title"]}
    if (conf.get("instagram_backend") or "graph").strip().lower() == "buffer":
        return _upload_instagram_buffer(item, text, conf, repo)
    try:
        return _upload_instagram_graph(item, text, conf, repo)
    except Exception as e:
        if not _buffer_fallback_enabled(conf):
            raise
        _log(f"instagram graph failed ({S.redact(str(e)[:200])}); falling back to Buffer")
        return _upload_instagram_buffer(item, text, conf, repo)


def run_ig_token_check() -> dict:
    conf = cfgmod.load()
    res = instagram_api.check_token(
        expected_username=conf.get("instagram_username") or "benreelr",
        version=conf.get("instagram_graph_version") or None,
    )
    _log(
        f"IG token check OK: @{res.get('username')} (id={res.get('id')}, "
        f"quota={res.get('quota_usage')}/{res.get('quota_total')}, "
        f"duration={res.get('quota_duration')}s, api={res.get('graph_version')})"
    )
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        lines = [
            "## Instagram Graph API token check",
            "",
            f"- **Username**: `@{res.get('username')}`",
            f"- **IG User ID**: `{res.get('id')}`",
            f"- **Publishing quota**: `{res.get('quota_usage')}/{res.get('quota_total')}` (window `{res.get('quota_duration')}s`)",
            f"- **Graph API version**: `{res.get('graph_version')}`",
        ]
        Path(summary_path).open("a").write("\n".join(lines) + "\n")
    out_path = os.environ.get("GITHUB_OUTPUT")
    if out_path:
        with open(out_path, "a") as f:
            f.write(f"username={res.get('username') or ''}\n")
            f.write(f"ig_user_id={res.get('id') or ''}\n")
            f.write(f"quota_usage={res.get('quota_usage')}\n")
            f.write(f"quota_total={res.get('quota_total')}\n")
    return res


def run_pass(mode: str, dry_run: bool = False, repo: str | None = None,
             stock_dir: Path | None = None, work_dir: Path | None = None,
             ledger_path: Path | None = None) -> dict:
    """Post exactly one ready video to YouTube and one to Instagram.

    mode=test and mode=slot share this behaviour (the workflow gate is what
    differs). Stock under warn_below is reported but does not abort a successful
    post. A failed platform is retried once, then the other platform still runs.
    """
    conf = cfgmod.load()
    repo = repo or os.environ.get("GITHUB_REPOSITORY") or "Bhupi820970/pro-edit"
    tag = conf.get("stock_release") or stock.STOCK_TAG
    work = Path(work_dir or os.environ.get("VC_STOCK_DIR") or "/tmp/vc-stock")
    work.mkdir(parents=True, exist_ok=True)

    led = ledger.load(ledger_path)
    posted = ledger.posted_ids(led)

    if stock_dir and Path(stock_dir).is_dir():
        items = stock.load_stock_dir(Path(stock_dir))
    else:
        # download manifest first, then only the files we pick
        stock.download_assets(repo, tag, work, [stock.MANIFEST_NAME, "*.mp4.json"])
        items = stock.load_stock_dir(work)
        # also merge manifest-only rows (files not yet downloaded)
    counts = stock.counts(items, posted)
    warn_at = int(conf.get("stock_warn_below") or 3)
    low = counts["youtube"] < warn_at or counts["instagram"] < warn_at
    _log(f"stock unposted: youtube={counts['youtube']} instagram={counts['instagram']} (warn below {warn_at})")
    if low:
        msg = (f"Ready stock is under {warn_at} "
               f"(YouTube {counts['youtube']}, Instagram {counts['instagram']})")
        _log("WARNING: " + msg)
        print(f"::warning::{msg}", flush=True)

    slot = None
    if mode == "slot" and not dry_run:
        slot = ledger.due_slot(conf, led)
        if not slot:
            _log("no due IST slot (already posted or none elapsed yet)")
            _write_summary([], counts, low, warn_at)
            _outputs([], counts, low)
            return {"results": [], "counts": counts, "low_stock": low, "ok": True, "skipped": "no_due_slot"}

    if not dry_run and (os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("VC_CLEAN_XFER") == "1"):
        try:
            transfer.cleanup_stale(repo)
        except Exception as e:
            _log(f"stale transfer cleanup skipped: {e}")

    gem = None if dry_run else _gemini()
    results = []
    for platform in ("youtube", "instagram"):
        item = stock.pick_next(items, platform, ledger.posted_ids(led))
        if not item:
            results.append({
                "platform": platform, "ok": False, "status": "no_stock",
                "error": f"no unposted ready video for {platform}",
            })
            continue
        try:
            path = _ensure_path(item, repo, tag, work)
            item["path"] = str(path)
            text = captions.build(item, platform, gem=gem)
            _log(f"{platform} {item['id']}: title={text['title']!r}")
            if platform == "youtube":
                rec = _upload_youtube(item, text, conf, dry_run)
            else:
                rec = _upload_instagram(item, text, conf, dry_run, repo)
            if rec.get("ok") and rec.get("status") != "dry_run":
                row = {
                    "id": item["id"],
                    "platform": platform,
                    "title": rec.get("title") or text["title"],
                    "youtube_id": rec.get("youtube_id"),
                    "youtube_url": rec.get("url") if platform == "youtube" else None,
                    "instagram_url": rec.get("url") if platform == "instagram" else None,
                    "ig_media_id": rec.get("ig_media_id"),
                    "ig_container_id": rec.get("ig_container_id"),
                    "buffer_post_id": rec.get("buffer_post_id"),
                    "buffer_status": rec.get("buffer_status"),
                    "run_id": os.environ.get("GITHUB_RUN_ID"),
                    "status": rec.get("status"),
                }
                if slot:
                    row["slot"] = slot
                ledger.record(led, row)
                ledger.save(led, ledger_path)
            results.append(rec)
        except Exception as e:
            _log(f"{platform} {item.get('id')}: FAILED: {S.redact(str(e)[:400])}")
            _log(S.redact(traceback.format_exc()[-800:]))
            results.append({
                "platform": platform, "video_id": item.get("id"), "ok": False,
                "status": "failed", "error": S.redact(str(e)[:400]),
            })
        # drop local copies of this video only (never the release, songs, or backups)
        try:
            p = Path(item.get("path") or "")
            if p.is_file() and work in p.parents:
                p.unlink()
            side = Path(item.get("sidecar") or "")
            if side.is_file() and work in side.parents:
                side.unlink()
        except OSError:
            pass

    # recount after posts
    posted = ledger.posted_ids(led)
    counts = stock.counts(items, posted)
    low = counts["youtube"] < warn_at or counts["instagram"] < warn_at
    _write_summary(results, counts, low, warn_at)
    _outputs(results, counts, low)

    # fail if a required post failed, or test mode had no stock on either side
    hard = [r for r in results if r.get("status") == "failed"]
    if mode == "test":
        missing = [r for r in results if r.get("status") == "no_stock"]
        if missing and not dry_run:
            hard.extend(missing)
    return {
        "results": results,
        "counts": counts,
        "low_stock": low,
        "ok": not hard,
    }
