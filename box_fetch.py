#!/usr/bin/env python3
"""box_fetch — the BOX downloads source clips for the GitHub Actions pipeline (zero bot usage).

Why: GitHub-hosted runners often hit YouTube's "confirm you're not a bot" wall and can't use X.
How it fits:
  1. Actions runs write download requests (category + Gemini-written search queries + links) to
     `fetch_queue.json` on branch `pipeline-state` whenever sourcing was thin or blocked.
  2. This script, running as a plain background loop on the box (no bot involved), reads the queue,
     downloads with yt-dlp, and uploads the clips + `manifest.json` to release tag `sources`.
     Every cycle it also tops up a pool of clips per category (enough for 2-3 days), downloads
     official song audio into draft release `songs` (with `manifest.json`), and uploads
     `box_heartbeat.json` (last run time) — status.json shows it as `box_fetcher`.
  3. Actions pulls the `sources` release into its source cache and the `songs` release into the
     songs cache. Gemini still watch-checks every clip. The runner uses box songs first.
If the box is offline, Actions still works: runner download (retries + alternate clients) ->
cached pool in the `sources` release -> the 7-video ready buffer covers the gap.

  python3 box_fetch.py once --category gta --limit 4 [--links urls.txt] [--query "..."]
  python3 box_fetch.py songs                         # top up official audio into draft release "songs"
  python3 box_fetch.py loop --interval 3600          # what scripts/box_fetch_service.sh runs
  bash scripts/box_fetch_service.sh start|stop|status|logs   # one command after a box reset: start

--cookies-from-browser is OFF by default (opt-in; yt-dlp then reads the local browser cookies for that
download only; cookies are never written to the repo, release, logs or manifest).
Source / video releases are always created as DRAFTS (owner/write access only), so copyrighted
footage stays private even if the repo is public. Never publish those drafts.

Security (see SECURITY.md): this script never runs code or commands from the repo, issues, PRs or
Gemini. It only reads a JSON queue (owner commits, or workflow-bot commits tied to a produce.yml run), validates every URL (https
youtube.com / youtu.be / x.com / twitter.com, <=200 chars, no shell characters) and query, and calls
yt-dlp with an argument list (never shell=True). The box never auto-pulls code; update it with
scripts/box_update.sh, which only fast-forwards main when the commits are authored by the owner.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import socket
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from pipeline_lib.util import run, log, now_local  # noqa: E402
from pipeline_lib import sourcing as S  # noqa: E402
from pipeline_lib.queue_trust import REPO as TRUST_REPO, trust_queue_commit  # noqa: E402

CFG = json.loads((HERE / "config" / "pipeline.json").read_text())
TAG = "sources"
OWNER = "Bhupi820970"
# Queue commits: owner, or the workflow bot + a produce.yml run check (see pipeline_lib/queue_trust.py).
URL_RX = re.compile(r"^https://(www\.|m\.|mobile\.)?(youtube\.com/(watch\?v=[\w-]{11}|shorts/[\w-]{11})|"
                    r"youtu\.be/[\w-]{11}|(x|twitter)\.com/\w{1,30}/status/\d{5,25})([?&][\w=&%.-]{0,60})?$")
QUERY_RX = re.compile(r"^[\w '.,:!?()&-]{3,120}$")
BAD_CHARS = set(";|`$<>\\\"{}[]*\n\r\t")


def safe_url(u) -> str | None:
    """Only plain https YouTube / X links; anything else (shell chars, other hosts, options) is dropped."""
    if not isinstance(u, str) or len(u) > 200 or u.startswith("-") or set(u) & BAD_CHARS:
        return None
    return u if URL_RX.match(u) else None


def safe_query(q) -> str | None:
    if not isinstance(q, str) or len(q) > 120 or q.startswith("-") or set(q) & BAD_CHARS:
        return None
    return q.strip() if QUERY_RX.match(q.strip()) else None


def safe_request(r) -> dict | None:
    if not isinstance(r, dict) or r.get("category") not in CFG["categories"]:
        return None
    rid = str(r.get("id", ""))
    if not re.match(r"^[\w.-]{1,80}$", rid):
        return None
    try:
        lim = max(1, min(5, int(r.get("limit", 3))))
    except Exception:
        lim = 3
    links = [u for u in (safe_url(x) for x in (r.get("links") or [])[:10]) if u]
    qs = [q for q in (safe_query(x) for x in (r.get("queries") or [])[:6]) if q]
    return {"id": rid, "category": r["category"], "limit": lim, "links": links, "queries": qs}
_DEFAULT_POOL = 15
POOL_TARGET = {cat: int((CFG.get("pool_target") or {}).get(cat, _DEFAULT_POOL))
               for cat in CFG["categories"]}
SONG_TAG = "songs"


def gh(*a, check=True, timeout=7200):
    return run(["gh", *a], check=check, timeout=timeout)


class Box:
    def __init__(self, a):
        self.a = a
        self.repo = a.repo
        self.dir = a.dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.extra = ["--cookies-from-browser", a.cookies_from_browser] if a.cookies_from_browser else []

    # ------------------------------------------------------------ release helpers
    def ensure_release(self):
        if gh("release", "view", TAG, "--repo", self.repo, check=False).returncode != 0:
            gh("release", "create", TAG, "--repo", self.repo, "--draft", "--title", "source clip pool",
               "--notes", "DRAFT: source clips for the Actions pipeline (box_fetch + runner pool). "
                          "Keep draft so footage stays private if the repo is public. Gemini vets each clip before use.")

    def manifest(self) -> dict:
        p = self.dir / "manifest.json"
        r = gh("release", "download", TAG, "--repo", self.repo, "-p", "manifest.json", "-D", str(self.dir),
               "--clobber", check=False)
        if r.returncode != 0 and not p.exists():
            return {"clips": []}
        return json.loads(p.read_text())

    def upload(self, files: list[Path]):
        self.ensure_release()
        gh("release", "upload", TAG, "--repo", self.repo, "--clobber", *[str(f) for f in files])

    # ------------------------------------------------------------ queue
    def _get_run(self, repo: str, run_id: str):
        r = gh("api", f"repos/{repo}/actions/runs/{run_id}", check=False, timeout=60)
        if r.returncode != 0:
            return None
        try:
            return json.loads(r.stdout)
        except Exception:
            return None

    def _get_compare(self, repo: str, sha: str):
        r = gh("api", f"repos/{repo}/compare/{sha}...pipeline-state", check=False, timeout=60)
        if r.returncode != 0:
            return None
        try:
            return json.loads(r.stdout)
        except Exception:
            return None

    def queue(self) -> list[dict]:
        """Read the plain-JSON queue (data only — never executed). The last commit touching each queue
        file must be on pipeline-state and either authored by the owner or by the workflow bot and
        tied to a produce.yml run (owner-triggered, no PR events). Every entry is validated."""
        reqs = []
        for path in ("youtube/fetch_queue.json", "instagram/fetch_queue.json"):
            c = gh("api", f"repos/{self.repo}/commits?sha=pipeline-state&path={path}&per_page=1",
                   check=False, timeout=60)
            try:
                commit = json.loads(c.stdout)[0]
            except Exception:
                log(f"queue {path}: could not read last commit -> ignored")
                continue
            ok, reason = trust_queue_commit(commit, path=path, repo=self.repo or TRUST_REPO,
                                            get_run=self._get_run, get_compare=self._get_compare)
            if not ok:
                log(f"queue {path}: {reason} -> ignored")
                continue
            r = gh("api", f"repos/{self.repo}/contents/{path}?ref={commit['sha']}", check=False, timeout=60)
            if r.returncode != 0:
                log(f"queue {path}: could not read file at {commit['sha'][:12]} -> ignored")
                continue
            try:
                raw = json.loads(base64.b64decode(json.loads(r.stdout)["content"]))
            except Exception:
                log(f"queue {path}: malformed JSON -> ignored")
                continue
            if not isinstance(raw, dict):
                log(f"queue {path}: top-level JSON is not an object -> ignored")
                continue
            for item in (raw.get("requests") or [])[-30:]:
                entry = safe_request(item)
                if entry:
                    reqs.append(entry)
                else:
                    log("queue entry rejected by validation:", str(item)[:120])
        return reqs

    # ------------------------------------------------------------ fetch
    def fetch(self, category: str, limit: int, links=(), queries=(), use_gemini=True) -> list[dict]:
        c = CFG["categories"][category]
        man = self.manifest()
        have = {m["id"] for m in man["clips"]}
        cands = []
        links = [u for u in (safe_url(x) for x in links) if u]
        queries = [q for q in (safe_query(x) for x in queries) if q]
        for u in links:
            m = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})", u)
            sid = m.group(1) if m else ("x" + re.search(r"status/(\d+)", u).group(1) if "status/" in u else None)
            if sid:
                cands.append({"id": sid, "url": u, "title": "", "channel": "", "via": "box-queue-link"})
        for ch in c.get("channels", []):
            rx = re.compile(ch.get("title_re", "."), re.I)
            skip = re.compile(ch.get("skip_re", r"$^"), re.I)
            for e in S.list_channel(ch["url"], int(ch.get("limit", 80))):
                if rx.search(e["title"]) and not skip.search(e["title"]) and \
                        c.get("min_len", 30) <= (e["duration"] or 0) <= min(c.get("max_len", 600), 1800):
                    cands.append({**e, "credit": ch.get("credit"), "via": "box-official-channel"})
        allow = {a.lower() for a in c.get("allow_channels", [])}
        for q in list(queries) + c.get("search", []):
            for e in S.search_youtube(q, 15):
                chn = (e.get("channel") or "").lower()
                if (e.get("verified") or chn in allow) and \
                        c.get("min_len", 30) <= (e["duration"] or 0) <= c.get("max_len", 600):
                    cands.append({**e, "credit": e["channel"], "via": "box-search"})
        if use_gemini and not self.a.no_gemini:
            try:
                from pipeline_lib.gemini import Gemini
                gem = Gemini(log=log)
                if gem.quota_exhausted():
                    log("gemini discovery skipped: all keys quota-exhausted")
                else:
                    cands += [{**e, "credit": e.get("channel")} for e in S.gemini_discover(gem, category, c)
                              if safe_url(e.get("url"))]
            except Exception as ex:
                log("gemini discovery skipped:", str(ex)[:120])
        got, recs = [], []
        for e in cands:
            if len(got) >= limit:
                break
            if e["id"] in have:
                continue
            have.add(e["id"])
            out = self.dir / f"{e['id']}.%(ext)s"
            ok, _ = S.ytdlp([*self.extra, "-f", "bv*[height<=1440]+ba/b[height<=1440]/bv*+ba/b", "-S", "res:1440,fps",
                             "--merge-output-format", "mkv", "--max-filesize", "1900M", "-o", str(out), e["url"]])
            f = next((p for p in self.dir.glob(f"{e['id']}.*") if p.suffix in (".mkv", ".mp4", ".webm")), None)
            if not ok or not f:
                log("download failed:", e["url"])
                continue
            rec = {"id": e["id"], "file": f.name, "url": e["url"], "title": e.get("title", ""),
                   "channel": e.get("channel", ""), "credit": e.get("credit") or e.get("channel", ""),
                   "category": category, "via": e.get("via"), "added": now_local().isoformat(), "by": "box"}
            got.append(f)
            recs.append(rec)
            log("fetched", f.name, rec["title"][:60])
        if got and not self.a.dry_run:
            man = self.manifest()          # re-read: Actions may have added pool clips meanwhile
            man["clips"] += recs
            (self.dir / "manifest.json").write_text(json.dumps(man, indent=1))
            self.upload(got + [self.dir / "manifest.json"])
            for f in got:                  # uploaded: free box disk
                f.unlink(missing_ok=True)
        return recs


    # ------------------------------------------------------------ songs (official audio for the runner)
    def ensure_songs_release(self):
        if gh("release", "view", SONG_TAG, "--repo", self.repo, check=False).returncode != 0:
            gh("release", "create", SONG_TAG, "--repo", self.repo, "--draft", "--title", "official song pool",
               "--notes", "DRAFT: official artist/label audio for the Actions pipeline (box_fetch). "
                          "Keep draft so audio stays private if the repo is public. Runner uses this pool first.")

    def song_manifest(self) -> dict:
        sdir = self.dir / "songs"
        sdir.mkdir(parents=True, exist_ok=True)
        p = sdir / "manifest.json"
        r = gh("release", "download", SONG_TAG, "--repo", self.repo, "-p", "manifest.json", "-D", str(sdir),
               "--clobber", check=False)
        if r.returncode != 0 and not p.exists():
            return {"songs": []}
        try:
            data = json.loads(p.read_text())
        except Exception:
            return {"songs": []}
        if not isinstance(data, dict) or not isinstance(data.get("songs"), list):
            return {"songs": []}
        return data

    def upload_songs(self, files: list[Path]):
        self.ensure_songs_release()
        gh("release", "upload", SONG_TAG, "--repo", self.repo, "--clobber", *[str(f) for f in files])

    def fetch_songs(self) -> int:
        """Download official audio for the static catalogue and upload to draft release 'songs'."""
        from pipeline_lib.songs import catalogue, find_official, _norm
        sdir = self.dir / "songs"
        sdir.mkdir(parents=True, exist_ok=True)
        man = self.song_manifest()
        have = {_norm(f'{s.get("title","")} - {s.get("artist","")}') for s in man["songs"] if isinstance(s, dict)}
        have_ids = {s.get("id") for s in man["songs"] if isinstance(s, dict)}
        got, recs = [], []
        for o in catalogue(CFG):
            tag = f'{o["title"]} - {o["artist"]}'
            if _norm(tag) in have:
                continue
            off = find_official(o["title"], o["artist"], extra=self.extra)
            url = safe_url((off or {}).get("url"))
            if not off or not url or not re.fullmatch(r"[\w-]{11}", off["id"]):
                log("no official upload for", tag)
                continue
            if off["id"] in have_ids:
                have.add(_norm(tag))
                continue
            def _audio():
                return next((p for p in sdir.glob(f"{off['id']}.*")
                             if p.suffix.lower() in {".m4a", ".mp3", ".opus", ".webm", ".wav", ".aac"}
                             and p.is_file() and p.stat().st_size > 1000), None)
            f = _audio()
            if not f:
                ok, _ = S.ytdlp([*self.extra, "-f", "ba/b", "-x", "--audio-format", "m4a", "--audio-quality", "0",
                                 "-o", str(sdir / f"{off['id']}.%(ext)s"), url], timeout=900)
                f = _audio()
                if not ok or not f:
                    log("song download failed:", tag)
                    continue
            rec = {"id": off["id"], "file": f.name, "url": url, "title": o["title"], "artist": o["artist"],
                   "channel": off.get("channel", ""), "why": o.get("why", ""),
                   "added": now_local().isoformat(), "by": "box"}
            got.append(f)
            recs.append(rec)
            have.add(_norm(tag))
            have_ids.add(off["id"])
            log("fetched song", f.name, tag)
        if got and not self.a.dry_run:
            man = self.song_manifest()
            have2 = {_norm(f'{s.get("title","")} - {s.get("artist","")}') for s in man["songs"] if isinstance(s, dict)}
            man["songs"] += [r for r in recs if _norm(f'{r["title"]} - {r["artist"]}') not in have2]
            (sdir / "manifest.json").write_text(json.dumps(man, indent=1))
            self.upload_songs(got + [sdir / "manifest.json"])
            for f in got:
                f.unlink(missing_ok=True)
        return len(recs)

    def heartbeat(self, info: dict):
        hb = self.dir / "box_heartbeat.json"
        old = json.loads(hb.read_text()) if hb.exists() else {}
        done = (old.get("processed") or [])[-300:]
        done += info.pop("processed_now", [])
        hb.write_text(json.dumps({"last_run": now_local().isoformat(), "host": socket.gethostname(),
                                  "processed": done, **info}, indent=1))
        if not self.a.dry_run:
            try:
                self.upload([hb])
            except Exception as ex:
                log("heartbeat upload failed:", str(ex)[:160])
        return done

    def cycle(self):
        hb = self.dir / "box_heartbeat.json"
        done = set(json.loads(hb.read_text()).get("processed", [])) if hb.exists() else set()
        fetched, errors, now_done = 0, [], []
        for r in self.queue():
            if r.get("id") in done or r.get("category") not in CFG["categories"]:
                continue
            try:
                recs = self.fetch(r["category"], int(r.get("limit", 3)), r.get("links", []), r.get("queries", []),
                                  use_gemini=False)
                fetched += len(recs)
                now_done.append(r["id"])
            except Exception as ex:
                errors.append(f"queue {r.get('id')}: {str(ex)[:150]}")
        # pool top-up: keep >=15 distinct source ids per category using seed searches (no Gemini)
        man = self.manifest()
        for cat, tgt in POOL_TARGET.items():
            have = {m["id"] for m in man["clips"] if m.get("category") == cat}
            while len(have) < tgt:
                need = tgt - len(have)
                try:
                    recs = self.fetch(cat, need, use_gemini=False)
                except Exception as ex:
                    errors.append(f"pool {cat}: {str(ex)[:150]}")
                    break
                if not recs:
                    log(f"pool {cat}: no more official candidates ({len(have)}/{tgt})")
                    break
                fetched += len(recs)
                have |= {r["id"] for r in recs}
                man = self.manifest()
                have |= {m["id"] for m in man["clips"] if m.get("category") == cat}
        try:
            fetched += self.fetch_songs()
        except Exception as ex:
            errors.append(f"songs: {str(ex)[:150]}")
        man = self.manifest()
        pool = {cat: len({m["id"] for m in man["clips"] if m.get("category") == cat}) for cat in CFG["categories"]}
        songs_n = len(self.song_manifest().get("songs") or [])
        self.heartbeat({"fetched_last_cycle": fetched, "errors": errors[-10:], "pool": pool, "songs": songs_n,
                        "processed_now": now_done, "interval_s": self.a.interval})
        log(f"cycle done: fetched {fetched}, pool {pool}, songs {songs_n}, errors {len(errors)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["once", "loop", "songs", "cycle"], nargs="?", default="once")
    ap.add_argument("--category", choices=list(CFG["categories"]))
    ap.add_argument("--limit", type=int, default=4)
    ap.add_argument("--links", type=Path, default=None, help="file with YouTube / X URLs (one per line)")
    ap.add_argument("--query", action="append", default=[], help="extra search query (repeatable)")
    ap.add_argument("--interval", type=int, default=3600)
    ap.add_argument("--no-gemini", action="store_true")
    ap.add_argument("--cookies-from-browser", default=None, help="opt-in: chrome / firefox (local only)")
    ap.add_argument("--repo", default="Bhupi820970/pro-edit")
    ap.add_argument("--dir", type=Path, default=Path(os.environ.get("BOX_FETCH_DIR", "/workspace/gta6/box_fetch")))
    ap.add_argument("--dry-run", action="store_true", help="download only, do not upload")
    a = ap.parse_args()
    b = Box(a)
    if a.cmd == "songs":
        n = b.fetch_songs()
        b.heartbeat({"mode": "songs", "songs": len(b.song_manifest().get("songs") or [])})
        log(f"songs command done: added {n}")
        return
    if a.cmd == "cycle":
        b.cycle()
        return
    if a.cmd == "once":
        if not a.category:
            ap.error("--category is required for once")
        links = a.links.read_text().split() if a.links and a.links.exists() else []
        b.fetch(a.category, a.limit, links, a.query)
        try:
            n = b.fetch_songs()
        except Exception as ex:
            log("songs top-up failed:", str(ex)[:160])
            n = 0
        b.heartbeat({"mode": "once", "songs": len(b.song_manifest().get("songs") or [])})
        log(f"once done, songs added {n}")
        return
    while True:
        try:
            b.cycle()
        except Exception:
            log("cycle crashed:", traceback.format_exc()[-800:])
        time.sleep(a.interval)


if __name__ == "__main__":
    main()

