#!/usr/bin/env python3
"""
Tests for Shorts + long-form watch:
- uploader-declared duration_secs (signed, validated 1..86400) on /api/upload/video
- /api/shorts: newest-first, paging, shorts filter (NULL or <180s)
- /shorts page markup (scroll-snap, autoplay wiring, badge, thread links)
- /watch/<uid>: theater player, badge, Short/Long-form chip, comments
- additive migration of duration_secs on legacy video_uploads tables

Run:  .venv/bin/python test_shorts.py
Throwaway SQLite db + Flask test client + temp DATA_DIR.
Nothing touches townsquare.db.
"""
import base64
import hashlib
import io
import os
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import videos
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-shorts.db"
TEST_DATA = "/tmp/test-townsquare-shorts-data"

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
    return {"X-Forwarded-For": "10.99.0.%d" % _ip_counter[0]}


def post_video(client, priv, fm_id, raw, duration=None, ai="0", filename="clip.mp4"):
    kw = dict(file_sha256=hashlib.sha256(raw).hexdigest(), ai_generated=ai)
    if duration is not None:
        kw["duration_secs"] = duration
    data = signed_body(priv, "upload", fm_id, **kw)
    data["video"] = (io.BytesIO(raw), filename, "video/mp4")
    return client.post("/api/upload/video", data=data,
                       content_type="multipart/form-data", headers=fresh_ip())


def main():
    client = setup()

    print("== validate_duration_secs ==")
    check("None -> None", videos.validate_duration_secs(None) is None)
    check("blank -> None", videos.validate_duration_secs("") is None and
          videos.validate_duration_secs("  ") is None)
    check("'45' -> 45", videos.validate_duration_secs("45") == 45)
    check("int 45 -> 45", videos.validate_duration_secs(45) == 45)
    check("boundary 1 ok", videos.validate_duration_secs("1") == 1)
    check("boundary 86400 ok", videos.validate_duration_secs("86400") == 86400)
    for bad in ["0", "-5", "86401", "abc", "4.5", "1e3"]:
        try:
            videos.validate_duration_secs(bad)
            check("reject %r" % bad, False, "accepted!")
        except ValueError:
            check("reject %r" % bad, True)

    print("== legacy migration: duration_secs column ==")
    leg_path = "/tmp/test-townsquare-shorts-legacy.db"
    if os.path.exists(leg_path):
        os.remove(leg_path)
    raw = sqlite3.connect(leg_path)
    raw.executescript("""
CREATE TABLE video_uploads (id INTEGER PRIMARY KEY AUTOINCREMENT, fm_id TEXT,
 handle TEXT NOT NULL, filename TEXT NOT NULL, stored_path TEXT NOT NULL,
 bytes INTEGER NOT NULL, mime TEXT NOT NULL, ai_generated INTEGER NOT NULL DEFAULT 0,
 created_at INTEGER NOT NULL);
INSERT INTO video_uploads (fm_id, handle, filename, stored_path, bytes, mime, ai_generated, created_at)
 VALUES ('fm_x','OldMuse','old.mp4','uploads/vid-1.mp4',100,'video/mp4',0,%d);
CREATE TABLE posts (id INTEGER PRIMARY KEY AUTOINCREMENT, community TEXT, handle TEXT,
 title TEXT, body TEXT, flair TEXT, created_at INTEGER);
CREATE TABLE comments (id INTEGER PRIMARY KEY AUTOINCREMENT, post_id INTEGER,
 parent_id INTEGER, handle TEXT, body TEXT, created_at INTEGER);
""" % int(time.time()))
    raw.commit()
    raw.close()

    class _Shim:
        def __init__(self, path):
            self.db = sqlite3.connect(path)
            self.db.row_factory = sqlite3.Row

        def _one(self, sql, args=()):
            return self.db.execute(sql, args).fetchone()

        def _exec(self, sql, args=()):
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    leg = _Shim(leg_path)
    videos.ensure_video_schema(leg)
    cols = [r["name"] for r in leg.db.execute("PRAGMA table_info(video_uploads)")]
    check("legacy video_uploads gained duration_secs", "duration_secs" in cols)
    r = leg._one("SELECT duration_secs FROM video_uploads WHERE id=1")
    check("legacy row readable, duration NULL", r is not None and
          r["duration_secs"] is None, str(r))
    os.remove(leg_path)

    print("== signed upload with duration_secs ==")
    priv, fm_id = register(client, "ShortMuse")
    raw = make_mp4(500)
    r = post_video(client, priv, fm_id, raw, duration="60", ai="1")
    j = r.get_json()
    check("upload with duration -> 200", r.status_code == 200,
          f"{r.status_code} {r.get_data(as_text=True)[:200]}")
    uid_short = j.get("id")
    check("duration echoed", j.get("duration_secs") == 60, str(j))
    u = videos.get_video_upload(appmod.db, uid_short)
    check("duration persisted", u and u["duration_secs"] == 60, str(u))

    # no duration -> NULL
    r = post_video(client, priv, fm_id, make_mp4(400))
    uid_unk = r.get_json()["id"]
    check("omitted duration -> 200 + NULL",
          r.status_code == 200 and
          videos.get_video_upload(appmod.db, uid_unk)["duration_secs"] is None)

    # invalid durations -> 400
    for bad in ["0", "86401", "abc"]:
        r = post_video(client, priv, fm_id, make_mp4(300), duration=bad)
        check("duration %r -> 400" % bad, r.status_code == 400,
              str(r.status_code))

    # tampered signed duration -> 401
    data = signed_body(priv, "upload", fm_id,
                       file_sha256=hashlib.sha256(raw).hexdigest(),
                       ai_generated="0", duration_secs="60")
    data["duration_secs"] = "600"
    data["video"] = (io.BytesIO(raw), "clip.mp4", "video/mp4")
    r = client.post("/api/upload/video", data=data,
                    content_type="multipart/form-data", headers=fresh_ip())
    check("tampered duration_secs -> 401", r.status_code == 401,
          str(r.status_code))

    # long video for the filter test
    r = post_video(client, priv, fm_id, make_mp4(600), duration="300")
    uid_long = r.get_json()["id"]
    check("300s upload -> 200", r.status_code == 200, str(r.status_code))

    print("== /api/shorts feed ==")
    r = client.get("/api/shorts")
    d = r.get_json()
    ids = [it["id"] for it in d["items"]]
    check("feed -> 200 ok", r.status_code == 200 and d["ok"])
    check("newest first", ids == sorted(ids, reverse=True), str(ids))
    check("300s video excluded from shorts",
          uid_long not in ids and uid_short in ids and uid_unk in ids, str(ids))
    check("unknown duration counts as short", uid_unk in ids)
    it = [x for x in d["items"] if x["id"] == uid_short][0]
    check("item has urls + flags",
          it["video_url"] == "/video/%d" % uid_short and
          it["watch_url"] == "/watch/%d" % uid_short and
          it["ai_generated"] is True and it["duration_secs"] == 60, str(it))

    # paging
    r = client.get("/api/shorts?limit=1")
    d1 = r.get_json()
    check("limit=1 returns one", len(d1["items"]) == 1, str(d1))
    r = client.get("/api/shorts?limit=1&before=%d" % d1["items"][0]["id"])
    d2 = r.get_json()
    check("before pages older", len(d2["items"]) == 1 and
          d2["items"][0]["id"] < d1["items"][0]["id"], str(d2))
    check("next_before cursor", d1["next_before"] == d1["items"][0]["id"])
    r = client.get("/api/shorts?before=1")
    check("before=1 -> empty", r.get_json()["items"] == [])

    # boundary: exactly 180s is NOT short
    r = post_video(client, priv, fm_id, make_mp4(700), duration="180")
    uid_edge = r.get_json()["id"]
    r = client.get("/api/shorts?limit=50")
    ids = [it["id"] for it in r.get_json()["items"]]
    check("180s exactly excluded (< 180 is short)", uid_edge not in ids, str(ids))

    print("== source linking: post + comment ==")
    r = client.post("/api/forum/post", json=signed_body(
        priv, "post", fm_id, community="lobby", title="my short",
        body="watch this", flair="discussion",
        video_url="/video/%d" % uid_short, video_ai=True), headers=fresh_ip())
    pid = r.get_json()["id"]
    check("post with video -> 200", r.status_code == 200, str(r.status_code))
    r = client.post("/api/forum/comment", json=signed_body(
        priv, "comment", fm_id, post_id=pid, body="first!",
        video_url="/video/%d" % uid_unk, video_ai=False), headers=fresh_ip())
    check("comment with video -> 200", r.status_code == 200, str(r.status_code))

    r = client.get("/api/shorts?limit=50")
    items = {it["id"]: it for it in r.get_json()["items"]}
    check("post-sourced item links thread + uses post title",
          items[uid_short]["thread_url"] == "/c/lobby/post/%d" % pid and
          items[uid_short]["title"] == "my short",
          str(items[uid_short]))
    cid = appmod.db.comment_tree(pid)[0]["id"]
    check("comment-sourced item links thread#c<id>",
          items[uid_unk]["thread_url"] == "/c/lobby/post/%d#c%d" % (pid, cid),
          str(items[uid_unk]))

    print("== /shorts page ==")
    html = client.get("/shorts").get_data(as_text=True)
    check("200", "<article" in html)
    check("scroll-snap feed markup",
          "shorts-feed" in html and "short-item" in html and
          'class="shorts-mode"' in html)
    check("badge renders for AI video", "AI-generated" in html)
    check("thread + watch links", "/c/lobby/post/%d" % pid in html and
          "/watch/%d" % uid_short in html)
    check("IntersectionObserver autoplay wiring", "IntersectionObserver" in html)
    check("tap-to-mute wiring", "short-mute" in html)
    check("nav has Shorts link", 'href="/shorts"' in html)

    # empty feed state
    appmod.db.db.execute("DELETE FROM video_uploads")
    appmod.db.db.commit()
    html = client.get("/shorts").get_data(as_text=True)
    check("empty feed shows friendly state", "No shorts yet" in html)
    # re-add one for the watch tests below
    r = post_video(client, priv, fm_id, make_mp4(500), duration="60", ai="1")
    uid_short = r.get_json()["id"]
    r = client.post("/api/forum/post", json=signed_body(
        priv, "post", fm_id, community="lobby", title="my short",
        body="watch this", flair="discussion",
        video_url="/video/%d" % uid_short, video_ai=True), headers=fresh_ip())
    pid = r.get_json()["id"]
    r = client.post("/api/forum/comment", json=signed_body(
        priv, "comment", fm_id, post_id=pid, body="so good"), headers=fresh_ip())
    assert r.status_code == 200

    print("== /watch/<uid> ==")
    html = client.get("/watch/%d" % uid_short).get_data(as_text=True)
    check("200 + theater player",
          '<video class="watch-player"' in html and
          'src="/video/%d"' % uid_short in html)
    check("badge on watch page", "AI-generated" in html)
    check("Short chip for 60s", ">Short<" in html)
    check("title + author", "my short" in html and "u/ShortMuse" in html)
    check("comment thread renders", "so good" in html)
    check("thread link present", "/c/lobby/post/%d" % pid in html)

    # long-form chip
    r = post_video(client, priv, fm_id, make_mp4(500), duration="1200")
    uid_lf = r.get_json()["id"]
    html = client.get("/watch/%d" % uid_lf).get_data(as_text=True)
    check("Long-form chip for 1200s", ">Long-form<" in html)
    check("unattached video renders", "isn't attached to a thread yet" in html)

    r = client.get("/watch/999999")
    check("unknown video -> 404", r.status_code == 404)

    print("== human form submit with duration ==")
    form = {"handle": "HumanClip", "community": "lobby", "title": "form short",
            "body": "hi", "flair": "discussion",
            "video_duration": "45",
            "video_file": (io.BytesIO(make_mp4(300)), "v.mp4", "video/mp4")}
    r = client.post("/submit", data=form, content_type="multipart/form-data",
                    headers=fresh_ip(), follow_redirects=False)
    check("form submit with duration -> redirect",
          r.status_code in (301, 302, 303), str(r.status_code))
    posts = [pp for pp in appmod.db.list_posts("lobby", sort="new", limit=50)
             if pp["handle"] == "HumanClip"]
    vurl = posts[0]["video_url"] if posts else ""
    uid = int(vurl.rsplit("/", 1)[-1]) if vurl else None
    check("form duration persisted",
          uid and videos.get_video_upload(appmod.db, uid)["duration_secs"] == 45,
          str(uid))
    form["video_duration"] = "0"
    form["video_file"] = (io.BytesIO(make_mp4(300)), "v.mp4", "video/mp4")
    r = client.post("/submit", data=form, content_type="multipart/form-data",
                    headers=fresh_ip(), follow_redirects=False)
    check("form duration 0 -> 400", r.status_code == 400, str(r.status_code))

    print("== anchored Shorts feed (?video=) ==")
    priv_a, fm_a = register(client, "AnchorMuse")
    anchor_ids = []
    for _ in range(12):
        r = post_video(client, priv_a, fm_a, make_mp4(200), duration="15")
        assert r.status_code == 200, r.get_data(as_text=True)[:200]
        anchor_ids.append(r.get_json()["id"])
    oldest, newest = anchor_ids[0], anchor_ids[-1]
    html = client.get("/shorts").get_data(as_text=True)
    check("oldest falls outside initial 10-page",
          'data-id="%d"' % oldest not in html)
    html = client.get("/shorts?video=%d" % oldest).get_data(as_text=True)
    check("anchor card included even outside page",
          'data-id="%d"' % oldest in html)
    check("anchor id passed to template", 'data-anchor="%d"' % oldest in html)
    check("anchor scroll wiring present",
          "scrollIntoView" in html and 'data-anchor=' in html)
    html = client.get("/shorts?video=%d" % newest).get_data(as_text=True)
    check("on-page anchor still anchors", 'data-anchor="%d"' % newest in html)
    for bad in ["999999", "abc", "0", "-3", ""]:
        html = client.get("/shorts?video=%s" % bad).get_data(as_text=True)
        check("bad ?video=%r ignored" % bad, 'data-anchor=""' in html)
    r = post_video(client, priv_a, fm_a, make_mp4(200), duration="600")
    uid_long2 = r.get_json()["id"]
    html = client.get("/shorts?video=%d" % uid_long2).get_data(as_text=True)
    check("long-form video never anchors into shorts",
          'data-anchor=""' in html and 'data-id="%d"' % uid_long2 not in html)
    r = client.get("/api/shorts?limit=1")
    it = r.get_json()["items"][0]
    check("_short_item carries feed_url",
          it.get("feed_url") == "/shorts?video=%d" % it["id"],
          str(it.get("feed_url")))
    html = client.get("/").get_data(as_text=True)
    check("home shorts open the anchored feed",
          "/shorts?video=" in html and 'class="vfeed-card"' in html)

    print("== anchored Muse FM shorts (?video=) ==")
    priv_b, fm_b = register(client, "FmAnchorA")
    priv_c, fm_c = register(client, "FmAnchorB")
    fm_ids = []
    for i in range(21):
        pp, ff = (priv_b, fm_b) if i < 11 else (priv_c, fm_c)
        r = post_video(client, pp, ff, make_mp4(200), duration="20")
        assert r.status_code == 200, r.get_data(as_text=True)[:200]
        uid = r.get_json()["id"]
        videos.set_series(appmod.db, uid, "musefm")
        fm_ids.append(uid)
    fm_oldest = fm_ids[0]
    html = client.get("/musefm/shorts").get_data(as_text=True)
    check("oldest musefm clip outside initial 20-page",
          'data-id="video-%d"' % fm_oldest not in html)
    html = client.get("/musefm/shorts?video=%d" % fm_oldest).get_data(as_text=True)
    check("musefm anchor card included outside page",
          'data-id="video-%d"' % fm_oldest in html)
    check("musefm anchor id passed to template",
          'data-anchor="%d"' % fm_oldest in html)
    html = client.get("/musefm/shorts?video=%d" % oldest).get_data(as_text=True)
    check("non-musefm clip not anchored into musefm feed",
          'data-anchor=""' in html)
    html = client.get("/musefm").get_data(as_text=True)
    check("musefm hub strip opens anchored feed",
          "/musefm/shorts?video=" in html)

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
