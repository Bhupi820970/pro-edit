# pro-edit · `vicestyle.py`

Beat-synced GTA 6 "Vice City" fan-edit generator for the **Vice Cartel** channel.
Feed it **one song + a folder of clips** and it cuts a ~52 s edit in this style:

| Element | What the script does |
|---|---|
| Beats | `librosa` beat tracking, then a straight tempo grid fit (disco = steady BPM). Every cut lands on a beat frame. |
| Song window | Auto-picks a phrase-aligned (16-beat) window with the most energy and a strong ending (or `--song-start`). |
| Shot classification | Scene-cut detection (HSV histogram jumps on a 10 fps proxy) + `faster-whisper` speech detection. Shots with speech become **dialogue**, the rest **montage**. Title cards, dark slates, clip heads/tails and pillar-boxed social-media inserts are rejected. |
| Structure | Hook (24 beats of 1–2 beat shots) → dialogue A → 12-beat montage → dialogue B → fast 1-beat montage → **cut to black on a beat**. |
| Montage | Full-screen 16:9, music at 100 %, hard cuts only, ~0.5–1 s per shot, never the same shot twice, sources round-robined. |
| Dialogue | 21:9 letterbox bars, vignette, music ducked to ~34 % with smooth ramps, original dialogue + SFX normalised loud. |
| Captions | Only while someone is speaking (whisper word timings), 1–3 word chunks, Anton font, hard black drop shadow + outline, 80 %→100 % pop-in. Filler words small/white, key words big in rotating neon (cyan/pink/yellow/purple). Placed **beside the speaker** using OpenCV YuNet face detection (falls back to alternating sides). Never captions song lyrics. |
| Name pops | **Off by default.** Big neon names (e.g. `JASON`) are drawn only with `--name-pops` and only where a plan sets `name` (never in auto mode). |
| Grade | Miami-Vice: contrast + saturation + vibrance, warm highs / teal shadows, auto exposure lift for dark shots, light film grain. |
| Watermark | Small, subtle `VICE CARTEL` text top-right (`--watermark ""` to disable). |
| Shorts | `--mode shorts` → 2160×3840, blurred fill background, the full 16:9 edit (no crop) with a white hook bar above it. |

## Install
```bash
sudo apt install ffmpeg
pip install -r requirements.txt
```
The YuNet face model (230 KB) is auto-downloaded to `models/` on first use.

## Use
Fully automatic (no manual shot picking):
```bash
python3 vicestyle.py auto --song give_it_up.wav --clips ./sources \
    --out out/vice_style_code.mp4 --duration 38 --plan-out plans/auto.json
```
Hand-picked edit list (EDL) — same renderer:
```bash
python3 vicestyle.py render --song give_it_up.wav --plan plans/hand_v1.json \
    --out out/vice_style_hand.mp4
```
Vertical Short:
```bash
python3 vicestyle.py render --song give_it_up.wav --plan plans/hand_v1.json \
    --mode shorts --hook "GTA 6 HITS DIFFERENT" --out out/short.mp4
```
Options: `--res 2560x1440` (default) or `1920x1080`, `--crf 18`, `--workers 4`, `--song-start SEC`.
Env: `VICESTYLE_WHISPER=small|medium`, `VICESTYLE_CACHE=/path` (beats, transcripts, cut cache).

## Plan format (`render`)
```json
{ "song_start": 60.2, "watermark": "VICE CARTEL",
  "segments": [
    {"kind": "montage",  "src": "../sources/trailer2.mkv", "start": 19.05, "beats": 3,
     "name": "JASON", "name_color": "cyan"},
    {"kind": "dialogue", "src": "../sources/el_823.mp4", "start": 67.8, "beats": 37,
     "fix": {"mommy": "MAMI"},
     "fix_phrases": [{"from": "for your plate", "to": "for you playboy"}]}
  ]}
```
Per segment: `zoom` (crop in, hides HUD), `gamma` (exposure, default auto), `side` (force caption side),
`drop` (words not to caption), `sfx` (montage source-audio level, default 0 = music only).

Every render also writes `OUT.mp4.json` with the song window, shot list and every caption shown.

## Notes
* Use clean sources (no channel logos / burned-in subtitles). The auto filter catches title cards and
  pillar-boxed inserts but not small corner logos or game HUDs — curate the `--clips` folder.
* Video/audio files are never committed (see `.gitignore`).

---

# Automated pipeline (Skywalker-style)

`pipeline.py` turns the editor above into a hands-off content factory. Finished videos land in the
draft release `vc-ready`. The `upload` workflow posts them to YouTube (`@vicecartelgta`) and Instagram
(`@benreelr`) — scheduled only when `SCHEDULE_ENABLED=true`, or one test upload via `workflow_dispatch`.

```
official channels ─► download (yt-dlp, client fallbacks) ─► Gemini WATCHES each clip ─► reject logos /
watermarks / subtitles / HUD / low quality ─► Gemini picks an original song ─► official audio ─►
vicestyle auto edit (approved look) + rotating caption pattern ─► QA ─► ready queue + status.json
```

| Piece | File |
|---|---|
| CLI (`produce`, `ensure`, `status`, `mark`, `cleanup`, `attach-release`, `merge-status`) | `pipeline.py` |
| YouTube Data API v3 + Buffer Instagram upload, captions, posted ledger | `pipeline_lib/publish/`, `scripts/upload.py`, `ledger/posted.json` |
| Disk cleanup after a video is marked `posted` (source clips + temp; finals kept 24h unless `--keep`) | `pipeline_lib/cleanup.py` |
| Gemini REST client — keys `GEMINI_API_KEY_1..6` from env only, rotation + failover on 429/quota/5xx, model fallback | `pipeline_lib/gemini.py` |
| Sourcing: links inbox → official channels (Rockstar Games etc.) → fallback YouTube search / Gemini Google-search discovery / box-fetched clips; Gemini Files-API vetting, bad-range (title cards, text) exclusion | `pipeline_lib/sourcing.py` |
| Song pick (original recordings, not used in the last 10 days) + official audio download | `pipeline_lib/songs.py` |
| Title / hook / description + credit block (song, official link, footage credit, "Edit by Vice Cartel") | `pipeline_lib/meta.py` |
| QA: duration, resolution, audio, black frames (except the ending), captions in safe area, captions clear of faces, captions only during dialogue, no name pops | `pipeline_lib/qa.py` |
| 15 caption patterns (all variations of the approved beside-speaker look) | `caption_patterns.py`, grid: `tools/pattern_grid.py` |
| Daily plan, channels, categories, buffer size | `config/pipeline.json` |
| Box-side downloader fallback | `box_fetch.py` |
| GitHub Actions (produce, upload, ci) | `.github/workflows/produce.yml`, `upload.yml`, `ci.yml` |


## Auto-upload (YouTube + Instagram)

Five slots a day, both platforms, all times IST: **08:00, 11:30, 15:00, 18:30, 21:30**.

* YouTube: Data API v3 resumable upload, public, not made for kids, channel `@vicecartelgta`.
* Instagram: Buffer API, channel selected by username `benreelr` (never a hardcoded channel id).
* The same video is never posted twice (`ledger/posted.json`). A failed upload is retried once, then the other platform still runs.
* Ready stock is the draft release **`vc-ready`** (backup stock, BIBI rule 27 Nov): ~20 unposted videos per platform. When a platform drops below 8, the watchdog / upload job dispatches `produce.yml` (3-4 videos per platform per run, Gemini-gated) and keeps topping up to 20 (`pipeline_lib/autoproduce.py`: TARGET / LOW / ALERT_AT). Uploads always take the oldest ready video. Ready <= 2 opens the `health` issue and flips `queue/stock_alert.json` (pipeline-state) to alert.
* Scheduled posting stays off until the repo variable `SCHEDULE_ENABLED` is set to `true`. A manual test:

```bash
# from the Actions tab: upload → Run workflow → mode=test
# or:
gh workflow run upload.yml -R Bhupi820970/pro-edit -f mode=test
```

Secrets (Actions only, never in the repo): `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN`, `BUFFER_API_KEY`.

## Daily plan (config)
* **YouTube (Vice Cartel)**: 5–6 per day, GTA (4–6) + other gaming (0–1), 16:9 2560×1440.
* **Instagram**: 6–7 per day — game 2–3, movie scenes 2–3, sports 1, love/romance 1 — vertical 2160×3840
  (blur-fill, white hook bar, no crop).
* **Backup buffer**: 7 ready videos per platform. When a platform drops to **3 or fewer**, `status.json`
  sets `"warning": true` and `"message": "BACKUP LOW: refill …"`.

## Caption patterns
All 15 keep the approved look (beside the speaker on the side away from the face, alternate sides when no face,
never over a face, Anton + hard shadow, pop-in, big neon key words + small white fillers). They differ only in
vertical rhythm (mid / upper / lower / stepping / wave), distance and nudge, 2- vs 3-word groups and size mix.
Every palette has 4–5 colours incl. the approved hot pink `#FF2EC4`. P01 = the approved edit exactly.
A pattern is not reused on the same platform within 2.5 days (`state.json`).

## Run
```bash
export GEMINI_API_KEY_1=... # (..._6) — never commit keys
python pipeline.py produce --platform youtube --count 2
python pipeline.py produce --platform instagram --category movie
python pipeline.py ensure --platform instagram          # top up to 7 ready
python pipeline.py status                               # status.json
python pipeline.py mark --id VID --state posted         # after a YouTube / Instagram upload
python pipeline.py mark --id VID --state posted --keep  # same, but keep the final mp4
python pipeline.py cleanup                              # sweep stale work + posted finals older than 24h
```
`VICE_PIPELINE_HOME` (default `<folder holding the repo>/pipeline`) holds `state.json`, `status.json`, `logs/`,
`ready/<platform>/*.mp4` (+ `.json`), `prev/*.mp4` (≤ 25 MB previews), source/song caches.

When a video is marked `posted` (the upload-success hook), the pipeline deletes that video's local
source clips, work/temp files and preview. The final mp4 stays for 24 hours, then an age sweep
removes it unless `--keep` was set. A sweep also drops work/temp files older than 3 days that no
pending ready/approved video still references. Cleanup only touches allowlisted data dirs
(`work/`, `sources/`, `prev/`, `ready/`, plus the box `dl/` / `cache/` / `<box root>/work`
when running on the real pipeline home) after a resolved-path check. Code, the songs library,
approved samples (`out/samples`, `out/demos/gta*`, `out/demos/gaming*`) and the 7+7 backup
buffer are never deleted.

## GitHub Actions
`.github/workflows/produce.yml` runs 4× a day (05:30 / 11:30 / 17:30 / 23:30 IST) and on manual dispatch,
one job per platform. Rendered videos + previews go to release `videos-<platform>-<date>` (prerelease),
previews + logs also as a run artifact; `state.json` / `status.json` are committed to the `pipeline-state` branch
(combined `status.json` at its root). Secrets: `GEMINI_API_KEY_1..6`.
YouTube often bot-checks GitHub runners: the workflow installs the latest yt-dlp + the bgutil PO-token provider,
and `sourcing.ytdlp()` retries with alternate player clients. If it is still blocked, run `box_fetch.py` on the box.

## box_fetch.py (box downloader, crontab)
On the box, `box/ensure_cron.sh` installs cron (if missing) and this crontab line (box clock = IST):
`50 1-23/3 * * * <REPO>/box/fetch_sources.sh` (full list in HANDOFF.md) — 40 min before each produce slot.
`box/fetch_sources.sh` runs `box_fetch.py cycle --no-gemini` (no AI on the box) with a lock, low priority and a
170-minute cap; log: `$VICE_DATA_DIR/fetch_sources.log`. The pool counts only clips Gemini has not
rejected and that pass the runner's title rules (no trailers, sports clips must name a star player).
produce.yml uses these release clips FIRST; runner YouTube downloads are a fallback and stop for the run after
`VICE_RUNNER_BOT_LIMIT` bot checks. If every Gemini key is out of quota, the run writes
`queue/needs_manual_pick.json` (branch `pipeline-state`) with the category + candidate clips; a bot can answer with
`queue/manual_picks.json` and the next run uses those picks. A category with no usable sources is skipped for the
rest of the run (sports: next star player first, then another category) so a run never burns hours on one genre.

Runs on the box, finds clips with the same queries (config channels/search + Gemini discovery + your links),
downloads with yt-dlp and uploads them + `manifest.json` to release tag `sources`. The workflow pulls that release
into its source cache; Gemini still vets every clip. `--cookies-from-browser` is opt-in and local only
(cookies are never written to the repo, release, logs or manifest).

## Links inbox
`inbox/links.txt`: `<category> <url> [credit]` per line (e.g. added by a scheduled Grok/Gemini task); these are tried first.

---

# Fan-edit style: `fanedit_stark` ("The North Remembers" structure)

`fanedit_stark.py` reuses the vicestyle renderer (beat layout, whisper word-synced captions, audio
ducking) with its own per-shot filter and caption pattern — the approved GTA style is untouched.

```bash
python3 fanedit_stark.py --spec plans/stark_daenerys.json --out out/daenerys.mp4
```

1. **Build** – dialogue segments play their *own* shot's audio (lip-safe), vocals isolated with
   demucs `htdemucs`; a mediapipe mouth-vs-speech check warns when the visible face is not the one
   talking (mark narration with `"offscreen": true`).
2. **Drop** – auto-detected (biggest quiet→loud jump, beat grid phase-locked to it): blur ramp,
   glass cracks, white flash, refracting shards with edge highlights + motion blur, glass SFX,
   cold → warm grade.
3. **Climax** – ≥1080p shots only (`min_height`), 0.5× motion-interpolated slow-mo, 2-beat slots,
   zoom-punch + RGB split per cut, sequence looped (`loops`).
4. **Ending** – real storm-cloud footage plate + pre-rendered 3D logo
   (`assets/fanedit_stark/logo_vice_cartel.webm`, re-render with `tools/render_logo_blender.py`).

Extra deps (optional, degrade gracefully): `demucs`, `mediapipe`. Credits: see `CREDITS.md`.

