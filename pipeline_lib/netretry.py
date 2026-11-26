"""3 tries with exponential backoff for network steps (yt-dlp, YouTube API, IG Graph API, gh release I/O)."""
from __future__ import annotations

import os
import random
import subprocess
import time


def retry(fn, tries: int = 3, base: float = 4.0, label: str = "network step", log=print,
          retry_on: tuple = (Exception,), give_up=None):
    """Call fn() up to `tries` times, sleeping base, 2*base, ... (+jitter) between tries.
    give_up(exc) -> True stops at once (e.g. 4xx auth errors that will not heal by waiting)."""
    for i in range(tries):
        try:
            return fn()
        except retry_on as e:
            if i == tries - 1 or (give_up and give_up(e)):
                raise
            wait = 0.0 if os.environ.get("PYTEST_CURRENT_TEST") else base * (2 ** i) + random.uniform(0, 1)
            try:
                from .publish.secrets import redact
                msg = redact(str(e))[:160]
            except Exception:
                msg = type(e).__name__
            log(f"{label}: try {i + 1}/{tries} failed ({msg}); retrying in {wait:.0f}s")
            time.sleep(wait)


# gh subcommands that are safe to repeat (idempotent reads / --clobber uploads)
_SAFE_GH = {("release", "download"), ("release", "upload"), ("release", "view"), ("release", "list"),
            ("run", "list"), ("run", "view")}


def is_retryable_cmd(cmd) -> bool:
    c = [str(x) for x in cmd]
    if not c:
        return False
    exe = c[0].rsplit("/", 1)[-1]
    if exe in ("yt-dlp",) or (exe in ("python", "python3") and c[1:3] == ["-m", "yt_dlp"]):
        return True
    if exe == "gh" and len(c) >= 3:
        if tuple(c[1:3]) in _SAFE_GH:
            return True
        if c[1] == "api" and "-X" not in c and "--method" not in c:   # GET only
            return True
    return False


def run_retry(cmd, tries: int = 3, base: float = 4.0, **kw) -> subprocess.CompletedProcess:
    """subprocess.run (argument list, never a shell) retried while it exits non-zero."""
    r = None
    for i in range(tries):
        r = subprocess.run([str(c) for c in cmd], **kw)
        if r.returncode == 0 or i == tries - 1:
            return r
        time.sleep(base * (2 ** i) + random.uniform(0, 1))
    return r
