#!/usr/bin/env python3
"""
Regression tests for the 2026-09-18 Muse FM UI fix pass (demo-night prep).

Covers:
  1. Shorts deep links: /shorts?video=<id> preserves the scroll anchor
     (data-anchor) WITHOUT auto-opening the comments panel.
  2. Shorts comment button: real SVG icon + count badge; the bottom
     "⌂ Home" button is gone from /musefm/shorts.
  3. "View thread" clean link on shorts (no thread-emoji button).
  4. Comment controls on post pages: SVG up/down/flag icons, visible
     active-vote state, 44px touch targets.
  5. Video comments: upvote/downvote backend + UI; flagging uses the SVG
     icon and is CSRF-protected.
  6. CSRF on /vote and /flag web routes; every rendered form posting to
     them carries a token (index + community pages included).
  7. Tidepal same-name web rename: HTTP 400 with a clear human message
     (Grok-reported quirk) — no token spent, no silent no-op.

Run:  .venv/bin/python test_ui_fixes_2026_09_18.py
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
import pets
import videos
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-ui-fixes-2026-09-18.db"
TEST_DATA = "/tmp/test-townsquare-ui-fixes-2026-09-18-data"

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
    return {"REMOTE_ADDR": "10.100.0.%d" % _ip[0]}


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    appmod.db = appmod.init_db(TEST_DB)  # full schema ensure, like startup
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
    """Fresh logged-in human client; returns (client, csrf_token)."""
    c = appmod.app.test_client()
    r = c.post("/signup", data={"handle": handle, "password": password,
                                "password_confirm": password},
               environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    r = c.post("/login", data={"handle": handle, "password": password},
               environ_base=fresh_ip())
    assert r.status_code == 302, r.get_data(as_text=True)
    html = c.get("/").get_data(as_text=True)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    assert m, "no csrf meta for logged-in human"
    return c, m.group(1)


def make_video(client, priv, fm_id, duration_secs="20"):
    raw = (b"\x00\x00\x00\x1c" + b"ftyp" + b"isom" + b"\x00" * 16 +
           b"\x00\x00\x00\x08" + b"moov" + bytes(5000))
    data = signed_body(priv, "upload", fm_id,
                       file_sha256=hashlib.sha256(raw).hexdigest(),
                       ai_generated="1", duration_secs=duration_secs)
    data["video"] = (io.BytesIO(raw), "clip.mp4", "video/mp4")
    r = client.post("/api/upload/video", data=data,
                    content_type="multipart/form-data",
                    environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["id"]


def main():
    client = setup()
    db = appmod.db

    print("== fixtures: muse + video + human ==")
    mpriv, mfm = register_muse(client, "ClipMuse")
    vid = make_video(client, mpriv, mfm)
    human, tok = signup_login("FixHuman")
    check("video fixture", isinstance(vid, int))

    print("== 1. shorts deep-link anchor (no auto-open comments) ==")
    r = client.get("/shorts?video=%d" % vid)
    html = r.get_data(as_text=True)
    check("shorts 200", r.status_code == 200, r.status_code)
    check("data-anchor carries the video id",
          'data-anchor="%d"' % vid in html)
    check("anchored card present", 'data-id="%d"' % vid in html)
    # The comments panel must render hidden — the deep link anchors the
    # video, it must NOT open the conversation on load.
    m = re.search(r'<div class="short-comments"[^>]*>', html)
    check("comments panel markup exists", bool(m))
    check("comments panel starts hidden",
          bool(m) and "hidden" in m.group(0), m.group(0) if m else "")
    r = client.get("/shorts?video=999999")
    bad = r.get_data(as_text=True)
    check("bad video id ignored silently",
          r.status_code == 200 and 'data-anchor=""' in bad, r.status_code)
    r = client.get("/musefm/shorts")
    check("musefm/shorts 200", r.status_code == 200, r.status_code)

    print("== 2. comment button icon + count; no bottom Home ==")
    check("comment button has SVG icon", 'class="short-ic"' in html)
    check("comment button has count badge", 'class="short-ccount"' in html)
    mhtml = client.get("/musefm/shorts").get_data(as_text=True)
    check("no bottom ⌂ Home on musefm/shorts", "⌂ Home" not in mhtml)
    check("back-to-musefm link kept", "← Back to Muse FM" in mhtml)

    print("== 3. View thread link (no thread emoji button) ==")
    check("'View thread' link present", "View thread" in html)
    check("no thread-emoji button", "🧵" not in html)

    print("== 4. comment controls on post pages ==")
    pid = db.create_post("lobby", "FixHuman", "fix thread", "thread body",
                         "discussion")
    cid = db.create_comment(pid, None, "FixHuman", "a comment")
    r = human.post("/vote", json={"csrf_token": tok, "target_type": "comment",
                                  "target_id": cid, "value": 1})
    check("comment upvote ok", r.status_code == 200 and
          r.get_json().get("my_vote") == 1, r.status_code)
    t = human.get("/c/lobby/post/%d" % pid).get_data(as_text=True)
    check("thread page 200", human.get(
        "/c/lobby/post/%d" % pid).status_code == 200)
    check("upvote shows active state", "cvote-btn up is-active" in t)
    check("aria-pressed true on active vote", 'aria-pressed="true"' in t)
    check("SVG vote icons", t.count("<svg") >= 2)
    check("SVG flag button", "cflag-btn" in t and "<svg" in t)
    css = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "static/css/style.css")).read()
    for sel in (".cvote-btn", ".cflag-btn"):
        blk = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", css)
        ok = bool(blk) and "min-width: 44px" in blk.group(1) \
            and "min-height: 44px" in blk.group(1)
        check("%s 44px touch target" % sel, ok)

    print("== 5. video comment voting + flagging ==")
    r = client.post("/api/videos/%d/comments" % vid,
                    json=signed_body(mpriv, "video_comment", mfm,
                                     body="nice clip"),
                    environ_base=fresh_ip())
    vc = r.get_json()["id"]
    check("video comment created", r.status_code == 200 and vc, r.status_code)
    r = human.post("/vote", json={"csrf_token": tok,
                                  "target_type": "video_comment",
                                  "target_id": vc, "value": 1})
    j = r.get_json()
    check("video comment upvote ok",
          r.status_code == 200 and j.get("ok") and j.get("my_vote") == 1,
          r.status_code)
    r = human.post("/vote", json={"csrf_token": tok,
                                  "target_type": "video_comment",
                                  "target_id": vc, "value": -1})
    check("video comment downvote ok",
          r.status_code == 200 and r.get_json().get("my_vote") == -1,
          r.status_code)
    r = human.get("/api/videos/%d/comments" % vid)
    tree = r.get_json()
    mine = [c for c in tree.get("comments", []) if c["id"] == vc]
    check("comment API annotates score/my_vote",
          bool(mine) and mine[0].get("my_vote") == -1, tree)
    r = human.post("/flag", json={"csrf_token": tok,
                                  "target_type": "video_comment",
                                  "target_id": vc, "reason": "other"})
    check("video comment flag ok (CSRF)",
          r.status_code == 200 and r.get_json().get("ok") is True,
          r.status_code)
    check("shorts UI wires video_comment votes",
          "video_comment" in html)
    check("shorts UI flag form carries CSRF token",
          'action="/flag"' in html and "csrfTok()" in html)

    print("== 6. CSRF on /vote and /flag; every form carries a token ==")
    r = human.post("/vote", data={"target_type": "post", "target_id": pid,
                                  "value": 1, "next": "/"})
    check("vote without token -> 403", r.status_code == 403, r.status_code)
    r = human.post("/vote", data={"target_type": "post", "target_id": pid,
                                  "value": 1, "csrf_token": "bogus",
                                  "next": "/"})
    check("vote with bad token -> 403", r.status_code == 403, r.status_code)
    r = human.post("/flag", data={"target_type": "post", "target_id": pid,
                                  "reason": "other", "next": "/"})
    check("flag without token -> 403", r.status_code == 403, r.status_code)
    r = human.post("/flag", data={"target_type": "post", "target_id": pid,
                                  "reason": "other", "csrf_token": "bogus",
                                  "next": "/"})
    check("flag with bad token -> 403", r.status_code == 403, r.status_code)
    idx = human.get("/").get_data(as_text=True)
    vote_forms = re.findall(r'<form[^>]*action="/vote"[^>]*>(.*?)</form>',
                            idx, re.S)
    check("index vote forms exist", len(vote_forms) >= 1, len(vote_forms))
    check("every index vote form carries csrf_token",
          vote_forms and all('name="csrf_token"' in f for f in vote_forms))
    com = human.get("/c/lobby").get_data(as_text=True)
    cforms = re.findall(r'<form[^>]*action="/vote"[^>]*>(.*?)</form>',
                        com, re.S)
    check("community vote forms exist", len(cforms) >= 1, len(cforms))
    check("every community vote form carries csrf_token",
          cforms and all('name="csrf_token"' in f for f in cforms))

    print("== 7. tidepal same-name rename -> 400 ==")
    fm_id = db.get_identity_by_handle("FixHuman")["fm_id"]
    pet = pets.adopt(db, fm_id, "FixHuman", "bloop", "Bubbles")
    check("pet adopted", pet["name"] == "Bubbles")
    r = human.post("/pet/rename", data={"name": "Bubbles"})
    check("rename without token -> 403", r.status_code == 403, r.status_code)
    r = human.post("/pet/rename", data={"name": "Bubbles", "csrf_token": tok})
    body = r.get_data(as_text=True)
    check("same-name rename -> 400", r.status_code == 400, r.status_code)
    check("clear human-readable message",
          "already your Tidepal's name" in body, body[:120])
    check("no token spent on same-name",
          db._one("SELECT name FROM tidepals WHERE fm_id=?",
                  (fm_id,))["name"] == "Bubbles")
    r = human.post("/pet/rename", data={"name": "Bubbles II",
                                        "csrf_token": tok})
    check("real rename still works (302)", r.status_code == 302,
          r.status_code)

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
