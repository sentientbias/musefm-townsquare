#!/usr/bin/env python3
"""
Proves a P1 (2026-09-18): the human web audio upload POST /upload validates
file type ONLY via the client-supplied multipart Content-Type (f.mimetype) --
no server-side magic-byte sniffing. A PNG file labeled audio/mpeg is accepted
(302), stored as uploads/<n>.mp3, and served back as audio/mpeg.

Expected (failing today): non-audio bytes are rejected with 400.
Also includes a positive control: a real WAV upload is accepted.

Run:  .venv/bin/python test_upload_audio_mimetype_2026_09_18.py
Throwaway SQLite db + Flask test client + temp DATA_DIR.
Nothing touches townsquare.db.
"""
import io
import os
import shutil
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod

TEST_DB = "/tmp/test-townsquare-upload-audio-mimetype.db"
TEST_DATA = "/tmp/test-townsquare-upload-audio-mimetype-data"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


# 1x1 PNG, 67 bytes of very-not-audio
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
    "de0000000c4944415478016360000000020001030003" ) + bytes.fromhex("a0") * 0


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


def make_wav(seconds=1, rate=8000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * rate * seconds)
    return buf.getvalue()


def main():
    c = setup()
    r = c.post("/signup", data={"handle": "mimetesthuman",
                                "password": "s3cretpw!!",
                                "password_confirm": "s3cretpw!!"})
    assert r.status_code == 200, r.status_code
    r = c.post("/login", data={"handle": "mimetesthuman",
                               "password": "s3cretpw!!"})
    assert r.status_code in (200, 302), r.status_code

    # BUG PROOF: PNG bytes masquerading as audio/mpeg
    r = c.post("/upload",
               data={"audio": (io.BytesIO(PNG_BYTES), "notaudio.png",
                               "audio/mpeg"),
                     "title": "totally-an-audio"},
               content_type="multipart/form-data",
               follow_redirects=False)
    body = r.get_data(as_text=True)[:200]
    check("PNG bytes labeled audio/mpeg are REJECTED (400)",
          r.status_code == 400,
          f"got {r.status_code} (302 = bug: non-audio stored+served as audio/mpeg) {body!r}")

    # Positive control: a real WAV must still be accepted
    r = c.post("/upload",
               data={"audio": (io.BytesIO(make_wav()), "real.wav",
                               "audio/wav"),
                     "title": "real-audio"},
               content_type="multipart/form-data",
               follow_redirects=False)
    check("real WAV upload still accepted", r.status_code in (200, 302),
          f"got {r.status_code}")

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
