#!/usr/bin/env python3
"""Post takedown tests (2026-10-02, Anthony).

Two new self-delete-only routes:
  POST /api/forum/post/<pid>/delete      (signed musefm-v1, action delete_post)
  POST /api/forum/post/<pid>/web/delete (human session + CSRF)

Hard rule under test: a requester can ONLY delete their own posts.

Agent path:
  1. owner deletes own post -> 200, post + comments gone
  2. cross-owner delete -> 403, victim post and comments survive
  3. X-Agent-Key + claimed handle -> rejected, victim post survives
     (regression: the shared-key claimed-handle path must not work here)
  4. wrong signed action -> 401
  5. unsigned / tampered body -> 401
  6. unknown pid -> 404; double delete -> first 200, second 404
  7. 11th delete in the window -> 429 (post_delete bucket, 10/hr)
Member path:
  8. member deletes own post (valid CSRF) -> 200, post gone
  9. member deletes another member's post -> 403, survives
 10. missing / bad CSRF -> 403
 11. anonymous POST -> 401
 12. delete affordance renders for the author, not for strangers
Shared:
 13. flags on a deleted post survive in post_flags (audit trail)
 14. reactions / signals / mentions for the deleted post are cleaned

Run: python3 test_post_delete_2026_10_02.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import base64
import json
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-postdelete.db"
os.environ["AGENT_KEY"] = "testkey123"
os.environ["SESSION_SECRET"] = "testsecret"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


_ip = [0]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.99.3.%d" % _ip[0]}


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def fresh_keypair():
    priv = Ed25519PrivateKey.generate()
    return (b64u(priv.private_bytes_raw()),
            b64u(priv.public_key().public_bytes_raw()))


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def register(client, handle):
    priv_b64, pub_b64 = fresh_keypair()
    r = client.post("/api/identity/register",
                    json={"handle": handle, "public_key": pub_b64},
                    environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    return priv_b64, r.get_json()["fm_id"]


def signed_post(client, priv, fm_id, title="hello", body="world", ip=None):
    body_ = signed_body(priv, "post", fm_id, community="lobby",
                        title=title, body=body, flair="discussion")
    r = client.post("/api/forum/post", json=body_,
                    environ_base=ip or fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    return r.get_json()["id"]


def signed_comment(client, priv, fm_id, pid, text="a comment"):
    b = signed_body(priv, "comment", fm_id, post_id=pid, body=text)
    r = client.post("/api/forum/comment", json=b, environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    return r.get_json()["id"]


def signed_delete(client, priv, fm_id, pid, ip=None, action="delete_post",
                  tamper=False):
    b = signed_body(priv, action, fm_id)
    if tamper:
        b["signature"] = b["signature"][:-2] + ("AA" if
                                                not b["signature"].endswith("AA")
                                                else "BB")
    return client.post(f"/api/forum/post/{pid}/delete", json=b,
                       environ_base=ip or fresh_ip())


def login_as(client, fm_id):
    with client.session_transaction() as s:
        s["fm_id"] = fm_id
        s["csrf_token"] = "tok123"


def main():
    client = setup()
    db = appmod.db
    a_priv, a_fm = register(client, "TakedownA")
    b_priv, b_fm = register(client, "TakedownB")
    h_priv, h_fm = register(client, "TakedownHuman")
    o_priv, o_fm = register(client, "TakedownOther")

    # ---------- agent path ----------
    print("== agent path ==")
    pid = signed_post(client, a_priv, a_fm, title="delete me",
                      body="bye @TakedownB")
    cid = signed_comment(client, b_priv, b_fm, pid, "a reply")
    r = signed_delete(client, a_priv, a_fm, pid)
    d = r.get_json()
    check("owner delete -> 200", r.status_code == 200 and d["deleted"],
          (r.status_code, d))
    check("post row gone", db.get_post(pid) is None)
    check("comments gone", db.get_comment(cid) is None)

    # cross-owner
    vpid = signed_post(client, b_priv, b_fm, title="victim thread",
                       body="do not touch")
    vcid = signed_comment(client, a_priv, a_fm, vpid, "victim reply")
    before = db.get_post(vpid)
    r = signed_delete(client, a_priv, a_fm, vpid)
    check("cross-owner delete -> 403", r.status_code == 403, r.status_code)
    check("victim post survives", db.get_post(vpid) == before)
    check("victim comments survive", db.get_comment(vcid) is not None)

    # X-Agent-Key claimed-handle regression
    r = client.post(f"/api/forum/post/{vpid}/delete",
                    json={"handle": "TakedownB", "action": "delete_post"},
                    headers={"X-Agent-Key": "testkey123"},
                    environ_base=fresh_ip())
    check("agent-key claimed handle rejected",
          r.status_code in (401, 403), r.status_code)
    check("victim post survives agent-key", db.get_post(vpid) is not None)

    # wrong action
    r = signed_delete(client, a_priv, a_fm, vpid, action="flag_post")
    check("wrong action -> 401", r.status_code == 401, r.status_code)

    # unsigned / tampered
    r = client.post(f"/api/forum/post/{vpid}/delete",
                    json={"action": "delete_post", "fm_id": a_fm},
                    environ_base=fresh_ip())
    check("unsigned body -> 401", r.status_code == 401, r.status_code)
    r = signed_delete(client, a_priv, a_fm, vpid, tamper=True)
    check("tampered signature -> 401", r.status_code == 401, r.status_code)
    check("victim post still survives", db.get_post(vpid) is not None)

    # unknown pid + double delete
    r = signed_delete(client, a_priv, a_fm, 999999)
    check("unknown pid -> 404", r.status_code == 404, r.status_code)
    dpid = signed_post(client, a_priv, a_fm, title="twice")
    r = signed_delete(client, a_priv, a_fm, dpid)
    check("first delete -> 200", r.status_code == 200, r.status_code)
    r = signed_delete(client, a_priv, a_fm, dpid)
    check("second delete -> 404", r.status_code == 404, r.status_code)

    # rate budget: 10/hr per IP
    rip = fresh_ip()
    pids = [db.create_post("lobby", "TakedownA", f"rate {i}", "x",
                           "discussion")
            for i in range(11)]
    codes = []
    for p in pids:
        r = signed_delete(client, a_priv, a_fm, p, ip=rip)
        codes.append(r.status_code)
    check("first 10 deletes -> 200", codes[:10] == [200] * 10, codes)
    check("11th delete -> 429", codes[10] == 429, codes)
    check("429 carries Retry-After",
          r.headers.get("Retry-After") is not None)
    check("11th post survives the 429",
          db.get_post(pids[10]) is not None)

    # case-insensitive owner match on the agent path
    cpid = db.create_post("lobby", "takedowna", "case", "x", "discussion")
    r = signed_delete(client, a_priv, a_fm, cpid)
    check("agent owner match is case-insensitive", r.status_code == 200,
          r.status_code)

    # ---------- member path ----------
    print("== member path ==")
    me = client  # reuse client, swap sessions via session_transaction
    login_as(me, h_fm)
    hpid = db.create_post("lobby", "TakedownHuman", "my thread", "mine",
                          "discussion")
    r = me.post(f"/api/forum/post/{hpid}/web/delete",
                json={"csrf_token": "tok123"}, environ_base=fresh_ip())
    d = r.get_json()
    check("member deletes own -> 200", r.status_code == 200 and d["ok"],
          (r.status_code, d))
    check("member post gone", db.get_post(hpid) is None)

    opid = db.create_post("lobby", "TakedownOther", "not yours", "nope",
                          "discussion")
    r = me.post(f"/api/forum/post/{opid}/web/delete",
                json={"csrf_token": "tok123"}, environ_base=fresh_ip())
    check("member deletes other's -> 403", r.status_code == 403,
          r.status_code)
    check("other member post survives", db.get_post(opid) is not None)

    r = me.post(f"/api/forum/post/{opid}/web/delete",
                json={}, environ_base=fresh_ip())
    check("missing csrf -> 403", r.status_code == 403, r.status_code)
    r = me.post(f"/api/forum/post/{opid}/web/delete",
                json={"csrf_token": "wrong"}, environ_base=fresh_ip())
    check("bad csrf -> 403", r.status_code == 403, r.status_code)

    anon = appmod.app.test_client()
    r = anon.post(f"/api/forum/post/{opid}/web/delete",
                  json={"csrf_token": "tok123"}, environ_base=fresh_ip())
    check("anonymous -> 401", r.status_code == 401, r.status_code)

    # form-encoded body also works (thread page posts a plain form)
    fpid = db.create_post("lobby", "TakedownHuman", "form thread", "x",
                          "discussion")
    r = me.post(f"/api/forum/post/{fpid}/web/delete",
                data={"csrf_token": "tok123"}, environ_base=fresh_ip())
    check("form-encoded delete -> 200", r.status_code == 200, r.status_code)

    # case-insensitive owner match on the web path
    login_as(me, o_fm)
    wcpid = db.create_post("lobby", "takedownother", "case", "x",
                           "discussion")
    r = me.post(f"/api/forum/post/{wcpid}/web/delete",
                json={"csrf_token": "tok123"}, environ_base=fresh_ip())
    check("web owner match is case-insensitive", r.status_code == 200,
          r.status_code)

    # affordance renders for the author, not for strangers
    login_as(me, h_fm)
    apid = db.create_post("lobby", "TakedownHuman", "affordance", "x",
                          "discussion")
    html = me.get(f"/c/lobby/post/{apid}").get_data(as_text=True)
    check("author sees delete affordance",
          f"/api/forum/post/{apid}/web/delete" in html)
    login_as(me, o_fm)
    html2 = me.get(f"/c/lobby/post/{apid}").get_data(as_text=True)
    check("stranger sees no delete affordance",
          f"/api/forum/post/{apid}/web/delete" not in html2)
    html3 = anon.get(f"/c/lobby/post/{apid}").get_data(as_text=True)
    check("anon sees no delete affordance",
          f"/api/forum/post/{apid}/web/delete" not in html3)

    # ---------- shared ----------
    print("== shared ==")
    fpid2 = db.create_post("lobby", "TakedownHuman", "flagged", "x",
                           "discussion")
    db.flag_post("post", fpid2, o_fm, "TakedownOther", "spam")
    login_as(me, h_fm)
    r = me.post(f"/api/forum/post/{fpid2}/web/delete",
                json={"csrf_token": "tok123"}, environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    row = db._one("SELECT * FROM post_flags WHERE target_type='post'"
                  " AND target_id=?", (fpid2,))
    check("flag history survives deletion", row is not None)

    rpid = db.create_post("lobby", "TakedownHuman", "reacted", "hi @TakedownA",
                          "discussion")
    db.react("post", rpid, o_fm, "TakedownOther", "🔥")
    mentioned, _pts = db.record_mentions(o_fm, "TakedownOther", "post",
                                         str(rpid), "hi @TakedownHuman")
    assert mentioned, "mention row not created"
    assert db.mentions_for("post", str(rpid)), "mentions_for empty"
    r = me.post(f"/api/forum/post/{rpid}/web/delete",
                json={"csrf_token": "tok123"}, environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    rxns = db._q("SELECT * FROM reactions WHERE target_type='post'"
                 " AND target_id=?", (rpid,))
    check("reactions cleaned", rxns == [], rxns)
    sigs = db._q("SELECT * FROM signals WHERE target_type='post'"
                 " AND target_id=?", (rpid,))
    check("signal reactions cleaned", sigs == [], sigs)
    check("mentions cleaned", db.mentions_for("post", str(rpid)) == [])

    print()
    if FAIL:
        print("FAILURES: %d  (%s)" % (len(FAIL), ", ".join(FAIL)))
        sys.exit(1)
    print("ALL %d CHECKS PASSED" % len(PASS))


if __name__ == "__main__":
    main()
