# Security model

This repo is designed to be safe even if it is made **public**.

## Secrets
* Gemini keys live only in **GitHub Actions secrets** `GEMINI_API_KEY_1..13` (set with `gh secret set` from env,
  never echoed) and in the box environment. Code reads them from `os.environ` only and never logs them.
* YouTube upload uses `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, and `YT_REFRESH_TOKEN` (Actions secrets only).
  The access token is requested at run time and immediately masked with `::add-mask::`. It is never committed
  or printed. Instagram posting uses `BUFFER_API_KEY` the same way.
* No cookies, tokens, `.env` files or media are ever committed (`.gitignore` blocks `*.mp4`, `*.wav`, `cookies*.txt`,
  `.env`, `*.key`, …). Git history was audited before any visibility change: only code, README and JSON plans.
  The posted ledger (`ledger/posted.json`) stores only video ids and public post URLs, never credentials.

## GitHub Actions
* Workflows trigger **only** on `push`, `schedule`, and `workflow_dispatch` — no `pull_request` / `pull_request_target`
  / `issue_comment` triggers, so code from forks, PRs or issues never runs with secrets.
* Jobs run only in `Bhupi820970/pro-edit` (never on forks). Manual runs of produce/upload are limited to the
  repo owner (produce also accepts `github-actions[bot]` so the upload workflow can request a refill).
  Scheduled produce and upload runs only when the repo variable `SCHEDULE_ENABLED=true`. Do not set that
  variable until a test upload has been approved.
* Workflow-level permissions are empty. `produce` gets `contents: write` (draft releases + the
  `pipeline-state` branch) and `actions: write` (artifacts). `upload` gets `contents: write` (the posted
  ledger) and `actions: write` (to dispatch a produce refill). `ci` is read-only.
* Third-party Actions are pinned to a full commit SHA (`actions/checkout`, `setup-python`, `cache`,
  `upload-artifact`). Checkout uses `persist-credentials: false`; later git/gh steps pass `GH_TOKEN` explicitly.
* Secrets are not available to workflows triggered from forks (GitHub default) and no workflow passes them to
  untrusted code.
* `main` is protected (no force-push, no deletion) once the repo is public. Enable GitHub secret scanning and
  push protection.

## Box fetcher (`box_fetch.py`, run by `box/fetch_sources.sh` from crontab)
* Plain script, no AI on the box: `box_fetch.py cycle --no-gemini`. Gemini search queries only arrive as data in
  the trust-checked `fetch_queue.json` written by produce runs.
* `box/fetch_sources.sh` reads box-only settings from `box/local.env` and `$VICE_DATA_DIR/box.env` (both outside git) by
  parsing a fixed set of `KEY=value` names with a strict value pattern — the file is never sourced or executed.
  No account names, passwords or browser data are stored in the repo.
* Never executes commands or code taken from the repo, issues, PRs, release assets or Gemini output.
* Reads only a plain JSON queue (`<platform>/fetch_queue.json` on branch `pipeline-state`). The last commit
  touching each queue file must be an ancestor of `pipeline-state` and either (a) authored by the owner
  (`Bhupi820970`) or (b) authored/committed as the workflow bot identity this repo uses
  (`vice-pipeline-bot` / `github-actions[bot]` + the Actions noreply email) *and* tied, via the GitHub API,
  to a `produce.yml` run in `Bhupi820970/pro-edit` whose triggering actor is the owner and whose
  event is `push`, `schedule`, or `workflow_dispatch` (never PR events). Anything that fails is ignored.
  Queue entries are still schema-validated (URLs, queries, limits) and are never executed.
* Every entry is validated: category must exist in `config/pipeline.json`; URLs must be plain `https://` links on
  `youtube.com` / `youtu.be` only (watch / shorts / youtu.be video ids) matching a strict pattern, ≤ 200 chars, no shell
  metacharacters, not starting with `-`; queries ≤ 120 chars from a safe character set; limit 1–5.
* yt-dlp is called through `subprocess.run([...])` with an argument list — never `shell=True`.
* The box never auto-updates its code. `scripts/box_update.sh` fast-forwards from `origin/main` only, and only if
  every new commit's GitHub author is the owner.
* Browser cookies (`--cookies-from-browser`) are **opt-in**, read locally by yt-dlp for a single download, and never
  written to the repo, releases, logs or manifests.

## Content
* Source clips and rendered videos are copyrighted footage/music used for fan edits. Ready stock lives in the
  **draft** release `vc-ready`. Source clips and official audio stay in the draft releases `sources` and `songs`.
  Never publish those draft releases, even if the repo itself is public for free Actions minutes.
* The upload workflow posts to YouTube (Data API v3, channel `@vicecartelgta`) and Instagram (Buffer API,
  `@benreelr`) only after `SCHEDULE_ENABLED=true` or a manual `upload` test dispatch by the owner. A video id
  in `ledger/posted.json` is never posted again.
* Buffer cannot fetch draft-release URLs. For each Instagram post the runner copies that one file to a
  short-lived **published** release `vc-xfer-<run>-<hex>` with a random filename, uses `shareNow`, waits until
  Buffer reports the post as sent, then deletes only that transfer release. Protected tags (`vc-ready`,
  `sources`, `songs`, `test-renders`) are never deleted. YouTube uploads go straight through the resumable
  API and do not need a public file URL.
* After an upload the runner may delete used files on the Actions runner only. It never deletes ready/source
  stock, the songs library, backups, or the repo.

