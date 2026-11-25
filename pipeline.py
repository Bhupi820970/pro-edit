#!/usr/bin/env python3
"""Vice Cartel content pipeline — Skywalker-style: source -> Gemini vet -> song -> edit -> QA -> ready queue.

  python pipeline.py produce --platform youtube --count 2          # make N videos
  python pipeline.py produce --platform instagram --category movie --count 1
  python pipeline.py ensure  --platform youtube [--target 7] [--max-minutes 300]   # top up the backup buffer
  python pipeline.py status                                         # rewrite + print status.json
  python pipeline.py mark --id VID --state approved|posted|rejected [--keep]
  python pipeline.py cleanup                                       # age-based sweep of posted finals + stale work
  python pipeline.py attach-release --tag videos-2026-11-19         # (Actions) upload ready mp4s, store URLs

Finished videos land in the draft release `vc-ready`. The upload workflow (gated by
SCHEDULE_ENABLED, plus a manual test-upload dispatch) posts them to YouTube and Instagram.
Env: GEMINI_API_KEY_1..13 (never printed), VICE_PIPELINE_HOME (state, sources, ready videos, status.json).
"""
from __future__ import annotations

import argparse
import json
import re
import os
import random
import shutil
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import vicestyle as V  # noqa: E402
import caption_patterns as CP  # noqa: E402
from pipeline_lib.util import home, log, run, save_json, load_json, now_local, today, Lock  # noqa: E402
from pipeline_lib.state import State  # noqa: E402
from pipeline_lib.qa import qa_video  # noqa: E402

CFG = json.loads((HERE / "config" / "pipeline.json").read_text())


def _posted_ids() -> set:
    try:
        from pipeline_lib.publish.ledger import posted_ids
        return posted_ids()
    except Exception:
        return set()


def ready_unposted(state: State, platform: str) -> list:
    posted = _posted_ids()
    return [v for v in state.videos(platform, "ready") if v["id"] not in posted]


def pick_category(state: State, platform: str) -> str:
    mix = CFG["platforms"][platform]["mix"]
    done = {}
    for v in state.produced_today(platform):
        done[v["category"]] = done.get(v["category"], 0) + 1
    # first fill each category's minimum, then up to its maximum, in random order
    cats = list(mix)
    random.shuffle(cats)
    for c in cats:
        if done.get(c, 0) < mix[c][0]:
            return c
    for c in cats:
        if done.get(c, 0) < mix[c][1]:
            return c
    return max(mix, key=lambda c: mix[c][1])


def make_preview(src: Path, dst: Path, fmt: str, max_mb: float = 24.0):
    dur = float(json.loads(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", src]).stdout)["format"]["duration"])
    vb = int((max_mb * 8e6 / dur) * 0.92 - 128e3)
    scale = "scale=1080:1920" if fmt == "shorts" else "scale=1920:1080"
    dst.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        run(["ffmpeg", "-y", "-v", "error", "-i", src, "-vf", scale, "-c:v", "libx264", "-preset", "medium",
             "-b:v", str(vb), "-maxrate", str(int(vb * 1.3)), "-bufsize", str(vb * 2), "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart", dst], timeout=3600)
        if dst.stat().st_size <= max_mb * 1e6 * 1.04:
            return
        vb = int(vb * 0.8)


def produce_one(platform: str, category: str | None, state: State, gem, sourcer, attempt_seed: int) -> dict:
    from pipeline_lib.songs import pick_song
    from pipeline_lib.meta import write_meta
    pcfg = dict(CFG["platforms"][platform])
    if os.environ.get("VICE_TEST_RES"):          # quick local smoke tests only
        pcfg["res"] = os.environ["VICE_TEST_RES"]
    fmt = pcfg["format"]
    category = category or pick_category(state, platform)
    vid = f"{platform[:2]}-{category}-{now_local().strftime('%Y%m%d-%H%M%S')}"
    log(f"=== {vid}: platform={platform} category={category} format={fmt} seed={attempt_seed}")
    ccfg = CFG["categories"][category]
    clips = sourcer.gather(category, attempt_seed, ccfg.get("sources_per_video", 3), need_dialogue=1)
    from pipeline_lib.titles import (
        SINGLE_WORK_CATEGORIES, restrict_to_single_player, restrict_to_single_work,
    )
    if category in SINGLE_WORK_CATEGORIES:
        clips = restrict_to_single_work(clips, category, attempt_seed)
        if not clips:
            raise RuntimeError(f"no clips of a single title for {category}")
    elif category == "sports":
        clips = restrict_to_single_player(clips, attempt_seed)
        if not clips:
            raise RuntimeError("no clips of a single star player for sports")
        if not any(c.get("has_dialogue") for c in clips):
            raise RuntimeError("sports edit needs a spoken interview/press clip of the same player")
    elif len(clips) < 2:
        raise RuntimeError(f"only {len(clips)} clean sources for {category}")
    if not any(c.get("has_dialogue") for c in clips):
        log("warning: no clip with dialogue; edit will be montage-only (no captions)")
    banned = set()
    song = pick_song(gem, state, category, clips, CFG, banned)
    log(f"song: {song['title']} – {song['artist']} ({song['url']})")
    pattern = state.pick_pattern(platform, CFG.get("pattern_cooldown_days", 2.5))
    log(f"caption pattern: {pattern} {CP.get(pattern)['name']}")
    work = home() / "work" / vid
    cdir = work / "clips"
    cdir.mkdir(parents=True, exist_ok=True)
    try:
        exclude = {}
        for c in clips:
            p = Path(c["path"])
            link = cdir / f"{c['id']}{p.suffix}"
            if not link.exists():
                link.symlink_to(p.resolve())
            exclude[p.name] = c.get("bad_ranges", [])   # auto_plan sees resolved file names
            exclude[link.name] = c.get("bad_ranges", [])
        song_p = Path(song["path"])
        binfo = V.detect_beats(song_p)
        prefer_dlg = []
        if category == "sports":
            pref_re = re.compile(r"interview|press conference|presser|media day|speaks|exit interview", re.I)
            prefer_dlg = [c["path"] for c in clips if pref_re.search(c.get("title") or "")]
        segs, _ = V.auto_plan(song_p, cdir, CFG["duration"], binfo, exclude=exclude, seed=attempt_seed,
                              avoid=state.avoid_windows(), open_with_dialogue=(category == "sports"),
                              prefer_dialogue_from=prefer_dlg)
        wide = work / f"{vid}_wide.mp4"
        meta = V.render(segs, song_p, None, wide, "wide", pcfg["res"], None, "VICE CARTEL", None,
                        workers=os.cpu_count() or 4, crf=18, pattern=pattern)
        lines = [c["text"] for c in meta["captions"]]
        m = write_meta(gem, platform, "Short/Reel" if fmt == "shorts" else "video", category, song, clips, lines, CFG)
        rdir = home() / "ready" / platform
        rdir.mkdir(parents=True, exist_ok=True)
        out = rdir / f"{vid}.mp4"
        if fmt == "shorts":
            V.make_shorts(wide, out, m["hook"], V.font_path())
        else:
            shutil.move(str(wide), out)
        qa = qa_video(str(out), meta, fmt, {**CFG, "platform_res": pcfg["res"]})
        prev = home() / "prev" / f"{vid}.mp4"
        make_preview(out, prev, fmt)
        entry = {
            "id": vid, "platform": platform, "category": category, "format": fmt, "state": "ready" if qa["pass"] else "qa_failed",
            "created": now_local().isoformat(), "path": str(out), "preview": str(prev),
            "preview_mb": round(prev.stat().st_size / 1e6, 1), "size_mb": round(out.stat().st_size / 1e6, 1),
            "title": m["title"], "hook": m["hook"], "description": m["description"], "hashtags": m["hashtags"],
            "song": {k: song[k] for k in ("title", "artist", "url", "official_channel", "spotify_search")},
            "song_credit": f'"{song["title"]}" – {song["artist"]}',
            "pattern": {"id": pattern, "name": CP.get(pattern)["name"]},
            "captions": lines, "duration": round(meta["duration"], 2),
            "sources": [{"id": c["id"], "src_id": c.get("src_id"), "path": c.get("path"),
                         "title": c.get("title"), "credit": c.get("credit"), "url": c.get("url")} for c in clips],
            "qa": qa,
        }
        save_json(Path(str(out) + ".json"), {**entry, "render": {k: meta[k] for k in ("song_start", "tempo", "shots", "segments")}})
        if qa["pass"]:
            state.use_pattern(pattern, vid, platform)
            state.use_song(song, vid)
            state.use_windows(meta["segments"], vid, binfo.get("period"))
            state.use_sources([c["src_id"] for c in clips], vid)
        state.add_video(entry)
        state.save()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    log(f"=== {vid}: QA {'PASS' if qa['pass'] else 'FAIL'} -> {out}")
    if not qa["pass"]:
        bad = {k: v for k, v in qa["checks"].items() if not v["ok"]}
        raise QAFail(f"QA failed: {json.dumps(bad)[:400]}")
    return entry


class QAFail(RuntimeError):
    pass


def box_health() -> dict:
    hb = load_json(home() / "sources" / "box_heartbeat.json", None)
    if not hb:
        return {"last_run": None, "healthy": False, "message": "box fetcher has never reported (start it on the box)"}
    import datetime as dt
    age = (now_local() - dt.datetime.fromisoformat(hb["last_run"])).total_seconds() / 3600
    lim = max(3.0, 3 * hb.get("interval_s", 3600) / 3600)
    return {"last_run": hb["last_run"], "age_hours": round(age, 1), "healthy": age <= lim, "pool": hb.get("pool"),
            "errors": hb.get("errors", [])[-3:],
            "message": "OK" if age <= lim else
            f"BOX FETCHER DOWN for {age:.0f}h: run `bash scripts/box_fetch_service.sh start` on the box"}


def write_status(state: State, extra: dict | None = None) -> dict:
    tgt, warn_at = CFG.get("buffer_target", 7), CFG.get("buffer_warn_at", 3)
    plats, msgs = {}, []
    for p in CFG["platforms"]:
        ready = ready_unposted(state, p)
        n = len(ready)
        w = n <= warn_at
        plats[p] = {"ready": n, "target": tgt, "warning": w, "produced_today": len(state.produced_today(p)),
                    "per_day": CFG["platforms"][p]["per_day"],
                    "message": f"BACKUP LOW: refill {p} ({n}/{tgt} ready)" if w else f"{n}/{tgt} ready"}
        if w:
            msgs.append(plats[p]["message"])
    st = {
        "updated": now_local().isoformat(), "timezone": "Asia/Calcutta (IST)",
        "warning": bool(msgs), "message": " | ".join(msgs) if msgs else "OK",
        "platforms": plats,
        "ready": [v for v in state.d["videos"] if v["state"] == "ready" and v["id"] not in _posted_ids()],
        "approved": [v for v in state.d["videos"] if v["state"] == "approved"],
        "recent_failures": state.d.get("failures", [])[-15:],
        "pattern_history": state.d["patterns"][-20:],
    }
    if extra:
        st["last_run"] = extra
    st["box_fetcher"] = box_health()
    save_json(home() / "status.json", st)
    return st


def queue_fetch(gem, sourcer, platform: str, made: list, fails: list):
    """Ask the box downloader for more clips when sourcing was thin or the runner was blocked."""
    from pipeline_lib.sourcing import gemini_queries
    cats = list(sourcer.stats.get("thin", []))
    if sourcer.stats.get("download_failed") and not cats:
        cats = list({f.split(":")[0] for f in []} or {v["category"] for v in made} or
                    [c for c in CFG["platforms"][platform]["mix"]])[:2]
    qp = home() / "fetch_queue.json"
    q = load_json(qp, {"requests": []})
    q["requests"] = q["requests"][-30:]
    for c in cats:
        rid = f"{platform}-{now_local().strftime('%Y%m%d%H%M')}-{c}"
        q["requests"].append({"id": rid, "category": c, "limit": 3, "at": now_local().isoformat(),
                              "reason": f"thin={c in sourcer.stats.get('thin', [])} "
                                        f"download_failed={sourcer.stats.get('download_failed', 0)} "
                                        f"bot_checks={sourcer.stats.get('bot_checks', 0)}",
                              "queries": gemini_queries(gem, c, CFG["categories"][c]),
                              "links": []})
        log("queued box fetch request", rid)
    q["updated"] = now_local().isoformat()
    save_json(qp, q)



def attach_video_assets(tag: str, repo: str, v: dict, attempts: int = 4):
    """Upload ready mp4, a uniquely named preview, and sidecar json one file at a time.

    Ready and preview were both named {id}.mp4, so a single `gh release upload` hit
    uploads.github.com with two assets of the same name and returned HTTP 404; only
    the .json sometimes landed. Unique names + retries fix that.
    """
    vid = Path(v["path"])
    jobs = []
    if vid.exists():
        jobs.append((vid, vid.name))
    preview = Path(v.get("preview") or "")
    if preview.exists() and preview.resolve() != vid.resolve():
        dest = preview.with_name(f"{vid.stem}.preview{preview.suffix}")
        if dest.exists() or dest == preview:
            named = preview
        else:
            try:
                shutil.copy2(preview, dest)
                named = dest
            except Exception:
                named = preview
        asset = f"{vid.stem}.preview{preview.suffix}"
        if named.name != asset:
            # upload via a uniquely named copy in a temp dir if needed
            tmp = Path(v["path"]).parent / asset
            if named.resolve() != tmp.resolve():
                shutil.copy2(named, tmp)
                named = tmp
        jobs.append((named, asset))
    sidecar = Path(str(vid) + ".json")
    if sidecar.exists():
        jobs.append((sidecar, sidecar.name))
    last_err = None
    uploaded = []
    for path, asset in jobs:
        ok = False
        for i in range(attempts):
            r = run(["gh", "release", "upload", tag, str(path), "--repo", repo, "--clobber"],
                    timeout=3600, check=False)
            if r.returncode == 0:
                ok = True
                uploaded.append(asset)
                break
            last_err = (r.stderr or r.stdout or "")[-800:]
            log(f"release upload {asset} attempt {i+1}/{attempts} failed:", last_err.splitlines()[-1] if last_err else "?")
            time.sleep(2 + i * 3)
        if not ok and asset.endswith(".mp4") and not asset.endswith(".preview.mp4"):
            raise RuntimeError(f"gh release upload failed for {asset}: {last_err}")
    base = f"https://github.com/{repo}/releases/download/{tag}/"
    if vid.name in uploaded or vid.exists():
        v["release_url"] = base + vid.name
    if f"{vid.stem}.preview{Path(v.get('preview') or '.mp4').suffix}" in uploaded:
        v["preview_url"] = base + f"{vid.stem}.preview{Path(v.get('preview') or '.mp4').suffix}"
    log(f"attached {uploaded} to {tag}")


def pool_sync(repo: str, max_gb: float = 8.0, per_cat: int = 15):
    """Upload Gemini-accepted runner downloads to the `sources` release so later runs have a pool
    even if YouTube blocks the runner; prune each category to the newest `per_cat` clips."""
    s = State()
    sdir = home() / "sources"
    man_p = sdir / "manifest.json"
    run(["gh", "release", "download", "sources", "--repo", repo, "-p", "manifest.json", "-D", sdir, "--clobber"],
        check=False)
    man = load_json(man_p, {"clips": []})
    have = {m["id"] for m in man["clips"]}
    up, total = [], 0
    for sid, v in s.d["vet"].items():
        if not v.get("accept") or sid in have:
            continue
        f = next((p for p in sdir.glob(f"{sid}.*") if p.suffix in (".mkv", ".mp4", ".webm")), None)
        if not f or f.is_symlink() or f.stat().st_size > 1.9e9 or total + f.stat().st_size > max_gb * 1e9:
            continue
        total += f.stat().st_size
        up.append(f)
        man["clips"].append({"id": sid, "file": f.name, "url": v.get("url"), "title": v.get("title"),
                             "channel": v.get("channel"), "credit": v.get("credit"), "category": v.get("category"),
                             "via": "pool", "added": now_local().isoformat(), "by": "actions",
                             "vet": {"accept": True, "mood": v.get("mood"), "has_dialogue": v.get("has_dialogue")}})
    # prune oldest per category
    drop = []
    for cat in CFG["categories"]:
        items = sorted((m for m in man["clips"] if m.get("category") == cat), key=lambda m: m.get("added", ""))
        drop += items[:-per_cat] if len(items) > per_cat else []
    if up or drop:
        if run(["gh", "release", "view", "sources", "--repo", repo], check=False).returncode != 0:
            run(["gh", "release", "create", "sources", "--repo", repo, "--draft", "--title", "source clip pool",
                 "--notes", "DRAFT: source clips for the Actions pipeline. Keep draft so footage stays private if the repo is public."])
        for m in drop:
            run(["gh", "release", "delete-asset", "sources", m["file"], "--repo", repo, "-y"], check=False)
            man["clips"].remove(m)
        save_json(man_p, man)
        run(["gh", "release", "upload", "sources", "--repo", repo, "--clobber", *up, man_p], timeout=7200)
    log(f"pool-sync: uploaded {len(up)} clips ({total / 1e9:.1f} GB), pruned {len(drop)}")


def cmd_produce(a, count: int | None = None, until_target: bool = False):
    from pipeline_lib.gemini import Gemini
    from pipeline_lib.sourcing import Sourcer
    state = State()
    gem = Gemini(log=log)
    sourcer = Sourcer(CFG, state, gem)
    made, fails = [], []
    t0 = time.time()
    target = a.target if until_target else None

    def more():
        if a.max_minutes and (time.time() - t0) / 60 > a.max_minutes:
            log("time budget reached")
            return False
        if until_target:
            return len(ready_unposted(state, a.platform)) < target
        return len(made) < count

    max_attempts = (count or target or 1) * 4 + 2
    attempts = 0
    while more() and attempts < max_attempts:
        attempts += 1
        seed = random.randrange(1 << 30)
        try:
            made.append(produce_one(a.platform, a.category, state, gem, sourcer, seed))
        except Exception as ex:   # never a failed slot: log and retry with new sources / song / seed
            msg = f"{type(ex).__name__}: {str(ex)[:300]}"
            log("attempt failed, retrying with different sources/song:", msg)
            if not isinstance(ex, QAFail):
                log(traceback.format_exc()[-1500:])
            state.d.setdefault("failures", []).append({"at": now_local().isoformat(), "platform": a.platform,
                                                       "category": a.category, "error": msg})
            fails.append(msg)
            state.save()
        sourcer.cleanup(CFG.get("source_cache_gb", 12))
    extra = {"platform": a.platform, "made": [v["id"] for v in made], "failures": fails,
             "sourcing": sourcer.stats, "gemini_calls": [{"key": k, "model": m, "ok": ok, "note": n}
                                                         for k, m, ok, n in gem.calls[-60:]]}
    queue_fetch(gem, sourcer, a.platform, made, fails)
    st = write_status(state, extra)
    save_json(home() / "logs" / f"run_{now_local().strftime('%Y%m%d-%H%M%S')}_{a.platform}.json", extra)
    from pipeline_lib.cleanup import sweep
    sweep(state)
    log(f"done: {len(made)} made, {len(fails)} failed attempts; {a.platform} buffer "
        f"{st['platforms'][a.platform]['ready']}/{st['platforms'][a.platform]['target']} — {st['message']}")
    return 0 if made or (until_target and not more()) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("produce", "ensure"):
        p = sub.add_parser(n)
        p.add_argument("--platform", choices=list(CFG["platforms"]), required=True)
        p.add_argument("--category", choices=list(CFG["categories"]), default=None)
        p.add_argument("--max-minutes", type=float, default=0)
        if n == "produce":
            p.add_argument("--count", type=int, default=1)
        else:
            p.add_argument("--target", type=int, default=CFG.get("buffer_target", 7))
    sub.add_parser("status")
    p = sub.add_parser("pool-sync")
    p.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "Bhupi820970/pro-edit"))
    p = sub.add_parser("merge-status")
    p.add_argument("--inputs", nargs="+", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("mark")
    p.add_argument("--id", required=True)
    p.add_argument("--state", choices=["approved", "posted", "rejected", "ready"], required=True)
    p.add_argument("--keep", action="store_true",
                   help="keep the final mp4 after posting (skip the 24h age sweep)")
    sub.add_parser("cleanup", help="sweep posted finals older than 24h and stale work/temp older than 3 days")
    p = sub.add_parser("attach-release")
    p.add_argument("--tag", default="vc-ready")
    p.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "Bhupi820970/pro-edit"))
    a = ap.parse_args(argv)
    if a.cmd == "status":
        print(json.dumps(write_status(State()), indent=1, ensure_ascii=False)[:20000])
        return 0
    if a.cmd == "pool-sync":
        pool_sync(a.repo)
        return 0
    if a.cmd == "merge-status":   # combine per-platform status.json files (Actions state branch)
        out = {"updated": now_local().isoformat(), "timezone": "Asia/Calcutta (IST)", "platforms": {}, "ready": [],
               "approved": [], "recent_failures": []}
        for f in a.inputs:
            d = load_json(Path(f), None)
            if not d:
                continue
            plat = Path(f).parent.name
            if plat in d.get("platforms", {}):
                out["platforms"][plat] = d["platforms"][plat]
            out["ready"] += d.get("ready", [])
            out["approved"] += d.get("approved", [])
            out["recent_failures"] += d.get("recent_failures", [])
        for p in CFG["platforms"]:
            if p not in out["platforms"]:
                out["platforms"][p] = {"ready": 0, "target": CFG["buffer_target"], "warning": True,
                                       "message": f"BACKUP LOW: refill {p} (0/{CFG['buffer_target']} ready)"}
        bh = [load_json(Path(f), {}).get("box_fetcher") for f in a.inputs]
        bh = [b for b in bh if b and b.get("last_run")]
        out["box_fetcher"] = max(bh, key=lambda b: b["last_run"]) if bh else \
            {"last_run": None, "healthy": False, "message": "box fetcher has never reported"}
        msgs = [v["message"] for v in out["platforms"].values() if v.get("warning")]
        out["warning"] = bool(msgs)
        out["message"] = " | ".join(msgs) if msgs else "OK"
        save_json(Path(a.out), out)
        return 0
    if a.cmd == "cleanup":
        from pipeline_lib.cleanup import sweep
        s = State()
        sweep(s)
        write_status(s)
        return 0
    if a.cmd == "mark":
        from pipeline_lib.cleanup import after_mark
        s = State()
        hit = None
        for v in s.d["videos"]:
            if v["id"] == a.id:
                v["state"] = a.state
                v[f"{a.state}_at"] = now_local().isoformat()
                if a.keep:
                    v["keep"] = True
                hit = v
        s.save()
        if hit and a.state == "posted":
            result = after_mark(hit, s, keep=bool(a.keep))
            hit["cleanup"] = {"at": now_local().isoformat(), "freed_mb": result.freed_mb,
                              "files": len(result.deleted)}
            s.save()
        write_status(s)
        return 0
    if a.cmd == "attach-release":
        from pipeline_lib.publish import stock as vc_stock
        s = State()
        posted = _posted_ids()
        new = [v for v in s.d["videos"]
               if v["state"] == "ready" and v["id"] not in posted and Path(v.get("path") or "").exists()]
        # Stable stock tag: re-attach anything not yet on vc-ready. Test tags only get new files.
        if a.tag == vc_stock.STOCK_TAG:
            new = [v for v in new if f"/download/{a.tag}/" not in (v.get("release_url") or "")]
        else:
            new = [v for v in new if not v.get("release_url")]
        if new:
            r = run(["gh", "release", "view", a.tag, "--repo", a.repo], check=False)
            if r.returncode != 0:
                notes = ("DRAFT: Vice Cartel ready stock (unposted renders). Keep draft so "
                         "footage stays private. The upload workflow reads from here.")
                if a.tag != vc_stock.STOCK_TAG:
                    notes = ("DRAFT: rendered videos waiting for owner approval (not posted anywhere). "
                             "Keep draft so assets stay private if the repo is public.")
                title = "Vice Cartel ready stock" if a.tag == vc_stock.STOCK_TAG else a.tag
                run(["gh", "release", "create", a.tag, "--repo", a.repo, "--title", title, "--draft",
                     "--notes", notes])
                time.sleep(2)
            for v in new:
                attach_video_assets(a.tag, a.repo, v)
            s.save()
        if a.tag == vc_stock.STOCK_TAG:
            recs = [vc_stock.video_record(v) for v in s.d["videos"]
                    if v["state"] == "ready" and v["id"] not in posted
                    and (f"/download/{a.tag}/" in (v.get("release_url") or "") or v in new)]
            dest_dir = Path("/tmp/vc-ready-man")
            dest_dir.mkdir(parents=True, exist_ok=True)
            run(["gh", "release", "download", a.tag, "--repo", a.repo, "-p", vc_stock.MANIFEST_NAME,
                 "-D", str(dest_dir), "--clobber"], check=False)
            dest = dest_dir / vc_stock.MANIFEST_NAME
            man = vc_stock.upsert_manifest(dest, recs)
            run(["gh", "release", "upload", a.tag, str(dest), "--repo", a.repo, "--clobber"],
                timeout=120, check=False)
            log(f"vc-ready manifest: {len(man.get('videos') or [])} videos")
        write_status(s)
        return 0
    with Lock():
        if a.cmd == "produce":
            return cmd_produce(a, count=a.count)
        return cmd_produce(a, until_target=True)


if __name__ == "__main__":
    sys.exit(main())

