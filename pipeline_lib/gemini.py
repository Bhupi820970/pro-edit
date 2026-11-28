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
# Backup tier: older/cheaper models on the SAME keys. Quota is per key AND per model, so when the main models
# are out for the day these usually still answer. Names are checked against the live list-models call.
FALLBACK_MODELS = [m for m in os.environ.get("GEMINI_FALLBACK_MODELS", "").split(",") if m] or [
    # checked 2026-11-26 with list-models + a test call: the 2.x models answer 404 "no longer available to new
    # users", so the backup tier is the lite preview + other flash generations + Gemma 4 (own, larger quota)
    "gemini-3.1-flash-lite-preview", "gemini-3.5-flash", "gemini-3.6-flash", "gemma-4-31b-it", "gemma-4-26b-a4b-it"]
DEFAULT_MODELS = [m for m in os.environ.get("GEMINI_MODELS", "").split(",") if m] or (
    LITE_MODELS + FULL_MODELS + FALLBACK_MODELS)
INVALID_RECHECK_S = 24 * 3600   # an invalid key is skipped for a day (a fixed secret is picked up next day)


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
            raise GeminiError("no GEMINI_API_KEY_1..20 in the environment")
        self.models = models or DEFAULT_MODELS
        self.log = log
        self.k = 0
        self.dead_until: dict[str, float] = {}   # key label -> time
        self.bad_models: set[str] = set()
        self.cooldown_s = cooldown_s
        self.calls = []                          # (key label, model, ok, note) for the run log
        self.invalid: dict[str, float] = {}      # key label -> recheck time (API key not valid)
        self._available: set[str] | None = None  # model ids from list-models (None = unknown)
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
        for name, dst in (("dead_until", self.dead_until), ("invalid", self.invalid)):
            for label, until in (d.get(name) or {}).items():
                try:
                    ts = float(until)
                except (TypeError, ValueError):
                    continue
                if ts > now:
                    dst[label] = max(dst.get(label, 0), ts)

    def _save_quota(self):
        now = time.time()
        save_json(self._quota_path(), {
            "date": today(),
            "dead_until": {k: v for k, v in self.dead_until.items() if v > now},   # "key3" or "key3|model"
            "invalid": {k: v for k, v in self.invalid.items() if v > now},
        })

    def _key_dead(self, label: str, now: float | None = None) -> bool:
        now = now or time.time()
        return self.invalid.get(label, 0) > now or self.dead_until.get(label, 0) > now

    def _combo_dead(self, label: str, model: str, now: float | None = None) -> bool:
        now = now or time.time()
        return self._key_dead(label, now) or self.dead_until.get(f"{label}|{model}", 0) > now

    def usable_models(self) -> list[str]:
        ms = [m for m in self.models if m not in self.bad_models]
        if self._available:
            ms = [m for m in ms if m in self._available]
        return ms

    def quota_exhausted(self, models: list[str] | None = None) -> bool:
        """True only when no (key, model) pair can be tried: every key invalid/parked, or every model on every
        live key out of quota (the backup models included)."""
        if not self.keys:
            return True
        now = time.time()
        ms = [m for m in (models or self.usable_models()) if m not in self.bad_models]
        if not ms:
            return all(self._key_dead(lab, now) for lab, _ in self.keys)
        return all(self._combo_dead(lab, m, now) for lab, _ in self.keys for m in ms)

    def status(self) -> dict:
        """Per-key / per-model state for status.json (labels and model names only, never keys)."""
        now = time.time()
        return {"invalid_keys": sorted(k for k, v in self.invalid.items() if v > now),
                "models": {m: sum(1 for lab, _ in self.keys if not self._combo_dead(lab, m, now))
                           for m in self.usable_models()},
                "exhausted": self.quota_exhausted(),
                "reset_at": next_quota_reset().strftime("%Y-%m-%d %H:%M IST")}

    def _key_order(self, model: str | None = None):
        now = time.time()
        n = len(self.keys)
        order = [self.keys[(self.k + i) % n] for i in range(n)]
        if model:
            return [kv for kv in order if not self._combo_dead(kv[0], model, now)]
        return [kv for kv in order if not self._key_dead(kv[0], now)]

    def list_models(self) -> set[str] | None:
        """Model ids the API offers (cheap GET with the first working key). None if no key can list."""
        if self._available is not None:
            return self._available
        for label, key in self._key_order():
            try:
                st, _, raw = self._req(key, "GET", f"{API}/v1beta/models?pageSize=200", timeout=30)
            except Exception:
                continue
            if st == 200:
                names = {m.get("name", "").split("/", 1)[-1] for m in (json.loads(raw).get("models") or [])
                         if "generateContent" in (m.get("supportedGenerationMethods") or [])}
                self._available = names or None
                return self._available
            if st in (400, 401, 403) and "api key not valid" in self._msg(raw).lower():
                self._mark(label, self._msg(raw))
        return None

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
        if self._available is None:
            try:
                self.list_models()
            except Exception:
                pass
        model_list = [m for m in (models or self.models) if m not in self.bad_models]
        if self._available:
            model_list = [m for m in model_list if m in self._available] or model_list
        if self.quota_exhausted(model_list):
            raise GeminiError("all Gemini keys/models (backup models included) are quota-exhausted until the "
                              "Pacific midnight reset; skipping retries")
        errors = []
        import base64
        img_parts = []
        for im in images or []:
            im = Path(im)
            if not im.is_file() or im.stat().st_size < 200:
                continue
            mime = "image/jpeg" if im.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
            img_parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(im.read_bytes()).decode()}})
        files: dict[str, dict] = {}      # key label -> uploaded video (files belong to the key's project)
        try:
            for model in model_list:     # main models first, then the backup tier, each on every live key
                if model in self.bad_models:
                    continue
                for label, key in self._key_order(model):
                    try:
                        if video is not None:
                            if label not in files:
                                files[label] = (key, self.upload(key, video))
                            fobj = files[label][1]
                            vparts = [{"file_data": {"mime_type": fobj.get("mimeType", "video/mp4"),
                                                     "file_uri": fobj["uri"]}}] + parts
                        elif img_parts:
                            vparts = list(parts) + img_parts
                        else:
                            vparts = parts
                    except Exception as e:
                        errors.append(f"{label}: {e}")
                        self.calls.append((label, "-", False, f"upload: {str(e)[:80]}"))
                        low = str(e).lower()
                        if "429" in low or "quota" in low or "api key not valid" in low:
                            self._mark(label, str(e))     # key-level: uploads failing means the key is out
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
                    next_model = False
                    for attempt in range(max(1, tries_per_combo)):
                        try:
                            st, _, raw = self._req(key, "POST", f"{API}/v1beta/models/{model}:generateContent",
                                                   json.dumps(body).encode(), {"Content-Type": "application/json"},
                                                   timeout=int(os.environ.get("VICE_GEMINI_TIMEOUT_S", "75")))
                        except Exception as ex:   # timeout / network: next key
                            self.log(f"gemini {label}/{model}: {type(ex).__name__} {str(ex)[:80]}")
                            # a model that hangs twice (any key) is dropped for this run: stop eating the budget
                            if "timed out" in str(ex).lower() or isinstance(ex, TimeoutError):
                                to = getattr(self, "_timeouts", {})
                                to[model] = to.get(model, 0) + 1
                                self._timeouts = to
                                if to[model] >= 2:
                                    self.bad_models.add(model)
                                    self.log(f"gemini: {model} timed out twice -> skipped for this run")
                                    next_model = True
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
                            if model in FALLBACK_MODELS and model not in (models or []):
                                self.log(f"gemini: answered by backup model {model} ({label})")
                            if schema:
                                return parse_json(txt)
                            return txt
                        msg = self._msg(raw)
                        self.log(f"gemini {label}/{model}: HTTP {st} {msg[:90]}")
                        self.calls.append((label, model, False, f"{st} {msg[:80]}"))
                        errors.append(f"{label}/{model}: {st} {msg[:100]}")
                        low = msg.lower()
                        if "api key not valid" in low or "api_key_invalid" in low or st == 401:
                            self._mark(label, msg)           # invalid key: skipped (all models), others go on
                            break
                        if st == 404 or "no longer available" in low or "not found" in low:
                            self.bad_models.add(model)
                            next_model = True
                            break
                        if st == 429 or "quota" in low or "exhausted" in low:
                            self._mark(label, msg, model=model)   # only this key+model cools down
                            break
                        if st == 403:
                            self._mark(label, msg)
                            break
                        if st == 400:                        # request shape this model can't take: next model
                            next_model = True
                            break
                        if st in (500, 502, 503, 504) and attempt + 1 < tries_per_combo:
                            time.sleep(2 + attempt * 3)
                            continue
                        break
                    if next_model:
                        break
        finally:
            for label, (key, fobj) in files.items():
                self.delete_file(key, fobj["name"])
        raise GeminiError("all Gemini keys/models failed: " + " | ".join(errors[-6:]))

    def _mark(self, label: str, msg: str, model: str | None = None):
        """Park a key (model=None) or one key+model pair. Invalid keys are skipped for a day, daily quota
        until the Pacific-midnight reset, per-minute limits for cooldown_s."""
        low = (msg or "").lower()
        per_min = ("per minute" in low) or ("per 60" in low)
        invalid = ("api key not valid" in low) or ("api_key_invalid" in low)
        if invalid:
            self.invalid[label] = time.time() + INVALID_RECHECK_S
            self.log(f"gemini: {label} is not a valid API key -> skipped (other keys keep going)")
            self._save_quota()
            return
        if model:
            label = f"{label}|{model}"
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


def check_models(log=print) -> dict:
    """Cheap health check: which configured model names exist (list-models) and whether each tier answers a
    one-word prompt right now. Prints model names, key labels and HTTP codes only, never keys."""
    g = Gemini(log=log)
    avail = g.list_models() or set()
    out = {"listed": bool(avail), "invalid_keys": [], "tiers": {},
           "available_flash_or_gemma": sorted(m for m in avail if "flash" in m or "gemma" in m)}
    for tier, ms in (("main", LITE_MODELS), ("full", FULL_MODELS), ("backup", FALLBACK_MODELS)):
        exist = [m for m in ms if not avail or m in avail]
        res = {"exists": exist, "missing": [m for m in ms if avail and m not in avail], "works": None}
        for m in exist:
            try:
                g.generate([{"text": "Reply with the single word OK."}], models=[m], temperature=0)
                res["works"] = m
                break
            except GeminiError as e:
                res.setdefault("errors", {})[m] = str(e)[-160:]
        out["tiers"][tier] = res
    out["invalid_keys"] = g.status()["invalid_keys"]
    out["status"] = g.status()
    return out


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



if __name__ == "__main__":   # python -m pipeline_lib.gemini check  (no heavy imports; names + codes only)
    import sys
    if sys.argv[1:2] == ["check"]:
        print(json.dumps(check_models(), indent=1))
