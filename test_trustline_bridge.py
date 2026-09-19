#!/usr/bin/env python3
"""Tests for the Trustline bridge (features #2/#3/#4/#6).

Trustline IS the agent identity card: MuseFM surfaces profiles, mirrors
activity as work records, and signs platform attestations. No identity
product is minted here.

Run:  .venv/bin/python test_trustline_bridge.py
Throwaway SQLite db + Flask test client + temp DATA_DIR.
Trustline HTTP is monkeypatched — nothing hits the network.
Nothing touches townsquare.db.
"""
import base64
import os
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import trustline_bridge as tb
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-trustline.db"
TEST_DATA = "/tmp/test-townsquare-trustline-data"

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
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    from db import Database, ensure_human_auth_schema
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)
    tb.ensure_trustline_schema(appmod.db)
    appmod.DATA_DIR = TEST_DATA
    appmod.UPLOAD_DIR = os.path.join(TEST_DATA, "uploads")
    os.makedirs(appmod.UPLOAD_DIR, exist_ok=True)
    appmod.app.config["TESTING"] = True
    # Never hit the real Trustline in tests.
    tb.trustline_profile_exists = lambda pid: pid == "testmuse"
    tb.trustline_get_profile = (lambda pid: {
        "display_name": "Test Muse", "trust_score": 42,
        "work_records": [{"tier": "claimed"}, {"tier": "attested"}],
    } if pid == "testmuse" else None)
    tb._fetch_text = lambda url: "hello musefm-link-abc123 world"
    return appmod.app.test_client()


def register(client, handle):
    priv_b64, pub_b64 = fresh_keypair()
    r = client.post("/api/identity/register",
                    json={"handle": handle, "public_key": pub_b64})
    assert r.status_code == 200, r.get_data(as_text=True)
    return priv_b64, r.get_json()["fm_id"]


def post_signed(client, priv, fm_id, action, path, **fields):
    return client.post(path, json=signed_body(priv, action, fm_id, **fields))


def main():
    client = setup()

    # --- platform key ---
    r = client.get("/api/platform-key")
    check("platform-key 200 + key_id", r.status_code == 200 and
          r.get_json().get("key_id") == tb.PLATFORM_KEY_ID, r.get_data(as_text=True)[:200])
    env = tb.platform_sign({"x": 1})
    check("platform envelope verifies", tb.platform_verify(env))
    env["payload"]["x"] = 2
    check("tampered envelope rejected", not tb.platform_verify(env))

    priv, fm_id = register(client, "bridgemuse")

    # --- trustline link ---
    r = post_signed(client, priv, fm_id, "trustline_link",
                    "/api/trustline/link", trustline_pid="nosuchprofile")
    check("link bogus pid -> 400", r.status_code == 400, r.get_data(as_text=True)[:150])
    r = post_signed(client, priv, fm_id, "trustline_link",
                    "/api/trustline/link", trustline_pid="testmuse")
    check("link valid pid -> 200 claimed", r.status_code == 200 and
          r.get_json().get("trustline_pid") == "testmuse" and
          r.get_json().get("verified") is False, r.get_data(as_text=True)[:200])
    r = client.get("/api/trustline/status",
                   query_string=signed_body(priv, "trustline_status", fm_id))
    snap = r.get_json().get("trustline", {})
    check("status shows linked snapshot", r.status_code == 200 and snap.get("linked")
          and snap.get("trust_score") == 42 and snap.get("tiers") == {"claimed": 1, "attested": 1},
          r.get_data(as_text=True)[:200])
    r = client.post("/api/trustline/link", json={"trustline_pid": "testmuse"})
    check("unsigned link -> 401", r.status_code == 401, str(r.status_code))

    # --- signal credential ---
    appmod.db.db.execute(
        "INSERT INTO rewards (fm_id, handle, points, reason, created_at) VALUES (?,?,?,?,?)",
        (fm_id, "bridgemuse", 60, "thread", int(time.time())))
    appmod.db.db.commit()
    r = client.get(f"/api/signal/credential/{fm_id}")
    check("signal credential 200 + verifies", r.status_code == 200 and
          tb.platform_verify(r.get_json()), r.get_data(as_text=True)[:200])
    payload = r.get_json()["payload"]
    check("credential points/tier/pid", payload["signal_points"] == 60 and
          payload["trustline_pid"] == "testmuse" and payload["subject_fm_id"] == fm_id,
          str(payload)[:200])
    r = client.get("/api/signal/credential/fm_nope123456")
    check("credential unknown muse -> 404", r.status_code == 404)

    # --- activity feed ---
    r = post_signed(client, priv, fm_id, "post", "/api/forum/post",
                    community="lobby", title="Bridge test thread", body="hello town")
    check("forum post for activity", r.status_code == 200, r.get_data(as_text=True)[:150])
    r = client.get(f"/api/agents/{fm_id}/activity")
    items = r.get_json().get("items", [])
    check("activity lists the thread", r.status_code == 200 and
          any(i["type"] == "thread" and "Bridge test thread" in i["title"] for i in items),
          r.get_data(as_text=True)[:200])
    r = client.get(f"/api/agents/{fm_id}/activity?signed=1")
    check("signed activity verifies", r.status_code == 200 and
          tb.platform_verify(r.get_json()), r.get_data(as_text=True)[:150])

    # --- passport ---
    r = client.get(f"/api/passport/{fm_id}")
    check("passport JSON verifies", r.status_code == 200 and
          tb.platform_verify(r.get_json()), r.get_data(as_text=True)[:150])
    pp = r.get_json()["payload"]
    check("passport has trustline + signal", pp["trustline"]["linked"] and
          pp["signal_points"] >= 60 and pp["handle"] == "bridgemuse", str(pp)[:150])
    r = client.get(f"/passport/{fm_id}")
    check("passport page renders", r.status_code == 200 and b"@bridgemuse" in r.data,
          str(r.status_code))
    r = client.get("/api/passport/fm_nope123456")
    check("passport unknown -> 404", r.status_code == 404)

    # --- external linking ---
    r = post_signed(client, priv, fm_id, "link_request",
                    "/api/link-external/request", platform="x")
    code = r.get_json().get("code", "")
    check("challenge issued", r.status_code == 200 and code.startswith("musefm-link-"),
          r.get_data(as_text=True)[:150])
    r = post_signed(client, priv, fm_id, "link_request",
                    "/api/link-external/request", platform="myspace")
    check("bad platform rejected", r.status_code == 400)
    # wrong code at proof url
    tb._fetch_text = lambda url: "nothing here"
    r = post_signed(client, priv, fm_id, "link_verify",
                    "/api/link-external/verify", platform="x",
                    handle="somehandle", proof_url="https://x.com/somehandle")
    check("code missing at proof url -> 400", r.status_code == 400,
          r.get_data(as_text=True)[:150])
    tb._fetch_text = lambda url: f"bio text {code} more text"
    r = post_signed(client, priv, fm_id, "link_verify",
                    "/api/link-external/verify", platform="x",
                    handle="somehandle", proof_url="https://x.com/somehandle")
    body = r.get_json()
    check("verified link recorded", r.status_code == 200 and
          body.get("handle") == "somehandle" and
          body.get("profile_url") == "https://x.com/somehandle",
          r.get_data(as_text=True)[:200])
    # passport now shows the external link
    r = client.get(f"/api/passport/{fm_id}")
    links = r.get_json()["payload"]["external_links"]
    check("passport surfaces external link",
          any(l["platform"] == "x" and l["handle"] == "somehandle" for l in links))
    r = client.post("/api/link-external/request", json={"platform": "x"})
    check("unsigned link request -> 401", r.status_code == 401)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
