"""Per-genre song stock: keep ~30 official songs per video genre in draft release 'songs'.

  python -m pipeline_lib.song_stock status            # counts per genre
  python -m pipeline_lib.song_stock refill [--dry-run] [--cookies-from-browser chrome]
  python -m pipeline_lib.song_stock publish           # runner: push songs the edit fetched (new_songs.json)

A genre at or below stock.refill_at (config/songs.json) is refilled to stock.target from curated seeds plus
Gemini suggestions. Gemini also validates the vibe of every candidate; mismatches are skipped. Gemini output
is data only: titles/artists must pass safe_search_term and audio only comes from the official-upload search.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

from .songs import (GENRES, compute_bpm, _norm, clean_genres, download_official, genre_index, genres_of,
                    load_songs_cfg, safe_search_term, tag_key)
from .util import log, now_local

TAG = "songs"
REPO_SLUG_ENV = "GITHUB_REPOSITORY"

SUGGEST_SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "title": {"type": "STRING"}, "artist": {"type": "STRING"}, "why": {"type": "STRING"}},
    "required": ["title", "artist"]}}
VET_SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "i": {"type": "INTEGER"}, "fits": {"type": "BOOLEAN"}}, "required": ["i", "fits"]}}

HINTS = {
    "gta": "GTA / Vice City crime swagger: hype hip-hop, synthwave, 80s retro, funk",
    "gaming": "gaming montage hype: hip-hop, EDM, rock anthems, synthwave",
    "movie": "cinematic, dark, dramatic, tense or epic",
    "sports": "pump-up sports anthems: hip-hop, rock, stadium hype",
    "romance": "love songs: Hindi/Bollywood romantic and English romantic pop/R&B",
    "series": "TV-series edit: dark, moody, dramatic, cinematic",
    "marvel": "superhero hype: epic rock, hip-hop and stadium anthems",
    "dc": "dark heroes: heavy rock, dark synth, brooding hip-hop",
    "anime": "famous anime openings (official label audio) and upbeat J-rock / J-pop",
    "celebrity": "red-carpet glam: confident pop and R&B hits",
    "comedy": "fun bouncy beats: funk, upbeat pop, playful hits",
}


def repo_slug() -> str:
    import os
    return os.environ.get(REPO_SLUG_ENV) or "Bhupi820970/pro-edit"


def gh(*args, check=True) -> subprocess.CompletedProcess:
    from .util import run as _run
    r = _run(["gh", *args], timeout=1800, check=False)
    if check and r.returncode != 0:
        raise RuntimeError(f"gh {args[0]} {args[1] if len(args) > 1 else ''} failed: {(r.stderr or '')[-200:]}")
    return r


def remote_manifest(d: Path) -> dict:
    r = gh("release", "download", TAG, "--repo", repo_slug(), "-p", "manifest.json", "-D", str(d), "--clobber",
           check=False)
    p = d / "manifest.json"
    if r.returncode != 0 or not p.exists():
        return {"songs": []}
    try:
        data = json.loads(p.read_text())
    except Exception:
        return {"songs": []}
    return data if isinstance(data.get("songs"), list) else {"songs": []}


def counts(man: dict, idx: dict) -> dict:
    c = {g: 0 for g in GENRES}
    for r in man.get("songs") or []:
        if isinstance(r, dict):
            for g in genres_of(r, idx):
                c[g] += 1
    return c


def tag_manifest(man: dict, idx: dict) -> int:
    """Write genre tags into manifest records that lack them (from config/songs.json)."""
    n = 0
    for r in man.get("songs") or []:
        if isinstance(r, dict) and not clean_genres(r.get("genres")):
            g = genres_of(r, idx)
            if g:
                r["genres"] = g
                n += 1
    return n


def annotate(man: dict, idx: dict, cfg: dict, sdir: Path, bpm_limit: int = 20) -> int:
    """Fill genre/mood on every manifest row and compute missing bpm (librosa, box only) for up to
    `bpm_limit` songs per run by pulling their audio from the release. Returns rows changed."""
    moods = cfg.get("genre_mood") or {}
    n, tried = 0, 0
    for r in man.get("songs") or []:
        if not isinstance(r, dict):
            continue
        g = clean_genres(r.get("genres")) or genres_of(r, idx)
        if g and not r.get("genre"):
            r["genre"] = g[0]
            n += 1
        if r.get("genre") and not r.get("mood") and moods.get(r["genre"]):
            r["mood"] = moods[r["genre"]]
            n += 1
        f = r.get("file") or ""
        if r.get("bpm") or r.get("bpm_failed") or tried >= bpm_limit or not f or "/" in f or f.startswith("."):
            continue
        tried += 1
        try:
            gh("release", "download", TAG, "--repo", repo_slug(), "--pattern", f, "--dir", str(sdir), "--clobber")
        except Exception:
            continue
        path = sdir / f
        bpm = compute_bpm(path) if path.is_file() else None
        path.unlink(missing_ok=True)
        if bpm:
            r["bpm"] = bpm
        else:
            r["bpm_failed"] = True
        n += 1
    return n


def suggest(gem, genre: str, have: list[str], n: int) -> list[dict]:
    try:
        out = gem.generate([{"text": (
            f"List {n} popular, well-known ORIGINAL studio songs (official upload on YouTube, no remixes/covers/"
            f"sped-up/live) that truly fit this vibe: {HINTS[genre]}. Good for 38s beat-synced edits. "
            f"Exclude these we already have: {'; '.join(have[-80:]) or 'none'}. JSON only.")}],
            schema=SUGGEST_SCHEMA, temperature=0.6)
    except Exception as ex:
        log("gemini suggest failed:", str(ex)[:160])
        return []
    return [{"title": o["title"], "artist": o["artist"], "why": (o.get("why") or "")[:80]}
            for o in out or [] if isinstance(o, dict)
            and safe_search_term(o.get("title") or "") and safe_search_term(o.get("artist") or "")]


def vet(gem, genre: str, cands: list[dict]) -> list[dict]:
    """Keep only candidates Gemini says truly fit the genre vibe (all kept if Gemini is unavailable)."""
    if gem is None or not cands:
        return cands
    listing = "\n".join(f"{i}. {c['title']} - {c['artist']}" for i, c in enumerate(cands))
    try:
        out = gem.generate([{"text": (
            f"Genre vibe: {HINTS[genre]}.\nFor each song say whether it truly fits that vibe for a fan edit "
            f"(be strict; reject mismatches):\n{listing}\nJSON only.")}], schema=VET_SCHEMA, temperature=0.1)
    except Exception as ex:
        log("gemini vet failed, keeping curated seeds only:", str(ex)[:160])
        return [c for c in cands if c.get("seed")]
    ok = {o.get("i") for o in out or [] if isinstance(o, dict) and o.get("fits") is True}
    kept = [c for i, c in enumerate(cands) if i in ok]
    for i, c in enumerate(cands):
        if i not in ok:
            log(f"gemini rejected for {genre}:", f"{c['title']} - {c['artist']}")
    return kept


def plan_refill(man: dict, idx: dict, cfg: dict, to_target: bool = False) -> dict:
    st = cfg.get("stock") or {}
    target, low = int(st.get("target", 30)), int(st.get("refill_at", 8))
    c = counts(man, idx)
    if to_target:
        return {g: target - n for g, n in c.items() if n < target}
    return {g: target - n for g, n in c.items() if n <= low}


def refill(dry_run: bool = False, extra: list | None = None, gem=None, workdir: Path | None = None,
           to_target: bool = False) -> int:
    cfg = load_songs_cfg()
    idx = genre_index(None)
    sdir = Path(workdir or tempfile.mkdtemp(prefix="songstock-"))
    sdir.mkdir(parents=True, exist_ok=True)
    man = remote_manifest(sdir)
    retagged = tag_manifest(man, idx)
    if not dry_run:
        retagged += annotate(man, idx, cfg, sdir)
    need = plan_refill(man, idx, cfg, to_target=to_target)
    log("song stock per genre:", counts(man, idx), "refill:", need or "none")
    max_run = int((cfg.get("stock") or {}).get("max_per_run", 12))
    have_keys = {tag_key(r.get("title", ""), r.get("artist", "")) for r in man["songs"] if isinstance(r, dict)}
    have_ids = {r.get("id") for r in man["songs"] if isinstance(r, dict)}
    got, added = [], 0
    for genre, n in need.items():
        if added >= max_run:
            break
        cands = [{**s, "seed": True} for s in (cfg.get("seeds") or {}).get(genre, []) if isinstance(s, dict)]
        if gem is not None:
            cands += suggest(gem, genre, [f'{r["title"]} - {r["artist"]}' for r in man["songs"]
                                          if isinstance(r, dict) and genre in genres_of(r, idx)], n + 10)
        seen, uniq = set(), []
        for c in cands:
            k = tag_key(c["title"], c["artist"])
            if k in seen or k in have_keys or not safe_search_term(c["title"]) or not safe_search_term(c["artist"]):
                continue
            seen.add(k)
            uniq.append(c)
        uniq = vet(gem, genre, uniq[:40])
        for c in uniq:
            if n <= 0 or added >= max_run:
                break
            if dry_run:
                log(f"[dry-run] would fetch for {genre}:", f"{c['title']} - {c['artist']}")
                n -= 1
                continue
            rec = download_official(c, sdir, [genre], "song-stock", extra=extra)
            if not rec or rec["id"] in have_ids:
                log("skip (no official upload / duplicate):", f"{c['title']} - {c['artist']}")
                continue
            rec.pop("seed", None)
            rec["genre"] = genre
            rec["mood"] = c.get("mood") or (cfg.get("genre_mood") or {}).get(genre)
            bpm = compute_bpm(sdir / rec["file"])
            if bpm:
                rec["bpm"] = bpm
            man["songs"].append(rec)
            have_ids.add(rec["id"])
            have_keys.add(tag_key(c["title"], c["artist"]))
            got.append(sdir / rec["file"])
            n -= 1
            added += 1
            log(f"stocked {genre}:", f"{rec['title']} - {rec['artist']}")
    if not dry_run and (got or retagged):
        (sdir / "manifest.json").write_text(json.dumps(man, indent=1))
        gh("release", "upload", TAG, "--repo", repo_slug(), "--clobber", *[str(f) for f in got],
           str(sdir / "manifest.json"))
        for f in got:
            f.unlink(missing_ok=True)
    log(f"song stock refill done: +{added}, retagged {retagged}")
    return added


def publish(songs_dir: Path) -> int:
    """Upload songs the runner fetched for an edit (new_songs.json) and merge them into the stock manifest."""
    p = songs_dir / "new_songs.json"
    if not p.exists():
        return 0
    new = [r for r in (json.loads(p.read_text()).get("songs") or []) if isinstance(r, dict) and r.get("file")]
    files = [songs_dir / r["file"] for r in new if "/" not in r["file"] and (songs_dir / r["file"]).is_file()]
    if not files:
        return 0
    tmp = Path(tempfile.mkdtemp(prefix="songpub-"))
    man = remote_manifest(tmp)
    ids = {r.get("id") for r in man["songs"] if isinstance(r, dict)}
    man["songs"] += [r for r in new if r.get("id") not in ids]
    (tmp / "manifest.json").write_text(json.dumps(man, indent=1))
    gh("release", "upload", TAG, "--repo", repo_slug(), "--clobber", *map(str, files), str(tmp / "manifest.json"))
    p.unlink()
    log(f"published {len(files)} new song(s) to the genre stock")
    return len(files)


def quarantine(dry_run: bool = False) -> list[dict]:
    """Move songs that fail the official-uploader check from manifest "songs" to manifest "quarantine".
    The audio files stay on the release (nothing is deleted); only the pool stops using them."""
    from .song_official import check as official_check
    tmp = Path(tempfile.mkdtemp(prefix="songq-"))
    man = remote_manifest(tmp)
    keep, moved = [], []
    for r in man.get("songs") or []:
        if not isinstance(r, dict):
            continue
        ok, why = official_check(r, r.get("artist"), r.get("title"))
        if ok:
            keep.append(r)
        else:
            moved.append({**r, "quarantined_at": now_local().isoformat(), "quarantine_reason": why})
    if moved:
        man["songs"] = keep
        man["quarantine"] = list(man.get("quarantine") or []) + moved
        log(f"quarantine: {len(moved)} unofficial song(s) moved out of the pool, {len(keep)} kept")
        for r in moved:
            log(f"  - {r.get('title')} - {r.get('artist')} [{r.get('channel')}] {r['quarantine_reason']}")
        if not dry_run:
            (tmp / "manifest.json").write_text(json.dumps(man, indent=1))
            gh("release", "upload", TAG, "--repo", repo_slug(), "--clobber", str(tmp / "manifest.json"))
    return moved


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["status", "refill", "publish", "quarantine"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cookies-from-browser", default="")
    ap.add_argument("--no-gemini", action="store_true")
    ap.add_argument("--to-target", action="store_true",
                    help="top up every genre below target (not only those at/below refill_at)")
    ap.add_argument("--songs-dir", default="")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        d = Path(tempfile.mkdtemp())
        man = remote_manifest(d)
        print(json.dumps(counts(man, genre_index(None))))
        return
    if a.cmd == "quarantine":
        quarantine(dry_run=a.dry_run)
        return
    if a.cmd == "publish":
        from .util import home
        publish(Path(a.songs_dir) if a.songs_dir else home() / "songs")
        return
    gem = None
    if not a.no_gemini:
        try:
            from .gemini import Gemini
            gem = Gemini(log=log)
        except Exception as ex:
            log("no Gemini for song vetting (curated seeds only):", str(ex)[:120])
    extra = ["--cookies-from-browser", a.cookies_from_browser] if a.cookies_from_browser else None
    refill(dry_run=a.dry_run, extra=extra, gem=gem, to_target=a.to_target)


if __name__ == "__main__":
    main()
