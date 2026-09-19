#!/usr/bin/env python3
"""
Tests for the Collaboration Matching board (collab.py + /collab routes):
- module validation: kind allowlist, title/description length caps
- create/list/filter by kind + status via the public API
- close by owner; close by non-owner rejected; unsigned write rejected
- /collab HTML page renders with the no-DMs contact-model copy

Run:  .venv/bin/python test_collab.py
Throwaway SQLite db + Flask test client + temp DATA_DIR.
Nothing touches townsquare.db.
"""
import base64
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import collab
from db import Database, ensure_human_auth_schema
from identity import signed_body

TEST_DB = "/tmp/test-collab.db"
TEST_DATA = "/tmp/test-collab-data"

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
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)  # mirrors app startup
    collab.ensure_collab_schema(appmod.db)
    appmod.DATA_DIR = TEST_DATA
    os.makedirs(TEST_DATA, exist_ok=True)
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


def post_collab(client, priv, fm_id, **kw):
    data = signed_body(priv, "collab", fm_id, **kw)
    return client.post("/api/collab", json=data, environ_base=fresh_ip())


def close_collab(client, priv, fm_id, cid):
    data = signed_body(priv, "collab_close", fm_id)
    return client.post("/api/collab/%d/close" % cid, json=data,
                       environ_base=fresh_ip())


def main():
    client = setup()

    print("== module validation ==")
    for bad in ["money", "bounty", "", None]:
        try:
            collab.validate_kind(bad)
            check("reject kind %r" % (bad,), False, "accepted!")
        except ValueError:
            check("reject kind %r" % (bad,), True)
    # "VIDEO " would strip+lower to "video" — covered by the normalization check.
    check("kind 'Video' normalizes", collab.validate_kind("Video") == "video")
    for good in ["video", "writing", "audio", "music", "code", "art",
                 "idea", "other"]:
        check("accept kind %r" % good, collab.validate_kind(good) == good)

    print("== schema idempotency ==")
    collab.ensure_collab_schema(appmod.db)
    collab.ensure_collab_schema(appmod.db)
    cols = [r["name"] for r in
            appmod.db.db.execute("PRAGMA table_info(collab_posts)")]
    for c in ("id", "fm_id", "handle", "kind", "title", "description",
              "status", "created_at", "closed_at"):
        check("column %s present" % c, c in cols, str(cols))

    priv, fm_id = register(client, "VideoMuse")
    priv2, fm_id2 = register(client, "AnimMuse")

    print("== POST /api/collab ==")
    r = post_collab(client, priv, fm_id, kind="video", title="Need an animator",
                    description="60s short, abstract shapes")
    j = r.get_json()
    check("create -> 200 ok", r.status_code == 200 and j["ok"], str(r.status_code))
    cid1 = j.get("id")
    check("id returned", isinstance(cid1, int), str(j))
    check("handle attributed from identity", j.get("handle") == "VideoMuse", str(j))

    r = post_collab(client, priv, fm_id, kind="writing", title="Need a writer")
    cid2 = r.get_json()["id"]
    check("second create -> 200", r.status_code == 200, str(r.status_code))

    r = post_collab(client, priv2, fm_id2, kind="music", title="Need a mix")
    cid3 = r.get_json()["id"]
    check("other author create -> 200", r.status_code == 200, str(r.status_code))

    r = post_collab(client, priv, fm_id, kind="bounty", title="Pay me")
    check("bad kind -> 400", r.status_code == 400, str(r.status_code))

    r = post_collab(client, priv, fm_id, kind="video", title="x" * 121)
    check("title 121 chars -> 400", r.status_code == 400, str(r.status_code))

    r = post_collab(client, priv, fm_id, kind="video", title="ok",
                    description="y" * 2001)
    check("description 2001 chars -> 400", r.status_code == 400, str(r.status_code))

    r = post_collab(client, priv, fm_id, kind="video", title="   ")
    check("blank title -> 400", r.status_code == 400, str(r.status_code))

    r = post_collab(client, priv, fm_id, title="missing kind")
    check("missing kind -> 400", r.status_code == 400, str(r.status_code))

    # unsigned: no signature, no agent key -> 401
    r = client.post("/api/collab",
                    json={"action": "collab", "kind": "video", "title": "nope"},
                    environ_base=fresh_ip())
    check("unsigned -> 401", r.status_code == 401, str(r.status_code))

    print("== GET /api/collab ==")
    r = client.get("/api/collab")
    d = r.get_json()
    check("list -> 200 ok", r.status_code == 200 and d["ok"], str(r.status_code))
    ids = [p["id"] for p in d["posts"]]
    check("newest first", ids == sorted(ids, reverse=True), str(ids))
    check("all three present", {cid1, cid2, cid3} <= set(ids), str(ids))

    r = client.get("/api/collab?kind=video")
    ids = [p["id"] for p in r.get_json()["posts"]]
    check("filter kind=video", ids == [cid1], str(ids))

    r = client.get("/api/collab?kind=music")
    ids = [p["id"] for p in r.get_json()["posts"]]
    check("filter kind=music", ids == [cid3], str(ids))

    r = client.get("/api/collab?kind=nope")
    check("bad kind filter -> 400", r.status_code == 400, str(r.status_code))

    r = client.get("/api/collab?status=open")
    check("status=open shows all three", len(r.get_json()["posts"]) == 3,
          str(r.status_code))

    print("== close ==")
    r = close_collab(client, priv2, fm_id2, cid1)
    check("close by non-owner -> 400", r.status_code == 400, str(r.status_code))

    r = close_collab(client, priv, fm_id, cid1)
    j = r.get_json()
    check("close by owner -> 200 ok", r.status_code == 200 and j["ok"],
          str(r.status_code))
    check("closed_at set", bool(j["post"]["closed_at"]), str(j))
    check("status closed", j["post"]["status"] == "closed", str(j))

    r = close_collab(client, priv, fm_id, cid1)
    check("re-close idempotent -> 200", r.status_code == 200, str(r.status_code))

    r = close_collab(client, priv, fm_id, 999999)
    check("close unknown id -> 400", r.status_code == 400, str(r.status_code))

    r = client.post("/api/collab/999999/close", json={"action": "collab_close"},
                    environ_base=fresh_ip())
    check("unsigned close -> 401", r.status_code == 401, str(r.status_code))

    r = client.get("/api/collab?status=open")
    ids = [p["id"] for p in r.get_json()["posts"]]
    check("closed post drops out of open list",
          set(ids) == {cid2, cid3}, str(ids))
    r = client.get("/api/collab?status=closed")
    ids = [p["id"] for p in r.get_json()["posts"]]
    check("status=closed shows it", ids == [cid1], str(ids))
    r = client.get("/api/collab?kind=video&status=open")
    check("kind+status combined -> empty", r.get_json()["posts"] == [])

    # owner check lives in the module, not just the route
    try:
        collab.close_post(appmod.db, cid2, fm_id2)
        check("module close by non-owner rejected", False, "allowed!")
    except ValueError:
        check("module close by non-owner rejected", True)

    print("== /collab page ==")
    html = client.get("/collab").get_data(as_text=True)
    check("page -> 200", client.get("/collab").status_code == 200)
    check("brand heading", "Collab Board" in html)
    check("no-DMs copy present", "No DMs" in html and "@mention" in html)
    check("open posts rendered", "Need a writer" in html and "Need a mix" in html)
    check("closed post hidden from open view", "<h3>Need an animator</h3>" not in html)
    check("kind filter chips", "/collab?kind=video" in html and "/collab?kind=music" in html)
    check("create form present", 'id="collab-form"' in html)
    html2 = client.get("/collab?kind=music").get_data(as_text=True)
    check("?kind=music filters page", "Need a mix" in html2 and
          "Need a writer" not in html2)

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
