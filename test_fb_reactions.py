#!/usr/bin/env python3
"""
Tests for Facebook-style reactions: the classic six, one per identity per
target, toggle semantics, no Signal awarded, signed API + trust-based web
route, widget rendering, and additive migration.

Run:  .venv/bin/python test_fb_reactions.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import base64
import os
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import fb_reactions
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-fbreactions.db"

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


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    from db import Database
    appmod.db = Database(TEST_DB)
    fb_reactions.ensure_fb_reactions_schema(appmod.db)
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
    return {"REMOTE_ADDR": "10.99.0.%d" % _ip_counter[0]}


def fb_react(client, priv, fm_id, target_type, target_id, reaction):
    return client.post("/api/forum/fb_react", json=signed_body(
        priv, "fb_react", fm_id, target_type=target_type,
        target_id=target_id, reaction=reaction), environ_base=fresh_ip())


def rewards_for(fm_id):
    return appmod.db._one(
        "SELECT COUNT(*) c FROM rewards WHERE fm_id=?", (fm_id,))["c"]


def main():
    client = setup()

    print("== schema ==")
    tables = [r[0] for r in appmod.db.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    check("fb_reactions table exists", "fb_reactions" in tables)
    fb_reactions.ensure_fb_reactions_schema(appmod.db)  # idempotent
    check("ensure idempotent", True)

    priv_a, fm_a = register(client, "AliceR")
    priv_b, fm_b = register(client, "BobR")

    r = client.post("/api/forum/post", json=signed_body(
        priv_a, "post", fm_a, community="lobby", title="react me",
        body="hello town", flair="discussion"), environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    pid = r.get_json()["id"]
    r = client.post("/api/forum/comment", json=signed_body(
        priv_b, "comment", fm_b, post_id=pid, body="first!"),
        environ_base=fresh_ip())
    cid = r.get_json()["id"]

    print("== signed add / switch / remove ==")
    r = fb_react(client, priv_a, fm_a, "post", pid, "like")
    d = r.get_json()
    check("add like -> 200/added", r.status_code == 200 and d["action"] == "added", str(d))
    check("counts {like:1}", d["counts"] == {"like": 1} and d["total"] == 1, str(d))
    check("top carries emoji", d["top"][0][1] == "\U0001F44D", str(d["top"]))

    r = fb_react(client, priv_a, fm_a, "post", pid, "like")
    d = r.get_json()
    check("same reaction toggles off", d["action"] == "removed" and d["counts"] == {}, str(d))

    fb_react(client, priv_a, fm_a, "post", pid, "like")
    r = fb_react(client, priv_a, fm_a, "post", pid, "love")
    d = r.get_json()
    check("different reaction switches", d["action"] == "switched" and d["counts"] == {"love": 1}, str(d))
    n = appmod.db._one(
        "SELECT COUNT(*) c FROM fb_reactions WHERE target_type='post' AND target_id=?",
        (pid,))["c"]
    check("one row per identity per target", n == 1, str(n))

    print("== counts / breakdown across identities ==")
    fb_react(client, priv_b, fm_b, "post", pid, "wow")
    d = client.get("/api/forum/post/%d" % pid).get_json()["post"]["fb"]
    check("two identities counted", d["counts"] == {"love": 1, "wow": 1} and d["total"] == 2, str(d))
    # tie -> classic order (love before wow)
    check("top3 tie-breaks by classic order",
          [t[0] for t in d["top"]] == ["love", "wow"], str(d["top"]))

    print("== validation ==")
    for bad in ["yeet", "", "LIKEE", "👍"]:
        r = fb_react(client, priv_a, fm_a, "post", pid, bad)
        check("reject reaction %r -> 400" % bad, r.status_code == 400, str(r.status_code))
    r = fb_react(client, priv_a, fm_a, "post", 424242, "like")
    check("unknown target -> 400", r.status_code == 400, str(r.status_code))
    r = fb_react(client, priv_a, fm_a, "planet", pid, "like")
    check("bad target_type -> 400", r.status_code == 400, str(r.status_code))

    print("== auth ==")
    r = client.post("/api/forum/fb_react",
                    json={"target_type": "post", "target_id": pid, "reaction": "like"},
                    environ_base=fresh_ip())
    check("unsigned -> 401", r.status_code == 401, str(r.status_code))
    body = signed_body(priv_a, "fb_react", fm_a, target_type="post",
                       target_id=pid, reaction="like")
    body["reaction"] = "angry"  # tamper after signing
    r = client.post("/api/forum/fb_react", json=body, environ_base=fresh_ip())
    check("tampered body -> 401", r.status_code == 401, str(r.status_code))
    body2 = signed_body(priv_a, "post", fm_a, target_type="post",
                        target_id=pid, reaction="like")  # wrong action
    r = client.post("/api/forum/fb_react", json=body2, environ_base=fresh_ip())
    check("wrong signed action -> 401", r.status_code == 401, str(r.status_code))

    print("== no Signal for FB reactions ==")
    before_rewards = rewards_for(fm_a)
    before_rewards_b = rewards_for(fm_b)
    before_lifetime = appmod.db.lifetime_points(fm_a)
    fb_react(client, priv_b, fm_b, "post", pid, "haha")   # bob -> alice's post
    fb_react(client, priv_a, fm_a, "comment", cid, "like")  # alice -> bob's comment
    check("no reward rows created",
          rewards_for(fm_a) == before_rewards and rewards_for(fm_b) == before_rewards_b, "")
    check("author lifetime Signal unchanged",
          appmod.db.lifetime_points(fm_a) == before_lifetime, "")

    print("== comments ==")
    r = fb_react(client, priv_a, fm_a, "comment", cid, "sad")
    d = r.get_json()
    check("react on comment -> 200", r.status_code == 200 and d["counts"] == {"sad": 1}, str(d))
    d = client.get("/api/forum/post/%d" % pid).get_json()["post"]
    check("api_post carries fb summary", d["fb"]["total"] == 2, str(d["fb"]))
    cfb = d["comments"][0]["fb"]
    check("comment fb summary correct",
          cfb["counts"] == {"sad": 1} and cfb["mine"] is None, str(cfb))

    print("== trust-based web route ==")
    r = client.post("/fb_react",
                    json={"target_type": "post", "target_id": pid,
                          "reaction": "haha", "handle": "Webby"},
                    environ_base=fresh_ip())
    d = r.get_json()
    check("web JSON react -> 200", r.status_code == 200 and d["action"] == "added"
          and d["mine"] == "haha", str(d))
    r = client.post("/fb_react",
                    json={"target_type": "post", "target_id": pid,
                          "reaction": "haha", "handle": "Webby"},
                    environ_base=fresh_ip())
    check("web toggle off", r.get_json()["action"] == "removed", "")
    r = client.post("/fb_react",
                    data={"target_type": "post", "target_id": str(pid),
                          "reaction": "wow", "handle": "Webby", "next": "/c/lobby"},
                    environ_base=fresh_ip())
    check("web form -> 302 redirect", r.status_code == 302, str(r.status_code))
    check("form redirect target", r.headers.get("Location", "").endswith("/c/lobby"),
          r.headers.get("Location"))
    r = client.post("/fb_react",
                    json={"target_type": "post", "target_id": pid,
                          "reaction": "nope", "handle": "Webby"},
                    environ_base=fresh_ip())
    check("web invalid reaction -> 400", r.status_code == 400, str(r.status_code))

    print("== widget rendering ==")
    html = client.get("/c/lobby/post/%d" % pid).get_data(as_text=True)
    check("picker renders on thread", "rxn-picker" in html and "rxn-opt" in html)
    check("six options rendered", html.count('data-reaction="') >= 6,
          str(html.count('data-reaction="')))
    check("breakdown renders", "rxn-breakdown" in html)
    check("comment widget renders", 'data-target-type="comment"' in html)
    home = client.get("/").get_data(as_text=True)
    check("feed renders compact widget", "rxn-compact" in home and "rxn-count" in home)
    check("reactions.js included", "js/reactions.js" in home)

    print("== migration on legacy DB ==")
    leg = "/tmp/test-townsquare-fblegacy.db"
    if os.path.exists(leg):
        os.remove(leg)
    con = sqlite3.connect(leg)
    con.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, community TEXT, handle TEXT)")
    con.execute("CREATE TABLE comments (id INTEGER PRIMARY KEY, post_id INTEGER, handle TEXT)")
    con.execute("INSERT INTO posts VALUES (1, 'lobby', 'Old')")
    con.commit()

    class _Shim:
        def __init__(self, c):
            self.db = c

    fb_reactions.ensure_fb_reactions_schema(_Shim(con))
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    check("legacy DB gains fb_reactions", "fb_reactions" in tables)
    check("legacy post row intact",
          con.execute("SELECT handle FROM posts WHERE id=1").fetchone()[0] == "Old")
    con.close()
    os.remove(leg)

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
