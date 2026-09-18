#!/usr/bin/env python3
"""
Tests for the session-seeded /shorts shuffle (Batch 2, Anthony requirement):
- each visitor gets a random seed in their session; different visitors get
  different orders; the same visitor keeps a stable order
- paging walks the shuffled deck with no repeats or skips
- ?video= anchor still works
- legacy ?before= cursor still returns newest-first
- Cache-Control is private on the shuffled feed (never shared-cached)
- /musefm/shorts stays newest-first (unchanged)

Run:  python3 test_shorts_shuffle.py
Throwaway SQLite db + Flask test client + temp DATA_DIR.
Nothing touches townsquare.db.
"""
import hashlib
import io
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import base64

import app as appmod
import videos
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-shorts-shuffle.db"
TEST_DATA = "/tmp/test-townsquare-shorts-shuffle-data"

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


def make_mp4(n=5000):
    return (b"\x00\x00\x00\x1c" + b"ftyp" + b"isom" + b"\x00" * 16 +
            b"\x00\x00\x00\x08" + b"moov" + bytes(n))


_ip = [0]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.77.0.%d" % _ip[0]}


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    from db import Database, ensure_human_auth_schema
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)
    videos.ensure_video_schema(appmod.db)
    appmod.DATA_DIR = TEST_DATA
    appmod.UPLOAD_DIR = os.path.join(TEST_DATA, "uploads")
    os.makedirs(appmod.UPLOAD_DIR, exist_ok=True)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def register(client, handle):
    priv_b64, pub_b64 = fresh_keypair()
    r = client.post("/api/identity/register",
                    json={"handle": handle, "public_key": pub_b64})
    assert r.status_code == 200, r.get_data(as_text=True)
    return priv_b64, r.get_json()["fm_id"]


def post_video(client, priv, fm_id, raw, duration="30"):
    data = signed_body(priv, "upload", fm_id,
                       file_sha256=hashlib.sha256(raw).hexdigest(),
                       ai_generated="0", duration_secs=duration)
    data["video"] = (io.BytesIO(raw), "clip.mp4", "video/mp4")
    r = client.post("/api/upload/video", data=data,
                    content_type="multipart/form-data",
                    environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    return r.get_json()["id"]


def all_ids(client):
    """Walk the whole deck for one client; return the ordered id list."""
    seen, page = [], 0
    for _ in range(20):
        d = client.get("/api/shorts?limit=5&page=%d" % page).get_json()
        assert d["ok"]
        seen.extend(it["id"] for it in d["items"])
        if d["next_page"] is None:
            break
        page = d["next_page"]
    return seen


def main():
    client = setup()
    priv, fm_id = register(client, "ShuffleMuse")
    ids = [post_video(client, priv, fm_id, make_mp4()) for _ in range(12)]

    print("== session seed: stable per visitor, different across visitors ==")
    c1 = appmod.app.test_client()
    c2 = appmod.app.test_client()
    d1 = c1.get("/api/shorts?limit=12").get_json()
    d2 = c2.get("/api/shorts?limit=12").get_json()
    o1 = [it["id"] for it in d1["items"]]
    o2 = [it["id"] for it in d2["items"]]
    check("two visitors both see all 12 clips",
          sorted(o1) == sorted(o2) == sorted(ids), f"{o1} {o2}")
    check("visitors get different orders", o1 != o2,
          f"order1={o1} order2={o2}")
    # same visitor keeps stable order across requests
    d1b = c1.get("/api/shorts?limit=12").get_json()
    check("same session keeps stable order",
          [it["id"] for it in d1b["items"]] == o1)

    print("== paging: no repeats, no skips ==")
    walked = all_ids(c1)
    check("full walk covers every clip exactly once",
          sorted(walked) == sorted(ids) and len(walked) == len(ids),
          str(walked))
    check("walk matches session order", walked == o1,
          f"{walked} vs {o1}")
    # page boundaries across the whole walk
    c3 = appmod.app.test_client()
    got, page = [], 0
    for _ in range(10):
        d = c3.get("/api/shorts?limit=7&page=%d" % page).get_json()
        got.extend(it["id"] for it in d["items"])
        if d["next_page"] is None:
            break
        page = d["next_page"]
    check("limit=7 walk covers all with no dupes",
          sorted(got) == sorted(ids) and len(got) == len(set(got)))
    d = c3.get("/api/shorts?limit=12&page=5").get_json()
    check("out-of-range page -> empty + next_page null",
          d["items"] == [] and d["next_page"] is None)

    print("== Cache-Control must be private on shuffled responses ==")
    r = c1.get("/api/shorts?limit=5")
    cc = r.headers.get("Cache-Control", "")
    check("api/shorts is private", "private" in cc and "public" not in cc, cc)
    r = c1.get("/shorts")
    cc = r.headers.get("Cache-Control", "")
    check("/shorts page is private", "private" in cc and "public" not in cc, cc)
    r = c1.get("/api/shorts?limit=5&before=%d" % max(ids))
    cc = r.headers.get("Cache-Control", "")
    check("legacy ?before= stays publicly cacheable",
          "public" in cc, cc)

    print("== legacy ?before= cursor still newest-first ==")
    d = c1.get("/api/shorts?limit=4&before=%d" % max(ids)).get_json()
    got = [it["id"] for it in d["items"]]
    check("before pages newest-first", got == sorted(ids, reverse=True)[1:5],
          str(got))
    check("legacy next_before cursor", d["next_before"] == got[-1])

    print("== ?video= anchor still works with shuffle ==")
    c4 = appmod.app.test_client()
    # find which clip is last in this visitor's deck
    deck = all_ids(c4)
    last = deck[-1]
    html = c4.get("/shorts?video=%d" % last).get_data(as_text=True)
    check("anchor on deep page included",
          'data-id="%d"' % last in html and 'data-anchor="%d"' % last in html)
    html = c4.get("/shorts").get_data(as_text=True)
    check("page renders with session order",
          html.count('class="short-item"') >= 10)

    print("== /musefm/shorts unchanged (newest-first) ==")
    priv2, fm2 = register(client, "FmClipper")
    fm_ids = []
    for _ in range(6):
        uid = post_video(client, priv2, fm2, make_mp4())
        videos.set_series(appmod.db, uid, "musefm")
        fm_ids.append(uid)
    html = c1.get("/musefm/shorts").get_data(as_text=True)
    import re
    found = [int(x) for x in re.findall(r'data-id="video-(\d+)"', html)]
    fm_found = [i for i in found if i in set(fm_ids)]
    check("musefm feed still newest-first",
          fm_found == sorted(fm_found, reverse=True),
          str(fm_found))

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
