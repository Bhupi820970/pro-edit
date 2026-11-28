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
    ids = set()
    try:
        from pipeline_lib.publish.ledger import posted_ids, fetch_remote
        ids = set(posted_ids())
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        if repo and os.environ.get("GITHUB_ACTIONS"):   # the checkout's ledger goes stale during a long run
            remote = fetch_remote(repo) or {}
            ids |= {r.get("id") for r in remote.get("posted") or [] if r.get("status") == "posted"}
    except Exception:
        pass
    return ids


def ready_unposted(state: State, platform: str) -> list:
    posted = _posted_ids()
    return [v for v in state.videos(platform, "ready") if v["id"] not in posted]


def pick_category(state: State, platform: str, skip=()) -> str | None:
    """Daily mix: fill each category's minimum, then up to its maximum (random order).

    Only videos that passed QA count toward today's mix (a QA-failed render is not a video), and
    categories in `skip` (no usable sources this run) are left out so the run falls back to another
    category instead of retrying a dead one for hours. None = every mix category is skipped.
    """
    mix = {c: r for c, r in CFG["platforms"][platform]["mix"].items() if c not in set(skip)}
    if not mix:
        return None
    done = {}
    for v in state.produced_today(platform):
        if v.get("state") in ("qa_failed", "rejected"):
            continue
        done[v["category"]] = done.get(v["category"], 0) + 1
    cats = list(mix)
    random.shuffle(cats)
    for c in cats:
        if done.get(c, 0) < mix[c][0]:
            return c
    for c in cats:
        if done.get(c, 0) < mix[c][1]:
            return c
    return max(mix, key=lambda c: (mix[c][1], c))


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


class AttemptTimeout(BaseException):
    """An attempt ran past its time cap (skipped so the run moves on to the next attempt).
    BaseException on purpose: the many `except Exception` blocks inside sourcing / vetting / song lookup
    must not swallow the cap (that is how a 10 min cap turned into 11-12 min gathers)."""


class StepTimer:
    """Wall-clock seconds per step of one attempt (logged + saved, so the slowest step is visible)."""

    def __init__(self):
        self.t0 = time.time()
        self.steps: dict[str, float] = {}
        self._cur = None
        self._ts = None

    def step(self, name: str):
        now = time.time()
        if self._cur:
            self.steps[self._cur] = round(self.steps.get(self._cur, 0.0) + now - self._ts, 1)
        self._cur, self._ts = name, now

    def done(self) -> dict:
        self.step(None)
        self.steps["total"] = round(time.time() - self.t0, 1)
        return dict(self.steps)


def _arm_budget(minutes: float, what: str):
    """Hard per-attempt cap (SIGALRM in the main thread; ffmpeg children of subprocess.run are killed)."""
    import signal
    if minutes <= 0 or not hasattr(signal, "SIGALRM"):
        return

    def _hit(signum, frame):
        raise AttemptTimeout(f"{what} took longer than {minutes:g} min -> skip attempt")
    signal.signal(signal.SIGALRM, _hit)
    signal.setitimer(signal.ITIMER_REAL, minutes * 60)


def _disarm_budget():
    import signal
    if hasattr(signal, "SIGALRM"):
        signal.setitimer(signal.ITIMER_REAL, 0)


def plan_seconds(segs, period: float, black_tail: float = 0.6) -> float:
    return sum(s.beats for s in segs) * period + black_tail


def produce_one(platform: str, category: str | None, state: State, gem, sourcer, attempt_seed: int,
                timer: StepTimer | None = None) -> dict:
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
    T = timer or StepTimer()
    pre_cap = float(os.environ.get("VICE_ATTEMPT_MAX_MIN", CFG.get("attempt_max_min", 10)))
    render_cap = float(os.environ.get("VICE_RENDER_MAX_MIN", CFG.get("render_max_min", 15)))
    _arm_budget(pre_cap, "sourcing + song + scan/plan")
    T.step("gather")
    want = ccfg.get("sources_per_video", 3)
    clips = sourcer.gather(category, attempt_seed, want, need_dialogue=1)
    from pipeline_lib.titles import (
        SINGLE_WORK_CATEGORIES, restrict_to_single_player, restrict_to_single_work,
    )

    def restrict(cl):
        if category in SINGLE_WORK_CATEGORIES:
            return restrict_to_single_work(cl, category, attempt_seed)
        if category == "sports":
            return restrict_to_single_player(cl, attempt_seed)
        return cl
    if category in SINGLE_WORK_CATEGORIES:
        clips = restrict_to_single_work(clips, category, attempt_seed)
        if not clips:
            raise CategoryUnavailable(f"no clips of a single title for {category}", category)
    elif category == "sports":
        clips = restrict_to_single_player(clips, attempt_seed)
        if not clips:
            raise CategoryUnavailable("no clips of a single star player for sports", category)
        if not any(c.get("has_dialogue") for c in clips):
            raise CategoryUnavailable("sports edit needs a spoken interview/press clip of the same player", category)
    elif len(clips) < 2:
        raise CategoryUnavailable(f"only {len(clips)} clean sources for {category}", category)
    # one upload (e.g. a single bloopers reel) is not enough for a real edit: need 2+ different videos
    # (gta: only a few official Rockstar trailers exist, chunks of one trailer are fine)
    min_src = int(ccfg.get("min_distinct_sources", 1 if category == "gta" else CFG.get("min_distinct_sources", 2)))
    if len({c.get("src_id") or c.get("id") for c in clips}) < min_src:
        raise CategoryUnavailable(f"only {len({c.get('src_id') or c.get('id') for c in clips})} distinct source "
                                  f"video(s) for {category}, need {min_src}", category)
    if not any(c.get("has_dialogue") for c in clips):
        log("warning: no clip with dialogue; edit will be montage-only (no captions)")
    banned = set()
    T.step("song")
    song = pick_song(gem, state, category, clips, CFG, banned)
    log(f"song: {song['title']} – {song['artist']} ({song['url']})")
    pattern = state.pick_pattern(platform, CFG.get("pattern_cooldown_days", 2.5))
    log(f"caption pattern: {pattern} {CP.get(pattern)['name']}")
    work = home() / "work" / vid
    cdir = work / "clips"
    cdir.mkdir(parents=True, exist_ok=True)
    try:
        exclude = {}

        def link_clips(cl):
            for c in cl:
                p = Path(c["path"])
                link = cdir / f"{c['id']}{p.suffix}"
                if not link.exists():
                    link.symlink_to(p.resolve())
                exclude[p.name] = c.get("bad_ranges", [])   # auto_plan sees resolved file names
                exclude[link.name] = c.get("bad_ranges", [])
        link_clips(clips)
        T.step("beats")
        song_p = Path(song["path"])
        binfo = V.detect_beats(song_p)
        prefer_dlg = []
        if category == "sports":
            pref_re = re.compile(r"interview|press conference|presser|media day|speaks|exit interview", re.I)
            prefer_dlg = [c["path"] for c in clips if pref_re.search(c.get("title") or "")]
        lo, hi = CFG.get("qa_duration", [28, 50])
        min_s = lo + 1.5      # margin over the QA floor (frame rounding of the beat grid)

        def plan(min_seconds=None):
            return V.auto_plan(song_p, cdir, CFG["duration"], binfo, exclude=exclude, seed=attempt_seed,
                               avoid=state.avoid_windows(), open_with_dialogue=(category == "sports"),
                               prefer_dialogue_from=prefer_dlg, min_seconds=min_seconds)[0]
        T.step("scan_plan")
        segs = plan()
        planned = plan_seconds(segs, binfo["period"])
        if planned < min_s:
            # 1) combine more box clips (more sources + more 5-min chunks of the vetted ones)
            T.step("more_clips")
            log(f"plan only {planned:.1f}s (< {min_s:.1f}s): adding more vetted box clips before render")
            more = restrict(sourcer.gather(category, attempt_seed, want + 3, need_dialogue=1,
                                           chunks_per_source=4)) or []
            have_ids = {c["id"] for c in clips}
            new = [c for c in more if c["id"] not in have_ids]
            if new:
                log(f"+{len(new)} clips: " + ", ".join(c["id"] for c in new)[:300])
                clips = clips + new
                link_clips(new)
            # 2) re-plan; if still short, extend/loop the beat-cut timeline to the song window
            T.step("scan_plan")
            segs = plan(min_seconds=min_s)
            planned = plan_seconds(segs, binfo["period"])
        if not (min_s - 0.01 <= planned <= hi):
            raise ShortPlan(f"plan is {planned:.1f}s, outside {lo}-{hi}s -> skipped before render")
        log(f"plan {planned:.1f}s, {len(segs)} shots -> render")
        _disarm_budget()
        _arm_budget(render_cap, "render + QA")
        T.step("render")
        wide = work / f"{vid}_wide.mp4"
        meta = V.render(segs, song_p, None, wide, "wide", pcfg["res"], None, "VICE CARTEL", None,
                        workers=os.cpu_count() or 4, crf=18, pattern=pattern)
        lines = [c["text"] for c in meta["captions"]]
        T.step("gemini_meta")
        m = write_meta(gem, platform, "Short/Reel" if fmt == "shorts" else "video", category, song, clips, lines, CFG)
        rdir = home() / "ready" / platform
        rdir.mkdir(parents=True, exist_ok=True)
        out = rdir / f"{vid}.mp4"
        if fmt == "shorts":
            T.step("shorts_9x16")
            V.make_shorts(wide, out, m["hook"], V.font_path())
        else:
            T.step("wide_4k")
            V.make_4k_wide(wide, out)
        T.step("qa")
        qa = qa_video(str(out), meta, fmt, {**CFG, "platform_res": pcfg["res"]})
        T.step("preview")
        prev = home() / "prev" / f"{vid}.mp4"
        make_preview(out, prev, fmt)
        _disarm_budget()
        T.step("save")
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
            "qa": qa, "timings": dict(T.steps),
        }
        save_json(Path(str(out) + ".json"), {**entry, "render": {k: meta[k] for k in ("song_start", "tempo", "shots", "segments")}})
        if qa["pass"]:
            state.use_pattern(pattern, vid, platform)
            state.use_song(song, vid)
            state.use_windows(meta["segments"], vid, binfo.get("period"))
            state.use_sources([c["src_id"] for c in clips], vid)
        state.add_video(entry)
        if qa["pass"]:
            twin = cross_post_twin(entry, out, CFG)
            if twin:
                state.add_video(twin)
        state.save()
    finally:
        _disarm_budget()
        shutil.rmtree(work, ignore_errors=True)
    log(f"=== {vid}: QA {'PASS' if qa['pass'] else 'FAIL'} -> {out}")
    if not qa["pass"]:
        bad = {k: v for k, v in qa["checks"].items() if not v["ok"]}
        raise QAFail(f"QA failed: {json.dumps(bad)[:400]}")
    return entry


def cross_post_twin(entry: dict, out: Path, cfg: dict) -> dict | None:
    """One render, both platforms: a passed 9:16 short is also queued for the other platform
    (same file copied to ready/<other>/, same QA). Off with "cross_post": false in pipeline.json."""
    if not cfg.get("cross_post", True) or entry.get("format") != "shorts":
        return None
    other = {"youtube": "instagram", "instagram": "youtube"}.get(entry["platform"])
    if not other:
        return None
    tid = f"{other[:2]}{entry['id'][2:]}"
    dest = home() / "ready" / other / f"{tid}.mp4"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(out, dest)
        prev = Path(entry["preview"])
        tprev = prev.with_name(f"{tid}.mp4")
        shutil.copy2(prev, tprev)
        side = load_json(Path(str(out) + ".json"), {}) or {}
    except Exception as ex:  # cross-post is a bonus; never fail the main video
        log(f"cross-post copy failed: {ex}")
        return None
    twin = {**entry, "id": tid, "platform": other, "path": str(dest), "preview": str(tprev),
            "twin_of": entry["id"], "release_url": None}
    twin.pop("release_url", None)
    save_json(Path(str(dest) + ".json"), {**side, **twin})
    log(f"cross-post: {entry['id']} also queued for {other} as {tid}")
    return twin


class QAFail(RuntimeError):
    pass


class ShortPlan(QAFail):
    """The beat-cut plan cannot reach the 28-50s window: fail fast, BEFORE the expensive render."""


class CategoryUnavailable(RuntimeError):
    """Not enough clean, vetted sources for this category right now (try another category)."""

    def __init__(self, msg: str, category: str):
        super().__init__(msg)
        self.category = category


def write_manual_pick(sourcer, platform: str, quota_out: bool) -> Path:
    """queue/needs_manual_pick.json: categories + candidates Gemini could not check (quota).

    Written every run (empty `categories` when nothing is waiting) so a stale request never lingers.
    """
    doc = sourcer.manual_pick_doc(platform)
    if not quota_out and not doc["categories"]:
        doc["reason"] = "nothing waiting: Gemini checked every clip this run"
    out = home() / "queue" / "needs_manual_pick.json"
    save_json(out, doc)
    n = sum(len(v) for v in doc["categories"].values())
    if n:
        log(f"gemini quota out: {n} clip(s) in {sorted(doc['categories'])} need a manual pick -> queue/needs_manual_pick.json")
    return out


def merge_manual_pick(inputs: list[str], out: Path) -> dict:
    """Combine per-platform needs_manual_pick.json files into one queue file (pipeline-state)."""
    merged = load_json(out, {}) or {}
    if not isinstance(merged, dict):
        merged = {}
    plats = merged.get("platforms") if isinstance(merged.get("platforms"), dict) else {}
    for f in inputs:
        d = load_json(Path(f), None)
        if isinstance(d, dict) and d.get("platform") in CFG["platforms"]:
            plats[d["platform"]] = d
    merged = {"updated": now_local().isoformat(), "platforms": plats,
              "waiting": sum(len(c) for d in plats.values() for c in (d.get("categories") or {}).values())}
    save_json(out, merged)
    return merged


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
            f"BOX FETCHER DOWN for {age:.0f}h: check the box crontab entry for box/fetch_sources.sh"}


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


def status_after_video(state: State, platform: str, video: dict, made: list) -> None:
    """After every finished video: move posted files out of vc-ready, refresh status.json (stock per platform,
    posted / unposted, last video) and push it to the pipeline-state branch so it is live mid-run."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        write_status(state, {"platform": platform, "last_video": video.get("id"), "made": [v["id"] for v in made]})
        return
    from pipeline_lib.publish import archive_posted as ap
    try:
        ap.move(repo)
    except Exception as ex:
        log("archive posted (non-fatal):", str(ex)[:200])
    st = write_status(state, {"platform": platform, "in_progress": True, "made": [v["id"] for v in made],
                              "last_video": {"id": video.get("id"), "category": video.get("category"),
                                             "title": video.get("title"), "at": now_local().isoformat()}})
    try:
        st["stock"] = ap.stock_status(repo)
        save_json(home() / "status.json", st)
        push_status_file(repo, f"{platform}/status.json", home() / "status.json")
    except Exception as ex:
        log("status push (non-fatal):", str(ex)[:200])


def push_status_file(repo: str, path: str, src: Path, branch: str = "pipeline-state") -> None:
    """PUT one file on the state branch via the contents API (argument lists, no shell)."""
    import base64
    import subprocess
    body = base64.b64encode(src.read_bytes()).decode()
    for _ in range(3):
        r = subprocess.run(["gh", "api", f"repos/{repo}/contents/{path}?ref={branch}", "-q", ".sha"],
                           capture_output=True, text=True, timeout=30)
        args = ["gh", "api", "-X", "PUT", f"repos/{repo}/contents/{path}", "-f", f"message=status: {path}",
                "-f", f"branch={branch}", "-f", f"content={body}"]
        if r.returncode == 0 and r.stdout.strip():
            args += ["-f", f"sha={r.stdout.strip()}"]
        if subprocess.run(args, capture_output=True, text=True, timeout=60).returncode == 0:
            return
    log("status push: gave up after 3 tries")


def queue_fetch(gem, sourcer, platform: str, made: list, fails: list, extra_cats=()):
    """Ask the box downloader for more clips when sourcing was thin or the runner was blocked.

    Gemini writes the search queries when it has quota; otherwise the category's seed searches are used."""
    from pipeline_lib.sourcing import gemini_queries
    cats = list(sourcer.stats.get("thin", []))
    cats += [c for c in extra_cats if c not in cats and c in CFG["categories"]]
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
    # prune per category down to per_cat: clips Gemini rejected go first, then the oldest
    drop = []
    rejected = {sid for sid, v in s.d["vet"].items() if v.get("accept") is False}
    for cat in CFG["categories"]:
        items = sorted((m for m in man["clips"] if m.get("category") == cat),
                       key=lambda m: (m.get("id") not in rejected, m.get("added", "")))
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


def _below_1080(res) -> bool:
    m = re.fullmatch(r"(\d{2,5})x(\d{2,5})", str(res or ""))
    return bool(m) and min(int(m.group(1)), int(m.group(2))) < 1080


def pull_sources(repo: str, max_gb: float = 30.0) -> int:
    """Download box clips from release "sources" selectively (the pool is now ~40 per category):
    skip clips Gemini already rejected, take Gemini-accepted first, then unvetted newest, up to max_gb.
    Plain gh calls with argument lists; asset names are checked against a strict file-name pattern."""
    sdir = home() / "sources"
    sdir.mkdir(parents=True, exist_ok=True)
    r = run(["gh", "release", "view", "sources", "--repo", repo, "--json", "assets"], check=False, timeout=120)
    if r.returncode != 0:
        log("pull-sources: release 'sources' not readable:", (r.stderr or "")[-200:])
        return 0
    assets = {a["name"]: int(a.get("size") or 0) for a in json.loads(r.stdout or "{}").get("assets", [])}
    man = load_json(sdir / "manifest.json", {}) or {}
    vet = (State().d.get("vet") or {})
    name_ok = re.compile(r"^[\w.-]{1,120}\.(mkv|mp4|webm)$")
    rows = []
    for c in man.get("clips", []):
        f, sid = c.get("file") or "", c.get("id") or ""
        if f not in assets or not name_ok.match(f):
            continue
        if c.get("unusable") or _below_1080(c.get("resolution")):
            continue                      # below the 1080p floor (marked by the box audit): never pulled
        v = vet.get(sid)
        if v is not None and not v.get("accept"):
            continue                      # Gemini rejected (watermark / logo / quality): never pulled
        rank = 0 if (v or {}).get("accept") else 1
        rows.append((rank, "" if rank == 0 else str(c.get("added") or ""), f))
    acc = [x for x in rows if x[0] == 0]
    unv = sorted((x for x in rows if x[0] == 1), key=lambda x: x[1], reverse=True)
    total, n = 0.0, 0
    for _, _, f in acc + unv:
        sz = assets[f] / 1e9
        if (sdir / f).exists():
            total += sz
            continue
        if total + sz > max_gb:
            continue
        rr = run(["gh", "release", "download", "sources", "--repo", repo, "--pattern", f, "--dir", str(sdir),
                  "--skip-existing"], check=False, timeout=900)
        if rr.returncode == 0:
            total += sz
            n += 1
    log(f"pull-sources: +{n} clips, {len(acc)} accepted / {len(unv)} unvetted listed, {total:.1f} GB local")
    return n


def cmd_produce(a, count: int | None = None, until_target: bool = False):
    from pipeline_lib.gemini import Gemini
    from pipeline_lib.sourcing import Sourcer
    state = State()
    gem = Gemini(log=log)
    sourcer = Sourcer(CFG, state, gem)
    from pipeline_lib.clip_reserve import Reservations
    owner = f"{os.environ.get('GITHUB_RUN_ID') or 'local'}-{os.environ.get('GITHUB_RUN_ATTEMPT') or '1'}-{a.platform}"
    reserv = Reservations(owner, a.platform, log=log)
    sourcer.reserv = reserv
    made, fails = [], []
    t0 = time.time()
    target = a.target if until_target else None
    # One attempt can take up to pre_cap + render_cap minutes, so none starts with less time left:
    # the job ends by max_minutes instead of max_minutes + a whole attempt.
    attempt_cap = (float(os.environ.get("VICE_ATTEMPT_MAX_MIN", CFG.get("attempt_max_min", 10)))
                   + float(os.environ.get("VICE_RENDER_MAX_MIN", CFG.get("render_max_min", 15))))

    if gem.quota_exhausted() and os.environ.get("VICE_IGNORE_QUOTA_GATE", "").lower() not in ("1", "true", "yes"):
        # Fail fast: with every key and every backup model out of quota nothing new can be vetted or
        # QA'd, so attempts only burn the job (2 h, 0 videos on 26 Nov). End now with a clear status;
        # the watchdog reads it (and gemini_quota.json) and waits for the daily reset.
        gs = gem.status()
        msg = (f"Gemini quota out on every key and every backup model until {gs.get('reset_at')}; "
               "no attempts this run")
        log(msg)
        print(f"::warning title=Gemini quota out::{msg}", flush=True)
        st = write_status(state, {"platform": a.platform, "made": [], "failures": [], "attempts": [],
                                  "quota_blocked": True, "gemini": gs, "message": msg})
        log(f"done: 0 made (quota blocked); {a.platform} buffer "
            f"{st['platforms'][a.platform]['ready']}/{st['platforms'][a.platform]['target']}")
        return 0

    def more():
        left = (a.max_minutes - (time.time() - t0) / 60) if a.max_minutes else None
        if left is not None and left <= 0:
            log("time budget reached")
            return False
        if left is not None and attempts and left < attempt_cap:
            log(f"time budget reached ({left:.0f} min left < {attempt_cap:.0f} min for one more attempt)")
            return False
        if until_target:
            return len(ready_unposted(state, a.platform)) < target
        return len(made) < count

    max_attempts = (count or target or 1) * 4 + 2
    attempts = 0
    skip: set[str] = set()          # categories with no usable sources this run
    cat_fails: dict[str, int] = {}
    forced = a.category
    attempt_log: list[dict] = []
    quota_stop = False
    while more() and attempts < max_attempts:
        attempts += 1
        made_before = len(made)
        seed = random.randrange(1 << 30)
        cat = forced if forced and forced not in skip else pick_category(state, a.platform, skip)
        if cat is None:
            log(f"no category has usable sources right now (skipped: {sorted(skip)}) -> stop, box fetcher refills")
            break
        timer = StepTimer()
        held_before = set(reserv.mine)
        try:
            made.append(produce_one(a.platform, cat, state, gem, sourcer, seed, timer))
            # clips of the finished video are marked used for good right now (not 2 days later after
            # posting); clips this attempt reserved but did not use go back to the pool
            used_now = [s.get("src_id") or s.get("id") for s in made[-1].get("sources") or []]
            try:
                reserv.confirm(used_now, made[-1]["id"])
            except Exception as rex:
                log("clip reservations: confirm failed (reservation stays until it expires):", type(rex).__name__)
            reserv.release(reserv.mine - held_before)
            # put each finished video into stock right away (Actions sets VICE_ATTACH_EACH_TAG), so an
            # upload slot during a long run already has it; the end-of-run attach step is idempotent
            each_tag = os.environ.get("VICE_ATTACH_EACH_TAG", "").strip()
            if each_tag:
                try:
                    attach_release(state, each_tag, os.environ.get("GITHUB_REPOSITORY", "Bhupi820970/pro-edit"))
                except Exception as aex:
                    log("per-video stock attach failed (end-of-run step retries):", str(aex)[:200])
            try:
                status_after_video(state, a.platform, made[-1], made)
            except Exception as sex:
                log("status after video (non-fatal):", str(sex)[:200])
        except (Exception, AttemptTimeout) as ex:   # never a failed slot: retry with new sources / song / seed
            msg = f"{type(ex).__name__}: {str(ex)[:300]}"
            log("attempt failed, retrying with different sources/song:", msg)
            if not isinstance(ex, (QAFail, CategoryUnavailable, AttemptTimeout)):
                log(traceback.format_exc()[-1500:])
            state.d.setdefault("failures", []).append({"at": now_local().isoformat(), "platform": a.platform,
                                                       "category": cat, "error": msg})
            fails.append(msg)
            reserv.release(reserv.mine - held_before)   # render / QA failed: its clips are free again
            state.save()
            # too little footage for a montage is the same as no sources: don't retry the same clip 6x
            if isinstance(ex, CategoryUnavailable) or "not enough distinct montage shots" in str(ex):
                cat_fails[cat] = cat_fails.get(cat, 0) + 1
                # quota out: Gemini can't vet anything new, so one miss is enough; else allow one retry
                # one miss is enough to move on (a second try at an empty topic only burns the time budget)
                if cat_fails[cat] >= 1:
                    skip.add(cat)
                    nxt = pick_category(state, a.platform, skip)
                    log(f"category {cat}: no usable sources -> fallback to {nxt or 'nothing (all skipped)'}")
            # User (Nov 28): a missing topic is never a reason to stop. If this category had no sources,
            # move on to the next one (gta -> other games -> movie/series...) even while Gemini is out;
            # the backup AI chain vets/picks. Stop on quota only when no fallback category is left.
            fallback_left = pick_category(state, a.platform, skip) is not None
            no_src = isinstance(ex, CategoryUnavailable) or "not enough distinct montage shots" in str(ex)
            if (gem.quota_exhausted() and not (no_src and fallback_left)
                    and os.environ.get("VICE_IGNORE_QUOTA_GATE", "").lower() not in ("1", "true", "yes")):
                quota_stop = True
        finally:
            _disarm_budget()
            tm = timer.done()
            ok = len(made) > made_before
            attempt_log.append({"attempt": attempts, "category": cat, "ok": ok, "timings": tm})
            log(f"attempt {attempts} {'OK' if ok else 'FAIL'} in {tm['total']:.0f}s: "
                + ", ".join(f"{k}={v:.0f}s" for k, v in tm.items() if k != "total"))
        sourcer.cleanup(CFG.get("source_cache_gb", 12))
        if quota_stop:
            log("Gemini quota ran out on every key and backup model mid-run -> stop (watchdog waits for the reset)")
            break
    step_tot: dict[str, float] = {}
    for at in attempt_log:
        for k, v in at["timings"].items():
            if k != "total":
                step_tot[k] = round(step_tot.get(k, 0.0) + v, 1)
    slowest = max(step_tot.items(), key=lambda kv: kv[1]) if step_tot else None
    if slowest:
        log(f"step totals over {len(attempt_log)} attempts: "
            + ", ".join(f"{k}={v / 60:.1f}min" for k, v in sorted(step_tot.items(), key=lambda kv: -kv[1]))
            + f" -> slowest step: {slowest[0]}")
    extra = {"platform": a.platform, "made": [v["id"] for v in made], "failures": fails,
             "attempts": attempt_log, "step_totals_s": step_tot, "slowest_step": slowest[0] if slowest else None,
             "sourcing": sourcer.stats, "gemini_calls": [{"key": k, "model": m, "ok": ok, "note": n}
                                                         for k, m, ok, n in gem.calls[-60:]]}
    extra["skipped_categories"] = sorted(skip)
    extra["quota_blocked"] = bool(quota_stop)
    try:
        gs = gem.status()                  # labels / model names only
        if isinstance(gs, dict):
            extra["gemini"] = gs
    except Exception:
        pass
    try:
        queue_fetch(gem, sourcer, a.platform, made, fails, extra_cats=sorted(skip))
    except Exception as ex:
        log("queue_fetch failed:", str(ex)[:200])
    write_manual_pick(sourcer, a.platform, gem.quota_exhausted())
    st = write_status(state, extra)
    save_json(home() / "logs" / f"run_{now_local().strftime('%Y%m%d-%H%M%S')}_{a.platform}.json", extra)
    from pipeline_lib.cleanup import sweep
    sweep(state)
    log(f"done: {len(made)} made, {len(fails)} failed attempts; {a.platform} buffer "
        f"{st['platforms'][a.platform]['ready']}/{st['platforms'][a.platform]['target']} — {st['message']}")
    return 0 if made or (until_target and not more()) else 1


def attach_release(s: State, tag: str, repo: str) -> int:
    """Upload ready, unposted videos (mp4 + preview + sidecar) to the draft release and update its manifest.
    Idempotent; used per video during a produce run (stock is usable right away) and again at the end."""
    from pipeline_lib.publish import stock as vc_stock
    posted = _posted_ids()
    new = [v for v in s.d["videos"]
           if v["state"] == "ready" and v["id"] not in posted and Path(v.get("path") or "").exists()]
    # Stable stock tag: re-attach anything not yet on vc-ready. Test tags only get new files.
    if tag == vc_stock.STOCK_TAG:
        new = [v for v in new if f"/download/{tag}/" not in (v.get("release_url") or "")]
    else:
        new = [v for v in new if not v.get("release_url")]
    if new:
        r = run(["gh", "release", "view", tag, "--repo", repo], check=False)
        if r.returncode != 0:
            notes = ("DRAFT: Vice Cartel ready stock (unposted renders). Keep draft so "
                     "footage stays private. The upload workflow reads from here.")
            if tag != vc_stock.STOCK_TAG:
                notes = ("DRAFT: rendered videos waiting for owner approval (not posted anywhere). "
                         "Keep draft so assets stay private if the repo is public.")
            title = "Vice Cartel ready stock" if tag == vc_stock.STOCK_TAG else tag
            run(["gh", "release", "create", tag, "--repo", repo, "--title", title, "--draft", "--notes", notes])
            time.sleep(2)
        for v in new:
            attach_video_assets(tag, repo, v)
        s.save()
    if tag == vc_stock.STOCK_TAG:
        recs = [vc_stock.video_record(v) for v in s.d["videos"]
                if v["state"] == "ready" and v["id"] not in posted
                and (f"/download/{tag}/" in (v.get("release_url") or "") or v in new)]
        dest_dir = Path("/tmp/vc-ready-man")
        dest_dir.mkdir(parents=True, exist_ok=True)
        run(["gh", "release", "download", tag, "--repo", repo, "-p", vc_stock.MANIFEST_NAME,
             "-D", str(dest_dir), "--clobber"], check=False)
        dest = dest_dir / vc_stock.MANIFEST_NAME
        man = vc_stock.upsert_manifest(dest, recs)
        run(["gh", "release", "upload", tag, str(dest), "--repo", repo, "--clobber"], timeout=120, check=False)
        log(f"vc-ready manifest: {len(man.get('videos') or [])} videos")
    return len(new)


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
    sub.add_parser("gemini-check", help="which Gemini models exist / answer now (names + codes only)")
    p = sub.add_parser("pool-sync")
    p.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "Bhupi820970/pro-edit"))
    p = sub.add_parser("pull-sources")
    p.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "Bhupi820970/pro-edit"))
    p.add_argument("--max-gb", type=float, default=30.0)
    p = sub.add_parser("merge-manual-pick")
    p.add_argument("--inputs", nargs="+", required=True)
    p.add_argument("--out", required=True)
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
    if a.cmd == "gemini-check":
        from pipeline_lib.gemini import check_models
        res = check_models(log=log)
        print(json.dumps(res, indent=1))
        save_json(home() / "gemini_check.json", {**res, "at": now_local().isoformat()})
        return 0
    if a.cmd == "pull-sources":
        pull_sources(a.repo, a.max_gb)
        return 0
    if a.cmd == "pool-sync":
        pool_sync(a.repo)
        return 0
    if a.cmd == "merge-manual-pick":
        m = merge_manual_pick(a.inputs, Path(a.out))
        print(f"needs_manual_pick: {m['waiting']} clip(s) waiting")
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
        s = State()
        attach_release(s, a.tag, a.repo)
        write_status(s)
        return 0
    with Lock():
        if a.cmd == "produce":
            return cmd_produce(a, count=a.count)
        return cmd_produce(a, until_target=True)


if __name__ == "__main__":
    sys.exit(main())

