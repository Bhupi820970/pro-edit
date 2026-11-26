#!/usr/bin/env python3
"""Box-only: decrypt the IG token handoff (ig-token.yml artifact) with the box private key and write the
token to STDOUT for `gh secret set` (stdin). Plain data handling: the artifact is parsed as JSON and
decrypted, never executed. Nothing is logged."""
import base64
import json
import sys

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

blob, keyfile = sys.argv[1], sys.argv[2]
with open(blob, "rb") as f:
    d = json.loads(f.read(4096))
with open(keyfile, "rb") as f:
    priv = serialization.load_pem_private_key(f.read(), password=None)
key = priv.decrypt(base64.b64decode(d["k"]),
                   padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
tok = AESGCM(key).decrypt(base64.b64decode(d["n"]), base64.b64decode(d["c"]), b"ig-handoff-v1").decode()
if not (20 <= len(tok) <= 1000) or any(c.isspace() for c in tok):
    sys.exit(2)
sys.stdout.write(tok)
