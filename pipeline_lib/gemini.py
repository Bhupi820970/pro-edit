"""Gemini REST client with key rotation + model fallback.

Keys come ONLY from the environment (GEMINI_API_KEY_1..N, or GEMINI_API_KEY); they are never
printed, logged or written to disk.  On 429 / quota / 5xx / network errors the client moves to the
next key; on "model unavailable / overloaded / not found" it moves to the next model.
Uploaded files belong to the key's project, so video vetting uploads with the same key it queries.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from .util import home, load_json, now_local, save_json, today

API = "https://generativelanguage.googleapis.com"
# Vetting / discovery uses flash-lite first; the larger flash models are only tried if lite fails.
LITE_MODELS = [m for m in os.environ.get("GEMINI_LITE_MODELS", "").split(",") if m] or [
    "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-flash-lite-latest"]
FULL_MODELS = [m for m in os.environ.get("GEMINI_FULL_MODELS", "").split(",") if m] or [
    "gemini-3-flash-preview", "gemini-3.8-flash", "gemini-flash-latest"]
DEFAULT_MODELS = [m for m in os.environ.get("GEMINI_MODELS", "").split(",") if m] or (LITE_MODELS + FULL_MODELS)


class GeminiError(RuntimeError):
    pass


def next_quota_reset(now: datetime | None = None) -> datetime:
    """Gemini / AI Studio daily quota resets at midnight America/Los_Angeles (~1:30 PM IST in winter)."""
    from .util import now_local
    now = now or now_local()
    try:
        pacific = now.astimezone(ZoneInfo("America/Los_Angeles"))
    except Exception:
        pacific = now.astimezone(timezone.utc).astimezone(timezone(timedelta(hours=-8)))
    nxt = pacific.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return nxt.astimezone(now.tzinfo or timezone(timedelta(hours=5, minutes=30)))


def _keys() -> list[tuple[str, str]]:
    ks = []
    for i in range(1, 21):
        v = os.environ.get(f"GEMINI_API_KEY_{i}")
        if v:
            ks.append((f"key{i}", v))
    if not ks and os.environ.get("GEMINI_API_KEY"):
        ks.append(("key", os.environ["GEMINI_API_KEY"]))
    return ks


class Gemini:
    def __init__(self, models: list[str] | None = None, log=print, cooldown_s: int = 120):
        self.keys = _keys()
        if not self.keys:
            raise GeminiError("no GEMINI_API_KEY_1..10 in the environment")
        self.models = models or DEFAULT_MODELS
        self.log = log
        self.k = 0
        self.dead_until: dict[str, float] = {}   # key label -> time
        self.bad_models: set[str] = set()
        self.cooldown_s = cooldown_s
        self.calls = []                          # (key label, model, ok, note) for the run log
        self._load_quota()

    # ---------------------------------------------------------------- http
    def _req(self, key: str, method: str, url: str, body: bytes | None = None, headers=None, timeout=300):
        h = {"x-goog-api-key": key}
        h.update(headers or {})
        r = urllib.request.Request(url, data=body, headers=h, method=method)
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), e.read()

    def _quota_path(self) -> Path:
        return home() / "gemini_quota.json"

    def _load_quota(self):
        d = load_json(self._quota_path(), {}) or {}
        now = time.time()
        for label, until in (d.get("dead_until") or {}).items():
            try:
                ts = float(until)
            except (TypeError, ValueError):
                continue
            if ts > now:
                self.dead_until[label] = max(self.dead_until.get(label, 0), ts)

    def _save_quota(self):
        now = time.time()
        save_json(self._quota_path(), {
            "date": today(),
            "dead_until": {k: v for k, v in self.dead_until.items() if v > now},
        })

    def quota_exhausted(self) -> bool:
        if not self.keys:
            return True
        now = time.time()
        return all(self.dead_until.get(lab, 0) > now for lab, _ in self.keys)

    def _key_order(self):
        now = time.time()
        n = len(self.keys)
        order = [self.keys[(self.k + i) % n] for i in range(n)]
        return [kv for kv in order if self.dead_until.get(kv[0], 0) < now]

    @staticmethod
    def _msg(raw: bytes) -> str:
        try:
            return json.loads(raw)["error"]["message"][:200]
        except Exception:
            return raw[:200].decode("utf8", "replace")

    # ---------------------------------------------------------------- files
    def upload(self, key: str, path: Path, mime: str | None = None) -> dict:
        path = Path(path)
        mime = mime or mimetypes.guess_type(str(path))[0] or "video/mp4"
        size = path.stat().st_size
        st, hd, raw = self._req(key, "POST", f"{API}/upload/v1beta/files", json.dumps(
            {"file": {"display_name": path.name}}).encode(), {
            "X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(size), "X-Goog-Upload-Header-Content-Type": mime,
            "Content-Type": "application/json"}, timeout=60)
        if st != 200:
            raise GeminiError(f"upload start {st}: {self._msg(raw)}")
        url = next((v for k, v in hd.items() if k.lower() == "x-goog-upload-url"), None)
        st, _, raw = self._req(key, "POST", url, path.read_bytes(), {
            "X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize",
            "Content-Length": str(size)}, timeout=900)
        if st != 200:
            raise GeminiError(f"upload {st}: {self._msg(raw)}")
        f = json.loads(raw)["file"]
        t0 = time.time()
        while f.get("state") == "PROCESSING":
            if time.time() - t0 > 600:
                raise GeminiError("file processing timeout")
            time.sleep(4)
            st, _, raw = self._req(key, "GET", f"{API}/v1beta/{f['name']}", timeout=60)
            if st == 200:
                f = json.loads(raw)
        if f.get("state") == "FAILED":
            raise GeminiError("file processing failed")
        return f

    def delete_file(self, key: str, name: str):
        try:
            self._req(key, "DELETE", f"{API}/v1beta/{name}", timeout=30)
        except Exception:
            pass

    # ---------------------------------------------------------------- generate
    def generate(self, parts: list[dict], schema: dict | None = None, search: bool = False,
                 video: Path | None = None, images: list[Path] | None = None,
                 temperature: float = 0.4, tries_per_combo: int = 1,
                 models: list[str] | None = None) -> dict | str:
        """Return parsed JSON (schema) or text.  Lite models first; larger ones only if lite fails."""
        if self.quota_exhausted():
            raise GeminiError("all Gemini keys are quota-exhausted until the Pacific midnight reset; skipping retries")
        errors = []
        live = self._key_order()
        if not live:
            raise GeminiError("all Gemini keys are cooling down; skipping retries")
        import base64
        img_parts = []
        for im in images or []:
            im = Path(im)
            if not im.is_file() or im.stat().st_size < 200:
                continue
            mime = "image/jpeg" if im.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
            img_parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(im.read_bytes()).decode()}})
        model_list = models or self.models
        for label, key in live:
            fobj = None
            try:
                if video is not None:
                    fobj = self.upload(key, video)
                    vparts = [{"file_data": {"mime_type": fobj.get("mimeType", "video/mp4"),
                                             "file_uri": fobj["uri"]}}] + parts
                elif img_parts:
                    vparts = list(parts) + img_parts
                else:
                    vparts = parts
            except Exception as e:
                errors.append(f"{label}: {e}")
                self.calls.append((label, "-", False, f"upload: {str(e)[:80]}"))
                if "429" in str(e) or "quota" in str(e).lower():
                    self._mark(label, str(e))
                continue
            try:
                for model in model_list:
                    if model in self.bad_models:
                        continue
                    body = {"contents": [{"role": "user", "parts": vparts}],
                            "generationConfig": {"temperature": temperature}}
                    if video is not None:   # fewer tokens per frame: faster + cheaper, logos still visible
                        body["generationConfig"]["mediaResolution"] = "MEDIA_RESOLUTION_MEDIUM"
                    if schema and not search:
                        body["generationConfig"]["responseMimeType"] = "application/json"
                        body["generationConfig"]["responseSchema"] = schema
                    if search:
                        body["tools"] = [{"google_search": {}}]
                    for attempt in range(tries_per_combo):
                        try:
                            st, _, raw = self._req(key, "POST", f"{API}/v1beta/models/{model}:generateContent",
                                                   json.dumps(body).encode(), {"Content-Type": "application/json"},
                                                   timeout=180)
                        except Exception as ex:   # timeout / network: next model
                            self.log(f"gemini {label}/{model}: {type(ex).__name__} {str(ex)[:80]}")
                            st, raw = 599, json.dumps({"error": {"message": str(ex)[:150]}}).encode()
                        if st == 200:
                            d = json.loads(raw)
                            try:
                                txt = "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"])
                            except Exception:
                                errors.append(f"{label}/{model}: empty response")
                                break
                            self.calls.append((label, model, True, ""))
                            self.k = (self.keys.index((label, key)) + 1) % len(self.keys)  # spread load
                            if schema:
                                return parse_json(txt)
                            return txt
                        msg = self._msg(raw)
                        self.log(f"gemini {label}/{model}: HTTP {st} {msg[:90]}")
                        self.calls.append((label, model, False, f"{st} {msg[:80]}"))
                        errors.append(f"{label}/{model}: {st} {msg[:100]}")
                        if st in (404,) or "no longer available" in msg or "not found" in msg.lower():
                            self.bad_models.add(model)
                            break
                        if st == 429 or "quota" in msg.lower() or st in (401, 403):
                            self._mark(label, msg)
                            raise _NextKey()
                        if st == 599:
                            break
                        if st in (500, 502, 503, 504):
                            time.sleep(2 + attempt * 3)
                            continue
                        break   # 400 etc: try next model
            except _NextKey:
                pass
            finally:
                if fobj:
                    self.delete_file(key, fobj["name"])
        raise GeminiError("all Gemini keys/models failed: " + " | ".join(errors[-6:]))

    def _mark(self, label: str, msg: str):
        low = (msg or "").lower()
        per_min = ("per minute" in low) or ("per 60" in low)
        daily = ("quota" in low) or ("per day" in low) or ("daily" in low) or ("exceeded your current" in low)
        if daily and not per_min:
            until = next_quota_reset().timestamp()  # ~1:30 PM IST (midnight Pacific)
            when = datetime.fromtimestamp(until, next_quota_reset().tzinfo).strftime("%Y-%m-%d %H:%M IST")
            self.log(f"gemini: {label} daily quota hit -> parked until {when}")
        else:
            until = time.time() + self.cooldown_s
            self.log(f"gemini: {label} rate-limited/failed -> next key")
        self.dead_until[label] = max(self.dead_until.get(label, 0), until)
        self._save_quota()


class _NextKey(Exception):
    pass


def parse_json(txt: str):
    txt = txt.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", txt, re.S)
    if m:
        txt = m.group(1)
    try:
        return json.loads(txt)
    except Exception:
        m = re.search(r"(\{.*\}|\[.*\])", txt, re.S)
        if m:
            return json.loads(m.group(1))
        raise

