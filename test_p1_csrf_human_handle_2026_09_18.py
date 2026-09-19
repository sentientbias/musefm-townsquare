#!/usr/bin/env python3
"""
Proof tests for the two P1s Anthony ordered fixed (2026-09-18 ~22:10 CDT).

P1 #1 (CSRF): templates/post.html, templates/episode_watch.html and
templates/watch.html rendered `{{ CSRF }}` from a self-referential
`{% set CSRF = CSRF if session_identity else "" %}` (always empty), so every
comment/reply/edit/flag/vote form on thread/video/episode detail pages
submitted an EMPTY token and 403'd. Fixed: the set line now resolves the real
token via `csrf_token()` (the function injected by app.inject_globals), so all
`{{ CSRF }}` usages and the vote_controls/flag_btn macro calls get a valid
token. Test asserts every hidden csrf_token input rendered on a logged-in
thread page, video watch page, and episode page is non-empty and consistent.

P1 #2 (authz): POST /api/identity/update let a muse self-assert an arbitrary
`human_handle` (with visibility=linked) with no pairing code, no human
consent, and no human_muse_links check. Fixed in db.update_identity: a
non-empty human_handle is accepted only when the muse has a verified
1:1 link row (human_muse_links) AND the asserted handle exactly equals the
linked human's actual handle. Empty string still clears. db.unlink now clears
the muse's stored human_handle so stale assertions can't linger.

Run:  python3 test_p1_csrf_human_handle_2026_09_18.py
Throwaway SQLite db + Flask test client + temp DATA_DIR. Nothing touches
data/townsquare.db or production.
"""
import base64
import hashlib
import io
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-p1fix-20260918.db"
TEST_DATA = "/tmp/test-townsquare-p1fix-20260918-data"

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


_ip = [0]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": f"127.0.0.{_ip[0]}"}


def setup():
    for p in (TEST_DB,):
        if os.path.exists(p):
            os.remove(p)
    if os.path.exists(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    os.makedirs(TEST_DATA, exist_ok=True)
    os.environ["DATA_DIR"] = TEST_DATA
    appmod.DATA_DIR = TEST_DATA
    appmod.UPLOAD_DIR = os.path.join(TEST_DATA, "uploads")
    os.makedirs(appmod.UPLOAD_DIR, exist_ok=True)
    from db import Database, ensure_human_auth_schema, ensure_linking_schema
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)
    ensure_linking_schema(appmod.db)
    import videos
    videos.ensure_video_schema(appmod.db)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def signup_login(client, handle, password="supersecret1"):
    r = client.post("/signup",
                    data={"handle": handle, "password": password,
                          "password_confirm": password},
                    environ_base=fresh_ip())
    assert r.status_code in (200, 302), (r.status_code, r.get_data(as_text=True)[:200])
    r = client.post("/login", data={"handle": handle, "password": password},
                    environ_base=fresh_ip())
    assert r.status_code in (200, 302), (r.status_code, r.get_data(as_text=True)[:200])
    return client


def register_muse(client, handle):
    priv, pub = fresh_keypair()
    r = client.post("/api/identity/register", json={"handle": handle, "public_key": pub})
    assert r.status_code == 200, (r.status_code, r.get_data(as_text=True)[:200])
    return priv, r.get_json()["fm_id"]


def make_video(client, priv, fm_id):
    raw = (b"\x00\x00\x00\x1c" + b"ftyp" + b"isom" + b"\x00" * 16 +
           b"\x00\x00\x00\x08" + b"moov" + bytes(5000))
    data = signed_body(priv, "upload", fm_id,
                       file_sha256=hashlib.sha256(raw).hexdigest(),
                       ai_generated="1", duration_secs="20")
    data["video"] = (io.BytesIO(raw), "clip.mp4", "video/mp4")
    r = client.post("/api/upload/video", data=data,
                    content_type="multipart/form-data",
                    environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    return r.get_json()["id"]


def csrf_inputs(html):
    return re.findall(r'name="csrf_token"\s+value="([^"]*)"', html)


def check_csrf_page(label, html, min_inputs=2):
    toks = csrf_inputs(html)
    check(f"{label}: renders csrf_token inputs", len(toks) >= min_inputs,
          f"found {len(toks)}")
    check(f"{label}: every rendered csrf_token input is non-empty",
          all(t.strip() for t in toks),
          f"empty: {len(toks) - len([t for t in toks if t.strip()])} of {len(toks)}")
    uniq = set(toks)
    check(f"{label}: rendered tokens are consistent (single session token)",
          len(uniq) == 1, f"{len(uniq)} distinct")


def main():
    client = setup()
    db = appmod.db

    print("== P1 #1: detail-page CSRF tokens render non-empty ==")
    me = signup_login(client, "P1Human")
    pid = db.create_post("lobby", "P1Human", "P1 thread title",
                         "P1 thread body", "discussion")
    html = me.get(f"/c/lobby/post/{pid}").get_data(as_text=True)
    check("thread page 200", 'P1 thread title' in html)
    check_csrf_page("thread page", html)

    priv_v, fm_v = register_muse(client, "P1VideoMuse")
    vid = make_video(client, priv_v, fm_v)
    # watch.html renders comment vote/flag macros (the CSRF carriers) from the
    # comment tree of the post the video is attached to — link one up.
    vpid = db.create_post("lobby", "P1Human", "P1 video post", "video body",
                          "discussion", video_url=f"/video/{vid}")
    db.create_comment(vpid, None, "P1Human", "P1 video comment")
    html = me.get(f"/watch/{vid}").get_data(as_text=True)
    check("video watch page 200", 'data-target-type="video"' in html)
    check_csrf_page("video watch page", html)

    db._exec("INSERT INTO episodes (slug,title,series,description,audio_file,"
             "duration_sec,published) " + "VALUES (" + ",".join(["?"] * 7) + ")",
             ("p1ep", "P1 Episode", "musefm", "", "ep.mp3", 60, "2026-09-18"))
    # NOTE: episode comments are broken at the schema level (episode_comments
    # has no parent_id column but db.add_episode_comment and
    # db.episode_comment_tree both reference it) — any POST to
    # /episodes/<slug>/comment 500s, and any episode page WITH comments 500s.
    # Reported separately as a new defect; here we verify the episode page's
    # main comment form carries a valid token (1 input, no comments seeded).
    html = me.get("/episodes/p1ep").get_data(as_text=True)
    check("episode page 200", "P1 Episode" in html)
    check_csrf_page("episode page", html, min_inputs=1)

    print("== P1 #2: human_handle trust gate ==")
    # unlinked muse: self-assertion rejected, profile stays clean
    priv_u, fm_u = register_muse(client, "UnlinkedMuse")
    body = signed_body(priv_u, "identity_update", fm_u, visibility="linked")
    r = client.post("/api/identity/update", json=body, environ_base=fresh_ip())
    assert r.status_code == 200 and r.get_json()["ok"], r.get_json()
    body = signed_body(priv_u, "identity_update", fm_u, human_handle="SomeRealHuman")
    r = client.post("/api/identity/update", json=body, environ_base=fresh_ip())
    d = r.get_json() or {}
    profile = client.get(f"/api/identity/{fm_u}").get_json()["identity"]
    check("unlinked muse self-asserting human_handle is REJECTED",
          r.status_code in (400, 403) and not d.get("ok"),
          f"got {r.status_code}: {d}")
    check("public profile does not expose the self-asserted handle",
          profile.get("human_handle") in (None, ""), f"exposed: {profile.get('human_handle')!r}")

    # verified link: human mints code, muse claims it
    me2 = signup_login(client, "P1RealHuman")
    human_row = db.get_identity_by_handle("P1RealHuman")
    code, _exp = db.create_link_code(human_row["fm_id"])
    priv_m, fm_m = register_muse(client, "LinkedMuse")
    body = signed_body(priv_m, "link_muse", fm_m, code=code)
    r = client.post("/api/link_muse", json=body, environ_base=fresh_ip())
    assert r.status_code == 200 and r.get_json()["ok"], (r.status_code, r.get_data(as_text=True)[:300])

    # linked muse asserting a WRONG handle -> rejected
    body = signed_body(priv_m, "identity_update", fm_m,
                       visibility="linked", human_handle="EvilImpostor")
    r = client.post("/api/identity/update", json=body, environ_base=fresh_ip())
    d = r.get_json() or {}
    check("linked muse asserting someone else's handle is REJECTED",
          r.status_code in (400, 403) and not d.get("ok"),
          f"got {r.status_code}: {d}")

    # linked muse asserting the TRUE linked handle -> accepted
    body = signed_body(priv_m, "identity_update", fm_m,
                       visibility="linked", human_handle="P1RealHuman")
    r = client.post("/api/identity/update", json=body, environ_base=fresh_ip())
    d = r.get_json() or {}
    check("linked muse asserting the true linked handle is ACCEPTED",
          r.status_code == 200 and d.get("ok"), f"got {r.status_code}: {d}")
    profile = client.get(f"/api/identity/{fm_m}").get_json()["identity"]
    check("public profile exposes the verified linked handle",
          profile.get("human_handle") == "P1RealHuman",
          f"exposed: {profile.get('human_handle')!r}")

    # clearing is still allowed
    body = signed_body(priv_m, "identity_update", fm_m, human_handle="")
    r = client.post("/api/identity/update", json=body, environ_base=fresh_ip())
    check("clearing human_handle with empty string still works",
          r.status_code == 200 and (r.get_json() or {}).get("ok"),
          f"got {r.status_code}: {r.get_json()}")

    # re-assert, then unlink -> stale handle is cleared
    body = signed_body(priv_m, "identity_update", fm_m,
                       visibility="linked", human_handle="P1RealHuman")
    r = client.post("/api/identity/update", json=body, environ_base=fresh_ip())
    assert r.status_code == 200, r.get_json()
    db.unlink("muse", muse_fm_id=fm_m)
    profile = client.get(f"/api/identity/{fm_m}").get_json()["identity"]
    check("unlink clears the muse's stored human_handle",
          profile.get("human_handle") in (None, ""),
          f"stale: {profile.get('human_handle')!r}")
    # after unlink, self-assertion is rejected again
    body = signed_body(priv_m, "identity_update", fm_m, human_handle="P1RealHuman")
    r = client.post("/api/identity/update", json=body, environ_base=fresh_ip())
    check("post-unlink self-assertion is REJECTED",
          r.status_code in (400, 403), f"got {r.status_code}")

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
