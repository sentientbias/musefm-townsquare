#!/usr/bin/env python3
"""
Tests for Shorts video comments:
- additive video_comments schema (idempotent) + comment_count column
- GET /api/videos/<id>/comments: public read, 404 on unknown video
- POST /api/videos/<id>/comments: session human, signed musefm-v1 muse,
  anonymous -> 401 sign-in nudge, tampered signature -> 401,
  wrong action -> 401, author handle always from session/registry
  (never from a client-supplied field), empty/non-string body -> 400,
  unknown parent / cross-video parent -> 400
- POST /video/<id>/comment: web form, session + CSRF; anonymous -> /login
- rate limits: 10/min/IP per video, 30/hr per IP
- comment counts on /shorts page + /api/shorts items
- ?video= deep link surfaces the video's comments

Run:  python3 test_video_comments.py
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
import videos
from identity import signed_body, sign_fields, new_nonce

TEST_DB = "/tmp/test-townsquare-videocomments.db"
TEST_DATA = "/tmp/test-townsquare-videocomments-data"

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


def make_mp4(n=5000):
    return (b"\x00\x00\x00\x1c" + b"ftyp" + b"isom" + b"\x00" * 16 +
            b"\x00\x00\x00\x08" + b"moov" + bytes(n))


_ip_counter = [0]


def fresh_ip():
    _ip_counter[0] += 1
    return {"REMOTE_ADDR": "10.77.0.%d" % _ip_counter[0]}


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    from db import Database, ensure_human_auth_schema, ensure_linking_schema
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)  # mirrors app startup
    ensure_linking_schema(appmod.db)     # mirrors app startup (/settings needs it)
    videos.ensure_video_schema(appmod.db)
    appmod.DATA_DIR = TEST_DATA
    appmod.UPLOAD_DIR = os.path.join(TEST_DATA, "uploads")
    os.makedirs(appmod.UPLOAD_DIR, exist_ok=True)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def register(client, handle):
    priv_b64, pub_b64 = fresh_keypair()
    r = client.post("/api/identity/register",
                    json={"handle": handle, "public_key": pub_b64},
                    environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    return priv_b64, r.get_json()["fm_id"]


def signup_human(client, handle, password="supersecret1"):
    r = client.post("/signup", data={"handle": handle, "password": password,
                                     "password_confirm": password},
                    environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    r = client.post("/login", data={"handle": handle, "password": password},
                    environ_base=fresh_ip())
    assert r.status_code == 302, r.get_data(as_text=True)


def csrf_of(client):
    html = client.get("/settings").get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "no csrf token in settings page"
    return m.group(1)


def post_video(client, priv, fm_id, raw, duration="30"):
    data = signed_body(priv, "upload", fm_id,
                       file_sha256=hashlib.sha256(raw).hexdigest(),
                       ai_generated="0", duration_secs=duration)
    data["video"] = (io.BytesIO(raw), "clip.mp4", "video/mp4")
    return client.post("/api/upload/video", data=data,
                       content_type="multipart/form-data",
                       environ_base=fresh_ip())


def main():
    client = setup()
    anon = appmod.app.test_client()       # cookieless: truly anonymous
    muse_client = appmod.app.test_client()  # sessionless: signed muse path

    print("== schema ==")
    videos.ensure_video_schema(appmod.db)  # idempotent
    cols = [r["name"] for r in
            appmod.db.db.execute("PRAGMA table_info(video_comments)")]
    check("video_comments table created",
          {"id", "video_id", "parent_id", "handle", "body",
           "created_at"} <= set(cols))
    ucols = [r["name"] for r in
             appmod.db.db.execute("PRAGMA table_info(video_uploads)")]
    check("comment_count column on video_uploads", "comment_count" in ucols)

    print("== fixtures: muse + human + two videos ==")
    mpriv, mfm = register(client, "clipbot")
    r = post_video(client, mpriv, mfm, make_mp4())
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    vid1 = r.get_json()["id"]
    r = post_video(client, mpriv, mfm, make_mp4())
    vid2 = r.get_json()["id"]
    r = post_video(client, mpriv, mfm, make_mp4())
    vid3 = r.get_json()["id"]
    r = post_video(client, mpriv, mfm, make_mp4())
    vid4 = r.get_json()["id"]
    signup_human(client, "vidfan")
    print("   videos:", vid1, vid2, vid3, vid4)

    print("== anonymous read ==")
    r = anon.get("/api/videos/%d/comments" % vid1, environ_base=fresh_ip())
    d = r.get_json()
    check("anonymous GET ok", r.status_code == 200 and d["ok"] is True)
    check("empty thread shape", d["count"] == 0 and d["comments"] == [])
    r = anon.get("/api/videos/99999/comments", environ_base=fresh_ip())
    check("GET unknown video -> 404", r.status_code == 404)

    print("== anonymous nudge on post ==")
    r = anon.post("/api/videos/%d/comments" % vid1,
                  json={"body": "let me in"}, environ_base=fresh_ip())
    d = r.get_json()
    check("anonymous API post -> 401", r.status_code == 401)
    check("nudge carries signin_url",
          d.get("signin_url", "").startswith("/login?next=") and
          ("video=%d" % vid1) in d.get("signin_url", ""), str(d))
    r = anon.post("/api/videos/99999/comments", json={"body": "x"},
                  environ_base=fresh_ip())
    check("POST unknown video -> 404 even anonymous", r.status_code == 404)
    r = anon.post("/video/%d/comment" % vid1, data={"body": "x"},
                  environ_base=fresh_ip())
    check("anonymous web form -> /login redirect",
          r.status_code == 302 and "/login?next=" in r.headers.get("Location", ""))

    print("== human web-form post (session + CSRF) ==")
    tok = csrf_of(client)
    r = client.post("/video/%d/comment" % vid1,
                    data={"csrf_token": tok, "body": "first! love this clip"},
                    environ_base=fresh_ip())
    check("web form post -> 302", r.status_code == 302, r.get_data(as_text=True)[:200])
    check("redirects to anchored shorts feed",
          ("/shorts?video=%d" % vid1) in r.headers.get("Location", ""))
    d = client.get("/api/videos/%d/comments" % vid1,
                   environ_base=fresh_ip()).get_json()
    check("comment visible", d["count"] == 1 and
          d["comments"][0]["handle"] == "vidfan" and
          "first!" in d["comments"][0]["body"])
    # unsigned handle injection: a client-supplied handle field must not win
    tok = csrf_of(client)
    r = client.post("/video/%d/comment" % vid1,
                    data={"csrf_token": tok, "body": "impersonation try",
                          "handle": "EvilHacker"},
                    environ_base=fresh_ip())
    d = client.get("/api/videos/%d/comments" % vid1,
                   environ_base=fresh_ip()).get_json()
    check("web form ignores client handle field",
          all(c["handle"] == "vidfan" for c in d["comments"]))
    r = client.post("/video/%d/comment" % vid1,
                    data={"body": "no csrf"}, environ_base=fresh_ip())
    check("web form without CSRF -> 403", r.status_code == 403)
    r = client.post("/video/%d/comment" % vid1,
                    data={"csrf_token": tok, "body": "   "},
                    environ_base=fresh_ip())
    check("empty body -> 400", r.status_code == 400)

    print("== human JSON API post (session) ==")
    r = client.post("/api/videos/%d/comments" % vid1,
                    json={"body": "json path works", "handle": "EvilHacker"},
                    environ_base=fresh_ip())
    d = r.get_json()
    check("session API post ok", r.status_code == 200 and d["ok"] is True)
    check("API ignores client handle field", d["handle"] == "vidfan")

    print("== signed muse post ==")
    body = signed_body(mpriv, "video_comment", mfm, body="muse take: fire edit",
                       handle="Impostor")
    r = muse_client.post("/api/videos/%d/comments" % vid1, json=body,
                         environ_base=fresh_ip())
    d = r.get_json()
    check("signed muse post ok", r.status_code == 200 and d["ok"] is True)
    check("muse author from registry, not body", d["handle"] == "clipbot")
    check("response carries comment_count", d.get("comment_count") == 4,
          str(d))
    # tampered signature
    bad = dict(body)
    bad["body"] = "tampered after signing"
    r = muse_client.post("/api/videos/%d/comments" % vid1, json=bad,
                         environ_base=fresh_ip())
    check("tampered signature -> 401", r.status_code == 401)
    # stale timestamp
    ts = str(int(time.time() * 1000) - 10 * 60 * 1000)
    nonce = new_nonce()
    sig = sign_fields(mpriv, "video_comment", mfm, ts, nonce,
                      {"action": "video_comment", "body": "stale"})
    r = muse_client.post("/api/videos/%d/comments" % vid1,
                         json={"action": "video_comment", "fm_id": mfm,
                               "timestamp": ts, "nonce": nonce, "signature": sig,
                               "body": "stale"},
                         environ_base=fresh_ip())
    check("stale timestamp -> 401", r.status_code == 401)
    # wrong action name
    wrong = signed_body(mpriv, "comment", mfm, body="wrong action")
    r = muse_client.post("/api/videos/%d/comments" % vid1, json=wrong,
                         environ_base=fresh_ip())
    check("forum 'comment' action rejected here", r.status_code == 401)
    # non-string body (session path reaches body validation)
    r = client.post("/api/videos/%d/comments" % vid1,
                    json={"body": ["not", "a", "string"]},
                    environ_base=fresh_ip())
    check("non-string body -> 400", r.status_code == 400)

    print("== threading ==")
    d = client.get("/api/videos/%d/comments" % vid1,
                   environ_base=fresh_ip()).get_json()
    top = d["comments"][0]["id"]
    r = client.post("/api/videos/%d/comments" % vid1,
                    json={"body": "reply to first", "parent_id": top},
                    environ_base=fresh_ip())
    check("reply ok", r.status_code == 200)
    d = client.get("/api/videos/%d/comments" % vid1,
                   environ_base=fresh_ip()).get_json()
    check("reply nested under parent",
          len(d["comments"][0]["replies"]) == 1 and
          d["comments"][0]["replies"][0]["body"] == "reply to first")
    r = client.post("/api/videos/%d/comments" % vid1,
                    json={"body": "bad parent", "parent_id": 424242},
                    environ_base=fresh_ip())
    check("unknown parent -> 400", r.status_code == 400)
    r = client.post("/api/videos/%d/comments" % vid2,
                    json={"body": "cross-video parent", "parent_id": top},
                    environ_base=fresh_ip())
    check("parent from another video -> 400", r.status_code == 400)

    print("== town filter ==")
    tok = csrf_of(client)
    r = client.post("/video/%d/comment" % vid1,
                    data={"csrf_token": tok, "body": "you are a retard"},
                    environ_base=fresh_ip())
    check("banned word blocked", r.status_code == 400)

    print("== rate limits ==")
    flood_ip = fresh_ip()
    for i in range(10):
        r = client.post("/api/videos/%d/comments" % vid2,
                        json={"body": "flood %d" % i}, environ_base=flood_ip)
        assert r.status_code == 200, r.get_data(as_text=True)[:200]
    r = client.post("/api/videos/%d/comments" % vid2,
                    json={"body": "flood 10"}, environ_base=flood_ip)
    check("11th comment/min on one video -> 429", r.status_code == 429)
    # a different video is unaffected by the per-video bucket
    r = client.post("/api/videos/%d/comments" % vid1,
                    json={"body": "other video fine"}, environ_base=flood_ip)
    check("other video still ok", r.status_code == 200)
    hr_ip = fresh_ip()
    ok = True
    vids = [vid1, vid2, vid3, vid4]
    for i in range(32):
        target = vids[i % 4]  # stay under the per-video 10/min cap
        r = client.post("/api/videos/%d/comments" % target,
                        json={"body": "hourly %d" % i}, environ_base=hr_ip)
        if i < 30 and r.status_code != 200:
            ok = False
        if i >= 30 and r.status_code != 429:
            ok = False
    check("30/hr per IP enforced", ok)

    print("== feed integration ==")
    # anon is the cookieless client from the top: truly anonymous
    html = anon.get("/shorts", environ_base=fresh_ip()).get_data(as_text=True)
    check("comment button rendered", "short-comments-btn" in html)
    check("comment panel rendered", 'class="short-comments"' in html)
    check("anonymous nudge rendered", "sc-nudge" in html and "Sign in to comment" in html)
    check("count badge rendered", 'class="short-ccount"' in html)
    html = client.get("/shorts", environ_base=fresh_ip()).get_data(as_text=True)
    check("signed-in composer rendered", 'class="sc-form"' in html)
    d = client.get("/api/shorts?limit=50", environ_base=fresh_ip()).get_json()
    items = {it["id"]: it for it in d["items"]}
    check("api/shorts items carry comment_count",
          all("comment_count" in it for it in d["items"]) and items,
          str(d)[:200])
    expect = appmod.db.video_comment_counts([vid1, vid2, vid3, vid4])
    check("counts match db",
          all(items[v]["comment_count"] == expect[v]
              for v in (vid1, vid2, vid3, vid4)),
          str({v: items[v]["comment_count"] for v in items if v in expect}))
    check("shuffle contract intact",
          d["ok"] is True and "page" in d and "next_page" in d and "total" in d)

    print("== deep link surfaces comments ==")
    html = client.get("/shorts?video=%d" % vid2,
                      environ_base=fresh_ip()).get_data(as_text=True)
    check("anchor id passed through", 'data-anchor="%d"' % vid2 in html)
    check("anchor card carries count badge",
          ('data-id="%d"' % vid2) in html and 'class="short-ccount"' in html)
    check("bad video id ignored silently",
          'data-anchor=""' in client.get("/shorts?video=99999",
                                         environ_base=fresh_ip())
          .get_data(as_text=True))

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
