from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


_home_fallback_noted = False


def home() -> Path:
    """Pipeline data dir. Falls back to /tmp/vice-ph if the configured path is not writable.

    GitHub-hosted runners could not create the old fixed box default path, which used to
    raise PermissionError while constructing the Gemini client on upload.yml.
    """
    h = Path(os.environ.get("VICE_PIPELINE_HOME") or (Path(os.environ.get("VICE_BOX_ROOT") or Path(__file__).resolve().parents[2]) / "pipeline"))
    try:
        h.mkdir(parents=True, exist_ok=True)
        return h
    except OSError as e:
        fb = Path(os.environ.get("TMPDIR") or "/tmp") / "vice-ph"
        fb.mkdir(parents=True, exist_ok=True)
        global _home_fallback_noted
        if not _home_fallback_noted:
            _home_fallback_noted = True
            print(f"[pipeline] VICE_PIPELINE_HOME {h} is not writable ({type(e).__name__}); using {fb}", flush=True)
        return fb


def now_local() -> dt.datetime:
    tz = dt.timezone(dt.timedelta(hours=5, minutes=30))   # Asia/Calcutta (channel owner's zone)
    return dt.datetime.now(tz)


def today() -> str:
    return now_local().strftime("%Y-%m-%d")


_LOGF = None


def log(*a):
    global _LOGF
    msg = f"[{now_local().strftime('%Y-%m-%d %H:%M:%S')} IST] " + " ".join(str(x) for x in a)
    print(msg, flush=True)
    if _LOGF is None:
        d = home() / "logs"
        d.mkdir(exist_ok=True)
        _LOGF = open(d / f"pipeline_{today()}.log", "a")
    _LOGF.write(msg + "\n")
    _LOGF.flush()


_NO_RETRY = ("sign in to confirm", "not a bot", "video unavailable", "private video", "members-only",
             "copyright", "not available", "http error 404", "unsupported url", "no video formats",
             "requested format is not available", "release not found", "could not find any assets",
             "no assets match", "not found (http 404)", "http 404", "http 401", "http 403", "bad credentials",
             "vpn or a proxy server", "not made this video available in your country")


def run(cmd, timeout=None, check=True, retries: int | None = None, **kw) -> subprocess.CompletedProcess:
    """subprocess.run with an argument list (never a shell). Network commands (yt-dlp, gh release
    up/download/view, gh api GET) get 3 tries with exponential backoff (4s, 8s) unless the error is
    permanent (bot check, unavailable, 404, auth)."""
    import random
    import time as _t
    from .netretry import is_retryable_cmd
    tries = retries if retries is not None else (3 if is_retryable_cmd(cmd) else 1)
    for i in range(tries):
        r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=timeout, **kw)
        if r.returncode == 0 or i == tries - 1:
            break
        err = ((r.stderr or "") + (r.stdout or "")[-500:]).lower()
        if any(x in err for x in _NO_RETRY):
            break
        if not os.environ.get("PYTEST_CURRENT_TEST"):
            _t.sleep(4 * (2 ** i) + random.uniform(0, 1))
    if check and r.returncode != 0:
        raise RuntimeError(f"{Path(str(cmd[0])).name} failed ({r.returncode}): {(r.stderr or r.stdout)[-1500:]}")
    return r


def load_json(p: Path, default):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def save_json(p: Path, data):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    os.replace(tmp, p)


class Lock:
    def __init__(self, name="pipeline"):
        self.p = home() / f".{name}.lock"

    def __enter__(self):
        self.f = open(self.p, "w")
        t0 = time.time()
        while True:
            try:
                fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.time() - t0 > 6 * 3600:
                    raise
                time.sleep(10)

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def probe(path) -> dict:
    r = run(["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,width,height", "-of", "json", path])
    return json.loads(r.stdout)

