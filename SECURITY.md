# Security model

This repo is designed to be safe even if it is made **public**.

## Secrets
* Gemini keys live only in **GitHub Actions secrets** `GEMINI_API_KEY_1..6` (set with `gh secret set` from env,
  never echoed) and in the box environment. Code reads them from `os.environ` only and never logs them.
* No cookies, tokens, `.env` files or media are ever committed (`.gitignore` blocks `*.mp4`, `*.wav`, `cookies*.txt`,
  `.env`, `*.key`, …). Git history was audited before any visibility change: only code, README and JSON plans.

## GitHub Actions
* `produce.yml` triggers **only** on `schedule` and `workflow_dispatch` — no `pull_request` / `pull_request_target`
  / `issue_comment` triggers, so code from forks, PRs or issues never runs with secrets.
* Jobs run only in `Bhupi820970/pro-edit` (never on forks), manual runs only when the actor is the repo
  owner, scheduled runs only when the repo variable `SCHEDULE_ENABLED=true`.
* Workflow-level permissions are empty. The `plan` job gets none; the `produce` job gets `contents: write`
  (draft releases + the `pipeline-state` branch) and `actions: write` (artifacts).
* Third-party Actions are pinned to a full commit SHA (`actions/checkout`, `setup-python`, `cache`,
  `upload-artifact`). Checkout uses `persist-credentials: false`; later git/gh steps pass `GH_TOKEN` explicitly.
* Secrets are not available to workflows triggered from forks (GitHub default) and no workflow passes them to
  untrusted code.
* `main` is protected (no force-push, no deletion) once the repo is public. Enable GitHub secret scanning and
  push protection.

## Box fetcher (`box_fetch.py`)
* Never executes commands or code taken from the repo, issues, PRs, release assets or Gemini output.
* Reads only a plain JSON queue (`<platform>/fetch_queue.json` on branch `pipeline-state`). The last commit
  touching each queue file must be an ancestor of `pipeline-state` and either (a) authored by the owner
  (`Bhupi820970`) or (b) authored/committed as the workflow bot identity this repo uses
  (`vice-pipeline-bot` / `github-actions[bot]` + the Actions noreply email) *and* tied, via the GitHub API,
  to a `produce.yml` run in `Bhupi820970/pro-edit` whose triggering actor is the owner and whose
  event is `push`, `schedule`, or `workflow_dispatch` (never PR events). Anything that fails is ignored.
  Queue entries are still schema-validated (URLs, queries, limits) and are never executed.
* Every entry is validated: category must exist in `config/pipeline.json`; URLs must be plain `https://` links on
  `youtube.com` / `youtu.be` / `x.com` / `twitter.com` matching a strict pattern, ≤ 200 chars, no shell
  metacharacters, not starting with `-`; queries ≤ 120 chars from a safe character set; limit 1–5.
* yt-dlp is called through `subprocess.run([...])` with an argument list — never `shell=True`.
* The box never auto-updates its code. `scripts/box_update.sh` fast-forwards from `origin/main` only, and only if
  every new commit's GitHub author is the owner.
* Browser cookies (`--cookies-from-browser`) are **opt-in**, read locally by yt-dlp for a single download, and never
  written to the repo, releases, logs or manifests.

## Content
* Source clips and rendered videos are copyrighted footage/music used for fan edits. They are stored only as
  **draft GitHub releases** (visible to users with write access, not to the public) and as short-lived workflow
  artifacts. Never publish those draft releases, even if the repo itself is public for free Actions minutes.
* Nothing is uploaded to YouTube / Instagram / X by this code; the owner approves every video.

