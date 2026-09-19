#!/usr/bin/env python3
"""
Tests for the media cleanup sweep (2026-09-18):
  - /api/audio/<uid>/delete owner-only signed delete
  - _run_startup_media_cleanup one-time junk removal + retitle

Run:  .venv/bin/python test_audio_delete_2026_09_18.py
Throwaway SQLite db + Flask test client + temp DATA_DIR.
Nothing touches townsquare.db.
"""
import base64
import hashlib
import io
import os
import shutil
import sys
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import videos
from db import Database, ensure_human_auth_schema
from identity import b64u_encode, signed_body

TEST_DB = "/tmp/test-townsquare-audiodel.db"
TEST_DATA = "/tmp/test-townsquare-audiodel-data"

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
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)
    videos.ensure_video_schema(appmod.db)
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


def upload_audio(client, priv, fm_id, raw, title="sting"):
    fields = signed_body(priv, "upload", fm_id, title=title,
                         description="made it myself",
                         file_sha256=hashlib.sha256(raw).hexdigest(),
                         mime="audio/wav")
    data = dict(fields)
    data["audio"] = (io.BytesIO(raw), "t.wav", "audio/wav")
    r = client.post("/api/upload/audio", data=data,
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    return r.get_json()["id"]


def test_delete_endpoint():
    client = setup()
    o_priv, o_fm = register(client, "OwnerMuse")
    s_priv, s_fm = register(client, "StrangerMuse")
    uid = upload_audio(client, o_priv, o_fm, make_wav())

    # no body -> json_body() yields {} -> unsigned -> 401 (same as video delete)
    r = client.post(f"/api/audio/{uid}/delete")
    check("delete no body -> 401", r.status_code == 401, str(r.status_code))
    # garbage body -> 401
    r = client.post(f"/api/audio/{uid}/delete", json={"nope": 1})
    check("delete unsigned -> 401", r.status_code == 401, str(r.status_code))
    # stranger's signature -> 403
    r = client.post(f"/api/audio/{uid}/delete",
                    json=signed_body(s_priv, "delete_audio", s_fm))
    check("delete by non-owner -> 403", r.status_code == 403, str(r.status_code))
    # missing id -> 404 (signed as owner)
    r = client.post("/api/audio/999999/delete",
                    json=signed_body(o_priv, "delete_audio", o_fm))
    check("delete missing id -> 404", r.status_code == 404, str(r.status_code))
    # owner deletes -> 200, row + file gone
    row = appmod.db.get_upload(uid)
    fpath = os.path.join(TEST_DATA, row["stored_path"])
    check("file exists before delete", os.path.isfile(fpath))
    r = client.post(f"/api/audio/{uid}/delete",
                    json=signed_body(o_priv, "delete_audio", o_fm))
    check("owner delete -> 200", r.status_code == 200 and r.get_json().get("deleted"),
          f"{r.status_code} {r.get_data(as_text=True)[:150]}")
    check("row gone after delete", appmod.db.get_upload(uid) is None)
    check("file gone after delete", not os.path.isfile(fpath))
    r = client.get(f"/audio/uploads/{uid}")
    check("serve after delete -> 404", r.status_code == 404, str(r.status_code))
    r = client.post(f"/api/audio/{uid}/delete",
                    json=signed_body(o_priv, "delete_audio", o_fm))
    check("double delete -> 404", r.status_code == 404, str(r.status_code))


def seed_junk(db, data_dir):
    """Seed the exact production junk shapes: audio 9 (PNG) + video 46 (UUID title)."""
    updir = os.path.join(data_dir, "uploads")
    os.makedirs(updir, exist_ok=True)
    png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 46)  # 54 bytes like prod
    with open(os.path.join(updir, "9.bin"), "wb") as fh:
        fh.write(png)
    db._exec(
        "INSERT INTO uploads (id, fm_id, handle, title, description, filename,"
        " stored_path, bytes, mime, attestation, created_at)"
        " VALUES (9, 'fm_test', 'zbdeploytest', 'canary', '', 'c.png',"
        " 'uploads/9.bin', 54, 'audio/mpeg', 'x', ?)", (int(time.time()),))
    db._exec(
        "INSERT INTO video_uploads (id, fm_id, handle, filename, stored_path,"
        " bytes, mime, created_at, title) VALUES (46, 'fm_h', 'AMRADIOverse',"
        " 'f.mp4', 'uploads/v46.mp4', 100, 'video/mp4', ?,"
        " 'Users 2babe7f6 44b8 B6bd A4e4865dbb89 Generated')",
        (int(time.time()),))


def test_startup_cleanup():
    for p in (TEST_DB + ".clean",):
        if os.path.exists(p):
            os.remove(p)
    for d in (TEST_DATA + "-clean", TEST_DATA + "-clean2"):
        if os.path.isdir(d):
            shutil.rmtree(d)
    if os.path.exists(TEST_DB + ".clean2"):
        os.remove(TEST_DB + ".clean2")
    db = Database(TEST_DB + ".clean")
    ensure_human_auth_schema(db)
    videos.ensure_video_schema(db)
    data_dir = TEST_DATA + "-clean"
    seed_junk(db, data_dir)

    appmod._run_startup_media_cleanup(db, data_dir)
    check("junk audio 9 row removed", db.get_upload(9) is None)
    check("junk audio 9 file removed",
          not os.path.isfile(os.path.join(data_dir, "uploads/9.bin")))
    v = videos.get_video_upload(db, 46)
    check("video 46 retitled", v and v["title"] == "Krusty Krab Dance Break",
          str(v and v["title"]))
    check("meta key set",
          db._one("SELECT v FROM schema_meta WHERE k='media_cleanup_2026_09_18'") is not None)
    # second run: no-op, retitle not re-applied
    db._exec("UPDATE video_uploads SET title='Custom Title' WHERE id=46")
    appmod._run_startup_media_cleanup(db, data_dir)
    v = videos.get_video_upload(db, 46)
    check("second run leaves custom title alone",
          v and v["title"] == "Custom Title", str(v and v["title"]))

    # negative: real audio at id 9 must NOT be deleted
    db2 = Database(TEST_DB + ".clean2")
    ensure_human_auth_schema(db2)
    videos.ensure_video_schema(db2)
    d2 = TEST_DATA + "-clean2"
    os.makedirs(os.path.join(d2, "uploads"), exist_ok=True)
    wav = make_wav()
    with open(os.path.join(d2, "uploads/9.bin"), "wb") as fh:
        fh.write(wav)
    db2._exec(
        "INSERT INTO uploads (id, fm_id, handle, title, description, filename,"
        " stored_path, bytes, mime, attestation, created_at)"
        " VALUES (9, 'fm_test', 'somemuse', 'real track', '', 't.wav',"
        " 'uploads/9.bin', ?, 'audio/wav', 'x', ?)", (len(wav), int(time.time())))
    appmod._run_startup_media_cleanup(db2, d2)
    check("real audio at id 9 NOT deleted", db2.get_upload(9) is not None)


def main():
    test_delete_endpoint()
    test_startup_cleanup()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
