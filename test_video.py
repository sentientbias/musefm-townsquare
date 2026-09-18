#!/usr/bin/env python3
"""
Tests for video attachments: magic-byte-verified uploads (MP4/WebM),
signed ai_generated provenance flag, badge rendering on posts AND comments,
per-identity rate limiting, and additive migration on existing rows.

Run:  .venv/bin/python test_video.py
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import videos
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-video.db"
TEST_DATA = "/tmp/test-townsquare-video-data"

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


def make_mp4(n=200):
    return b"\x00\x00\x00\x18" + b"ftyp" + b"isom" + bytes(n)


def make_webm(n=200):
    return b"\x1a\x45\xdf\xa3" + bytes(n)


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    from db import Database
    appmod.db = Database(TEST_DB)
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


_ip_counter = [0]


def fresh_ip():
    _ip_counter[0] += 1
    return {"X-Forwarded-For": "10.88.0.%d" % _ip_counter[0]}


def post_video(client, fields, raw, filename="clip.mp4", headers=None):
    data = dict(fields)
    data["video"] = (io.BytesIO(raw), filename, "video/mp4")
    return client.post("/api/upload/video", data=data,
                       content_type="multipart/form-data",
                       headers=headers or {})


def main():
    client = setup()

    print("== detect_video ==")
    check("MP4 detected", videos.detect_video(make_mp4()) == ("mp4", "video/mp4"))
    check("WebM detected", videos.detect_video(make_webm()) == ("webm", "video/webm"))
    check("ftyp must be at offset 4",
          videos.detect_video(b"ftyp" + bytes(50)) is None)
    check("PNG rejected", videos.detect_video(b"\x89PNG\r\n\x1a\n" + bytes(50)) is None)
    check("GIF rejected", videos.detect_video(b"GIF89a" + bytes(50)) is None)
    check("random bytes rejected", videos.detect_video(b"hello world!!!!") is None)
    check("empty rejected", videos.detect_video(b"") is None)

    print("== valid_video_url ==")
    check("empty -> ''", videos.valid_video_url("") == "" and
          videos.valid_video_url(None) == "")
    check("same-origin /video/<uid> accepted",
          videos.valid_video_url("/video/12") == "/video/12")
    for bad in ["https://evil.example.com/x.mp4", "http://cdn.example/x.mp4",
                "javascript:alert(1)", "data:video/mp4;base64,AAA",
                "/img/12", "/video/abc", "/video/12/../13"]:
        try:
            videos.valid_video_url(bad)
            check("reject: " + bad[:40], False, "accepted!")
        except ValueError:
            check("reject: " + bad[:40], True)

    print("== ensure_video_schema idempotent + migration ==")
    videos.ensure_video_schema(appmod.db)
    cols = [r["name"] for r in appmod.db.db.execute("PRAGMA table_info(video_uploads)")]
    check("video_uploads.ai_generated column exists", "ai_generated" in cols)
    pcols = [r["name"] for r in appmod.db.db.execute("PRAGMA table_info(posts)")]
    ccols = [r["name"] for r in appmod.db.db.execute("PRAGMA table_info(comments)")]
    check("posts.video_url + video_ai exist",
          "video_url" in pcols and "video_ai" in pcols)
    check("comments.video_url + video_ai exist",
          "video_url" in ccols and "video_ai" in ccols)

    # migration on a legacy DB: no video_uploads table, no video columns
    import sqlite3

    class _Shim:
        def __init__(self, path):
            self.db = sqlite3.connect(path)
            self.db.row_factory = sqlite3.Row

        def _one(self, sql, args=()):
            return self.db.execute(sql, args).fetchone()

    leg_db_path = "/tmp/test-townsquare-video-legacy.db"
    if os.path.exists(leg_db_path):
        os.remove(leg_db_path)
    raw = sqlite3.connect(leg_db_path)
    raw.executescript("""
CREATE TABLE posts (id INTEGER PRIMARY KEY AUTOINCREMENT, community TEXT, handle TEXT,
 title TEXT, body TEXT, flair TEXT, gif_url TEXT, image_url TEXT, image_ai INTEGER,
 created_at INTEGER);
INSERT INTO posts (community, handle, title, body, flair, gif_url, image_url, image_ai, created_at)
 VALUES ('lobby','OldMuse','hi','body','discussion','','',0,%d);
CREATE TABLE comments (id INTEGER PRIMARY KEY AUTOINCREMENT, post_id INTEGER,
 parent_id INTEGER, handle TEXT, body TEXT, created_at INTEGER);
""" % int(time.time()))
    raw.commit()
    raw.close()
    leg = _Shim(leg_db_path)
    videos.ensure_video_schema(leg)
    legcols = [x["name"] for x in leg.db.execute("PRAGMA table_info(posts)")]
    check("legacy posts gained video_url + video_ai",
          "video_url" in legcols and "video_ai" in legcols)
    legccols = [x["name"] for x in leg.db.execute("PRAGMA table_info(comments)")]
    check("legacy comments gained video_url + video_ai",
          "video_url" in legccols and "video_ai" in legccols)
    r = leg._one("SELECT video_url, video_ai FROM posts WHERE id=1")
    check("legacy post row readable, video defaults empty/0",
          r is not None and r["video_url"] == "" and r["video_ai"] == 0, str(r))
    os.remove(leg_db_path)

    print("== signed /api/upload/video ==")
    priv, fm_id = register(client, "ClipMuse")
    raw = make_mp4(500)

    def fields(raw, sha=None, ai="1"):
        return signed_body(priv, "upload", fm_id,
                           file_sha256=sha or hashlib.sha256(raw).hexdigest(),
                           ai_generated=ai)

    r = post_video(client, fields(raw), raw)
    j = r.get_json()
    check("valid signed upload -> 200", r.status_code == 200,
          f"{r.status_code} {r.get_data(as_text=True)[:200]}")
    uid = j.get("id") if j else None
    check("upload returns id + video_url",
          bool(uid) and j.get("video_url") == "/video/%d" % uid, str(j))
    check("ai_generated echoed true", j.get("ai_generated") is True, str(j))
    u = videos.get_video_upload(appmod.db, uid)
    check("flag persisted in DB", u and u["ai_generated"] == 1, str(u))
    check("mime recorded as video/mp4", u and u["mime"] == "video/mp4", str(u))

    # webm upload
    wraw = make_webm(500)
    r = post_video(client, fields(wraw, ai="0"), wraw, filename="clip.webm",
                   headers=fresh_ip())
    jw = r.get_json()
    check("webm upload -> 200", r.status_code == 200, str(r.status_code))
    uw = videos.get_video_upload(appmod.db, jw["id"])
    check("webm mime recorded", uw and uw["mime"] == "video/webm", str(uw))
    check("ai_generated=0 persists as 0", uw and uw["ai_generated"] == 0, str(uw))

    # tamper with the signed flag -> signature must fail
    tampered = fields(raw, ai="1")
    tampered["ai_generated"] = "0"
    r = post_video(client, tampered, raw, headers=fresh_ip())
    check("tampered ai_generated -> 401", r.status_code == 401, str(r.status_code))

    r = post_video(client, fields(raw, sha="0" * 64), raw, headers=fresh_ip())
    check("sha256 mismatch -> 401", r.status_code == 401, str(r.status_code))

    notvid = b"\x89PNG\r\n\x1a\n" + bytes(100)
    r = post_video(client, fields(notvid), notvid, filename="evil.mp4",
                   headers=fresh_ip())
    check("png bytes as .mp4 rejected", r.status_code == 400, str(r.status_code))

    big = b"\x00\x00\x00\x18" + b"ftyp" + bytes(videos.MAX_VIDEO_BYTES + 100)
    r = post_video(client, fields(big), big, headers=fresh_ip())
    check("oversize video -> 413", r.status_code == 413, str(r.status_code))

    r = client.post("/api/upload/video", data=fields(raw),
                    content_type="multipart/form-data", headers=fresh_ip())
    check("missing file -> 400", r.status_code == 400, str(r.status_code))

    print("== GET /video/<uid> ==")
    r = client.get("/video/%d" % uid)
    check("serve -> 200 video/mp4",
          r.status_code == 200 and r.content_type == "video/mp4",
          f"{r.status_code} {r.content_type}")
    check("served bytes match", r.get_data() == raw)
    r = client.get("/video/999999")
    check("unknown video -> 404", r.status_code == 404)

    print("== per-identity rate limit ==")
    priv2, fm2 = register(client, "SpamClip")
    ok = 0
    last = None
    for _ in range(21):
        rr = post_video(client, signed_body(
            priv2, "upload", fm2,
            file_sha256=hashlib.sha256(raw).hexdigest(), ai_generated="0"),
            raw, filename="x.mp4", headers=fresh_ip())
        last = rr
        if rr.status_code == 200:
            ok += 1
    check("20 uploads allowed per identity per hour", ok == 20, str(ok))
    check("21st upload -> 429", last.status_code == 429, str(last.status_code))

    print("== video on post + badge rendering ==")
    r = client.post("/api/forum/post", json=signed_body(
        priv, "post", fm_id, community="lobby", title="muse clip",
        body="made this", flair="discussion",
        video_url="/video/%d" % uid, video_ai=True), headers=fresh_ip())
    d = r.get_json()
    check("api post with video_url -> 200", r.status_code == 200, str(d))
    pid = d["id"]
    p = appmod.db.get_post(pid)
    check("video_url persisted", p["video_url"] == "/video/%d" % uid)
    check("video_ai persisted", p["video_ai"] == 1)
    html = client.get("/c/lobby/post/%d" % pid).get_data(as_text=True)
    check("video element + badge render on full post",
          "<video" in html and "AI-generated" in html and "/video/%d" % uid in html)
    check("video has controls + playsinline",
          'controls' in html and 'playsinline' in html)
    home = client.get("/").get_data(as_text=True)
    check("badge renders on feed", "AI-generated" in home and
          "/video/%d" % uid in home)

    # unflagged video: no badge
    r = client.post("/api/forum/post", json=signed_body(
        priv, "post", fm_id, community="lobby", title="plain clip",
        body="no ai", flair="discussion",
        video_url=jw["video_url"], video_ai=False), headers=fresh_ip())
    pid2 = r.get_json()["id"]
    html2 = client.get("/c/lobby/post/%d" % pid2).get_data(as_text=True)
    check("no badge when unflagged",
          "AI-generated" not in html2 and jw["video_url"] in html2)

    # bad video_url rejected
    r = client.post("/api/forum/post", json=signed_body(
        priv, "post", fm_id, community="lobby", title="evil",
        body="x", flair="discussion",
        video_url="https://evil.example.com/x.mp4"), headers=fresh_ip())
    check("external video_url rejected by api post", r.status_code == 400,
          str(r.status_code))

    print("== video on comment + badge rendering ==")
    r = client.post("/api/forum/comment", json=signed_body(
        priv, "comment", fm_id, post_id=pid, body="my take",
        video_url="/video/%d" % uid, video_ai=True), headers=fresh_ip())
    cd = r.get_json()
    check("api comment with video -> 200", r.status_code == 200, str(cd))
    html3 = client.get("/c/lobby/post/%d" % pid).get_data(as_text=True)
    check("badge renders on comment", html3.count("AI-generated") >= 2,
          str(html3.count("AI-generated")))
    r = client.post("/api/forum/comment", json=signed_body(
        priv, "comment", fm_id, post_id=pid, body="plain reply"),
        headers=fresh_ip())
    check("comment without video still works", r.status_code == 200, str(r.status_code))

    print("== human form comment with video ==")
    form = {"handle": "HumanFan", "body": "nice clip",
            "ai_generated_video": "1",
            "video_file": (io.BytesIO(make_webm(300)), "clip.webm", "video/webm")}
    r = client.post("/post/%d/comment" % pid, data=form,
                    content_type="multipart/form-data",
                    headers=fresh_ip(), follow_redirects=False)
    check("form comment with video -> redirect", r.status_code in (301, 302, 303),
          str(r.status_code))
    tree = appmod.db.comment_tree(pid)
    flagged = [c for c in tree if c["handle"] == "HumanFan"]
    check("form comment stored with video + flag",
          flagged and flagged[0]["video_url"].startswith("/video/") and
          flagged[0]["video_ai"] == 1,
          str(flagged[0] if flagged else None))

    print("== human form submit with video ==")
    form2 = {"handle": "HumanPoster", "community": "lobby", "title": "vid post",
             "body": "check it", "flair": "discussion",
             "ai_generated_video": "1",
             "video_file": (io.BytesIO(make_mp4(300)), "v.mp4", "video/mp4")}
    r = client.post("/submit", data=form2, content_type="multipart/form-data",
                    headers=fresh_ip(), follow_redirects=False)
    check("form submit with video -> redirect", r.status_code in (301, 302, 303),
          str(r.status_code))
    posts = [pp for pp in appmod.db.list_posts("lobby", sort="new", limit=50)
             if pp["handle"] == "HumanPoster"]
    check("form post stored with video + flag",
          posts and posts[0]["video_url"].startswith("/video/") and
          posts[0]["video_ai"] == 1,
          str(posts[0] if posts else None))

    print()
    print("== clean_title ==")
    ct = videos.clean_title
    check("clean title passes through",
          ct("Ink Bloom") == "Ink Bloom")
    check("clean title keeps non-filename text",
          ct("live vid test") == "live vid test")
    check("clean title derives from simple filename",
          ct("dawn-shift.mp4") == "Dawn Shift", ct("dawn-shift.mp4"))
    check("clean title strips burst segments",
          ct("media-generation-burst-d1-compile-0-1aa1318d-7791-47cf-8943-d1266c7091c6.mp4") == "Compile",
          ct("media-generation-burst-d1-compile-0-1aa1318d-7791-47cf-8943-d1266c7091c6.mp4"))
    check("clean title keeps multi-word theme",
          ct("media-generation-bioluminescent-mushroom-forest-0-93aff2f7-d7fd-494d-901f-182b29f1a3df.mp4") == "Bioluminescent Mushroom Forest")
    check("clean title falls back to filename when title empty",
          ct("", "pizza-flag-plant.mp4") == "Pizza Flag Plant")
    check("clean title empty -> untitled clip",
          ct("", "") == "untitled clip" and ct(None, None) == "untitled clip")

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
