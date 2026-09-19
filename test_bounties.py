#!/usr/bin/env python3
"""
Tests for the nonfinancial Bounty Board (bounties.py + route patch spec):
- module: schema idempotency, create/get/list, reward cap, status rules
- HTTP: full lifecycle create -> claim -> complete with Signal awarded,
  rule rejections, unsigned writes rejected, /bounties page renders.

The routes below are the exact app.py patch-spec source, exec'd against
the test app with app.py's own helpers — what is tested here is exactly
what the patch adds to app.py.

Run:  .venv/bin/python test_bounties.py
Throwaway SQLite db + Flask test client + temp DATA_DIR.
Nothing touches townsquare.db.
"""
import base64
import os
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import bounties
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-bounties.db"
TEST_DATA = "/tmp/test-townsquare-bounties-data"

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


# Exact app.py patch-spec source for the bounty routes. Tested verbatim:
# the parent applies this text to app.py (anchors in the patch spec).
BOUNTY_ROUTES = '''
@app.route("/bounties")
def bounties_page():
    bounties.ensure_bounty_schema(db)
    open_bounties = bounties.list_bounties(db, status="open", limit=100)
    return render_template("bounties.html", bounties=open_bounties)


@app.route("/api/bounties")
def api_list_bounties():
    bounties.ensure_bounty_schema(db)
    status = request.args.get("status")
    try:
        limit = min(100, max(1, int(request.args.get("limit", 50))))
    except ValueError:
        limit = 50
    try:
        items = bounties.list_bounties(db, status=status, limit=limit)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "bounties": items})


@app.route("/api/bounties", methods=["POST"])
@require_agent_or_signature("bounty")
def api_create_bounty():
    hit = check_limit("bounty", 5)
    if hit:
        return hit
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    if not g.author_identity:
        return api_error("bounty writes require a signed musefm-v1 identity")
    try:
        title = _fs(data, "title")
        description = _fs(data, "description")
        try:
            reward = int(data.get("signal_reward", 10))
        except (TypeError, ValueError):
            raise ValueError("signal_reward must be an integer")
        b = bounties.create_bounty(db, g.author_identity["fm_id"],
                                   g.author_handle, title, description, reward)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "bounty": b})


@app.route("/api/bounties/<sqlite_int:bid>/claim", methods=["POST"])
@require_agent_or_signature("bounty_claim")
def api_claim_bounty(bid):
    hit = check_limit("bounty_claim", 10)
    if hit:
        return hit
    if not g.author_identity:
        return api_error("bounty claims require a signed musefm-v1 identity")
    try:
        b = bounties.claim_bounty(db, bid, g.author_identity["fm_id"],
                                  g.author_handle)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "bounty": b})


@app.route("/api/bounties/<sqlite_int:bid>/complete", methods=["POST"])
@require_agent_or_signature("bounty_complete")
def api_complete_bounty(bid):
    hit = check_limit("bounty_complete", 10)
    if hit:
        return hit
    if not g.author_identity:
        return api_error("bounty completion requires a signed musefm-v1 identity")
    try:
        claimer_fm_id, claimer_handle, reward = bounties.complete_bounty(
            db, bid, g.author_identity["fm_id"])
    except ValueError as e:
        return api_error(str(e))
    # Signal, not money: the claimer's reputation grows by the bounty reward.
    signal_earned = db.award(claimer_fm_id, claimer_handle, reward,
                             "bounty", "bounty", str(bid))
    bounties._log_bounty_done(db, claimer_fm_id, bid)
    return jsonify({"ok": True, "bounty_id": bid,
                    "claimer_fm_id": claimer_fm_id,
                    "claimer_handle": claimer_handle,
                    "signal_earned": signal_earned})


@app.route("/api/bounties/<sqlite_int:bid>/cancel", methods=["POST"])
@require_agent_or_signature("bounty_cancel")
def api_cancel_bounty(bid):
    hit = check_limit("bounty_cancel", 10)
    if hit:
        return hit
    if not g.author_identity:
        return api_error("bounty cancellation requires a signed musefm-v1 identity")
    try:
        b = bounties.cancel_bounty(db, bid, g.author_identity["fm_id"])
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "bounty": b})
'''


def install_routes():
    if "api_list_bounties" in appmod.app.view_functions:
        return  # patch already applied to app.py — test the real routes
    ns = {
        "app": appmod.app, "db": appmod.db, "bounties": bounties,
        "render_template": appmod.render_template, "request": appmod.request,
        "g": appmod.g, "jsonify": appmod.jsonify,
        "require_agent_or_signature": appmod.require_agent_or_signature,
        "check_limit": appmod.check_limit, "json_body": appmod.json_body,
        "_fs": appmod._fs, "api_error": appmod.api_error,
    }
    exec(compile(BOUNTY_ROUTES, "<bounty_routes>", "exec"), ns)


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    from db import Database, ensure_human_auth_schema
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)  # mirrors app startup
    bounties.ensure_bounty_schema(appmod.db)
    appmod.DATA_DIR = TEST_DATA
    appmod.app.config["TESTING"] = True
    install_routes()
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


def main():
    client = setup()
    db = appmod.db

    print("== module: schema idempotency + validation ==")
    bounties.ensure_bounty_schema(db)
    bounties.ensure_bounty_schema(db)
    cols = {r["name"] for r in
            db.db.execute("PRAGMA table_info(bounties)").fetchall()}
    for c in ("id", "title", "description", "poster_fm_id", "poster_handle",
              "signal_reward", "status", "claimed_by_fm_id",
              "claimed_by_handle", "created_at", "closed_at"):
        check("schema has %s" % c, c in cols)
    check("default reward -> 10", bounties.validate_signal_reward(None) == 10)
    check("boundary 1 ok", bounties.validate_signal_reward(1) == 1)
    check("boundary 100 ok", bounties.validate_signal_reward(100) == 100)
    for bad in [0, 101, -5, "abc"]:
        try:
            bounties.validate_signal_reward(bad)
            check("reward %r rejected" % (bad,), False, "accepted!")
        except ValueError:
            check("reward %r rejected" % (bad,), True)

    print("== module: create/get/list ==")
    b = bounties.create_bounty(db, "fm_poster", "PosterMuse", "Test bounty",
                               "Do the thing.", 25)
    check("create returns row", b["id"] == 1 and b["title"] == "Test bounty"
          and b["status"] == "open" and b["signal_reward"] == 25
          and b["closed_at"] is None and b["created_at"], str(b))
    got = bounties.get_bounty(db, 1)
    check("get_bounty round-trip", got == b)
    check("unknown id -> None", bounties.get_bounty(db, 999) is None)
    bounties.create_bounty(db, "fm_poster", "PosterMuse", "Second", "", 5)
    all_b = bounties.list_bounties(db)
    check("list newest-first", [x["id"] for x in all_b] == [2, 1], str(all_b))
    check("list limit clamps", len(bounties.list_bounties(db, limit=1)) == 1)
    check("status filter open", len(bounties.list_bounties(db, status="open")) == 2)
    try:
        bounties.list_bounties(db, status="bogus")
        check("bad status filter -> ValueError", False, "no error")
    except ValueError:
        check("bad status filter -> ValueError", True)
    for bad_create in [("",), ("x" * 121,)]:
        try:
            bounties.create_bounty(db, "fm_p", "H", bad_create[0][:121] if len(bad_create[0]) > 1 else "")
            check("bad title rejected", False, "no error")
        except ValueError:
            check("bad title rejected", True)
    try:
        bounties.create_bounty(db, "fm_p", "H", "ok", "d" * 2001)
        check("long description rejected", False, "no error")
    except ValueError:
        check("long description rejected", True)
    try:
        bounties.create_bounty(db, "fm_p", "H", "ok", "", 150)
        check("reward cap 100 enforced", False, "accepted!")
    except ValueError:
        check("reward cap 100 enforced", True)
    db.db.execute("DELETE FROM bounties")
    db.db.commit()

    print("== unsigned writes rejected ==")
    r = client.post("/api/bounties", json={"title": "hi"},
                    environ_base=fresh_ip())
    check("unsigned POST -> 401", r.status_code == 401, str(r.status_code))
    r = client.post("/api/bounties/1/claim", json={},
                    environ_base=fresh_ip())
    check("unsigned claim -> 401", r.status_code == 401, str(r.status_code))

    print("== full lifecycle via HTTP ==")
    priv_a, fm_a = register(client, "BountyPoster")
    priv_b, fm_b = register(client, "BountyHunter")
    priv_c, fm_c = register(client, "BountyWatcher")

    r = client.post("/api/bounties", json=signed_body(
        priv_a, "bounty", fm_a, title="Review my skill",
        description="Read it, run it, report bugs.", signal_reward=25),
        environ_base=fresh_ip())
    j = r.get_json()
    check("create -> 200", r.status_code == 200,
          "%s %s" % (r.status_code, r.get_data(as_text=True)[:200]))
    bid = j["bounty"]["id"]
    check("create echoes fields",
          j["bounty"]["title"] == "Review my skill" and
          j["bounty"]["status"] == "open" and
          j["bounty"]["signal_reward"] == 25 and
          j["bounty"]["poster_handle"] == "BountyPoster", str(j["bounty"]))

    # invalid creates -> 400
    for payload, label in [
            ({"title": "t", "signal_reward": 101}, "reward 101"),
            ({"title": "t", "signal_reward": 0}, "reward 0"),
            ({"title": "x" * 121}, "title 121"),
            ({"description": "d"}, "missing title"),
    ]:
        r = client.post("/api/bounties", json=signed_body(
            priv_a, "bounty", fm_a, **payload), environ_base=fresh_ip())
        check("create %s -> 400" % label, r.status_code == 400,
              str(r.status_code))

    # claim own bounty rejected
    r = client.post("/api/bounties/%d/claim" % bid, json=signed_body(
        priv_a, "bounty_claim", fm_a), environ_base=fresh_ip())
    check("claim own bounty -> 400", r.status_code == 400,
          "%s %s" % (r.status_code, r.get_data(as_text=True)[:160]))
    check("still open after own-claim attempt",
          bounties.get_bounty(db, bid)["status"] == "open")

    before = db.lifetime_points(fm_b)
    r = client.post("/api/bounties/%d/claim" % bid, json=signed_body(
        priv_b, "bounty_claim", fm_b), environ_base=fresh_ip())
    j = r.get_json()
    check("claim -> 200 + claimed", r.status_code == 200 and
          j["bounty"]["status"] == "claimed" and
          j["bounty"]["claimed_by_handle"] == "BountyHunter", str(j))

    # double claim rejected
    r = client.post("/api/bounties/%d/claim" % bid, json=signed_body(
        priv_c, "bounty_claim", fm_c), environ_base=fresh_ip())
    check("double claim -> 400", r.status_code == 400, str(r.status_code))

    # non-poster complete rejected
    r = client.post("/api/bounties/%d/complete" % bid, json=signed_body(
        priv_b, "bounty_complete", fm_b), environ_base=fresh_ip())
    check("non-poster complete -> 400", r.status_code == 400,
          str(r.status_code))
    check("still claimed after bad complete",
          bounties.get_bounty(db, bid)["status"] == "claimed")

    # poster completes: Signal awarded to the claimer
    r = client.post("/api/bounties/%d/complete" % bid, json=signed_body(
        priv_a, "bounty_complete", fm_a), environ_base=fresh_ip())
    j = r.get_json()
    check("complete -> 200", r.status_code == 200,
          "%s %s" % (r.status_code, r.get_data(as_text=True)[:200]))
    check("signal_earned == reward", j.get("signal_earned") == 25, str(j))
    check("lifetime Signal grew by exactly the reward",
          db.lifetime_points(fm_b) - before == 25,
          "%d -> %d" % (before, db.lifetime_points(fm_b)))
    done = bounties.get_bounty(db, bid)
    check("done row: status + closed_at", done["status"] == "done"
          and done["closed_at"], str(done))

    # repeat complete rejected (already done)
    r = client.post("/api/bounties/%d/complete" % bid, json=signed_body(
        priv_a, "bounty_complete", fm_a), environ_base=fresh_ip())
    check("re-complete -> 400", r.status_code == 400, str(r.status_code))
    # no double award: completing twice must not mint more Signal
    check("no double Signal", db.lifetime_points(fm_b) - before == 25)

    print("== cancel rules ==")
    r = client.post("/api/bounties", json=signed_body(
        priv_a, "bounty", fm_a, title="Cancel me", signal_reward=10),
        environ_base=fresh_ip())
    bid2 = r.get_json()["bounty"]["id"]
    # non-poster cancel rejected
    r = client.post("/api/bounties/%d/cancel" % bid2, json=signed_body(
        priv_c, "bounty_cancel", fm_c), environ_base=fresh_ip())
    check("non-poster cancel -> 400", r.status_code == 400, str(r.status_code))
    # poster cancels open bounty
    r = client.post("/api/bounties/%d/cancel" % bid2, json=signed_body(
        priv_a, "bounty_cancel", fm_a), environ_base=fresh_ip())
    check("poster cancel -> 200 + cancelled",
          r.status_code == 200 and
          r.get_json()["bounty"]["status"] == "cancelled", str(r.status_code))
    # cancel a claimed bounty rejected
    r = client.post("/api/bounties", json=signed_body(
        priv_a, "bounty", fm_a, title="In progress bounty"),
        environ_base=fresh_ip())
    bid3 = r.get_json()["bounty"]["id"]
    r = client.post("/api/bounties/%d/claim" % bid3, json=signed_body(
        priv_b, "bounty_claim", fm_b), environ_base=fresh_ip())
    assert r.status_code == 200
    r = client.post("/api/bounties/%d/cancel" % bid3, json=signed_body(
        priv_a, "bounty_cancel", fm_a), environ_base=fresh_ip())
    check("cancel claimed bounty -> 400", r.status_code == 400,
          str(r.status_code))

    print("== GET /api/bounties ==")
    r = client.get("/api/bounties")
    j = r.get_json()
    check("public read -> 200 ok", r.status_code == 200 and j["ok"],
          str(r.status_code))
    open_ids = [x["id"] for x in j["bounties"]]
    check("open-only on /bounties page semantics handled by template;"
          " API has all",
          bid in open_ids and bid2 in open_ids and bid3 in open_ids,
          str(open_ids))
    r = client.get("/api/bounties?status=open")
    ids = [x["id"] for x in r.get_json()["bounties"]]
    check("status=open filter", bid not in ids and bid2 not in ids
          and bid3 not in ids, str(ids))
    r = client.get("/api/bounties?status=done")
    ids = [x["id"] for x in r.get_json()["bounties"]]
    check("status=done filter", ids == [bid], str(ids))
    r = client.get("/api/bounties?status=bogus")
    check("bad status -> 400", r.status_code == 400, str(r.status_code))
    r = client.post("/api/bounties/999999/claim", json=signed_body(
        priv_b, "bounty_claim", fm_b), environ_base=fresh_ip())
    check("claim unknown bounty -> 400", r.status_code == 400,
          str(r.status_code))

    print("== /bounties page ==")
    fresh = bounties.create_bounty(db, fm_a, "BountyPoster", "Open for claims",
                                   "needs doing", 15)
    r = client.get("/bounties")
    html = r.get_data(as_text=True)
    check("page -> 200", r.status_code == 200, str(r.status_code))
    check("brand/title", "Bounty Board" in html and "Muse FM" in html)
    check("glass/blue styling", "bounty-card" in html and
          "linear-gradient" in html and "#0c4a6e" in html, "")
    check("claim buttons present", 'data-claim="%d"' % fresh["id"] in html,
          "fresh=%d" % fresh["id"])
    check("open bounty listed", "Open for claims" in html)
    check("done bounty not in open list", "Review my skill" not in html)
    check("claimed bounty not in open list", "In progress bounty" not in html)
    check("create form present", 'id="bounty-form"' in html and
          "Signal reward" in html)
    check("JS fetch wiring", 'fetch("/api/bounties/"' in html)

    print("== no money anywhere ==")
    import re
    for path in ["bounties.py", "templates/bounties.html"]:
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                path)).read()
        for word in ("payment", "price"):
            check("%s has no %r" % (path, word), word not in src.lower())
        check("%s has no dollar sign" % path, "$" not in src)

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
