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
  python3 box_fetch.py sync                          # re-upload local / re-download clips missing from "sources"
  python3 box_fetch.py cycle --no-gemini --interval 10800   # what box/fetch_sources.sh (crontab) runs
  (the old `loop` command and scripts/box_fetch_service.sh are retired: crontab runs box/fetch_sources.sh)

--cookies-from-browser is OFF by default (opt-in; yt-dlp then reads the local browser cookies for that
download only; cookies are never written to the repo, release, logs or manifest).
Source / video releases are always created as DRAFTS (owner/write access only), so copyrighted
footage stays private even if the repo is public. Never publish those drafts.

Security (see SECURITY.md): this script never runs code or commands from the repo, issues, PRs or
Gemini. It only reads a JSON queue (owner commits, or workflow-bot commits tied to a produce.yml run), validates every URL (https
youtube.com / youtu.be only, <=200 chars, no shell characters) and query, and calls
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
import subprocess
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
# URL allowlist: plain https YouTube video links only (youtube.com watch/shorts, youtu.be).
URL_RX = re.compile(r"^https://(www\.|m\.)?(youtube\.com/(watch\?v=[\w-]{11}|shorts/[\w-]{11})|"
                    r"youtu\.be/[\w-]{11})([?&][\w=&%.-]{0,60})?$")
QUERY_RX = re.compile(r"^[\w '.,:!?()&-]{3,120}$")
BAD_CHARS = set(";|`$<>\\\"{}[]*\n\r\t")


def safe_url(u) -> str | None:
    """Only plain https YouTube links; anything else (shell chars, other hosts, options) is dropped."""
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


VIDEO_EXT = {".mkv", ".mp4", ".webm"}
AUDIO_EXT = {".m4a", ".mp3", ".opus", ".webm", ".wav", ".aac"}


def parse_commits_list(returncode, stdout, stderr=None) -> tuple[dict | None, str]:
    """Parse `gh api .../commits` output. An empty list means the path has no history yet."""
    raw = (stdout or "").strip()
    if not raw:
        rc = returncode if returncode is not None else -1
        tail = (stderr or "").strip().replace("\n", " ")[:160]
        extra = f": {tail}" if tail else ""
        return None, f"could not read last commit (gh exit {rc}{extra})"
    try:
        data = json.loads(raw)
    except Exception:
        return None, "could not read last commit (invalid JSON)"
    if isinstance(data, list):
        if not data:
            return None, "no queue file"
        first = data[0]
        if isinstance(first, dict) and first.get("sha"):
            return first, "ok"
        return None, "could not read last commit (malformed entry)"
    if isinstance(data, dict):
        msg = str(data.get("message") or data.get("error") or "unexpected object")[:160]
        return None, f"could not read last commit ({msg})"
    return None, "could not read last commit (unexpected response)"


def asset_covers(sid: str, filename: str, assets: set[str]) -> bool:
    if filename and filename in assets:
        return True
    if sid:
        return any(Path(a).stem == sid for a in assets)
    return False


def usable_for(category: str, title: str, channel: str = "") -> bool:
    """Same title rules the Actions pipeline applies (pipeline_lib/sourcing.py candidates/gather), so the
    box never fills the pool with clips the runner throws away: no trailers / teasers / promos anywhere,
    no GTA in "gaming", and sports clips must name a star player (sports edits lock to one player)."""
    from pipeline_lib.titles import is_gta_title, player_name, trailer_blocked
    title = title or ""
    if trailer_blocked(title, category):
        return False
    if category == "gaming" and is_gta_title(title):
        return False
    top = (CFG["categories"].get("gaming") or {}).get("top_games_re") if category == "gaming" else None
    if top and not re.search(top, title, re.I):
        return False            # gaming = the top-10 games only (one title per video)
    if category == "sports" and not player_name(title, channel or ""):
        return False
    return True


def rejected_from_states(states: list) -> set[str]:
    """Source ids Gemini (or a manual pick) rejected in the Actions state files — plain JSON data only."""
    out = set()
    for d in states:
        vet = d.get("vet") if isinstance(d, dict) else None
        if not isinstance(vet, dict):
            continue
        for sid, v in vet.items():
            if isinstance(v, dict) and v.get("accept") is False and isinstance(sid, str):
                out.add(sid.split("_c")[0] if re.fullmatch(r"[\w-]{11}_c\d+", sid) else sid)
    return out


def usable_pool(man: dict, assets: set[str], rejected: set[str], category: str) -> set[str]:
    """Clip ids in the release that the runner can actually use for `category`."""
    return {m["id"] for m in (man.get("clips") or []) if isinstance(m, dict) and m.get("category") == category
            and m.get("id") not in rejected and not clip_unusable(m)
            and asset_covers(m.get("id") or "", m.get("file") or "", assets)
            and usable_for(category, m.get("title") or "", m.get("channel") or m.get("credit") or "")}


def local_media(directory: Path, sid: str, exts: set[str], min_size: int = 1000) -> Path | None:
    if not sid:
        return None
    for p in directory.glob(f"{sid}.*"):
        if p.suffix.lower() in exts and p.is_file() and p.stat().st_size > min_size:
            return p
    return None


MEDIA_EXT = (".mkv", ".mp4", ".webm", ".part", ".ytdl")
SIBLING_CATS = {"movie", "series", "romance", "gaming", "sports", "marvel", "dc", "anime", "celebrity", "comedy"}


# 1080p floor (BiBi): only 1080p..1440p streams; if a video has none, yt-dlp fails and the next candidate is
# tried. Anything that still lands below 1080p is rejected after download (never upscaled as a cheat).
YTDLP_FORMAT = "bv*[height>=1080][height<=1440]+ba/b[height>=1080][height<=1440]"
MIN_HEIGHT = max(1080, int(os.environ.get("BOX_FETCH_MIN_HEIGHT", "") or CFG.get("min_height", 1080)))
POOL_TOTAL = {"target": 100, "refill_below": 40, "release_asset_limit": 1000, "asset_cleanup_at": 800,
              **(CFG.get("pool_total") or {})}


def video_size(path: Path) -> tuple[int, int] | None:
    """(width, height) of the first video stream via ffprobe (arg list, no shell)."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
                           capture_output=True, text=True, timeout=60)
        w, h = (r.stdout or "").strip().splitlines()[0].split("x")[:2]
        return int(w), int(h)
    except Exception:
        return None


def meets_min_res(size: tuple[int, int] | None, min_h: int | None = None) -> bool:
    """1080p rule: the short side must be >= 1080 (works for landscape and vertical uploads)."""
    return bool(size) and min(size) >= (min_h or MIN_HEIGHT)


def parse_res(value) -> tuple[int, int] | None:
    m = re.fullmatch(r"(\d{2,5})x(\d{2,5})", str(value or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def clip_unusable(m: dict) -> str:
    """Reason a manifest clip must be skipped (marked by the audit or a box check), else ''."""
    if m.get("unusable"):
        return str(m["unusable"])
    size = parse_res(m.get("resolution"))
    if size and not meets_min_res(size):
        return f"below {MIN_HEIGHT}p ({size[0]}x{size[1]})"
    return ""


def pool_refill_plan(counts: dict, targets: dict, refilling: bool, total_cfg: dict = POOL_TOTAL) -> tuple[dict, bool]:
    """Which genres to top up this cycle and the new refill flag.
    total < refill_below -> start a refill to ~target spread over genres (per-genre targets);
    total >= target -> stop; any genre with fewer than 2 usable clips (one title group) is always filled."""
    total = sum(counts.values())
    if total < int(total_cfg.get("refill_below", 40)):
        refilling = True
    elif total >= int(total_cfg.get("target", 100)):
        refilling = False
    need = {}
    for cat, tgt in targets.items():
        have = counts.get(cat, 0)
        if refilling and have < tgt:
            need[cat] = tgt - have
        elif have < 2:
            need[cat] = max(0, min(tgt, 3) - have)
    return {k: v for k, v in need.items() if v > 0}, refilling


def assets_to_clean(man: dict, assets: set[str], dead: set[str], limit_at: int, keep_below: int) -> list[tuple[str, str]]:
    """(clip id, asset name) pairs of used/rejected clips to delete from the release, oldest first, so the
    release stays under GitHub's ~1000-asset limit. Never touches clips that are still usable."""
    if len(assets) <= limit_at:
        return []
    rows = sorted((m for m in (man.get("clips") or []) if isinstance(m, dict) and m.get("id") in dead
                   and m.get("file") in assets), key=lambda m: m.get("added") or "")
    out, n = [], len(assets)
    for m in rows:
        if n <= keep_below:
            break
        out.append((m["id"], m["file"]))
        n -= 1
    return out


def free_gb(path: Path) -> float:
    st = os.statvfs(str(path))
    return st.f_bavail * st.f_frsize / 1e9


def prune_used_clips(directory: Path, min_free_gb: float) -> int:
    """Disk under min_free_gb: delete the OLDEST clips in this fetcher's own cache whose source id is in
    the used-clips ledger (already in a posted video), until there is room again. Only media files at
    the top of `directory` (and its clips/ folder); never backups, renders or anything elsewhere."""
    if free_gb(directory) >= min_free_gb:
        return 0
    try:
        from pipeline_lib.used_clips import used_ids
        used = set(used_ids())
    except Exception:
        return 0
    files = []
    for d in (directory, directory / "clips"):
        if d.is_dir():
            files += [f for f in d.iterdir() if f.is_file() and not f.is_symlink()
                      and f.suffix.lower() in MEDIA_EXT and f.stem.split(".")[0] in used]
    n = 0
    for f in sorted(files, key=lambda f: f.stat().st_mtime):
        if free_gb(directory) >= min_free_gb:
            break
        f.unlink(missing_ok=True)
        n += 1
    if n:
        log(f"disk: freed space by deleting {n} oldest USED clip(s) from the box cache")
    return n


def prune_own_media(directory: Path, older_than_s: float) -> int:
    """Delete ONLY this fetcher's own leftover downloads (top level of its run dir, video/partial files).
    Clips are uploaded to the release and removed right after, so anything left here is a crashed/partial
    download. Never touches anything outside `directory` (no backups, no other videos)."""
    n = 0
    now = time.time()
    for f in directory.iterdir():
        if f.is_file() and not f.is_symlink() and f.suffix.lower() in MEDIA_EXT and \
                now - f.stat().st_mtime > older_than_s:
            f.unlink(missing_ok=True)
            n += 1
    return n


def title_key(category: str, title: str, channel: str = "") -> str:
    """Group key for the 2-3 extra videos of one movie / series / game / star player."""
    from pipeline_lib.titles import _CHANNEL_BITS, player_name, work_title
    if category == "sports":
        return (player_name(title or "", channel or "") or "").strip()
    t = (title or "").replace("\u2013", "-").replace("\u2014", "-")
    parts = [x.strip() for x in t.split("|") if x.strip()]
    parts = [x for x in parts if not _CHANNEL_BITS.match(x) and not re.match(r"(?i)^(movieclips|official\b)", x)
             and (channel or "").strip().lower() != x.lower()]
    if not parts:
        return ""
    name = re.split(r"\s+-\s+|:\s+(?=(?:official|the\s+)?\w+\s+(?:scene|clip|gameplay|trailer)\b)",
                    parts[0], maxsplit=1, flags=re.I)[0]
    name = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", name)
    name = re.sub(r"(?i)\b(official|gameplay|full\s+scene|scene|clip|4k|hd)\b.*$", " ", name)
    name = work_title(re.sub(r"\s+", " ", name).strip(" -:"), category) or ""
    name = re.sub(r"\s+", " ", name).strip(" -:")
    return name if len(name) >= 3 and name.lower() not in {"the", "official", "movie", "scene"} else ""


class Box:
    def __init__(self, a):
        self.a = a
        self.repo = a.repo
        self.dir = a.dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.extra = ["--cookies-from-browser", a.cookies_from_browser] if a.cookies_from_browser else []
        self.rejected: set[str] = set()
        # clips the plain-script overlay check rejected at download time (never uploaded, never retried)
        self.box_rejects_path = self.dir / "box_overlay_rejects.json"
        try:
            self.box_rejects: dict = json.loads(self.box_rejects_path.read_text())
        except Exception:
            self.box_rejects = {}

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

    def _last_queue_commit(self, path: str) -> tuple[dict | None, str]:
        """Latest pipeline-state commit that touched a queue file, or (None, reason)."""
        reason = "could not read last commit"
        for attempt in range(1, 4):
            c = gh("api", f"repos/{self.repo}/commits", "--method", "GET",
                   "-f", "sha=pipeline-state", "-f", f"path={path}", "-F", "per_page=1",
                   check=False, timeout=60)
            commit, reason = parse_commits_list(c.returncode, c.stdout, c.stderr)
            if commit or reason == "no queue file":
                return commit, reason
            if attempt < 3:
                time.sleep(1.2 * attempt)
        return None, reason

    def load_rejected(self) -> set[str]:
        """Ids Gemini rejected, read from pipeline-state {youtube,instagram}/state.json (data only)."""
        states = []
        for plat in CFG["platforms"]:
            r = gh("api", f"repos/{self.repo}/contents/{plat}/state.json?ref=pipeline-state",
                   "-H", "Accept: application/vnd.github.raw", check=False, timeout=120)
            if r.returncode != 0:
                log(f"rejected list: could not read {plat}/state.json -> treating as empty")
                continue
            try:
                states.append(json.loads(r.stdout))
            except Exception:
                log(f"rejected list: {plat}/state.json is not JSON -> ignored")
        self.rejected = rejected_from_states(states)
        try:  # clips already used by posted videos never come back (ledger/used_clips.json + box copy)
            from pipeline_lib.used_clips import used_ids
            self.rejected |= used_ids()
        except Exception as ex:
            log("used-clips ledger unreadable (ignored):", str(ex)[:100])
        log(f"rejected list: {len(self.rejected)} source id(s) Gemini rejected (not counted, not re-fetched)")
        return self.rejected

    def release_assets(self, tag: str) -> set[str] | None:
        """Asset names on a release. Empty set if the release does not exist; None if the lookup failed."""
        r = gh("release", "view", tag, "--repo", self.repo, "--json", "assets", check=False, timeout=120)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").lower()
            if "not found" in err or "release not found" in err:
                return set()
            log("could not list release assets:", tag, (r.stderr or r.stdout or "")[-160:].replace("\n", " "))
            return None
        try:
            data = json.loads(r.stdout or "{}")
            return {a["name"] for a in (data.get("assets") or []) if isinstance(a, dict) and a.get("name")}
        except Exception:
            return None

    def _download_clip(self, url: str, sid: str) -> Path | None:
        url = safe_url(url)
        if not url or not re.fullmatch(r"[\w-]{11}", sid or ""):
            return None
        existing = local_media(self.dir, sid, VIDEO_EXT, min_size=100_000)
        if existing:
            return existing
        out = self.dir / f"{sid}.%(ext)s"
        ok, _ = S.ytdlp([*self.extra, "-f", YTDLP_FORMAT, "-S", "res:1440,fps",
                         "--merge-output-format", "mkv", "--max-filesize", "1900M", "-o", str(out), url])
        f = local_media(self.dir, sid, VIDEO_EXT, min_size=100_000)
        if not ok or not f:
            log("download failed:", url)
            return None
        return f

    def sync_missing_clips(self, categories: list[str] | None = None, max_download: int = 25) -> int:
        """Upload manifest clips that are missing from the sources release (re-download if needed)."""
        man = self.manifest()
        clips = [c for c in (man.get("clips") or []) if isinstance(c, dict) and c.get("id")]
        assets = self.release_assets(TAG)
        if assets is None:
            log("sync skipped: could not list sources release assets")
            return 0
        want = list(categories or CFG["categories"])
        order = [c for c in (["gta"] + [x for x in want if x != "gta"]) if c in want]
        missing = []
        for cat in order:
            for rec in clips:
                if rec.get("category") != cat:
                    continue
                if asset_covers(rec.get("id", ""), rec.get("file") or "", assets):
                    continue
                missing.append(rec)
        if not missing:
            return 0
        got, updated, downloads = [], [], 0
        for rec in missing:
            sid = rec["id"]
            f = local_media(self.dir, sid, VIDEO_EXT, min_size=100_000)
            if not f:
                if downloads >= max_download:
                    continue
                f = self._download_clip(rec.get("url") or "", sid)
                if not f:
                    continue
                downloads += 1
            rec = {**rec, "file": f.name}
            got.append(f)
            updated.append(rec)
        if not got:
            log(f"sync: {len(missing)} clip(s) still missing from the sources release")
            return 0
        by_id = {r["id"]: r for r in updated}
        man["clips"] = [by_id.get(rec["id"], rec) for rec in clips]
        (self.dir / "manifest.json").write_text(json.dumps(man, indent=1))
        if not self.a.dry_run:
            self.upload(got + [self.dir / "manifest.json"])
            for f in got:
                f.unlink(missing_ok=True)
        log(f"sync: uploaded {len(got)} missing source clip(s)")
        return len(got)

    def clean_release_assets(self, man: dict, assets: set[str], dead: set[str]) -> int:
        """Keep `sources` under the ~1000-asset release limit: past asset_cleanup_at (800), delete the
        oldest USED / REJECTED clip assets (never usable ones) and drop them from the manifest."""
        limit_at = int(POOL_TOTAL.get("asset_cleanup_at", 800))
        pairs = assets_to_clean(man, assets, dead, limit_at, limit_at - 100)
        if not pairs or self.a.dry_run:
            return 0
        gone = set()
        for sid, name in pairs:
            r = gh("release", "delete-asset", TAG, name, "--repo", self.repo, "-y", check=False, timeout=120)
            if r.returncode == 0:
                gone.add(sid)
        if gone:
            man2 = self.manifest()
            man2["clips"] = [m for m in man2.get("clips") or [] if not (isinstance(m, dict) and m.get("id") in gone)]
            (self.dir / "manifest.json").write_text(json.dumps(man2, indent=1))
            self.upload([self.dir / "manifest.json"])
            log(f"release cleanup: deleted {len(gone)} used/rejected clip asset(s) ({len(assets)} assets > {limit_at})")
        return len(gone)

    def audit_resolution(self, max_full_mb: int = 900) -> dict:
        """Mark release clips below the 1080p floor as unusable in the manifest (assets are NOT deleted).
        Reads only the first 3 MB of each asset (HTTP Range) for ffprobe; falls back to a full temporary
        download when the header is not at the front (mp4 with moov at the end). Arg lists only."""
        r = gh("api", f"repos/{self.repo}/releases?per_page=100", check=False, timeout=120)
        rid = next((x.get("id") for x in json.loads(r.stdout or "[]") if x.get("tag_name") == TAG), None) \
            if r.returncode == 0 else None
        if not rid:
            log("audit: sources release not found")
            return {}
        ids = {}
        for page in range(1, 6):
            r = gh("api", f"repos/{self.repo}/releases/{rid}/assets?per_page=100&page={page}", check=False, timeout=120)
            rows = json.loads(r.stdout or "[]") if r.returncode == 0 else []
            ids.update({a["name"]: (a["id"], int(a.get("size") or 0)) for a in rows if isinstance(a, dict)})
            if len(rows) < 100:
                break
        man = self.manifest()
        tmp = self.dir / "audit_tmp"
        tmp.mkdir(exist_ok=True)
        marks, seen = {}, 0
        for m in man.get("clips") or []:
            if not isinstance(m, dict) or m.get("file") not in ids:
                continue
            size = parse_res(m.get("resolution"))
            if not size:
                aid, nbytes = ids[m["file"]]
                part = tmp / ("probe" + Path(m["file"]).suffix)
                for rng in ("bytes=0-3000000", None):
                    if rng is None and nbytes > max_full_mb * 1e6:
                        break
                    hdr = ["-H", f"Range: {rng}"] if rng else []
                    with open(part, "wb") as fh:
                        rr = subprocess.run(["gh", "api", "-H", "Accept: application/octet-stream", *hdr,
                                             f"repos/{self.repo}/releases/assets/{aid}"],
                                            stdout=fh, stderr=subprocess.DEVNULL, timeout=1800)
                    size = video_size(part) if rr.returncode == 0 else None
                    if size:
                        break
                part.unlink(missing_ok=True)
            seen += 1
            if not size:
                marks[m["id"]] = {"unusable": "resolution unknown (could not probe)"}
                continue
            mk = {"resolution": f"{size[0]}x{size[1]}"}
            if not meets_min_res(size):
                mk["unusable"] = f"below {MIN_HEIGHT}p ({size[0]}x{size[1]})"
            marks[m["id"]] = mk
        if marks and not self.a.dry_run:
            man2 = self.manifest()            # re-read right before writing; merge only our fields
            for m in man2.get("clips") or []:
                if isinstance(m, dict) and m.get("id") in marks:
                    m.update(marks[m["id"]])
            (self.dir / "manifest.json").write_text(json.dumps(man2, indent=1))
            self.upload([self.dir / "manifest.json"])
        low = {k: v["unusable"] for k, v in marks.items() if v.get("unusable")}
        log(f"audit: probed {seen} clip(s); {len(low)} marked unusable (below {MIN_HEIGHT}p / unknown)")
        return low

    def _pool_count(self, man: dict, assets: set[str] | None) -> dict:
        """Usable clips per category (same rules as the top-up)."""
        names = assets if assets is not None else set()
        rejected = getattr(self, "rejected", set())
        return {cat: len(usable_pool(man, names, rejected, cat)) for cat in CFG["categories"]}

    def queue(self) -> list[dict]:
        """Read the plain-JSON queue (data only — never executed). The last commit touching each queue
        file must be on pipeline-state and either authored by the owner or by the workflow bot and
        tied to a produce.yml run (owner-triggered, no PR events). Every entry is validated."""
        reqs = []
        for path in ("youtube/fetch_queue.json", "instagram/fetch_queue.json"):
            commit, reason = self._last_queue_commit(path)
            if not commit:
                log(f"queue {path}: {reason} -> ignored")
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
            sid = m.group(1) if m else None
            if sid:
                cands.append({"id": sid, "url": u, "title": "", "channel": "", "via": "box-queue-link"})
        for ch in c.get("channels", []):
            rx = re.compile(ch.get("title_re", "."), re.I)
            skip = re.compile(ch.get("skip_re", r"$^"), re.I)
            for e in S.list_channel(ch["url"], int(ch.get("limit", 80))):
                if rx.search(e["title"]) and not skip.search(e["title"]) and \
                        c.get("min_len", 30) <= (e["duration"] or 0) <= min(c.get("max_len", 600), 1800):
                    cands.append({**e, "credit": ch.get("credit"), "via": "box-official-channel"})
        # spread over channels / titles: round-robin the official-channel candidates so one channel (or one
        # game / show) cannot fill the whole genre target
        links_n = len(links)
        chan = cands[links_n:]
        groups: dict = {}
        for e in chan:
            groups.setdefault(e.get("credit") or "", []).append(e)
        rr = []
        while any(groups.values()):
            for k in list(groups):
                if groups[k]:
                    rr.append(groups[k].pop(0))
        cands = cands[:links_n] + rr
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
        skip_ids = set(getattr(self, "rejected", set())) | set(getattr(self, "box_rejects", {}))
        n_sib = max(0, int(os.environ.get("BOX_FETCH_SIBLINGS", "2") or 2))  # 2-3 clips per title
        min_free = float(os.environ.get("BOX_FETCH_MIN_FREE_GB", "10") or 10)

        def flush():
            # upload what we have and free the box disk right away (keeps local use to a few clips)
            if got and not self.a.dry_run:
                man2 = self.manifest()          # re-read: Actions may have added pool clips meanwhile
                man2["clips"] += [r for r in recs if r.get("_new")]
                for r in recs:
                    r.pop("_new", None)
                (self.dir / "manifest.json").write_text(json.dumps(man2, indent=1))
                self.upload(got + [self.dir / "manifest.json"])
            for f in got:
                f.unlink(missing_ok=True)
            got.clear()

        def download(e: dict, work: str = "") -> bool:
            if free_gb(self.dir) < min_free:
                flush()
                prune_own_media(self.dir, 3600)
                prune_used_clips(self.dir, min_free)
                if free_gb(self.dir) < min_free:
                    log(f"disk guard: only {free_gb(self.dir):.1f} GB free (< {min_free:g}) -> stop downloading")
                    self.disk_low = True
                    return False
            have.add(e["id"])
            out = self.dir / f"{e['id']}.%(ext)s"
            ok, _ = S.ytdlp([*self.extra, "-f", YTDLP_FORMAT, "-S", "res:1440,fps",
                             "--merge-output-format", "mkv", "--max-filesize", "1900M", "-o", str(out), e["url"]])
            f = next((p for p in self.dir.glob(f"{e['id']}.*") if p.suffix in (".mkv", ".mp4", ".webm")), None)
            if not ok or not f:
                log("download failed:", e["url"])
                return False
            # download-time watermark / logo / burned-text check (plain numpy + ffmpeg, no AI): reject and
            # delete here so a bad source never reaches the release or wastes an edit attempt
            if os.environ.get("BOX_FETCH_OVERLAY_CHECK", "1") != "0":
                from pipeline_lib.overlay_check import check_clip
                try:
                    clean, why, _ = check_clip(f)
                except Exception as ex:
                    clean, why = False, f"overlay check error: {str(ex)[:80]}"
                if not clean:
                    f.unlink(missing_ok=True)
                    self.box_rejects[e["id"]] = {"reason": why, "title": (e.get("title") or "")[:120],
                                                 "category": category, "at": now_local().isoformat()}
                    try:
                        self.box_rejects_path.write_text(json.dumps(self.box_rejects, indent=1))
                    except OSError:
                        pass
                    self.overlay_rejected = getattr(self, "overlay_rejected", 0) + 1
                    log(f"REJECT at download [{e['id']}] {(e.get('title') or '')[:60]} — {why} (deleted; next candidate)")
                    return False
            size = video_size(f)
            if not meets_min_res(size):
                f.unlink(missing_ok=True)
                why = f"below {MIN_HEIGHT}p ({'x'.join(map(str, size)) if size else 'unknown size'})"
                self.box_rejects[e["id"]] = {"reason": why, "title": (e.get("title") or "")[:120],
                                             "category": category, "at": now_local().isoformat()}
                try:
                    self.box_rejects_path.write_text(json.dumps(self.box_rejects, indent=1))
                except OSError:
                    pass
                log(f"REJECT at download [{e['id']}] {(e.get('title') or '')[:60]} — {why}")
                return False
            rec = {"id": e["id"], "file": f.name, "url": e["url"], "title": e.get("title", ""),
                   "channel": e.get("channel", ""), "credit": e.get("credit") or e.get("channel", ""),
                   "category": category, "genre": category, "resolution": f"{size[0]}x{size[1]}",
                   # has_dialogue / vetted are filled by the Actions vet (Gemini); the box only runs
                   # the plain overlay + resolution checks, no AI
                   "has_dialogue": None, "vetted": False,
                   "box_checks": {"overlay": "clean", "min_res": MIN_HEIGHT},
                   "via": e.get("via"), "added": now_local().isoformat(), "by": "box", "_new": True}
            if work:
                rec["work"] = work
            got.append(f)
            recs.append(rec)
            log("fetched", f.name, rec["title"][:60] + (f"  [title group: {work}]" if work else ""))
            if len(got) >= 4:
                flush()
            return True

        allow_sib = {a.lower() for a in c.get("allow_channels", [])}

        def siblings(e: dict, work: str) -> int:
            """2-3 MORE different official YouTube videos of the same movie / series / game / player."""
            n = 0
            kinds = ("official gameplay", "gameplay walkthrough cinematic") if category == "gaming" else \
                ("interview", "highlights") if category == "sports" else ("official clip", "scene")
            seen_q = set()
            for kind in kinds:
                q = safe_query(f"{work} {kind}")
                if not q or q in seen_q:
                    continue
                seen_q.add(q)
                for x in S.search_youtube(q, 15):
                    if n >= n_sib or len(recs) >= limit or getattr(self, "disk_low", False):
                        return n
                    if not x.get("id") or x["id"] in have or x["id"] in skip_ids or not safe_url(x.get("url")):
                        continue
                    t = x.get("title") or ""
                    chn = (x.get("channel") or "")
                    if work.lower() not in t.lower():
                        continue
                    if not (x.get("verified") or chn.lower() in allow_sib):
                        continue
                    if not (c.get("min_len", 30) <= (x.get("duration") or 0) <= min(c.get("max_len", 600), 1800)):
                        continue
                    if not usable_for(category, t, chn):
                        continue
                    if download({**x, "credit": chn, "via": "box-title-group"}, work):
                        n += 1
            return n

        for e in cands:
            if len(recs) >= limit or getattr(self, "disk_low", False):
                break
            if not e.get("id") or e["id"] in have or e["id"] in skip_ids:
                continue
            if not safe_url(e.get("url")):
                continue
            if not usable_for(category, e.get("title") or "", e.get("channel") or e.get("credit") or "") \
                    and e.get("via") != "box-queue-link":
                continue
            work = title_key(category, e.get("title") or "", e.get("channel") or "") \
                if category in SIBLING_CATS else ""
            if not download(e, work):
                continue
            if work and n_sib:
                got_n = siblings(e, work)
                log(f"title group '{work}': +{got_n} extra source video(s)")
        flush()
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
        assets = self.release_assets(SONG_TAG)
        if assets is None:
            log("songs top-up skipped: could not list songs release assets")
            return 0
        have, have_ids = set(), set()
        for s in man["songs"]:
            if not isinstance(s, dict):
                continue
            # Skip only when the audio is already on the release (a local-only manifest
            # after a repo switch would otherwise never re-upload the pool).
            if asset_covers(s.get("id") or "", s.get("file") or "", assets):
                have.add(_norm(f'{s.get("title","")} - {s.get("artist","")}'))
                have_ids.add(s.get("id"))
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
            from pipeline_lib.song_official import check as official_check
            ok_off, why = official_check({**off, "source": "youtube"}, o["artist"], o["title"])
            if not ok_off:
                log(f"skip unofficial upload for {tag}: {why}")
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
                   "channel": off.get("channel", ""), "why": o.get("why", ""), "genres": o.get("genres") or [],
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
        self.disk_low = False
        n = prune_own_media(self.dir, 6 * 3600)
        if n:
            log(f"disk: removed {n} stale partial/leftover download(s) from {self.dir}")
        prune_used_clips(self.dir, float(os.environ.get("BOX_FETCH_MIN_FREE_GB", "10") or 10))
        log(f"disk: {free_gb(self.dir):.1f} GB free on {self.dir} (stop below "
            f"{os.environ.get('BOX_FETCH_MIN_FREE_GB', '10')} GB)")
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
        # re-upload / re-download clips that are in the manifest but missing from the release
        try:
            fetched += self.sync_missing_clips()
        except Exception as ex:
            errors.append(f"sync: {str(ex)[:150]}")
        # pool top-up (no Gemini): ~100 clean unused clips spread over the genres. Refill starts when the
        # total drops below pool_total.refill_below (40) and runs until ~target (100); an empty genre is
        # always filled. Count only clips that exist as release assets, are not used (ledger/used_clips.json)
        # and not rejected, so a stale local manifest cannot hide an empty GitHub pool.
        man = self.manifest()
        assets = self.release_assets(TAG) or set()
        rejected = self.load_rejected()
        try:
            self.clean_release_assets(man, assets, rejected | set(getattr(self, "box_rejects", {})))
            man = self.manifest()
            assets = self.release_assets(TAG) or set()
        except Exception as ex:
            errors.append(f"asset cleanup: {str(ex)[:150]}")
        state_p = self.dir / "pool_state.json"
        try:
            refilling = bool(json.loads(state_p.read_text()).get("refilling"))
        except Exception:
            refilling = True       # first cycle on this code: build the stock up to ~target once
        counts = {cat: len(usable_pool(man, assets, rejected, cat)) for cat in POOL_TARGET}
        plan, refilling = pool_refill_plan(counts, POOL_TARGET, refilling)
        log(f"pool: {sum(counts.values())} usable clips {counts}; refilling={refilling}; plan={plan or 'none'}")
        cap = max(1, int(os.environ.get("BOX_FETCH_MAX_PER_CAT", "35") or 35))
        for cat in sorted(plan, key=lambda k: counts.get(k, 0)):
            tgt = counts.get(cat, 0) + plan[cat]
            have = usable_pool(man, assets, rejected, cat)
            fetched_cat = 0
            while len(have) < tgt and fetched_cat < cap and not self.disk_low:
                need = min(tgt - len(have), cap - fetched_cat)
                try:
                    recs = self.fetch(cat, need, use_gemini=False)
                except Exception as ex:
                    errors.append(f"pool {cat}: {str(ex)[:150]}")
                    break
                if not recs:
                    log(f"pool {cat}: no more official candidates ({len(have)}/{tgt})")
                    break
                fetched += len(recs)
                fetched_cat += len(recs)
                have |= {r["id"] for r in recs}
                man = self.manifest()
                assets = self.release_assets(TAG) or set()
                have |= usable_pool(man, assets, rejected, cat)
        counts = {cat: len(usable_pool(man, assets, rejected, cat)) for cat in POOL_TARGET}
        if sum(counts.values()) >= int(POOL_TOTAL.get("target", 100)):
            refilling = False
        try:
            state_p.write_text(json.dumps({"refilling": refilling, "counts": counts,
                                           "at": now_local().isoformat()}, indent=1))
        except OSError:
            pass
        try:
            fetched += self.fetch_songs()
            # per-genre stock: refill any genre at/below stock.refill_at from curated seeds (box cookies)
            from pipeline_lib import song_stock
            fetched += song_stock.refill(dry_run=self.a.dry_run, extra=self.extra or None)
        except Exception as ex:
            errors.append(f"songs: {str(ex)[:150]}")
        man = self.manifest()
        assets = self.release_assets(TAG) or set()
        pool = self._pool_count(man, assets)
        song_assets = self.release_assets(SONG_TAG) or set()
        songs_n = sum(1 for s in (self.song_manifest().get("songs") or [])
                      if isinstance(s, dict) and asset_covers(s.get("id") or "", s.get("file") or "", song_assets))
        self.heartbeat({"fetched_last_cycle": fetched, "errors": errors[-10:], "pool": pool, "songs": songs_n,
                        "overlay_rejected_last_cycle": getattr(self, "overlay_rejected", 0),
                        "processed_now": now_done, "interval_s": self.a.interval})
        log(f"cycle done: fetched {fetched}, overlay-rejected {getattr(self, 'overlay_rejected', 0)}, "
            f"pool {pool}, songs {songs_n}, errors {len(errors)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["once", "loop", "songs", "cycle", "sync", "audit-res"], nargs="?", default="once")
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
    if a.cmd == "loop":   # retired 2026-11-26: the crontab fetcher (box/fetch_sources.sh) is the only fetcher
        log("retired: the old 'loop' fetcher is off; the crontab fetcher (box/fetch_sources.sh) is the only fetcher")
        return
    b = Box(a)
    if a.cmd == "songs":
        n = b.fetch_songs()
        song_assets = b.release_assets(SONG_TAG) or set()
        songs_n = sum(1 for s in (b.song_manifest().get("songs") or [])
                      if isinstance(s, dict) and asset_covers(s.get("id") or "", s.get("file") or "", song_assets))
        b.heartbeat({"mode": "songs", "songs": songs_n})
        log(f"songs command done: added {n}")
        return
    if a.cmd == "audit-res":
        b.audit_resolution()
        return
    if a.cmd == "sync":
        n = b.sync_missing_clips(max_download=80)
        try:
            n += b.fetch_songs()
        except Exception as ex:
            log("songs top-up failed:", str(ex)[:160])
        man = b.manifest()
        assets = b.release_assets(TAG) or set()
        song_assets = b.release_assets(SONG_TAG) or set()
        songs_n = sum(1 for s in (b.song_manifest().get("songs") or [])
                      if isinstance(s, dict) and asset_covers(s.get("id") or "", s.get("file") or "", song_assets))
        b.heartbeat({"mode": "sync", "fetched_last_cycle": n, "pool": b._pool_count(man, assets),
                     "songs": songs_n, "errors": []})
        log(f"sync command done: uploaded {n}, pool {b._pool_count(man, assets)}, songs {songs_n}")
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

