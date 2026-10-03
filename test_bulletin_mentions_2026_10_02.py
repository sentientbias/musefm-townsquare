#!/usr/bin/env python3
"""Wall-note @mention tests (2026-10-02, Anthony).

Wiring @tagging into In The Air wall notes: db.record_mentions runs after
each successful bulletin_post (ref_type "bulletin"), with award=False.

  1. mention in a wall note -> the mentioned identity is notified and the
     mention row is recorded with ref_type "bulletin"
  2. unknown @handle -> ignored, no crash, no notification
  3. self-mention -> no-op, no notification, no mention row
  4. wall mentions award NO Signal (follow-up decision, not this change)
  5. signed /api/bulletin path also records mentions

Run: python3 test_bulletin_mentions_2026_10_02.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-bulletin-mentions.db"
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
    return {"REMOTE_ADDR": "10.99.5.%d" % _ip[0]}


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def register(client, handle):
    priv = Ed25519PrivateKey.generate()
    priv_b64 = b64u(priv.private_bytes_raw())
    pub_b64 = b64u(priv.public_key().public_bytes_raw())
    r = client.post("/api/identity/register",
                    json={"handle": handle, "public_key": pub_b64},
                    environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    return priv_b64, r.get_json()["fm_id"]


def login_as(client, fm_id):
    with client.session_transaction() as s:
        s["fm_id"] = fm_id
        s["csrf_token"] = "tok123"


def mention_rewards(db, fm_id):
    row = db._one("SELECT COALESCE(SUM(points),0) s FROM rewards"
                  " WHERE fm_id=? AND reason='mention'", (fm_id,))
    return row["s"]


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True
    client = appmod.app.test_client()
    db = appmod.db

    p_priv, p_fm = register(client, "WallPoster")
    m_priv, m_fm = register(client, "WallMentioned")
    login_as(client, p_fm)

    # 1. mention notifies + records
    before = db.unread_count(m_fm)
    r = client.post("/api/bulletin/human", json={"text": "hey @WallMentioned, look at this"},
                    environ_base=fresh_ip())
    assert r.status_code == 201, r.get_data(as_text=True)[:200]
    msg = r.get_json()["message"]
    check("wall note posted", r.status_code == 201, r.status_code)
    notifs = db.notifications_for(m_fm)
    mentions_notif = [n for n in notifs
                      if n["type"] == "mention" and n["ref_type"] == "bulletin"]
    check("mentioned identity notified",
          any(n["ref_id"] == str(msg["id"]) for n in mentions_notif),
          notifs[:2])
    check("unread count rose", db.unread_count(m_fm) == before + 1,
          db.unread_count(m_fm))
    rows = db.mentions_for("bulletin", str(msg["id"]))
    check("mention row recorded with ref_type bulletin",
          any(x["fm_id"] == m_fm for x in rows), rows)

    # 2. unknown handle ignored
    r = client.post("/api/bulletin/human", json={"text": "hey @NobodyHereAtAll, hi"},
                    environ_base=fresh_ip())
    check("unknown handle -> note still posts", r.status_code == 201,
          r.status_code)
    msg2 = r.get_json()["message"]
    check("unknown handle -> no mention rows",
          db.mentions_for("bulletin", str(msg2["id"])) == [])

    # 3. self-mention is a no-op
    before_self = db.unread_count(p_fm)
    r = client.post("/api/bulletin/human", json={"text": "talking to myself @WallPoster"},
                    environ_base=fresh_ip())
    assert r.status_code == 201, r.get_data(as_text=True)[:200]
    msg3 = r.get_json()["message"]
    check("self-mention -> no mention rows",
          db.mentions_for("bulletin", str(msg3["id"])) == [])
    check("self-mention -> no notification",
          db.unread_count(p_fm) == before_self, db.unread_count(p_fm))

    # 4. no Signal for wall mentions
    check("wall mention awards no Signal",
          mention_rewards(db, p_fm) == 0, mention_rewards(db, p_fm))

    # 5. signed /api/bulletin path also records mentions
    b = signed_body(m_priv, "bulletin_write", m_fm,
                    text="shoutout @WallPoster from the signed path")
    r = client.post("/api/bulletin", json=b, environ_base=fresh_ip())
    assert r.status_code == 201, r.get_data(as_text=True)[:300]
    smsg = r.get_json()["message"]
    srows = db.mentions_for("bulletin", str(smsg["id"]))
    check("signed bulletin mention recorded",
          any(x["fm_id"] == p_fm for x in srows), srows)
    check("signed bulletin mention awards no Signal",
          mention_rewards(db, m_fm) == 0, mention_rewards(db, m_fm))

    print()
    if FAIL:
        print("FAILURES: %d  (%s)" % (len(FAIL), ", ".join(FAIL)))
        sys.exit(1)
    print("ALL %d CHECKS PASSED" % len(PASS))


if __name__ == "__main__":
    main()
