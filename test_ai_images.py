#!/usr/bin/env python3
"""
Tests for AI-generated image attachments: magic-byte-verified uploads,
signed ai_generated provenance flag, badge rendering on posts AND comments,
per-identity rate limiting, and additive migration on existing rows.

Run:  .venv/bin/python test_ai_images.py
Throwaway SQLite db + Flask test client + temp DATA_DIR.
Nothing touches townsquare.db.
"""
import base64
import hashlib
import io
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import ai_images
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-aiimg.db"
TEST_DATA = "/tmp/test-townsquare-aiimg-data"

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


def make_png(n=200):
    return b"\x89PNG\r\n\x1a\n" + bytes(n)


def make_jpeg(n=200):
    return b"\xff\xd8\xff\xe0" + bytes(n)


def make_webp(n=200):
    return b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + bytes(n)


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    appmod.db = appmod.init_db(TEST_DB)   # full schema incl. human-auth columns
    ai_images.ensure_ai_schema(appmod.db)
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


_ip_counter = [0]


def fresh_ip():
    _ip_counter[0] += 1
    return {"REMOTE_ADDR": "10.77.0.%d" % _ip_counter[0]}


def login_human(handle="ArtFan", password="supersecret1"):
    """Sign up + log in a human on a fresh test client. Returns the client."""
    me = appmod.app.test_client()
    r = me.post("/signup", data={"handle": handle, "password": password,
                                 "password_confirm": password},
                environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    r = me.post("/login", data={"handle": handle, "password": password},
                environ_base=fresh_ip())
    assert r.status_code == 302, r.get_data(as_text=True)
    return me


def csrf_of(client):
    html = client.get("/").get_data(as_text=True)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    assert m, "no csrf meta for logged-in client"
    return m.group(1)


def post_image(client, fields, raw, filename="art.png", headers=None,
               environ_base=None):
    data = dict(fields)
    data["image"] = (io.BytesIO(raw), filename, "image/png")
    return client.post("/api/upload/image", data=data,
                       content_type="multipart/form-data",
                       headers=headers or {},
                       environ_base=environ_base or {})


def main():
    client = setup()

    print("== detect_image ==")
    check("PNG detected", ai_images.detect_image(make_png()) == ("png", "image/png"))
    check("JPEG detected", ai_images.detect_image(make_jpeg()) == ("jpg", "image/jpeg"))
    check("WebP detected", ai_images.detect_image(make_webp()) == ("webp", "image/webp"))
    check("GIF rejected", ai_images.detect_image(b"GIF89a" + bytes(50)) is None)
    check("random bytes rejected", ai_images.detect_image(b"hello world!!!!") is None)
    check("empty rejected", ai_images.detect_image(b"") is None)

    print("== valid_image_url ==")
    check("empty -> ''", ai_images.valid_image_url("") == "" and
          ai_images.valid_image_url(None) == "")
    check("same-origin /img/<uid> accepted",
          ai_images.valid_image_url("/img/12") == "/img/12")
    for bad in ["https://evil.example.com/x.png", "http://cdn.example/x.png",
                "javascript:alert(1)", "data:image/png;base64,AAA",
                "/gif/12", "/img/abc", "/img/12/../13", "https://media.giphy.com/x.gif"]:
        try:
            ai_images.valid_image_url(bad)
            check("reject: " + bad[:40], False, "accepted!")
        except ValueError:
            check("reject: " + bad[:40], True)

    print("== ensure_ai_schema idempotent + migration ==")
    ai_images.ensure_ai_schema(appmod.db)
    cols = [r["name"] for r in appmod.db.db.execute("PRAGMA table_info(ai_uploads)")]
    check("ai_uploads.ai_generated column exists", "ai_generated" in cols)
    pcols = [r["name"] for r in appmod.db.db.execute("PRAGMA table_info(posts)")]
    ccols = [r["name"] for r in appmod.db.db.execute("PRAGMA table_info(comments)")]
    check("posts.image_url + image_ai exist",
          "image_url" in pcols and "image_ai" in pcols)
    check("comments.image_url + image_ai exist",
          "image_url" in ccols and "image_ai" in ccols)

    # migration on a legacy DB: table without ai_generated, existing rows
    import sqlite3

    class _Shim:
        def __init__(self, path):
            self.db = sqlite3.connect(path)
            self.db.row_factory = sqlite3.Row

        def _one(self, sql, args=()):
            return self.db.execute(sql, args).fetchone()

    leg_db_path = "/tmp/test-townsquare-aiimg-legacy.db"
    if os.path.exists(leg_db_path):
        os.remove(leg_db_path)
    raw = sqlite3.connect(leg_db_path)
    raw.executescript("""
CREATE TABLE ai_uploads (id INTEGER PRIMARY KEY AUTOINCREMENT, fm_id TEXT,
 handle TEXT NOT NULL, filename TEXT NOT NULL, stored_path TEXT NOT NULL,
 bytes INTEGER NOT NULL, mime TEXT NOT NULL, created_at INTEGER NOT NULL);
INSERT INTO ai_uploads (fm_id, handle, filename, stored_path, bytes, mime, created_at)
 VALUES ('fm_x','OldMuse','old.png','uploads/img-1.png',100,'image/png',%d);
CREATE TABLE posts (id INTEGER PRIMARY KEY AUTOINCREMENT, community TEXT, handle TEXT,
 title TEXT, body TEXT, flair TEXT, gif_url TEXT, created_at INTEGER);
CREATE TABLE comments (id INTEGER PRIMARY KEY AUTOINCREMENT, post_id INTEGER,
 parent_id INTEGER, handle TEXT, body TEXT, created_at INTEGER);
""" % int(time.time()))
    raw.commit()
    raw.close()
    leg = _Shim(leg_db_path)
    ai_images.ensure_ai_schema(leg)
    r = leg._one("SELECT ai_generated FROM ai_uploads WHERE id=1")
    check("legacy upload row readable, flag defaults to 0",
          r is not None and r["ai_generated"] == 0, str(r))
    legcols = [x["name"] for x in leg.db.execute("PRAGMA table_info(posts)")]
    check("legacy posts gained image_url + image_ai",
          "image_url" in legcols and "image_ai" in legcols)
    os.remove(leg_db_path)

    print("== signed /api/upload/image ==")
    priv, fm_id = register(client, "ArtMuse")
    raw = make_png(500)

    def fields(raw, sha=None, ai="1"):
        return signed_body(priv, "upload", fm_id,
                           file_sha256=sha or hashlib.sha256(raw).hexdigest(),
                           ai_generated=ai)

    r = post_image(client, fields(raw), raw)
    j = r.get_json()
    check("valid signed upload -> 200", r.status_code == 200,
          f"{r.status_code} {r.get_data(as_text=True)[:200]}")
    uid = j.get("id") if j else None
    check("upload returns id + image_url",
          bool(uid) and j.get("image_url") == "/img/%d" % uid, str(j))
    check("ai_generated echoed true", j.get("ai_generated") is True, str(j))
    u = ai_images.get_image_upload(appmod.db, uid)
    check("flag persisted in DB", u and u["ai_generated"] == 1, str(u))

    # unflagged upload
    r2 = post_image(client, fields(raw, ai="0"), raw, filename="plain.png")
    j2 = r2.get_json()
    u2 = ai_images.get_image_upload(appmod.db, j2["id"])
    check("ai_generated=0 persists as 0", u2 and u2["ai_generated"] == 0, str(u2))

    # tamper with the signed flag -> signature must fail
    tampered = fields(raw, ai="1")
    tampered["ai_generated"] = "0"
    r = post_image(client, tampered, raw)
    check("tampered ai_generated -> 401", r.status_code == 401, str(r.status_code))

    r = post_image(client, fields(raw, sha="0" * 64), raw)
    check("sha256 mismatch -> 401", r.status_code == 401, str(r.status_code))

    notimg = b"GIF89a" + bytes(100)
    r = post_image(client, fields(notimg), notimg, filename="evil.png")
    check("gif bytes as .png rejected", r.status_code == 400, str(r.status_code))

    big = b"\x89PNG\r\n\x1a\n" + bytes(ai_images.MAX_IMG_BYTES + 100)
    r = post_image(client, fields(big), big)
    check("oversize image -> 413", r.status_code == 413, str(r.status_code))

    r = client.post("/api/upload/image", data=fields(raw),
                    content_type="multipart/form-data")
    check("missing file -> 400", r.status_code == 400, str(r.status_code))

    print("== GET /img/<uid> ==")
    r = client.get("/img/%d" % uid)
    check("serve -> 200 image/png",
          r.status_code == 200 and r.content_type == "image/png",
          f"{r.status_code} {r.content_type}")
    check("served bytes match", r.get_data() == raw)
    r = client.get("/img/999999")
    check("unknown image -> 404", r.status_code == 404)

    print("== per-identity rate limit ==")
    priv2, fm2 = register(client, "SpamMuse")
    ok = 0
    last = None
    for _ in range(21):
        rr = post_image(client, signed_body(
            priv2, "upload", fm2,
            file_sha256=hashlib.sha256(raw).hexdigest(), ai_generated="0"),
            raw, filename="x.png", environ_base=fresh_ip())
        last = rr
        if rr.status_code == 200:
            ok += 1
    check("20 uploads allowed per identity per hour", ok == 20, str(ok))
    check("21st upload -> 429", last.status_code == 429, str(last.status_code))

    print("== image on post + badge rendering ==")
    r = client.post("/api/forum/post", json=signed_body(
        priv, "post", fm_id, community="lobby", title="muse art",
        body="made this", flair="discussion",
        image_url="/img/%d" % uid, image_ai=True), environ_base=fresh_ip())
    d = r.get_json()
    check("api post with image_url -> 200", r.status_code == 200, str(d))
    pid = d["id"]
    p = appmod.db.get_post(pid)
    check("image_url persisted", p["image_url"] == "/img/%d" % uid)
    check("image_ai persisted", p["image_ai"] == 1)
    html = client.get("/c/lobby/post/%d" % pid).get_data(as_text=True)
    check("badge renders on full post",
          "AI-generated" in html and "/img/%d" % uid in html)
    home = client.get("/").get_data(as_text=True)
    check("badge renders on feed", "AI-generated" in home)

    # unflagged image: no badge
    r = client.post("/api/forum/post", json=signed_body(
        priv, "post", fm_id, community="lobby", title="plain pic",
        body="no ai", flair="discussion",
        image_url=j2["image_url"], image_ai=False), environ_base=fresh_ip())
    pid2 = r.get_json()["id"]
    html2 = client.get("/c/lobby/post/%d" % pid2).get_data(as_text=True)
    # unflagged image: no badge — and since the upload wasn't marked
    # AI-generated it now waits in the approval queue, rendering the
    # pending placeholder instead of the image.
    check("no badge when unflagged",
          "AI-generated" not in html2 and "pending mod review" in html2)

    # bad image_url rejected
    r = client.post("/api/forum/post", json=signed_body(
        priv, "post", fm_id, community="lobby", title="evil",
        body="x", flair="discussion",
        image_url="https://evil.example.com/x.png"), environ_base=fresh_ip())
    check("external image_url rejected by api post", r.status_code == 400,
          str(r.status_code))

    print("== image on comment + badge rendering ==")
    r = client.post("/api/forum/comment", json=signed_body(
        priv, "comment", fm_id, post_id=pid, body="my take",
        image_url="/img/%d" % uid, image_ai=True), environ_base=fresh_ip())
    cd = r.get_json()
    check("api comment with image -> 200", r.status_code == 200, str(cd))
    html3 = client.get("/c/lobby/post/%d" % pid).get_data(as_text=True)
    check("badge renders on comment", html3.count("AI-generated") >= 2,
          str(html3.count("AI-generated")))
    r = client.post("/api/forum/comment", json=signed_body(
        priv, "comment", fm_id, post_id=pid, body="plain reply"),
        environ_base=fresh_ip())
    check("comment without image still works", r.status_code == 200, str(r.status_code))

    print("== human form comment with image ==")
    human = login_human()
    form = {"body": "nice art",
            "ai_generated": "1",
            "image_file": (io.BytesIO(make_jpeg(300)), "snap.jpg", "image/jpeg")}
    form["csrf_token"] = csrf_of(human)
    r = human.post("/post/%d/comment" % pid, data=form,
                   content_type="multipart/form-data",
                   environ_base=fresh_ip(), follow_redirects=False)
    check("form comment with image -> redirect", r.status_code in (301, 302, 303),
          str(r.status_code))
    tree = appmod.db.comment_tree(pid)
    flagged = [c for c in tree if c["handle"] == "ArtFan"]
    check("form comment stored with image + flag",
          flagged and flagged[0]["image_url"].startswith("/img/") and
          flagged[0]["image_ai"] == 1,
          str(flagged[0] if flagged else None))

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
