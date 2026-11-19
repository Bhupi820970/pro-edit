"""Trust checks for pipeline-state fetch-queue commits (data only — never executed).

A queue file is used only when ALL of the following hold:
  * We found it as the latest commit touching that path on branch `pipeline-state`
    (and a compare confirms the commit is an ancestor of that branch).
  * The commit is by the repo owner, OR it is the exact workflow-bot identity
    (github-actions[bot] / vice-pipeline-bot + the Actions noreply email) AND it
    can be tied to a produce.yml run in Bhupi820970/pro-edit whose
    triggering actor is the owner and whose event is push, schedule, or
    workflow_dispatch (never PR events).
Anything that fails is skipped. Callers still must schema-validate every entry.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

OWNER = "Bhupi820970"
REPO = "Bhupi820970/pro-edit"
BRANCH = "pipeline-state"
WORKFLOW_PATH = ".github/workflows/produce.yml"
ALLOWED_EVENTS = frozenset({"push", "schedule", "workflow_dispatch"})
BOT_NAMES = frozenset({"vice-pipeline-bot", "github-actions[bot]"})
BOT_EMAILS = frozenset({
    "actions@users.noreply.github.com",
    "41898282+github-actions[bot]@users.noreply.github.com",
})
BOT_LOGINS = frozenset({"actions", "github-actions[bot]"})
MSG_RX = re.compile(r"^state: (youtube|instagram) run (\d{6,12})$")
SHA_RX = re.compile(r"^[0-9a-f]{40}$")
SLACK = timedelta(minutes=5)


def _login(user) -> str:
    return ((user or {}).get("login") or "").strip()


def _person(block) -> tuple[str, str, str]:
    block = block or {}
    return (block.get("name") or "").strip(), (block.get("email") or "").strip().lower(), block.get("date") or ""


def identity(commit: dict) -> dict:
    c = (commit or {}).get("commit") or {}
    aname, aemail, adate = _person(c.get("author"))
    cname, cemail, _ = _person(c.get("committer"))
    return {
        "sha": (commit.get("sha") or "").strip(),
        "author_login": _login(commit.get("author")),
        "committer_login": _login(commit.get("committer")),
        "author_name": aname,
        "author_email": aemail,
        "committer_name": cname,
        "committer_email": cemail,
        "date": adate,
        "message": (c.get("message") or "").strip(),
    }


def is_owner_commit(commit: dict) -> bool:
    ident = identity(commit)
    return ident["author_login"] == OWNER or ident["committer_login"] == OWNER


def is_workflow_bot_commit(commit: dict) -> bool:
    """Exact bot identity the workflow uses (or the standard github-actions[bot])."""
    ident = identity(commit)
    names = {ident["author_name"], ident["committer_name"]}
    emails = {ident["author_email"], ident["committer_email"]}
    logins = {ident["author_login"], ident["committer_login"]}
    if not (names & BOT_NAMES):
        return False
    if not (emails & BOT_EMAILS):
        return False
    # login is supporting evidence: GitHub maps the Actions noreply email to "actions"
    # or the official bot login. A missing login is not enough to pass.
    if not (logins & BOT_LOGINS):
        return False
    return True


def parse_state_message(message: str, path: str) -> tuple[str, str] | None:
    m = MSG_RX.match((message or "").splitlines()[0].strip())
    if not m:
        return None
    platform, run_id = m.group(1), m.group(2)
    if not (path or "").startswith(platform + "/"):
        return None
    return platform, run_id


def parse_ts(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    s = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def commit_in_run_window(commit: dict, run: dict) -> bool:
    ident = identity(commit)
    at = parse_ts(ident["date"])
    start = parse_ts(run.get("started_at")) or parse_ts(run.get("created_at"))
    end = parse_ts(run.get("completed_at")) or parse_ts(run.get("updated_at")) or datetime.now(timezone.utc)
    if not at or not start:
        return False
    return (start - SLACK) <= at <= (end + SLACK)


def run_is_trusted(run: dict | None, commit: dict, repo: str, run_id: str) -> tuple[bool, str]:
    if not isinstance(run, dict):
        return False, f"produce.yml run {run_id} not found"
    if str(run.get("id") or "") != str(run_id):
        return False, f"run id mismatch (got {run.get('id')})"
    full = ((run.get("repository") or {}).get("full_name") or run.get("repository") or "")
    if full != repo:
        return False, f"run is not in {repo}"
    if (run.get("path") or "") != WORKFLOW_PATH:
        return False, f"run is not {WORKFLOW_PATH}"
    event = run.get("event") or ""
    if event not in ALLOWED_EVENTS:
        return False, f"run event '{event}' is not allowed"
    actor = _login(run.get("triggering_actor")) or _login(run.get("actor"))
    # triggering_actor is required when present on the payload; do not fall back if
    # the field exists but is a different user.
    if run.get("triggering_actor") is not None:
        actor = _login(run.get("triggering_actor"))
    if actor != OWNER:
        return False, f"run triggering actor '{actor}' is not the owner"
    if (run.get("head_branch") or "") != "main":
        return False, f"run head branch is '{run.get('head_branch')}', not main"
    if not commit_in_run_window(commit, run):
        return False, f"commit time is outside produce.yml run {run_id} window"
    return True, "ok"


def on_pipeline_state(compare: dict | None) -> bool:
    """True if compare(commit...pipeline-state) shows the commit is an ancestor (ahead/identical, not behind)."""
    if not isinstance(compare, dict):
        return False
    if compare.get("status") not in {"ahead", "identical"}:
        return False
    try:
        return int(compare.get("behind_by") or 0) == 0
    except (TypeError, ValueError):
        return False


def trust_queue_commit(commit: dict, path: str, repo: str = REPO,
                       get_run=None, get_compare=None) -> tuple[bool, str]:
    """Return (ok, reason). get_run(repo, run_id) and get_compare(repo, sha) are injected I/O."""
    if not isinstance(commit, dict):
        return False, "malformed commit"
    ident = identity(commit)
    sha = ident["sha"]
    if not SHA_RX.match(sha):
        return False, "commit sha missing or invalid"
    if get_compare is not None and not on_pipeline_state(get_compare(repo, sha)):
        return False, f"commit {sha[:12]} is not on branch {BRANCH}"
    if is_owner_commit(commit):
        return True, f"owner commit by {OWNER}"
    if not is_workflow_bot_commit(commit):
        who = ident["author_login"] or ident["author_name"] or "unknown"
        return False, f"last commit by '{who}' is not a trusted owner/workflow-bot identity"
    parsed = parse_state_message(ident["message"], path)
    if not parsed:
        return False, "bot commit message is not 'state: <platform> run <id>' or platform does not match the queue path"
    _platform, run_id = parsed
    if get_run is None:
        return False, "no workflow-run lookup provided"
    ok, reason = run_is_trusted(get_run(repo, run_id), commit, repo, run_id)
    if not ok:
        return False, reason
    return True, f"workflow bot commit tied to produce.yml run {run_id}"

