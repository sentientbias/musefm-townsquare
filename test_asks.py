#!/usr/bin/env python3
"""
Tests for the Human Asks board (asks.py):
- schema creation is idempotent and self-healing
- validation: title/description length, reward cap, kinds, statuses
- muse lifecycle: create -> claim -> done, Signal awarded to the claimer
- human-session create (mirrors /submit's session pattern: signup + login,
  then the asker's session fm_id feeds create_ask as asker_kind="human")
- claim by a second muse is rejected; done by a non-asker is rejected
- tampered/unsigned musefm-v1 bodies fail verification (signed-write discipline)

NOTE: the HTTP routes (GET /asks, /api/asks, /api/asks/<id>/claim|done) are
delivered as an app.py patch spec (they touch the shared Flask app) — so
these tests exercise asks.py directly and emulate exactly what the route
layer does on done:
    fm_id, handle, reward = asks.mark_done(db, aid, kind, ref)
    db.award(fm_id, handle, reward, "ask", "ask", str(aid))

Run:  .venv/bin/python test_asks.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import base64
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import asks
from identity import IdentityError, signed_body, verify_signed_body

TEST_DB = "/tmp/test-townsquare-asks.db"

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
    from db import Database, ensure_human_auth_schema
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)  # mirrors app startup
    asks.ensure_asks_schema(appmod.db)
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
    return {"REMOTE_ADDR": "10.99.1.%d" % _ip_counter[0]}


def signup_login(handle, password="supersecret1"):
    """Mirror /submit's human session pattern: signup -> login, then read
    the session's fm_id exactly like current_session_identity() does."""
    human = appmod.app.test_client()
    r = human.post("/signup", data={"handle": handle, "password": password,
                                    "password_confirm": password},
                   environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    r = human.post("/login", data={"handle": handle, "password": password},
                   environ_base=fresh_ip())
    assert r.status_code == 302, r.get_data(as_text=True)
    # read the session's fm_id exactly like current_session_identity() does
    with human.session_transaction() as sess:
        fm_id = sess.get("fm_id")
    assert fm_id, "no fm_id in human session"
    return human, fm_id


def expect_value_error(name, fn, *args, **kw):
    try:
        fn(*args, **kw)
    except ValueError:
        check(name, True)
        return
    except Exception as e:  # noqa: BLE001 - any other error is also a fail
        check(name, False, f"wrong exception: {type(e).__name__}: {e}")
        return
    check(name, False, "accepted, expected ValueError")


def main():
    client = setup()
    db = appmod.db

    print("== schema: idempotent + self-healing ==")
    asks.ensure_asks_schema(db)  # second call must be a no-op
    asks.ensure_asks_schema(db)
    cols = [r["name"] for r in db.db.execute("PRAGMA table_info(asks)")]
    check("asks table has all columns",
          set(["id", "asker_kind", "asker_ref", "title", "description",
               "signal_reward", "status", "claimed_by_fm_id",
               "claimed_by_handle", "created_at", "done_at"]) <= set(cols),
          str(cols))
    check("rowcount unchanged by re-ensure", len(asks.list_asks(db)) == 0)

    print("== create validation ==")
    aid = asks.create_ask(db, "muse", "fm_asker1", "Summarize this thread",
                          "a short recap, please", signal_reward=8)
    check("create -> int id", isinstance(aid, int) and aid > 0)
    a = asks.get_ask(db, aid)
    check("stored fields",
          a["title"] == "Summarize this thread" and
          a["description"] == "a short recap, please" and
          a["signal_reward"] == 8 and a["status"] == "open" and
          a["asker_kind"] == "muse" and a["asker_ref"] == "fm_asker1" and
          a["claimed_by_fm_id"] is None and a["done_at"] is None, str(a))
    expect_value_error("blank title rejected",
                       asks.create_ask, db, "muse", "fm_x", "   ")
    expect_value_error("title >120 rejected",
                       asks.create_ask, db, "muse", "fm_x", "t" * 121)
    check("title exactly 120 ok",
          asks.create_ask(db, "muse", "fm_x", "t" * 120) > 0)
    expect_value_error("description >2000 rejected",
                       asks.create_ask, db, "muse", "fm_x", "ok", "d" * 2001)
    expect_value_error("bad asker_kind rejected",
                       asks.create_ask, db, "robot", "fm_x", "ok")
    expect_value_error("blank asker_ref rejected",
                       asks.create_ask, db, "muse", "  ", "ok")

    print("== reward cap (nonfinancial framing: Signal only, capped) ==")
    for bad in [0, -3, 21, 100, "lots", None, ""]:
        expect_value_error("reward %r rejected" % (bad,),
                           asks.create_ask, db, "muse", "fm_x", "ok", "", bad)
    check("reward 1 ok", asks.create_ask(db, "muse", "fm_x", "ok",
                                        signal_reward=1) > 0)
    check("reward 20 ok (cap)",
          asks.create_ask(db, "muse", "fm_x", "ok", signal_reward=20) > 0)
    check("default reward is 5",
          asks.get_ask(db, asks.create_ask(db, "muse", "fm_x", "ok"))
          ["signal_reward"] == 5)
    check("string '7' coerces to 7",
          asks.get_ask(db, asks.create_ask(db, "muse", "fm_x", "ok",
                                           signal_reward="7"))["signal_reward"] == 7)

    print("== muse lifecycle: create -> claim -> done + Signal ==")
    priv_asker, fm_asker = register(client, "AskPoster")
    priv_doer, fm_doer = register(client, "HelpfulMuse")
    before = db.lifetime_points(fm_doer)
    aid = asks.create_ask(db, "muse", fm_asker, "Make me a birthday short",
                          "my friend turns 30 on Friday", signal_reward=12)
    claimed = asks.claim_ask(db, aid, fm_doer, "HelpfulMuse")
    check("claim -> status claimed, claimer recorded",
          claimed["status"] == "claimed" and
          claimed["claimed_by_fm_id"] == fm_doer and
          claimed["claimed_by_handle"] == "HelpfulMuse", str(claimed))
    # the route layer on done:
    fm_id, handle, reward = asks.mark_done(db, aid, "muse", fm_asker)
    check("mark_done returns claimer triple",
          (fm_id, handle, reward) == (fm_doer, "HelpfulMuse", 12),
          str((fm_id, handle, reward)))
    paid = db.award(fm_id, handle, reward, "ask", "ask", str(aid))
    check("db.award paid out", paid == 12, str(paid))
    check("claimer lifetime Signal rose by the reward",
          db.lifetime_points(fm_doer) == before + 12,
          f"before={before} after={db.lifetime_points(fm_doer)}")
    done = asks.get_ask(db, aid)
    check("done ask has status + done_at",
          done["status"] == "done" and done["done_at"], str(done))
    hist = db.reward_history(fm_doer, limit=5)
    check("reward ledger row reason/ref",
          any(r["reason"] == "ask" and r["ref_type"] == "ask" and
              r["ref_id"] == str(aid) for r in hist), str(hist))
    # idempotent payout: route re-run can't double-pay (UNIQUE constraint)
    check("repeat award returns 0 (no double Signal)",
          db.award(fm_id, handle, reward, "ask", "ask", str(aid)) == 0)

    print("== claim by a second muse rejected ==")
    aid2 = asks.create_ask(db, "muse", fm_asker, "Another favor")
    priv_rival, fm_rival = register(client, "RivalMuse")
    asks.claim_ask(db, aid2, fm_doer, "HelpfulMuse")
    expect_value_error("second claim rejected",
                       asks.claim_ask, db, aid2, fm_rival, "RivalMuse")
    expect_value_error("claim of unknown ask rejected",
                       asks.claim_ask, db, 999999, fm_rival, "RivalMuse")
    expect_value_error("claim without identity rejected",
                       asks.claim_ask, db, aid2, "", "")
    expect_value_error("claim of done ask rejected",
                       asks.claim_ask, db, aid, fm_rival, "RivalMuse")

    print("== done by non-asker rejected ==")
    aid3 = asks.create_ask(db, "muse", fm_asker, "Third favor")
    asks.claim_ask(db, aid3, fm_doer, "HelpfulMuse")
    expect_value_error("done by different muse rejected",
                       asks.mark_done, db, aid3, "muse", fm_rival)
    expect_value_error("done by human ref on muse ask rejected",
                       asks.mark_done, db, aid3, "human", fm_asker)
    expect_value_error("done on open (unclaimed) ask rejected",
                       asks.mark_done, db,
                       asks.create_ask(db, "muse", fm_asker, "open one"),
                       "muse", fm_asker)
    expect_value_error("done on unknown ask rejected",
                       asks.mark_done, db, 999999, "muse", fm_asker)

    print("== human-session create (mirrors /submit session pattern) ==")
    human, human_fm = signup_login("HumanAsker")
    hid = asks.create_ask(db, "human", human_fm, "Birthday short for me",
                          "make it cheerful", signal_reward=5)
    ha = asks.get_ask(db, hid)
    check("human ask stored with kind=human, session fm_id ref",
          ha["asker_kind"] == "human" and ha["asker_ref"] == human_fm,
          str(ha))
    # full human flow: human posts, muse claims, human marks done
    asks.claim_ask(db, hid, fm_doer, "HelpfulMuse")
    before_h = db.lifetime_points(fm_doer)
    fm_id, handle, reward = asks.mark_done(db, hid, "human", human_fm)
    paid = db.award(fm_id, handle, reward, "ask", "ask", str(hid))
    check("human flow: muse earned Signal", paid == 5 and
          db.lifetime_points(fm_doer) == before_h + 5)
    expect_value_error("muse can't mark human's ask done",
                       asks.mark_done, db,
                       asks.create_ask(db, "human", human_fm, "mine"),
                       "muse", fm_doer)

    print("== unsigned muse write rejected (signed-write discipline) ==")
    priv_bad, fm_bad = register(client, "BadActor")
    body = signed_body(priv_bad, "ask_create", fm_bad,
                       title="legit", description="x", signal_reward="5")
    body["title"] = "tampered"  # mutate after signing
    try:
        verify_signed_body(body, db, expected_action="ask_create")
        check("tampered body rejected", False, "verified!")
    except IdentityError:
        check("tampered body rejected", True)
    # wrong action label
    body2 = signed_body(priv_bad, "post", fm_bad, title="x")
    try:
        verify_signed_body(body2, db, expected_action="ask_create")
        check("wrong-action body rejected", False, "verified!")
    except IdentityError:
        check("wrong-action body rejected", True)
    # missing signature entirely
    try:
        verify_signed_body({"action": "ask_create", "fm_id": fm_bad,
                            "title": "x"}, db, expected_action="ask_create")
        check("unsigned body rejected", False, "verified!")
    except IdentityError:
        check("unsigned body rejected", True)

    print("== list_asks filters ==")
    opens = asks.list_asks(db, status="open")
    dones = asks.list_asks(db, status="done")
    claimeds = asks.list_asks(db, status="claimed")
    check("open filter only open",
          all(a["status"] == "open" for a in opens) and len(opens) >= 1)
    check("done filter only done",
          all(a["status"] == "done" for a in dones) and len(dones) == 2,
          str(len(dones)))
    check("claimed filter only claimed",
          all(a["status"] == "claimed" for a in claimeds) and
          len(claimeds) >= 1)
    check("newest first", opens[0]["id"] > opens[-1]["id"]
          if len(opens) > 1 else True)
    all_a = asks.list_asks(db)
    check("unfiltered returns all",
          len(all_a) == len(opens) + len(dones) + len(claimeds))
    expect_value_error("bad status filter rejected",
                       asks.list_asks, db, "bogus")
    check("get unknown -> None", asks.get_ask(db, 424242) is None)
    check("get garbage -> None", asks.get_ask(db, "abc") is None)

    print("== events sink guard ==")
    if asks._log_event is None:
        check("board works with no events module", True)
    else:
        # the sink exists in this checkout: ask_posted/ask_claimed rows
        # should have landed, and board ops must not depend on the sink
        r = db._one("SELECT COUNT(*) c FROM events WHERE type='ask_posted'")
        check("ask_posted event logged", r and r["c"] >= 1, str(r))
        r = db._one("SELECT COUNT(*) c FROM events WHERE type='ask_claimed'"
                    " AND ref_type='ask'")
        check("ask_claimed event logged with ref_type=ask",
              r and r["c"] >= 1, str(r))
        # ask_done is not a known type yet: unknown types must be swallowed,
        # never break the board (already covered above by passing tests)

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
