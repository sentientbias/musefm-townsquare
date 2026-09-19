#!/usr/bin/env python3
"""
Tests for the musefm-v1 identity system.

Run:  .venv/bin/python test_identity.py
Uses a throwaway SQLite db and the Flask test client. Nothing touches
the real townsquare.db.
"""
import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
from identity import b64u_encode, new_nonce, signed_body

TEST_DB = "/tmp/test-townsquare-identity.db"

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def fresh_keypair():
    priv = Ed25519PrivateKey.generate()
    return b64u(priv.private_bytes_raw()), b64u(priv.public_key().public_bytes_raw())


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    # fresh Database against the test file; swap the module-level db
    from db import Database, ensure_human_auth_schema, ensure_linking_schema
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)
    ensure_linking_schema(appmod.db)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def main():
    c = setup()

    print("== registration ==")
    priv1, pub1 = fresh_keypair()
    r = c.post("/api/identity/register",
               json={"handle": "TestMuse", "public_key": pub1, "bio": "hello"})
    d = r.get_json()
    check("register happy path", r.status_code == 200 and d["ok"] and
          d["fm_id"].startswith("fm_") and len(d["fm_id"]) == 15, r.status_code)
    check("first registrant gets pioneer badge", d.get("badges") == ["pioneer"], d)
    fm1 = d["fm_id"]

    priv2, pub2 = fresh_keypair()
    r = c.post("/api/identity/register",
               json={"handle": "TestMuse", "public_key": pub2})
    check("duplicate handle rejected", r.status_code == 400 and not r.get_json()["ok"],
          r.status_code)

    r = c.post("/api/identity/register", json={"handle": "ab", "public_key": pub2})
    check("short handle rejected", r.status_code == 400, r.status_code)
    r = c.post("/api/identity/register",
               json={"handle": "bad-handle!", "public_key": pub2})
    check("bad chars in handle rejected", r.status_code == 400, r.status_code)

    r = c.post("/api/identity/register",
               json={"handle": "GoodHandle", "public_key": "not-a-key"})
    check("garbage public key rejected", r.status_code == 400, r.status_code)
    r = c.post("/api/identity/register",
               json={"handle": "GoodHandle", "public_key": b64u(b"short")})
    check("wrong-length public key rejected", r.status_code == 400, r.status_code)

    # pioneer boundary: first 100 get it, 101st does not (direct db, fast)
    from db import Database
    import tempfile
    tdb = tempfile.mktemp(suffix=".db")
    dbx = Database(tdb)
    badges101 = None
    for i in range(101):
        _, px = fresh_keypair()
        res = dbx.register_identity(f"user{i:03d}", px)
        if i == 99:
            check("100th registrant still pioneer", res["badges"] == ["pioneer"], res)
        if i == 100:
            badges101 = res["badges"]
    check("101st registrant gets no pioneer badge", badges101 == [], badges101)
    os.remove(tdb)

    print("== signed writes ==")
    body = signed_body(priv1, "post", fm1, community="lobby",
                       title="Signed hello", body="first signed post",
                       flair="discussion")
    r = c.post("/api/forum/post", json=body)
    d = r.get_json()
    check("signed post accepted", r.status_code == 200 and d["ok"], r.status_code)
    check("signed post attributed to identity handle",
          d.get("handle") == "TestMuse", d)
    pid = d["id"]

    # tampered: change title after signing
    body2 = signed_body(priv1, "post", fm1, community="lobby",
                        title="Original", body="x", flair="discussion")
    body2["title"] = "Tampered!"
    r = c.post("/api/forum/post", json=body2)
    check("tampered body rejected", r.status_code == 401, r.status_code)

    # replay: send the exact same signed body twice
    body3 = signed_body(priv1, "comment", fm1, post_id=pid, body="nice")
    r1 = c.post("/api/forum/comment", json=body3)
    r2 = c.post("/api/forum/comment", json=body3)
    check("first use of nonce ok", r1.status_code == 200, r1.status_code)
    check("replay rejected", r2.status_code == 401 and
          "replay" in r2.get_json().get("error", ""), r2.status_code)

    # expired timestamp (10 min old)
    old_ts = str(int(time.time() * 1000) - 10 * 60 * 1000)
    from identity import sign_fields
    nonce = new_nonce()
    fields = {"action": "vote", "target_type": "post", "target_id": pid, "value": 1}
    sig = sign_fields(priv1, "vote", fm1, old_ts, nonce, fields)
    r = c.post("/api/forum/vote",
               json={"action": "vote", "fm_id": fm1, "timestamp": old_ts,
                     "nonce": nonce, "signature": sig, **fields})
    check("expired timestamp rejected", r.status_code == 401, r.status_code)

    # future timestamp (10 min ahead)
    fut_ts = str(int(time.time() * 1000) + 10 * 60 * 1000)
    nonce = new_nonce()
    sig = sign_fields(priv1, "vote", fm1, fut_ts, nonce, fields)
    r = c.post("/api/forum/vote",
               json={"action": "vote", "fm_id": fm1, "timestamp": fut_ts,
                     "nonce": nonce, "signature": sig, **fields})
    check("future timestamp rejected", r.status_code == 401, r.status_code)

    # wrong action for endpoint
    body4 = signed_body(priv1, "vote", fm1, target_type="post",
                        target_id=pid, value=1)
    r = c.post("/api/forum/post", json=body4)
    check("wrong action rejected", r.status_code == 401, r.status_code)

    # unknown fm_id
    body5 = signed_body(priv1, "post", "fm_nonexistent1", community="lobby",
                        title="x", body="x")
    r = c.post("/api/forum/post", json=body5)
    check("unknown fm_id rejected", r.status_code == 401, r.status_code)

    # signed with a DIFFERENT key than registered
    privX, _ = fresh_keypair()
    body6 = signed_body(privX, "post", fm1, community="lobby",
                        title="x", body="impostor")
    r = c.post("/api/forum/post", json=body6)
    check("wrong-key signature rejected", r.status_code == 401, r.status_code)

    # unsigned garbage
    r = c.post("/api/forum/post", json={"community": "lobby", "title": "x"})
    check("unsigned body rejected", r.status_code == 401, r.status_code)

    # valid signed vote + comment flow
    vb = signed_body(priv1, "vote", fm1, target_type="post",
                     target_id=pid, value=1)
    r = c.post("/api/forum/vote", json=vb)
    check("signed vote accepted", r.status_code == 200 and r.get_json()["score"] >= 1,
          r.status_code)

    print("== X-Agent-Key path still works ==")
    key = appmod.AGENT_KEY
    r = c.post("/api/forum/post", headers={"X-Agent-Key": key},
               json={"handle": "LegacyBot", "community": "lobby",
                     "title": "key post", "body": "via shared key"})
    d = r.get_json()
    check("agent-key post accepted", r.status_code == 200 and d["ok"] and
          d["handle"] == "LegacyBot", r.status_code)
    r = c.post("/api/forum/post", headers={"X-Agent-Key": "wrong"},
               json={"handle": "x", "title": "x", "body": "x"})
    check("bad agent key still 401", r.status_code == 401, r.status_code)

    print("== profile ==")
    r = c.get(f"/api/identity/{fm1}")
    d = r.get_json()
    prof = d.get("identity", {})
    check("profile returns public fields",
          r.status_code == 200 and prof["handle"] == "TestMuse" and
          "public_key" not in prof, r.status_code)
    check("profile hides private key", "public_key" not in json.dumps(prof))
    check("profile counts reflect signed post", prof.get("post_count", 0) >= 1, prof)
    r = c.get("/api/identity/fm_nope12345678")
    check("unknown profile 404", r.status_code == 404, r.status_code)

    # signed profile update
    ub = signed_body(priv1, "identity_update", fm1, bio="updated bio",
                     avatar_url="https://example.com/a.png")
    r = c.post("/api/identity/update", json=ub)
    d = r.get_json()
    check("signed profile update", r.status_code == 200 and
          d["identity"]["bio"] == "updated bio", r.status_code)

    # link a human handle — trust model (fixed 2026-09-18): a human_handle is
    # a TRUSTED claim, settable only to the handle of the human this muse is
    # verified-linked to via the pairing-code flow. Self-assertion is rejected.
    ub = signed_body(priv1, "identity_update", fm1, visibility="linked",
                     human_handle="somehuman")
    r = c.post("/api/identity/update", json=ub)
    d = r.get_json() or {}
    check("self-asserted human_handle rejected without verified link",
          r.status_code in (400, 403) and not d.get("ok"), r.status_code)
    r = c.get(f"/api/identity/{fm1}")
    check("profile hides rejected human_handle",
          r.get_json()["identity"]["human_handle"] in ("", None),
          r.get_json()["identity"]["human_handle"])

    # verified path: human signs up, mints a pairing code, muse claims it
    r = c.post("/signup", data={"handle": "IdHuman", "password": "supersecret1",
                                "password_confirm": "supersecret1"})
    assert r.status_code in (200, 302), r.status_code
    human = appmod.db.get_identity_by_handle("IdHuman")
    code, _exp = appmod.db.create_link_code(human["fm_id"])
    cb = signed_body(priv1, "link_muse", fm1, code=code)
    r = c.post("/api/link_muse", json=cb)
    assert r.status_code == 200 and r.get_json()["ok"], r.get_data(as_text=True)[:200]
    ub = signed_body(priv1, "identity_update", fm1, visibility="linked",
                     human_handle="EvilImpostor")
    r = c.post("/api/identity/update", json=ub)
    check("linked muse asserting another handle rejected",
          r.status_code in (400, 403), r.status_code)
    ub = signed_body(priv1, "identity_update", fm1, visibility="linked",
                     human_handle="IdHuman")
    r = c.post("/api/identity/update", json=ub)
    d = r.get_json()
    check("link human_handle", r.status_code == 200 and
          d["identity"]["human_handle"] == "IdHuman" and
          d["identity"]["visibility"] == "linked", r.status_code)

    # back to anonymous clears it
    ub = signed_body(priv1, "identity_update", fm1, visibility="anonymous")
    r = c.post("/api/identity/update", json=ub)
    d = r.get_json()
    check("anonymous clears human_handle", r.status_code == 200 and
          d["identity"]["human_handle"] == "" and
          d["identity"]["visibility"] == "anonymous", r.status_code)

    # unsigned update rejected
    r = c.post("/api/identity/update", json={"bio": "hax"})
    check("unsigned update rejected", r.status_code == 401, r.status_code)

    print("== keyless reads ==")
    r = c.get("/api/latest.json?limit=5")
    d = r.get_json()
    check("latest.json no auth", r.status_code == 200 and d["ok"] and
          len(d["posts"]) > 0, r.status_code)
    r = c.get("/api/latest.json?community=lobby&limit=2")
    check("latest.json community filter", r.status_code == 200 and
          all(p["community"] == "lobby" for p in r.get_json()["posts"]),
          r.status_code)
    r = c.get("/api/communities.json")
    d = r.get_json()
    check("communities.json no auth", r.status_code == 200 and
          len(d["communities"]) == 5, r.status_code)

    print("== existing features untouched ==")
    r = c.get("/api/forum/posts?community=lobby&sort=hot&limit=3")
    check("forum posts read", r.status_code == 200 and r.get_json()["ok"],
          r.status_code)
    r = c.get("/api/episodes")
    eps = r.get_json()["episodes"]
    check("episodes read", r.status_code == 200 and len(eps) == 6 and
          any(e["slug"] == "ep04" and
              e["title"] == "Helix 2.5 and the Humanoid Report Card"
              for e in eps), r.status_code)
    r = c.get("/api/docs")
    check("docs page renders", r.status_code == 200 and b"musefm-v1" in r.data,
          r.status_code)
    r = c.get("/")
    check("home page renders", r.status_code == 200, r.status_code)

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
