from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def home() -> Path:
    h = Path(os.environ.get("VICE_PIPELINE_HOME", "/workspace/gta6/pipeline"))
    h.mkdir(parents=True, exist_ok=True)
    return h


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


def run(cmd, timeout=None, check=True, **kw) -> subprocess.CompletedProcess:
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=timeout, **kw)
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

