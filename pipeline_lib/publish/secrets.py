"""Never print or log credential values. Mask newly issued tokens on GitHub Actions."""
from __future__ import annotations

import os
import re

SECRET_ENV = (
    "YT_CLIENT_ID",
    "YT_CLIENT_SECRET",
    "YT_REFRESH_TOKEN",
    "YT_ACCESS_TOKEN",
    "BUFFER_API_KEY",
    "IG_ACCESS_TOKEN",
)

_EXTRA: list[str] = []


def remember(*values: str | None) -> None:
    for v in values:
        if v and v not in _EXTRA and len(v) >= 8:
            _EXTRA.append(v)
            mask_github(v)


def mask_github(value: str) -> None:
    if not value or not os.environ.get("GITHUB_ACTIONS"):
        return
    # Actions only masks exact substrings; print once, immediately, before any other output.
    print(f"::add-mask::{value}", flush=True)


def env(name: str) -> str:
    v = os.environ.get(name) or ""
    if v:
        remember(v)
    return v


def redact(text: str | None) -> str:
    """Strip known secret values from a string so it is safe to log."""
    s = text or ""
    vals = [os.environ.get(n) or "" for n in SECRET_ENV] + list(_EXTRA)
    # longest first so a token contained in a longer secret still gets fully covered
    for v in sorted({x for x in vals if len(x) >= 8}, key=len, reverse=True):
        s = s.replace(v, "***")
    s = re.sub(r"(?i)(Bearer )\S+", r"\1***", s)
    s = re.sub(r"(?i)(access_token=)[^&\s]+", r"\1***", s)
    return s
