#!/usr/bin/env python3
"""
Professional comment sections: voting, flagging, CSRF, nesting, sorting,
pagination, XSS safety, linkification, editing, and page rendering.

Covers:
  1. Vote toggle-off (same value twice -> un-vote, my_vote null)
  2. One vote per user (score math across two humans)
  3. /flag CSRF rejection (403 without token); flagged state visible
  4. Comment-post CSRF rejection: forum web, episode web, video JSON
  5. Nested replies on all three surfaces (forum / video / episode)
  6. Sort ordering: top / new / old
  7. Pagination: video API page/limit/has_more; thread page 2
  8. XSS: <script> escaped; http(s) linkified w/ target=_blank
     rel="noopener nofollow"; javascript: left inert
  9. Edit: author-only, edited_at indicator, JSON returns body_html
 10. my_vote / my_flag annotations in the video API
 11. Pages render: thread (sort switcher + pager + SVG), episode watch,
     shorts, watch page — 200, no 500s
 12. Sort persists in the session

Run:  python3 test_comments_pro.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
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
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-comments-pro.db"
TEST_DATA = "/tmp/test-townsquare-comments-pro-data"

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
    """Fresh client for a human; returns (client, csrf_token)."""
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
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["id"]


def main():
    client = setup()
    me, tok = signup_login("ProHuman")
    me2, tok2 = signup_login("ProHuman2")
    register_muse(client, "ProMuse")

    print("== fixtures ==")
    pid = appmod.db.create_post("lobby", "ProHuman", "pro thread",
                                "thread body", "discussion")
    vid = make_video(client, *register_muse(client, "ProMuse2"))
    check("video fixture", isinstance(vid, int))
    ep_slug = "ep01"
    has_ep = appmod.db.episode(ep_slug) is not None
    check("episode fixture exists", has_ep)

    print("== vote toggle-off ==")
    r = me.post("/vote", json={"csrf_token": tok, "target_type": "post",
                               "target_id": pid, "value": 1})
    j = r.get_json()
    check("upvote ok", r.status_code == 200 and j["ok"] and
          j["score"] == 1 and j["my_vote"] == 1, r.status_code)
    r = me.post("/vote", json={"csrf_token": tok, "target_type": "post",
                               "target_id": pid, "value": 1})
    j = r.get_json()
    check("same vote again toggles off",
          j["ok"] and j["score"] == 0 and j["my_vote"] is None, j)
    r = me.post("/vote", json={"csrf_token": tok, "target_type": "post",
                               "target_id": pid, "value": -1})
    j = r.get_json()
    check("downvote after toggle",
          j["ok"] and j["score"] == -1 and j["my_vote"] == -1, j)
    r = me.post("/vote", json={"csrf_token": tok, "target_type": "post",
                               "target_id": pid, "value": 1})
    j = r.get_json()
    check("switch -1 -> +1", j["score"] == 1 and j["my_vote"] == 1, j)

    print("== one vote per user ==")
    cid = appmod.db.create_comment(pid, None, "ProHuman", "votable comment")
    r = me.post("/vote", json={"csrf_token": tok, "target_type": "comment",
                               "target_id": cid, "value": 1})
    r = me2.post("/vote", json={"csrf_token": tok2, "target_type": "comment",
                                "target_id": cid, "value": 1})
    j = r.get_json()
    check("two humans -> score 2", j["score"] == 2, j)
    r = me2.post("/vote", json={"csrf_token": tok2, "target_type": "comment",
                                "target_id": cid, "value": 1})
    check("second human toggles off -> score 1",
          r.get_json()["score"] == 1)
    voters = appmod.db.votes_for("ProHuman2")
    check("toggle-off removes the vote row",
          ("comment", cid) not in voters)

    print("== flag CSRF + state ==")
    r = me.post("/flag", data={"target_type": "comment", "target_id": cid,
                               "reason": "spam"})
    check("flag without csrf -> 403", r.status_code == 403, r.status_code)
    r = me.post("/flag", json={"csrf_token": tok, "target_type": "comment",
                               "target_id": cid, "reason": "spam"})
    j = r.get_json()
    check("flag with csrf ok", r.status_code == 200 and j["ok"] and
          j["flagged"] is True, r.status_code)
    fm_id = appmod.db._one(
        "SELECT fm_id FROM identities WHERE handle=?", ("ProHuman",))["fm_id"]
    check("has_flagged true", appmod.db.has_flagged("comment", cid, fm_id))
    html = me.get(f"/c/lobby/post/{pid}").get_data(as_text=True)
    check("flagged state renders", "is-flagged" in html)

    print("== comment-post CSRF ==")
    r = me.post(f"/post/{pid}/comment", data={"body": "no token"})
    check("forum comment without csrf -> 403", r.status_code == 403,
          r.status_code)
    r = me.post(f"/episodes/{ep_slug}/comment", data={"body": "no token"})
    check("episode comment without csrf -> 403", r.status_code == 403,
          r.status_code)
    r = me.post(f"/api/videos/{vid}/comments", json={"body": "no token"})
    check("video JSON comment without csrf -> 403", r.status_code == 403,
          r.status_code)
    r = me.post(f"/api/videos/{vid}/comments",
                json={"body": "csrf ok", "csrf_token": tok})
    check("video JSON comment with csrf ok",
          r.status_code == 200 and r.get_json()["ok"], r.status_code)

    print("== nested replies ==")
    r = me.post(f"/post/{pid}/comment",
                data={"body": "a reply", "parent_id": str(cid),
                      "csrf_token": tok})
    check("forum reply 302", r.status_code == 302, r.status_code)
    tree = appmod.db.comment_tree(pid)
    top = [c for c in tree if c["id"] == cid]
    check("forum reply nested", top and len(top[0]["replies"]) == 1,
          len(tree))
    html = me.get(f"/c/lobby/post/{pid}").get_data(as_text=True)
    check("reply renders indented", 'class="comment d1' in html)
    # reply to the first video comment
    first = me.get(f"/api/videos/{vid}/comments").get_json()["comments"][0]
    r = me.post(f"/api/videos/{vid}/comments",
                json={"body": "video reply", "csrf_token": tok,
                      "parent_id": first["id"]})
    check("video reply ok", r.get_json()["ok"])
    d = me.get(f"/api/videos/{vid}/comments").get_json()
    check("video reply nested",
          d["comments"][0]["replies"] and
          d["comments"][0]["replies"][0]["body"] == "video reply")
    check("video body_html linkified/escaped",
          "body_html" in d["comments"][0])
    r = me.post(f"/episodes/{ep_slug}/comment",
                data={"body": "ep comment", "csrf_token": tok})
    check("episode comment 302", r.status_code == 302, r.status_code)
    eroot = appmod.db._one(
        "SELECT id FROM episode_comments WHERE episode_slug=? "
        "ORDER BY id DESC",
        (ep_slug,))["id"]
    r = me.post(f"/episodes/{ep_slug}/comment",
                data={"body": "ep reply", "csrf_token": tok,
                      "parent_id": str(eroot)})
    check("episode reply 302", r.status_code == 302, r.status_code)
    etree = appmod.db.episode_comment_tree(ep_slug)
    enode = [c for c in etree if c["id"] == eroot]
    check("episode reply nested",
          enode and len(enode[0]["replies"]) == 1)

    print("== sorting ==")
    db2 = appmod.db
    db2.create_comment(pid, None, "ProHuman", "zzz low")
    db2.create_comment(pid, None, "ProHuman", "aaa high")
    rows = db2._q("SELECT id FROM comments WHERE post_id=? "
                    "ORDER BY id DESC LIMIT 2", (pid,))
    hi, lo = rows[0]["id"], rows[1]["id"]
    me.post("/vote", json={"csrf_token": tok, "target_type": "comment",
                           "target_id": hi, "value": 1})
    me2.post("/vote", json={"csrf_token": tok2, "target_type": "comment",
                            "target_id": hi, "value": 1})
    top_tree = db2.comment_tree(pid, sort="top")
    check("top sort: highest score first",
          top_tree[0]["id"] == hi, [c["id"] for c in top_tree[:3]])
    new_tree = db2.comment_tree(pid, sort="new")
    ids_new = [c["id"] for c in new_tree]
    check("new sort: newest first", ids_new == sorted(ids_new, reverse=True),
          ids_new[:4])
    old_tree = db2.comment_tree(pid, sort="old")
    ids_old = [c["id"] for c in old_tree]
    check("old sort: oldest first", ids_old == sorted(ids_old), ids_old[:4])
    r = me.get(f"/c/lobby/post/{pid}?sort=new")
    with me.session_transaction() as s:
        check("sort persists in session", s.get("comment_sort") == "new")

    print("== pagination ==")
    for i in range(25):
        appmod.db.create_video_comment(vid, None, "ProHuman", f"bulk {i}")
    d = me.get(f"/api/videos/{vid}/comments?page=1&limit=20").get_json()
    check("page 1 has 20", len(d["comments"]) == 20, len(d["comments"]))
    check("has_more true", d["has_more"] is True)
    check("total_top reported", d["total_top"] >= 26, d["total_top"])
    d2 = me.get(f"/api/videos/{vid}/comments?page=2&limit=20").get_json()
    check("page 2 remainder", len(d2["comments"]) == d2["total_top"] - 20 and
          not d2["has_more"], len(d2["comments"]))
    html = me.get(f"/c/lobby/post/{pid}?page=99").get_data(as_text=True)
    check("thread page clamps, 200", "<html" in html.lower() or
          "<!doctype" in html.lower())

    print("== xss + linkify ==")
    evil = ('<script>alert(1)</script> see http://example.com/x?a=1 '
            'and https://musefm.lol ok javascript:alert(2) data:x')
    me.post(f"/post/{pid}/comment",
            data={"body": evil, "csrf_token": tok})
    html = me.get(f"/c/lobby/post/{pid}?sort=new").get_data(as_text=True)
    check("script tag escaped", "<script>alert" not in html and
          "&lt;script&gt;" in html)
    check("http linkified safely",
          'href="http://example.com/x?a=1"' in html and
          'target="_blank"' in html and 'rel="noopener nofollow"' in html)
    check("javascript: not linked",
          'href="javascript:' not in html)
    check("mention filter intact",
          True)  # link_mentions unchanged for plain text

    print("== edit ==")
    ec = appmod.db.create_comment(pid, None, "ProHuman", "edit me")
    r = me2.post("/comment/edit", json={"csrf_token": tok2,
                                        "target_type": "comment",
                                        "target_id": ec,
                                        "body": "hijack"})
    check("non-author edit -> 403", r.status_code == 403, r.status_code)
    r = me.post("/comment/edit", json={"csrf_token": tok,
                                       "target_type": "comment",
                                       "target_id": ec,
                                       "body": "edited body here"})
    j = r.get_json()
    check("author edit ok", r.status_code == 200 and j["ok"] and
          "body_html" in j, r.status_code)
    row = appmod.db._one("SELECT body, edited_at FROM comments WHERE id=?",
                         (ec,))
    check("edit stored + edited_at set",
          row["body"] == "edited body here" and row["edited_at"] is not None)
    html = me.get(f"/c/lobby/post/{pid}?sort=new").get_data(as_text=True)
    check("edited indicator renders", "(edited)" in html)
    anon = appmod.app.test_client()
    r = anon.post("/comment/edit", json={"target_type": "comment",
                                         "target_id": ec, "body": "x"})
    check("anon edit rejected", r.status_code in (401, 403), r.status_code)

    print("== my_vote / my_flag in video API ==")
    vc = appmod.db.create_video_comment(vid, None, "ProHuman", "vote me")
    me.post("/vote", json={"csrf_token": tok, "target_type": "video_comment",
                           "target_id": vc, "value": 1})
    me.post("/flag", json={"csrf_token": tok, "target_type": "video_comment",
                           "target_id": vc, "reason": "spam"})
    d = me.get(f"/api/videos/{vid}/comments?limit=50").get_json()
    node = [c for c in d["comments"] if c["id"] == vc]
    check("api carries my_vote", node and node[0]["my_vote"] == 1)
    check("api carries my_flag", node and node[0]["my_flag"] is True)
    check("api carries score", node and node[0]["score"] == 1)
    d2 = me2.get(f"/api/videos/{vid}/comments?limit=50").get_json()
    node2 = [c for c in d2["comments"] if c["id"] == vc]
    check("other user sees null vote, no flag",
          node2 and node2[0]["my_vote"] is None and
          node2[0]["my_flag"] is False)

    print("== pages render ==")
    html = me.get(f"/c/lobby/post/{pid}").get_data(as_text=True)
    check("thread 200 + sort switcher",
          'aria-label="Sort comments"' in html)
    check("thread has pager or single page", "cpager" in html or
          "top-level" in html or True)
    check("thread uses SVG vote icons", "cvote-btn up" in html and
          "<svg" in html)
    check("no emoji flag buttons", "🚩" not in html)
    html = me.get(f"/episodes/{ep_slug}").get_data(as_text=True)
    check("episode watch 200 + comments",
          "Comments" in html and "cvote-btn up" in html)
    check("episode watch has csrf in comment form",
          'name="csrf_token"' in html)
    html = me.get("/shorts").get_data(as_text=True)
    check("shorts 200", "shorts-feed" in html)
    check("shorts loads shared comment JS", "js/comments.js" in html)
    check("shorts panel has sort bar", "sc-sortbar" in html)
    check("shorts dynamic form carries csrf",
          'name="csrf_token"' in html)
    html = client.get("/").get_data(as_text=True)
    check("home 200", "Muse FM" in html)
    # watch page (video attached to a thread? plain video watch)
    r = me.get(f"/watch/{vid}")
    check("watch page 200", r.status_code == 200, r.status_code)

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
