"""Instagram token check + no-new-secret auto-refresh (runs in .github/workflows/ig-token.yml).

* check   : safe metadata only (token family, valid, days left) -> JSON on stdout + step summary.
* refresh : Instagram-Login tokens only (graph.instagram.com/refresh_access_token, no app secret needed).
            The new token is NEVER printed: it is encrypted to the box's public key
            (config/ig_handoff_pub.pem: RSA-OAEP-SHA256 wraps a random AES-256-GCM key) and written to a
            file that the workflow uploads as a 1-day artifact. Only the box holds the private key; its
            weekly cron (box/ig_token_refresh.sh) decrypts it and pipes it into `gh secret set` via stdin.
            A Facebook-Login token (graph.facebook.com) cannot be refreshed without the app id + secret:
            that is reported, never worked around.
The token is registered with publish.secrets so every log line is redacted."""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PUBKEY = REPO_ROOT / "config" / "ig_handoff_pub.pem"


def family(tok: str) -> str:
    t = (tok or "").strip()
    if t.startswith("IG"):
        return "instagram-login"
    if t.startswith("EAA"):
        return "facebook-login"
    return "unknown" if t else "missing"


def _get(url: str, timeout: int = 30) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def info(tok: str) -> dict:
    """Safe facts about the token (never the token itself)."""
    from .publish import secrets as S
    S.remember(tok)
    fam = family(tok)
    out = {"family": fam, "valid": None, "days_left": None, "refreshable_without_app_secret": fam == "instagram-login"}
    try:
        if fam == "facebook-login":
            d = (_get("https://graph.facebook.com/debug_token?" + urllib.parse.urlencode(
                {"input_token": tok, "access_token": tok})).get("data") or {})
            out["valid"] = d.get("is_valid")
            out["type"] = d.get("type")
            exp = int(d.get("expires_at") or 0)
            out["days_left"] = None if exp == 0 else round((exp - time.time()) / 86400, 1)
            out["never_expires"] = exp == 0
            dax = int(d.get("data_access_expires_at") or 0)
            out["data_access_days_left"] = None if dax == 0 else round((dax - time.time()) / 86400, 1)
        elif fam == "instagram-login":
            d = _get("https://graph.instagram.com/me?" + urllib.parse.urlencode(
                {"fields": "user_id,username", "access_token": tok}))
            out["valid"] = bool(d.get("user_id") or d.get("id"))
    except Exception as e:
        out["error"] = S.redact(str(e))[:160]
    return out


def refresh_instagram(tok: str) -> tuple[str, int]:
    from .publish import secrets as S
    d = _get("https://graph.instagram.com/refresh_access_token?" + urllib.parse.urlencode(
        {"grant_type": "ig_refresh_token", "access_token": tok}))
    new = (d.get("access_token") or "").strip()
    S.remember(new)
    return new, int(d.get("expires_in") or 0)


def encrypt_handoff(token: str, pub_pem: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    pub = serialization.load_pem_public_key(pub_pem)
    key, nonce = AESGCM.generate_key(256), os.urandom(12)
    wrapped = pub.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    ct = AESGCM(key).encrypt(nonce, token.encode(), b"ig-handoff-v1")
    return json.dumps({"v": 1, "k": base64.b64encode(wrapped).decode(), "n": base64.b64encode(nonce).decode(),
                       "c": base64.b64encode(ct).decode()}).encode()


def decrypt_handoff(blob: bytes, priv_pem: bytes) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    d = json.loads(blob)
    priv = serialization.load_pem_private_key(priv_pem, password=None)
    key = priv.decrypt(base64.b64decode(d["k"]),
                       padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    return AESGCM(key).decrypt(base64.b64decode(d["n"]), base64.b64decode(d["c"]), b"ig-handoff-v1").decode()


def _summary(res: dict) -> None:
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a") as f:
            f.write("### Instagram token\n\n```\n" + json.dumps(res, indent=1) + "\n```\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "refresh"])
    ap.add_argument("--out", type=Path, default=Path("ig_handoff.bin"))
    a = ap.parse_args(argv)
    tok = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    res = info(tok)
    res["result"] = "checked"
    rc = 0
    if a.cmd == "refresh":
        if res["family"] != "instagram-login":
            res["result"] = ("not refreshable without app id + app secret (Facebook-Login token)"
                             if res["family"] == "facebook-login" else "no usable token")
            rc = 0 if res.get("never_expires") else 3
        else:
            try:
                new, exp = refresh_instagram(tok)
                if not new:
                    raise RuntimeError("refresh returned no token")
                a.out.write_bytes(encrypt_handoff(new, PUBKEY.read_bytes()))
                res["result"] = "refreshed (encrypted handoff written)"
                res["new_days_left"] = round(exp / 86400, 1) if exp else None
            except Exception as e:
                from .publish import secrets as S
                res["result"] = "refresh failed: " + S.redact(str(e))[:160]
                rc = 2
    print(json.dumps(res))
    _summary(res)
    return rc


if __name__ == "__main__":
    sys.exit(main())
