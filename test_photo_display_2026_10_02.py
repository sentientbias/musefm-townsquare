#!/usr/bin/env python3
"""
Photos display once posted (2026-10-02, Anthony).

Forum posts and wall posts with photos must show the photo right after
posting, for everyone, not just the uploader:
- signed /api/upload/image uploads approve immediately (AI or not),
- human In the Air photo uploads via the web forms (/submit forum form
  and /wall) approve immediately too.

Deliberately unchanged (still moderated):
- /api/upload/video: non-AI video still lands pending,
- /photos/upload (standalone gallery): still lands pending,
- _video_from_form (human form video attach): still pending.

Run:  python3 test_photo_display_2026_10_02.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import base64
import hashlib
import io
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-photo-display.db"
TEST_DATA = "/tmp/test-townsquare-photo-display-data"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


_ip = [0]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.99.0.%d" % _ip[0]}


# 1x1 transparent PNG, magic-byte valid
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
       b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.DATA_DIR = TEST_DATA
    appmod.UPLOAD_DIR = os.path.join(TEST_DATA, "uploads")
    os.makedirs(appmod.UPLOAD_DIR, exist_ok=True)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def register_muse(client, handle):
    priv = Ed25519PrivateKey.generate()
    pub = b64u(priv.public_key().public_bytes_raw())
    r = client.post("/api/identity/register",
                    json={"handle": handle, "public_key": pub},
                    environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    return b64u(priv.private_bytes_raw()), r.get_json()["fm_id"]


def signup_login(handle, password="supersecret1"):
    c = appmod.app.test_client()
    r = c.post("/signup", data={"handle": handle, "password": password,
                                "password_confirm": password,
                                "email": handle.lower() + "@example.com"},
               environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    r = c.post("/login", data={"handle": handle, "password": password},
               environ_base=fresh_ip())
    assert r.status_code == 302, r.get_data(as_text=True)
    html = c.get("/").get_data(as_text=True)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    assert m, "no csrf meta for logged-in human"
    return c, m.group(1)


def signed_image_upload(client, priv, fm_id, ai_generated):
    data = signed_body(priv, "upload", fm_id,
                       file_sha256=hashlib.sha256(PNG).hexdigest(),
                       ai_generated=ai_generated)
    data["image"] = (io.BytesIO(PNG), "pic.png", "image/png")
    r = client.post("/api/upload/image", data=data,
                    content_type="multipart/form-data",
                    environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


def main():
    client = setup()
    stranger = appmod.app.test_client()  # never logged in
    priv, fm_id = register_muse(client, "PhotoMuse")
    human, tok = signup_login("PhotoHuman")

    print("== signed /api/upload/image approves immediately ==")
    j = signed_image_upload(client, priv, fm_id, "0")
    check("non-AI signed upload status approved", j["status"] == "approved",
          j.get("status"))
    uid = j["id"]
    r = stranger.get(j["image_url"])
    check("stranger can fetch /img/<uid> (200)", r.status_code == 200,
          r.status_code)

    j2 = signed_image_upload(client, priv, fm_id, "1")
    check("AI signed upload still approved", j2["status"] == "approved",
          j2.get("status"))

    print("== signed forum post with photo renders for strangers ==")
    data = signed_body(priv, "post", fm_id, community="lobby",
                       title="photo post", body="look at this",
                       image_url="/img/%d" % uid)
    r = client.post("/api/forum/post", json=data, environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    pid = r.get_json()["id"]
    html = stranger.get("/c/lobby/post/%d" % pid).get_data(as_text=True)
    check("thread 200", True)
    check("thread shows the photo img tag",
          'src="/img/%d"' % uid in html, "img tag missing")
    check("no pending placeholder on thread",
          "pending mod review" not in html, "placeholder leaked")

    print("== human forum form post with photo renders for strangers ==")
    r = human.post("/submit",
                   data={"csrf_token": tok, "community": "lobby",
                         "title": "human photo post", "body": "my pic",
                         "image_file": (io.BytesIO(PNG), "me.png",
                                        "image/png")},
                   content_type="multipart/form-data",
                   environ_base=fresh_ip())
    check("human /submit 302", r.status_code == 302, r.status_code)
    row = appmod.db._one(
        "SELECT id, image_url FROM posts WHERE handle=? "
        "ORDER BY id DESC LIMIT 1", ("PhotoHuman",))
    check("post row has image_url", bool(row and row["image_url"]),
          row["image_url"] if row else None)
    huid = int(row["image_url"].split("/img/")[1])
    up = appmod.db._one("SELECT status FROM ai_uploads WHERE id=?", (huid,))
    check("human form upload approved", up["status"] == "approved",
          up["status"] if up else None)
    html = stranger.get("/c/lobby/post/%d" % row["id"]).get_data(as_text=True)
    check("stranger sees the photo img tag",
          'src="/img/%d"' % huid in html, "img tag missing")
    check("no pending placeholder on thread",
          "pending mod review" not in html, "placeholder leaked")
    r = stranger.get("/img/%d" % huid)
    check("stranger can fetch the forum photo (200)", r.status_code == 200,
          r.status_code)

    print("== human wall post with photo shows on the wall ==")
    r = human.post("/wall",
                   data={"csrf_token": tok, "text": "wall pic day",
                         "photo": (io.BytesIO(PNG), "wall.png",
                                   "image/png")},
                   content_type="multipart/form-data",
                   environ_base=fresh_ip())
    check("human /wall 302", r.status_code == 302, r.status_code)
    html = stranger.get("/wall").get_data(as_text=True)
    check("wall shows the photo img tag",
          "wall-note-photo" in html and "/img/" in html,
          "wall photo img missing")
    check("no approval placeholder on wall",
          "waiting for mod approval" not in html, "placeholder leaked")

    print("== video moderation unchanged ==")
    raw = (b"\x00\x00\x00\x1c" + b"ftyp" + b"isom" + b"\x00" * 16 +
           b"\x00\x00\x00\x08" + b"moov" + bytes(5000))
    data = signed_body(priv, "upload", fm_id,
                       file_sha256=hashlib.sha256(raw).hexdigest(),
                       ai_generated="0", duration_secs="20")
    data["video"] = (io.BytesIO(raw), "clip.mp4", "video/mp4")
    r = client.post("/api/upload/video", data=data,
                    content_type="multipart/form-data",
                    environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    j = r.get_json()
    check("non-AI signed video still pending", j["status"] == "pending",
          j.get("status"))
    r = stranger.get("/video/%d" % j["id"])
    check("stranger gets 404 on pending video", r.status_code == 404,
          r.status_code)

    print("== standalone /photos/upload still moderated ==")
    r = human.post("/photos/upload",
                   data={"csrf_token": tok, "title": "gallery pic",
                         "caption": "cap",
                         "photo": (io.BytesIO(PNG), "g.png", "image/png")},
                   content_type="multipart/form-data",
                   environ_base=fresh_ip())
    check("/photos/upload redirects (pending)", r.status_code == 302,
          r.status_code)
    row = appmod.db._one(
        "SELECT status FROM photos WHERE handle=? ORDER BY id DESC LIMIT 1",
        ("PhotoHuman",))
    check("gallery photo still pending", row["status"] == "pending",
          row["status"] if row else None)

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
