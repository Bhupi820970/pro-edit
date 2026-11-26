"""Orchestrate one upload pass: 1 YouTube + 1 Instagram, ledger, retry-once, stock warn."""
from __future__ import annotations

import os
import time
import traceback
from pathlib import Path

from pipeline_lib.util import now_local

from . import buffer_ig, captions, cfg as cfgmod, instagram_api, ledger, live_check, stock, transfer, youtube
from . import secrets as S


class UploadSkip(Exception):
    pass


def _log(msg: str) -> None:
    print(f"[upload {now_local().strftime('%H:%M:%S')}] {S.redact(str(msg))}", flush=True)


def _retry(fn, once: bool, label: str, check=None, tries: int = 3, base: float = 8.0):
    """Up to `tries` attempts with exponential backoff (8s, 16s). Before every retry `check()` asks the
    platform whether the previous attempt actually went live; if it did, that result is returned and
    nothing is posted again."""
    n = tries if once else 1
    if os.environ.get("VC_REMOTE_LEDGER") != "1":
        check = None             # live checks need the real accounts (upload.yml); local/tests retry plainly
    if os.environ.get("PYTEST_CURRENT_TEST"):
        base = 0.0
    for i in range(n):
        try:
            return fn()
        except Exception as e:
            if i == n - 1:
                raise
            wait = base * (2 ** i)
            _log(f"{label}: attempt {i + 1}/{n} failed ({S.redact(str(e)[:300])}); retry in {wait:.0f}s")
            time.sleep(wait)
            if check:
                try:
                    hit = check()
                except Exception as ce:
                    _log(f"{label}: live check failed ({S.redact(str(ce)[:160])}) -> not retrying (no double post)")
                    raise e
                if hit:
                    _log(f"{label}: previous attempt is live after all -> not posting again")
                    return hit


def _gemini():
    try:
        from pipeline_lib.gemini import Gemini
        return Gemini(log=lambda *a: _log(" ".join(str(x) for x in a)))
    except Exception as e:
        _log(f"Gemini unavailable, using template captions: {type(e).__name__}: {S.redact(str(e)[:200])}")
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

    def live():
        hit = live_check.yt_live_copy(youtube.access_token_from_env(), item["id"], [text["title"]])
        return {"id": hit["id"], "url": f"https://youtube.com/shorts/{hit['id']}"} if hit else None
    info = _retry(go, bool(conf.get("retry_once", True)), f"youtube {item['id']}", check=live)
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
    def live():
        hit = live_check.ig_live_copy(text.get("song_credit") or "", captions.footage_credit(item.get("sources")) or "")
        return {"media_id": hit["id"], "url": hit.get("permalink")} if hit else None
    try:
        res = _retry(lambda: instagram_api.publish_reel(
            xfer["url"],
            text["ig_caption"],
            expected_username=conf.get("instagram_username") or "benreelr",
            share_to_feed=True,
            retry_once=False,          # retries are done here, each one behind a live check
            version=conf.get("instagram_graph_version") or None,
            log=_log,
        ), retry_once, f"instagram publish {item['id']}", check=live)
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

    # Dry-run still constructs Gemini so caption generation can be verified without posting.
    gem = _gemini()
    if dry_run and gem:
        sample = {
            "id": "dry-caption-check", "platform": "youtube", "category": "gta",
            "title": "Vice City never sleeps", "hook": "NIGHT DRIVE",
            "hashtags": ["GTA6"],
            "song": {"title": "Give It Up", "artist": "KC and the Sunshine Band"},
            "sources": [{"credit": "Rockstar Games", "title": "GTA VI Trailer 2"}],
        }
        try:
            got = captions._from_gemini(gem, sample, "youtube")
            if got and got.get("title"):
                _log(f"Gemini caption check: ok title={got['title']!r}")
            else:
                _log("Gemini caption check: no usable draft, would use template captions")
        except Exception as e:
            _log(f"Gemini caption check failed; template captions would be used: {type(e).__name__}: {S.redact(str(e)[:200])}")
    results = []
    remote_guard = not dry_run and os.environ.get("VC_REMOTE_LEDGER") == "1"   # set by upload.yml
    run_id = os.environ.get("GITHUB_RUN_ID") or "local"
    stale_min = float(conf.get("intent_stale_minutes") or 25)

    def live_copy(platform: str, item: dict, title: str | None) -> dict | None:
        """Is this render already live? (hidden YouTube tag / title; IG song + footage credit lines)"""
        if platform == "youtube":
            hit = live_check.yt_live_copy(youtube.access_token_from_env(), item["id"], [title or item.get("title") or ""])
            return {"youtube_id": hit["id"], "youtube_url": f"https://youtube.com/shorts/{hit['id']}"} if hit else None
        credit = captions.song_credit(item.get("song"), item.get("song_credit"))
        hit = live_check.ig_live_copy(credit or "", captions.footage_credit(item.get("sources")) or "")
        return {"ig_media_id": hit["id"], "instagram_url": hit.get("permalink")} if hit else None

    def write(row: dict, guard=None) -> bool:
        """Record a row locally and (Actions) on main with compare-and-swap. guard(d) -> reason aborts."""
        nonlocal led
        if not remote_guard:
            ledger.record(led, row)
            ledger.save(led, ledger_path)
            return True
        aborted = []

        def mut(d):
            if guard:
                why = guard(d)
                if why:
                    aborted.append(why)
                    return None
            ledger.record(d, row)
            return d
        res = ledger.cas_update(repo, mut, f"upload: {row.get('status')} {row['id']} run {run_id}", log=_log)
        if res is None or aborted:
            if aborted:
                _log(f"{row['platform']}: not posting ({aborted[0]})")
            if res is not None:
                led = ledger.merge(led, res)
            return False
        led = ledger.merge(led, res)
        ledger.record(led, row)
        ledger.save(led, ledger_path)
        return True

    by_id = {it.get("id"): it for it in items}
    for platform in ("youtube", "instagram"):
        if remote_guard:
            # another upload run may have posted since this one checked out: re-read main's ledger first
            remote = ledger.fetch_remote(repo)
            if remote is None and slot:
                _log(f"{platform}: cannot read the ledger on main -> not posting (idempotency guard)")
                results.append({"platform": platform, "ok": True, "status": "skipped_ledger_unreadable"})
                continue
            if remote is not None:
                led = ledger.merge(led, remote)
            # reconcile intents left by runs that died between "posting" and the result
            busy = None
            for e in ledger.open_intents(led, platform):
                if e.get("run_id") == run_id:
                    continue
                if ledger._age_min(e) < stale_min:
                    busy = e
                    continue
                try:
                    hit = live_copy(platform, by_id.get(e["id"]) or {"id": e["id"], "title": e.get("title")},
                                    e.get("title"))
                except Exception as ex:
                    _log(f"{platform}: cannot verify old intent {e['id']} ({S.redact(str(ex)[:160])}) -> not posting")
                    busy = e
                    continue
                if hit:
                    write({**e, **hit, "status": "posted", "reconciled": True})
                    _log(f"{platform}: old intent {e['id']} is live -> recorded as posted")
                else:
                    write({**e, "status": "abandoned", "slot": None, "abandoned_slot": e.get("slot")})
                    _log(f"{platform}: old intent {e['id']} never went live -> abandoned (can be posted later)")
            if busy:
                _log(f"{platform}: run {busy.get('run_id')} is posting {busy.get('id')} right now -> skip")
                results.append({"platform": platform, "ok": True, "status": "another_run_posting",
                                "video_id": busy.get("id")})
                continue
        taken = ledger.slot_taken(led, slot, platform)
        if taken:
            _log(f"{platform}: slot {slot} already has {taken.get('id')} (run {taken.get('run_id')}) -> skip")
            results.append({"platform": platform, "ok": True, "status": "slot_already_posted",
                            "video_id": taken.get("id")})
            continue
        item = None
        for _ in range(3):   # skip renders that turn out to be live already (recorded, never re-posted)
            item = stock.pick_next(items, platform, ledger.posted_ids(led))
            if not item or not remote_guard:
                break
            try:
                hit = live_copy(platform, item, item.get("title"))
            except Exception as ex:
                _log(f"{platform}: live pre-check failed ({S.redact(str(ex)[:160])}) -> not posting this slot")
                item = "check_failed"
                break
            if not hit:
                break
            write({"id": item["id"], "platform": platform, "status": "posted", "already_live": True,
                   "run_id": run_id, "title": item.get("title"), **hit})
            _log(f"{platform}: {item['id']} is already live -> recorded, picking the next one")
            results.append({"platform": platform, "video_id": item["id"], "ok": True, "status": "already_live"})
            item = None
        if item == "check_failed":
            results.append({"platform": platform, "ok": False, "status": "failed",
                            "error": "live pre-check failed; not posting blind"})
            continue
        if not item:
            if not any(r.get("platform") == platform and r.get("status") == "already_live" for r in results):
                results.append({
                    "platform": platform, "ok": False, "status": "no_stock",
                    "error": f"no unposted ready video for {platform}",
                })
            continue
        intent = None
        try:
            path = _ensure_path(item, repo, tag, work)
            item["path"] = str(path)
            text = captions.build(item, platform, gem=gem)
            if platform == "youtube":
                text["tags"] = list(text.get("tags") or [])[:12] + [live_check.vc_tag(item["id"])]
            _log(f"{platform} {item['id']}: title={text['title']!r}")
            if remote_guard:        # intent lock on main (Actions upload only)
                intent = {"id": item["id"], "platform": platform, "status": "posting", "run_id": run_id,
                          "intent_at": now_local().isoformat(), "title": text["title"]}
                if slot:
                    intent["slot"] = slot

                def guard(d, _p=platform, _id=item["id"]):
                    if ledger.slot_taken(d, slot, _p):
                        return f"slot {slot} taken meanwhile"
                    if any(e.get("run_id") != run_id and ledger._age_min(e) < stale_min
                           for e in ledger.open_intents(d, _p)):
                        return "another run is posting"
                    if _id in ledger.posted_ids(d) and (ledger.entry_for(d, _id) or {}).get("run_id") != run_id:
                        return "video recorded by another run"
                    return None
                if not write(intent, guard):
                    results.append({"platform": platform, "ok": True, "status": "lock_not_acquired",
                                    "video_id": item["id"]})
                    intent = None
                    continue
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
                if not write(row):
                    _log(f"{platform}: posted but main's ledger write failed; the local ledger + commit step keep it")
                    ledger.record(led, row)
                    ledger.save(led, ledger_path)
            results.append(rec)
        except Exception as e:
            _log(f"{platform} {item.get('id')}: FAILED: {S.redact(str(e)[:400])}")
            _log(S.redact(traceback.format_exc()[-800:]))
            if intent:   # reconcile: did it go live anyway? never leave a silent double-post path
                hit = None
                try:
                    hit = live_copy(platform, item, intent.get("title"))
                except Exception as ce:
                    _log(f"{platform}: post-failure live check failed ({S.redact(str(ce)[:160])}); intent kept as lock")
                    hit = "unknown"
                if hit == "unknown":
                    pass        # row stays "posting" -> next run reconciles after intent_stale_minutes
                elif hit:
                    write({**intent, **hit, "status": "posted", "reconciled": True})
                else:
                    write({**intent, "status": "failed", "slot": None, "failed_slot": slot,
                           "error": S.redact(str(e)[:200])})
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
