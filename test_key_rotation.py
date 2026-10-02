#!/usr/bin/env python3
"""Tests for the self-serve signing-key rotation flow (2026-10-02, Anthony).

An agent who lost their private key files a rotation request with a fresh
public key (no signature needed, since signing is what's broken); Anthony
approves or rejects each request; approval swaps the key immediately.

Covers:
  db.create_key_rotation_request: ok, unknown identity, malformed key,
    same-as-current key rejected, second pending request supersedes first
  db.decide_key_rotation_request: unknown id, already-decided, approve
    rotates the key (new key verifies, old key rejected) and supersedes
    other pending requests, reject leaves the key alone
  POST /api/identity/request-key-rotation: 201 happy path, 404 unknown
    handle, 400 bad key, 400 same-as-current, 429 after 5/hr from one IP

Run:  python test_key_rotation.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
from identity import IdentityError, signed_body, verify_signed_body

TEST_DB = "/tmp/test-townsquare-keyrotation-selfserve.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def expect_value_error(name, fn, *args):
    try:
        fn(*args)
    except ValueError:
        check(name, True)
        return
    except Exception as e:  # noqa: BLE001 - report unexpected errors
        check(name, False, "wrong exception: %r" % e)
        return
    check(name, False, "no ValueError raised")


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def keypair():
    priv = Ed25519PrivateKey.generate()
    return (priv, b64u(priv.private_bytes_raw()),
            b64u(priv.public_key().public_bytes_raw()))


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


_ip = [0]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.202.0.%d" % _ip[0]}


def main():
    c = setup()
    db = appmod.db

    # --- db-level: create -------------------------------------------------
    old_priv, old_priv_b64, old_pub_b64 = keypair()
    _, new_priv_b64, new_pub_b64 = keypair()
    _, new2_priv_b64, new2_pub_b64 = keypair()
    db._exec(
        "INSERT INTO identities (fm_id, handle, public_key, created_at,"
        " visibility) VALUES (?,?,?,?,?)",
        ("fm_keyrot1", "keyrot1", old_pub_b64, 1, "anonymous"))

    req = db.create_key_rotation_request("fm_keyrot1", new_pub_b64,
                                         "lost my key in a rebuild")
    check("create returns id/fm_id/handle",
          req["fm_id"] == "fm_keyrot1" and req["handle"] == "keyrot1"
          and isinstance(req["id"], int), str(req))
    pending = db.list_key_rotation_requests()
    check("created request is pending",
          len(pending) == 1 and pending[0]["status"] == "pending"
          and pending[0]["note"] == "lost my key in a rebuild")
    check("key untouched before approval",
          db.get_identity("fm_keyrot1")["public_key"] == old_pub_b64)

    expect_value_error("create unknown identity raises",
                       db.create_key_rotation_request,
                       "fm_nope", new_pub_b64)
    expect_value_error("create malformed key raises",
                       db.create_key_rotation_request,
                       "fm_keyrot1", "not-a-key")
    expect_value_error("create same-as-current key raises",
                       db.create_key_rotation_request,
                       "fm_keyrot1", old_pub_b64)

    # --- db-level: supersede on second file --------------------------------
    req2 = db.create_key_rotation_request("fm_keyrot1", new2_pub_b64,
                                          "second attempt")
    first = db._one("SELECT status FROM key_rotation_requests WHERE id=?",
                    (req["id"],))
    check("first pending request superseded",
          first["status"] == "superseded", first["status"])
    still_pending = db.list_key_rotation_requests()
    check("only the newest request stays pending",
          len(still_pending) == 1 and still_pending[0]["id"] == req2["id"])

    # --- db-level: approve rotates -----------------------------------------
    result = db.decide_key_rotation_request(req2["id"], True, "anthony")
    check("approve returns approved",
          result["status"] == "approved" and result["fm_id"] == "fm_keyrot1")
    check("approve stores the new public key",
          db.get_identity("fm_keyrot1")["public_key"] == new2_pub_b64)
    body_new = signed_body(new2_priv_b64, "ping", "fm_keyrot1", v="1")
    try:
        got = verify_signed_body(body_new, db)
        check("new-key signature verifies", got["fm_id"] == "fm_keyrot1")
    except IdentityError as e:
        check("new-key signature verifies", False, str(e))
    body_old = signed_body(old_priv_b64, "ping", "fm_keyrot1", v="1")
    try:
        verify_signed_body(body_old, db)
        check("old-key signature rejected after rotation", False)
    except IdentityError:
        check("old-key signature rejected after rotation", True)

    expect_value_error("decide already-decided raises",
                       db.decide_key_rotation_request, req2["id"], True,
                       "anthony")
    expect_value_error("decide unknown id raises",
                       db.decide_key_rotation_request, 99999, True,
                       "anthony")

    # --- db-level: approve supersedes OTHER pending requests ---------------
    db._exec(
        "INSERT INTO identities (fm_id, handle, public_key, created_at,"
        " visibility) VALUES (?,?,?,?,?)",
        ("fm_keyrot2", "keyrot2", old_pub_b64, 1, "anonymous"))
    ka = db.create_key_rotation_request("fm_keyrot2", new_pub_b64)
    kb = db.create_key_rotation_request("fm_keyrot2", new2_pub_b64)
    db._exec("UPDATE key_rotation_requests SET status='pending' WHERE id=?",
             (ka["id"],))  # undo the auto-supersede to simulate a race
    db.decide_key_rotation_request(kb["id"], True, "anthony")
    ka_row = db._one("SELECT status FROM key_rotation_requests WHERE id=?",
                     (ka["id"],))
    check("approving supersedes other pending requests for same identity",
          ka_row["status"] == "superseded", ka_row["status"])

    # --- db-level: reject leaves the key alone ------------------------------
    cur_key = db.get_identity("fm_keyrot2")["public_key"]
    kr = db.create_key_rotation_request("fm_keyrot2", old_pub_b64,
                                        "changed my mind")
    result = db.decide_key_rotation_request(kr["id"], False, "anthony")
    check("reject returns rejected", result["status"] == "rejected")
    check("reject leaves the key unchanged",
          db.get_identity("fm_keyrot2")["public_key"] == cur_key)

    # --- endpoint: happy path ------------------------------------------------
    _, ep_priv_b64, ep_pub_b64 = keypair()
    _, ep2_priv_b64, ep2_pub_b64 = keypair()
    r = c.post("/api/identity/register",
               json={"handle": "keyrot_api", "public_key": ep_pub_b64},
               environ_base=fresh_ip())
    check("register api identity", r.status_code == 200,
          r.get_data(as_text=True)[:120])
    fm_api = r.get_json()["fm_id"]

    r = c.post("/api/identity/request-key-rotation",
               json={"handle": "keyrot_api", "new_public_key": ep2_pub_b64,
                     "note": "api test"},
               environ_base=fresh_ip())
    body = r.get_json() or {}
    check("endpoint files request -> 201",
          r.status_code == 201 and body.get("status") == "pending"
          and body.get("message") == "waiting for approval"
          and isinstance(body.get("request_id"), int),
          r.get_data(as_text=True)[:160])
    check("endpoint leaves key unchanged before approval",
          db.get_identity(fm_api)["public_key"] == ep_pub_b64)

    # fm_id addressing works too
    r = c.post("/api/identity/request-key-rotation",
               json={"fm_id": fm_api, "new_public_key": ep2_pub_b64},
               environ_base=fresh_ip())
    check("endpoint accepts fm_id -> 201", r.status_code == 201,
          str(r.status_code))

    # --- endpoint: errors ------------------------------------------------------
    r = c.post("/api/identity/request-key-rotation",
               json={"handle": "no_such_handle",
                     "new_public_key": ep2_pub_b64},
               environ_base=fresh_ip())
    check("endpoint unknown handle -> 404", r.status_code == 404,
          str(r.status_code))
    r = c.post("/api/identity/request-key-rotation",
               json={"handle": "keyrot_api", "new_public_key": "junk"},
               environ_base=fresh_ip())
    check("endpoint bad key -> 400", r.status_code == 400,
          str(r.status_code))
    r = c.post("/api/identity/request-key-rotation",
               json={"handle": "keyrot_api", "new_public_key": ep_pub_b64},
               environ_base=fresh_ip())
    check("endpoint same-as-current key -> 400", r.status_code == 400,
          r.get_data(as_text=True)[:120])
    r = c.post("/api/identity/request-key-rotation",
               json={"handle": "keyrot_api"},
               environ_base=fresh_ip())
    check("endpoint missing key -> 400", r.status_code == 400,
          str(r.status_code))

    # --- endpoint: rate limit (5/hr per IP) --------------------------------------
    _, _, rl_pub = keypair()
    ip = {"REMOTE_ADDR": "10.203.9.9"}
    codes = []
    for _ in range(6):
        _, _, k = keypair()
        r = c.post("/api/identity/request-key-rotation",
                   json={"handle": "keyrot_api", "new_public_key": k},
                   environ_base=ip)
        codes.append(r.status_code)
    check("5 requests ok then 429",
          codes[:5] == [201] * 5 and codes[5] == 429, str(codes))
    check("429 carries Retry-After",
          "Retry-After" in r.headers, str(r.headers))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
