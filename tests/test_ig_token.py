import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from pipeline_lib import health, ig_token as T

ROOT = Path(__file__).resolve().parents[1]
FAKE_IG = "IGAAfaketoken1234567890abcdefXYZ"
NEW_IG = "IGAAnewtoken0987654321zyxwvuQRS"


def _keys():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return priv, pub


class IgToken(unittest.TestCase):
    def test_family(self):
        self.assertEqual(T.family("IGAAxx"), "instagram-login")
        self.assertEqual(T.family("EAAGxx"), "facebook-login")
        self.assertEqual(T.family(""), "missing")

    def test_handoff_roundtrip_and_box_decrypt_script(self):
        priv, pub = _keys()
        blob = T.encrypt_handoff(NEW_IG, pub)
        self.assertNotIn(NEW_IG.encode(), blob)
        self.assertEqual(T.decrypt_handoff(blob, priv), NEW_IG)
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "h.bin").write_bytes(blob)
            (Path(td) / "k.pem").write_bytes(priv)
            r = subprocess.run([sys.executable, str(ROOT / "box" / "ig_handoff_decrypt.py"),
                                str(Path(td) / "h.bin"), str(Path(td) / "k.pem")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, NEW_IG)

    def test_refresh_never_prints_token(self):
        priv, pub = _keys()

        def fake_get(url, timeout=30):
            if "refresh_access_token" in url:
                return {"access_token": NEW_IG, "expires_in": 5184000}
            return {"user_id": "1", "username": "benreelr"}
        with tempfile.TemporaryDirectory() as td, patch.object(T, "_get", side_effect=fake_get), \
                patch.object(T, "PUBKEY", Path(td) / "pub.pem"), \
                patch.dict(os.environ, {"IG_ACCESS_TOKEN": FAKE_IG}, clear=False):
            (Path(td) / "pub.pem").write_bytes(pub)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = T.main(["refresh", "--out", str(Path(td) / "h.bin")])
            out = buf.getvalue()
            self.assertEqual(rc, 0)
            self.assertNotIn(FAKE_IG, out)
            self.assertNotIn(NEW_IG, out)
            self.assertIn("refreshed", json.loads(out)["result"])
            self.assertEqual(T.decrypt_handoff((Path(td) / "h.bin").read_bytes(), priv), NEW_IG)

    def test_facebook_token_is_reported_not_refreshed(self):
        def fake_get(url, timeout=30):
            self.assertNotIn("refresh_access_token", url)
            return {"data": {"is_valid": True, "type": "PAGE", "expires_at": 0}}
        with tempfile.TemporaryDirectory() as td, patch.object(T, "_get", side_effect=fake_get), \
                patch.dict(os.environ, {"IG_ACCESS_TOKEN": "EAAfakefacebooktoken123"}, clear=False):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = T.main(["refresh", "--out", str(Path(td) / "h.bin")])
            res = json.loads(buf.getvalue())
            self.assertEqual(rc, 0)                         # never-expiring page token: nothing to do
            self.assertIn("app id + app secret", res["result"])
            self.assertFalse((Path(td) / "h.bin").exists())

    def test_health_alerts_on_failed_refresh(self):
        with patch.dict(os.environ, {"IG_TOKEN_REFRESH": "failed: run 1 ended 'completed failure'"}):
            p = health.problems({"youtube": 3, "instagram": 3}, {"posts": []}, [], [], None, True)
        self.assertTrue(any("auto-refresh failed" in x for x in p))
        with patch.dict(os.environ, {"IG_TOKEN_REFRESH": "ok: refreshed"}):
            p = health.problems({"youtube": 3, "instagram": 3}, {"posts": []}, [], [], None, True)
        self.assertFalse(any("Instagram" in x for x in p))


if __name__ == "__main__":
    unittest.main()
