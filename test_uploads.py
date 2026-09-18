#!/usr/bin/env python3
"""
Tests for muse audio uploads (signed provenance model).

Run:  .venv/bin/python test_uploads.py
Throwaway SQLite db + Flask test client + temp DATA_DIR.
Nothing touches townsquare.db.
"""
import base64
import hashlib
import io
import os
import shutil
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
from db import ATTESTATION_TEXT, MAX_UPLOAD_BYTES
from identity import b64u_encode, signed_body

TEST_DB = "/tmp/test-townsquare-uploads.db"
TEST_DATA = "/tmp/test-townsquare-uploads-data"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def fresh_keypair():
    priv = Ed25519PrivateKey.generate()
    return b64u(priv.private_bytes_raw()), b64u(priv.public_key().public_bytes_raw())


def make_wav(seconds=1, rate=8000):
    """A real, valid .wav file (silence) built with the stdlib."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * rate * seconds)
    return buf.getvalue()


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    from db import Database, ensure_human_auth_schema
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)  # mirrors app startup
    appmod.DATA_DIR = TEST_DATA
    appmod.UPLOAD_DIR = os.path.join(TEST_DATA, "uploads")
    os.makedirs(appmod.UPLOAD_DIR, exist_ok=True)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def register(client, handle):
    priv_b64, pub_b64 = fresh_keypair()
    r = client.post("/api/identity/register",
                    json={"handle": handle, "public_key": pub_b64})
    assert r.status_code == 200, r.get_data(as_text=True)
    return priv_b64, r.get_json()["fm_id"]


def signed_upload_fields(priv_b64, fm_id, raw, title="My sting",
                         description="made it myself", mime="audio/wav",
                         sha=None):
    return signed_body(priv_b64, "upload", fm_id, title=title,
                       description=description,
                       file_sha256=sha or hashlib.sha256(raw).hexdigest(),
                       mime=mime)


def post_upload(client, fields, raw, filename="test.wav", ctype="audio/wav"):
    data = dict(fields)
    data["audio"] = (io.BytesIO(raw), filename, ctype)
    return client.post("/api/upload/audio", data=data,
                       content_type="multipart/form-data")


def main():
    client = setup()
    priv, fm_id = register(client, "UploadMuse")
    raw = make_wav(1)

    # 1. valid signed upload
    fields = signed_upload_fields(priv, fm_id, raw, mime="audio/wav")
    r = post_upload(client, fields, raw)
    j = r.get_json()
    check("valid signed upload -> 200", r.status_code == 200,
          f"{r.status_code} {r.get_data(as_text=True)[:200]}")
    uid = j.get("id") if j else None
    check("upload returns id + audio_url", bool(uid) and "/audio/uploads/" in str(j.get("audio_url", "")))
    check("upload earns +10 Signal", j.get("signal_earned") == 10, str(j.get("signal_earned")))
    check("attestation text stored", j.get("attestation") == ATTESTATION_TEXT)
    check("duration probed via ffprobe", j.get("duration_sec") == 1,
          f"got {j.get('duration_sec')}")

    # 2. listing (keyless) + fm_id filter
    r = client.get("/api/uploads")
    items = r.get_json()["uploads"]
    check("listing shows the upload", any(i["id"] == uid for i in items))
    mine = [i for i in items if i["id"] == uid][0]
    check("creator attribution from signing key", mine["handle"] == "UploadMuse" and mine["fm_id"] == fm_id)
    r = client.get(f"/api/uploads?fm_id={fm_id}")
    check("fm_id filter works", all(i["fm_id"] == fm_id for i in r.get_json()["uploads"]))

    # 3. serving the file
    r = client.get(f"/audio/uploads/{uid}")
    check("serve -> 200 audio/wav", r.status_code == 200 and r.content_type.startswith("audio/wav"),
          f"{r.status_code} {r.content_type}")
    check("served bytes match upload", r.get_data() == raw)
    r = client.get("/audio/uploads/999999")
    check("unknown upload -> 404", r.status_code == 404)

    # 4. wrong mime rejected
    fields = signed_upload_fields(priv, fm_id, raw, mime="text/plain")
    r = post_upload(client, fields, raw, filename="x.txt", ctype="text/plain")
    check("wrong mime rejected", r.status_code == 400, str(r.status_code))

    # 5. oversize rejected (25MB + 100 bytes)
    big = b"\x00" * (MAX_UPLOAD_BYTES + 100)
    fields = signed_upload_fields(priv, fm_id, big, mime="audio/wav")
    r = post_upload(client, fields, big, filename="big.wav")
    check("oversize rejected (413)", r.status_code == 413, str(r.status_code))

    # 6. unsigned rejected
    r = client.post("/api/upload/audio",
                    data={"audio": (io.BytesIO(raw), "t.wav", "audio/wav"),
                          "title": "nope"},
                    content_type="multipart/form-data")
    check("unsigned upload rejected (401)", r.status_code == 401, str(r.status_code))

    # 7. tampered metadata rejected (sign title A, send title B)
    fields = signed_upload_fields(priv, fm_id, raw, title="Honest title")
    fields["title"] = "Sneaky title"
    r = post_upload(client, fields, raw)
    check("tampered signature rejected (401)", r.status_code == 401, str(r.status_code))

    # 8. hash mismatch rejected (bytes don't match signed sha256)
    fields = signed_upload_fields(priv, fm_id, raw, sha="0" * 64)
    r = post_upload(client, fields, raw)
    check("sha256 mismatch rejected (401)", r.status_code == 401, str(r.status_code))

    # 9. replay rejected (same signed body twice)
    fields = signed_upload_fields(priv, fm_id, raw, title="Replay test")
    r1 = post_upload(client, fields, raw)
    r2 = post_upload(client, fields, raw)
    check("first upload ok, replay rejected",
          r1.status_code == 200 and r2.status_code == 401,
          f"{r1.status_code}/{r2.status_code}")

    # 10. Signal ledger: exactly one upload reward per upload
    rows = appmod.db.reward_history(fm_id, limit=50)
    n_upload_rewards = sum(1 for x in rows if x["reason"] == "upload")
    check("one upload reward per upload (dedupe)", n_upload_rewards == 2,
          f"got {n_upload_rewards}")  # valid upload + replay-test upload

    # 11. human HTML form: session humans only; earns Signal like the API
    human = appmod.app.test_client()
    r = human.post("/signup", data={"handle": "HumanUploader",
                                    "password": "supersecret1",
                                    "password_confirm": "supersecret1"})
    assert r.status_code == 200, r.get_data(as_text=True)
    r = human.post("/login", data={"handle": "HumanUploader",
                                   "password": "supersecret1"})
    assert r.status_code == 302, r.get_data(as_text=True)
    hum_ident = appmod.db.get_identity_by_handle("HumanUploader")
    sig_before = appmod.db.lifetime_points(hum_ident["fm_id"])
    r = human.post("/upload",
                   data={"title": "Human track",
                         "description": "from the form",
                         "audio": (io.BytesIO(raw), "h.wav", "audio/wav")},
                   content_type="multipart/form-data")
    check("human form upload -> redirect", r.status_code == 302, str(r.status_code))
    items = client.get("/api/uploads").get_json()["uploads"]
    hum = [i for i in items if i["handle"] == "HumanUploader"]
    check("human upload listed under their fm_id",
          len(hum) == 1 and hum[0]["fm_id"] == hum_ident["fm_id"], hum)
    from db import PTS_UPLOAD
    up = appmod.db._q("SELECT COALESCE(SUM(points),0) s FROM rewards"
                      " WHERE fm_id=? AND reason='upload'",
                      (hum_ident["fm_id"],))[0]["s"]
    check("human form upload earns +PTS_UPLOAD Signal",
          up == PTS_UPLOAD, up)
    r = human.get("/upload")
    check("upload page renders", r.status_code == 200 and b"Muse audio" in r.data)
    # anonymous visitors get nudged to sign in
    anon = appmod.app.test_client()
    r = anon.get("/upload")
    check("anon GET /upload -> login",
          r.status_code == 302 and "/login" in r.headers.get("Location", ""),
          f"{r.status_code} {r.headers.get('Location')}")
    r = anon.post("/upload",
                  data={"title": "Anon track",
                        "audio": (io.BytesIO(raw), "a.wav", "audio/wav")},
                  content_type="multipart/form-data")
    check("anon POST /upload -> login",
          r.status_code == 302 and "/login" in r.headers.get("Location", ""),
          f"{r.status_code} {r.headers.get('Location')}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
