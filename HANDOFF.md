# Vice Cartel (pro-edit): handoff

This repo makes short edits and posts them on a schedule:
- **YouTube:** @vicecartelgta
- **Instagram:** @benreelr

The heavy work (picking clips, Gemini checks, rendering, uploading) runs in **GitHub Actions**.

A small **box side** (any always-on Linux or Mac computer) does two jobs:
- It downloads source clips and songs from YouTube over a normal home IP, because GitHub's IPs get bot-checked.
- It sends backup "kicks", because GitHub sometimes skips scheduled runs.

The box never renders and never runs code from issues, queues or AI output.

Written 2026-11-26 (IST). All times are IST (Asia/Kolkata).

---

## 1. Box cron lines (run them on ONE computer only)

Replace `<REPO>` with the folder where this repo is cloned. The computer's clock must be IST; otherwise convert the times.

```
50 1-23/3 * * * <REPO>/box/fetch_sources.sh        # clip + song downloader, every 3 h (01:50, 04:50 ... 22:50)
3 8,15 * * * <REPO>/box/upload_slot_kick.sh         # backup kick for the 08:00 / 15:00 upload slots
33 11,18,21 * * * <REPO>/box/upload_slot_kick.sh    # backup kick for the 11:30 / 18:30 / 21:30 slots
25 4 */2 * * <REPO>/box/cleanup_posted.sh           # used-clip ledger + disk cleanup, every 2 days
28,58 * * * * <REPO>/box/watchdog_kick.sh           # backup kick for watchdog.yml + restarts a dead / idle clip fetcher
17 3 * * 0 <REPO>/box/ig_token_refresh.sh           # weekly IG token refresh (Instagram-Login tokens only)
```

Useful commands:
- `bash box/ensure_cron.sh install` adds exactly these lines with the right paths.
- `bash box/ensure_cron.sh status` shows them.
- `bash box/ensure_cron.sh remove` deletes them.

If two computers run these lines, everything runs twice. Downloads are locked per computer only. Uploads are protected by the ledger lock, but don't rely on that.

## 2. What each box script does (folder `box/`)

| Script | What it does |
|---|---|
| `common.sh` | Shared settings and helpers. Reads `box/local.env` (paths and gh login; not in git). Works on Linux and macOS. |
| `fetch_sources.sh` → `box_fetch.py cycle` | Tops up the clip pool: about 100 clean clips over 11 genres. A refill starts when usable clips drop to **50 or below** and runs back to about 100. The biggest shares go to anime/cartoon (15), games (16, top-10 titles), web series (11), romance (11), action movies (13), Marvel (9) and DC (7); GTA, sports, celebrity and comedy keep smaller stocks. A fetch error is retried with backoff (60 s, 180 s, 540 s) inside the cycle. Only official channels, at least 1080p, and rejects logos, watermarks and burned-in captions. Uploads clips and `manifest.json` to the draft release `sources`, refills the `songs` release (about 30 famous original songs per genre, with bpm), and frees disk. |
| `upload_slot_kick.sh` | Asks GitHub to run `upload.yml mode=slot` after each slot. It can't double-post, because `upload.py` checks the ledger. |
| `watchdog_kick.sh` | Asks GitHub to run `watchdog.yml`. It also starts `fetch_sources.sh` when the fetcher died (no finished cycle for 4 h), or when the pool is at 50 or below. The wait between those retries is 30, 60, 120 and then 180 min while cycles add nothing, and the lock stops two fetches running at once (`box_fetch.py should-run`). |
| `cleanup_posted.sh` → `cleanup_posted.py` | Writes the clips of posted videos into `ledger/used_clips.json` (so they're never reused), then deletes their local files. |
| `ig_token_refresh.sh` + `ig_handoff_decrypt.py` | Runs `ig-token.yml mode=refresh`, decrypts the encrypted artifact with the box-only key, and pipes the new token into `gh secret set IG_ACCESS_TOKEN` through stdin. Writes the outcome to repo variable `IG_TOKEN_REFRESH`. |
| `ensure_cron.sh` | Installs, shows or removes the cron lines. |
| `setup_box.sh` | One-time setup on a new computer (see section 5). |
| `requirements-box.txt` | Python packages for the box. |
| `local.env.example` | Template for `box/local.env`. |

## 3. GitHub workflows (`.github/workflows/`)

| Workflow | When it runs | What it does |
|---|---|---|
| `produce.yml` | 05:30 / 11:30 / 17:30 / 23:30 IST, and on dispatch | Picks clips (box `sources` release first; runner download only as a fallback). Gemini checks every clip (no captions, text, logos or watermarks). Picks a song that fits the mood, renders the 4K vertical master (2160x3840), runs QA (fails anything not 2160x3840), and adds it to the draft stock release `vc-ready`. |
| `upload.yml` | Every slot, plus every 20 min as a catch-up | Posts 1 video per due slot. YouTube gets the 4K master; Instagram gets a 1080x1920 copy, because the Instagram API allows at most 1920 px wide and 25 Mbps. The slot is locked in `ledger/posted.json` before posting and the post is checked afterwards, so a video is never posted twice. |
| `watchdog.yml` | :13 and :43 every hour, plus box kicks | Auto-produce: when a platform has fewer than 3 unposted videos, it starts produce for 3–4 videos. While Instagram is low, Instagram goes first and alone. It also keeps ONE GitHub issue labelled `health` open while something is wrong: a missed slot, stock at 0, 2 failed produce runs in a row, a token problem, or a failed IG refresh. The issue closes by itself when things recover. |
| `songs.yml` | 02:45 IST daily | Tops up the `songs` release from GitHub (backup to the box). |
| `ig-token.yml` | Owner dispatch (from the box cron) | Checks the IG token and shows only safe facts. In refresh mode, Instagram-Login tokens are refreshed with no app secret, and the new token leaves only encrypted to the box's public key. |
| `ig-first.yml` | ONE-OFF, 2026-11-27 13:35 IST | Right after the Gemini reset, runs the auto-produce check, so the first produce is Instagram-only. It does nothing on any other day. Delete it afterwards. |
| `maintenance.yml` | Owner, manual | `list` shows live posts next to the ledger; `delete-duplicate` is a guarded delete of one duplicate. |
| `dedupe-1830.yml` | Owner, manual | One-shot cleanup from the 2026-11-26 18:30 double upload. Can be deleted. |
| `ci.yml` | Every push | Compile check plus unit tests. |

Scheduled runs only happen while repo variable `SCHEDULE_ENABLED=true`.

### Produce time caps + Gemini quota gate (2026-11-26)
- `pipeline_lib/quota_gate.py`: when every Gemini key AND every backup model is out of quota (per the
  saved `{platform}/gemini_quota.json` cool-downs on pipeline-state; no Gemini call is made), produce.yml's
  plan job ends the run in about a minute with a "Gemini quota out until ..." notice and no produce job.
  `pipeline.py produce` checks again at start (ends at once, status.json `last_run.quota_blocked`) and
  stops mid-run if the quota runs out. The watchdog (autoproduce) does not dispatch until the reset (~13:30 IST).
  Manual override for manual-pick runs: dispatch input `ignore_quota_gate=true`.
- Hard caps: attempt 10 min before render + 20 min render/4K/QA (the cap can no longer be swallowed by an
  `except Exception`), run budget `max_minutes` 60 and no attempt starts with < 30 min left,
  Produce step 75 min, restore step 20 min, job 100 min.
- Every final is a 4K master: Shorts/Reels 2160x3840, YouTube (wide) 3840x2160 (Lanczos upscale of the
  2560x1440 render). Until 26 Nov night QA wanted 2160x3840 for YouTube too, so every YouTube video failed.
- Render cuts use a unique temp file per worker (looped timelines repeat a cut; the shared .part file raced).
- Instagram + YouTube run in parallel in one run (matrix, Instagram listed first); the watchdog dispatches
  `both` when both are low. Each platform job still has its own concurrency group, so a second run waits
  for the same platform's job of the previous run (now at most ~100 min, usually far less).

## 4. Where things live

| What | Where |
|---|---|
| Ready stock (unposted videos) | Draft release **`vc-ready`** (`.mp4`, `.preview.mp4` and `.mp4.json` per video, plus `manifest.json`) |
| Posted videos (archive) | Draft release **`vc-posted-archive`** (moved there by `archive_posted.py`) |
| Clip pool from the box | Draft release **`sources`**: clips plus `manifest.json` (id, genre, title, url, resolution, has_dialogue, vetted, unusable) |
| Song stock | Draft release **`songs`**: audio plus `manifest.json` (title, artist, genre, mood, bpm) |
| Post ledger | `ledger/posted.json` on `main`. Statuses: posting (lock), posted, failed. Duplicates are noted under `duplicates`. |
| Used clips (never reused) | Produce-time reservations: **`clip_reservations.json` on branch `pipeline-state`** (reserved / used). Posted clips: `ledger/used_clips.json` on `main` |
| Run status, Gemini quota, Gemini vet results, fetch queue | Branch **`pipeline-state`**: `status.json` and `{youtube,instagram}/state.json`, `fetch_queue.json` |
| Gemini quota state between runs | `gemini_quota.json` in the produce cache |
| Clips that need a manual pick (Gemini down) | `queue/needs_manual_pick.json` |
| Box working data | `$VICE_DATA_DIR` (default `~/vice-cartel/box_fetch`): logs, `box_heartbeat.json`, `pool_state.json`, `box_rejects.json` |
| Settings | `config/pipeline.json` (genres, official channels, pool targets, 1080p floor), `config/songs.json` (song seeds), `config/publish.json` (slots, releases) |

### GitHub Secrets (names only)

| Purpose | Secret names |
|---|---|
| YouTube | `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN` |
| Instagram | `IG_ACCESS_TOKEN`, `IG_USER_ID` |
| Gemini | `GEMINI_API_KEY_1` … `GEMINI_API_KEY_10`, `GEMINI_API_KEY_12`, `GEMINI_API_KEY_13` (12 keys; `GEMINI_API_KEY_11` was invalid and is deleted; empty slots are skipped) |
| Buffer (optional fallback, off) | `BUFFER_API_KEY` |
| Would allow IG refresh for the current token type | `IG_APP_ID`, `IG_APP_SECRET` (not set) |

Repo variables: `SCHEDULE_ENABLED`, `IG_TOKEN_REFRESH` (status text only).

## 5. Setting up a new computer

1. Install `git`, `python3` (3.10+), `ffmpeg`, `gh` (GitHub CLI) and `deno` (yt-dlp needs it for YouTube). On macOS, also run `brew install coreutils` to get `gtimeout`.
2. Clone the repo: `git clone https://github.com/Bhupi820970/pro-edit.git && cd pro-edit`
3. Log in: `gh auth login` with the account that owns the repo. It needs the repo and workflow scopes.
4. Run `bash box/setup_box.sh`. It checks the tools, installs the Python packages, creates `box/local.env`, and creates the IG handoff key pair if it's missing.
5. Edit `box/local.env`:
   - `VICE_DATA_DIR`
   - optionally `GH_CONFIG_DIR`
   - `VICE_YT_COOKIES_BROWSER` (a browser profile logged in to YouTube; cookies are read from the browser, never copied into git)
   - `VICE_YTDLP_JS_RUNTIME`
6. **On ONE computer only:** first run `bash box/ensure_cron.sh remove` on the old computer. Then run `bash box/ensure_cron.sh install` on the new one.

**IG handoff key pair.** The private key is NOT in git, and it's needed only for the IG token refresh. To make a new pair:

```
umask 077; mkdir -p ~/.config/vice-cartel
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out ~/.config/vice-cartel/ig_handoff_rsa.pem
openssl pkey -in ~/.config/vice-cartel/ig_handoff_rsa.pem -pubout -out config/ig_handoff_pub.pem
git add -f config/ig_handoff_pub.pem && git commit -m "new IG handoff public key" && git push
```

Point `VICE_IG_HANDOFF_KEY` in `box/local.env` at the private key if it's somewhere else.

## 6. Known issues (2026-11-26)

- **Gemini quota.** The daily quota resets at **13:30 IST** (midnight Pacific). The next reset after this handoff is 2026-11-27 13:30 IST.
  - Model order is set in `pipeline_lib/gemini.py`: the flash-lite models first, then flash, then a backup tier on the same keys (`gemini-3.1-flash-lite-preview`, other flash versions, Gemma 4).
  - When every key and model is out, produce writes `queue/needs_manual_pick.json`, and auto-produce waits until the reset.
- **Instagram first.** Instagram stock was 0 on 2026-11-26. An Instagram-only auto run started at 20:58 IST that evening.
  - Auto-produce now always runs Instagram alone while it's under 3.
  - Backup stock (27 Nov): target 20 ready per platform, refill starts below 8 (3-4/run, 60 min apart while <3, 120 min while topping up), alert at <= 2 (health issue + `queue/stock_alert.json` on pipeline-state). Thresholds: `pipeline_lib/autoproduce.py`.
  - `ig-first.yml` repeats the check at 13:35 IST on 2026-11-27.
- **IG token type.** It's a **Facebook-Login** token (graph.facebook.com). It works today, but its expiry can't be read without an app token.
  - The weekly refresh only works for Instagram-Login tokens. Refreshing this type needs secrets **`IG_APP_ID` + `IG_APP_SECRET`**; alternatively, make the token a never-expiring Page token.
  - If uploads start failing with error 190, create a new token and update `IG_ACCESS_TOKEN`.
- **IG duplicate.** The 2026-11-26 18:30 slot was posted twice on Instagram.
  - Duplicate: `https://www.instagram.com/reel/DgNLBERIlWL/`. Original: `https://www.instagram.com/reel/DgNKzWdhHL9/`.
  - At 20:02 IST the API showed the duplicate still live and the original **not** live. **`DgNLBERIlWL` may now be the only copy: check before deleting it.** The ledger notes this under `duplicates`.
  - The YouTube duplicate `GoCHmaSRViw` was deleted; the original `4DJCHbfbcbk` is live.
- **Marvel clips are scarce.** Almost every upload on Marvel's official channel has a text line near the top, so the caption and text check rejects it. Expect few or no Marvel clips. DC, anime, celebrity and comedy started from 0 on 2026-11-26 and fill over a few fetch cycles.
- **1080p floor.**
  - Scope uploads (1920x800) are excluded because they are under 1080 lines; that costs some movie clips.
  - 10 older clips in `sources` are marked `unusable` (4 below 1080p, 6 couldn't be measured). They were kept, not deleted.
- **Fetch queue from auto runs is ignored.** The box only trusts queue requests from owner-started runs, so requests written by auto runs (`github-actions[bot]`) are ignored. The pool refill works without them.
- **Clip reuse (fixed on 2026-11-26).** A clip is now reserved the moment a produce run picks it (`pipeline_lib/clip_reserve.py`), and marked **used** as soon as its video is rendered, not 2 days later.
  - The ledger is `clip_reservations.json` on `pipeline-state`. Every write is a compare-and-swap on the file version, so the parallel YouTube and Instagram jobs can never take the same clip.
  - If a render or QA fails, its clips are released. A crashed run's reservations expire after 8 h.
  - If the ledger can't be reached, the clip is skipped, so a clip is never repeated.
  - GTA is no longer an exception: a used GTA clip is never picked again either, so GTA videos depend on new GTA clips.
- **IG account permissions.** The Instagram API can't delete media ("insufficient permissions"); delete duplicates in the app.
- **Shell-start hook on the old box.** Box resets re-run `ensure_cron.sh install` through a hook in `~/.profile` and `~/.bashrc` on the old box. `ensure_cron.sh remove` also deletes that hook.
