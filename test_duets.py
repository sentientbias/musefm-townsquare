#!/usr/bin/env python3
"""
Tests for Shorts duets / remix chains:
- additive duet_of migration on legacy video_uploads tables
- create_video_upload(duet_of=...): bad parent rejected, non-short duet
  rejected, deleted parent rejected, success persists the pointer
- duet_parent / duet_children / duet_chain assembly + full-depth API tree
- orphaning: deleting a parent NULLs children's duet_of
- duet_marks: feed tile annotations (is_duet, duet_count)
- route-level (works now): unsigned uploads rejected; duet uploads add
  ZERO Signal (no signal_earned in the response, rewards table untouched)
- PATCH-GATED section (runs only when the app.py duet patch is live):
  route accepts/rejects signed duet_of, /api/shorts marks duets,
  GET /api/video/<uid>/duets returns the chain.

Run:  .venv/bin/python test_duets.py
Throwaway SQLite db + Flask test client + temp DATA_DIR.
Nothing touches townsquare.db.
"""
import base64
import hashlib
import io
import os
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import videos
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-duets.db"
TEST_DATA = "/tmp/test-townsquare-duets-data"

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
    # Structurally valid minimal MP4 (ftyp + moov); >= MIN_VIDEO_BYTES.
    return (b"\x00\x00\x00\x1c" + b"ftyp" + b"isom" + b"\x00" * 16 +
            b"\x00\x00\x00\x08" + b"moov" + bytes(n))


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    from db import Database, ensure_human_auth_schema
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)  # mirrors app startup
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


_ip_counter = [100]


def fresh_ip():
    _ip_counter[0] += 1
    return {"REMOTE_ADDR": "10.99.1.%d" % _ip_counter[0]}


def post_video(client, priv, fm_id, raw, duration=None, ai="1",
               filename="clip.mp4", duet_of=None):
    kw = dict(file_sha256=hashlib.sha256(raw).hexdigest(), ai_generated=ai)
    if duration is not None:
        kw["duration_secs"] = duration
    if duet_of is not None:
        kw["duet_of"] = duet_of
    data = signed_body(priv, "upload", fm_id, **kw)
    data["video"] = (io.BytesIO(raw), filename, "video/mp4")
    return client.post("/api/upload/video", data=data,
                       content_type="multipart/form-data", environ_base=fresh_ip())


def signal_count(db, fm_id):
    r = db._one("SELECT COUNT(*) c FROM rewards WHERE fm_id=?", (fm_id,))
    return r["c"] if r else 0


def main():
    client = setup()
    db = appmod.db

    print("== legacy migration: duet_of column ==")
    leg_path = "/tmp/test-townsquare-duets-legacy.db"
    if os.path.exists(leg_path):
        os.remove(leg_path)
    raw = sqlite3.connect(leg_path)
    raw.executescript("""
CREATE TABLE video_uploads (id INTEGER PRIMARY KEY AUTOINCREMENT, fm_id TEXT,
 handle TEXT NOT NULL, filename TEXT NOT NULL, stored_path TEXT NOT NULL,
 bytes INTEGER NOT NULL, mime TEXT NOT NULL, ai_generated INTEGER NOT NULL DEFAULT 0,
 created_at INTEGER NOT NULL);
INSERT INTO video_uploads (fm_id, handle, filename, stored_path, bytes, mime, ai_generated, created_at)
 VALUES ('fm_x','OldMuse','old.mp4','uploads/vid-1.mp4',100,'video/mp4',0,%d);
CREATE TABLE posts (id INTEGER PRIMARY KEY AUTOINCREMENT, community TEXT, handle TEXT,
 title TEXT, body TEXT, created_at INTEGER);
CREATE TABLE comments (id INTEGER PRIMARY KEY AUTOINCREMENT, post_id INTEGER,
 parent_id INTEGER, handle TEXT, body TEXT, created_at INTEGER);
""" % int(time.time()))
    raw.commit()
    raw.close()

    class _Shim:
        def __init__(self, path):
            self.db = sqlite3.connect(path)
            self.db.row_factory = sqlite3.Row

        def _one(self, sql, args=()):
            return self.db.execute(sql, args).fetchone()

        def _exec(self, sql, args=()):
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    leg = _Shim(leg_path)
    videos.ensure_video_schema(leg)
    cols = [r["name"] for r in leg.db.execute("PRAGMA table_info(video_uploads)")]
    check("legacy video_uploads gained duet_of", "duet_of" in cols)
    u = videos.get_video_upload(leg, 1)
    check("legacy row readable, duet_of NULL",
          u is not None and u["duet_of"] is None, str(u))
    check("legacy upload has no duet parent",
          videos.duet_parent(leg, 1) is None)
    check("legacy upload has no duet children",
          videos.duet_children(leg, 1) == [])
    os.remove(leg_path)

    print("== create_video_upload duet_of validation (function level) ==")
    priv, fm_id = register(client, "DuetMuse")
    a, _ = videos.create_video_upload(db, fm_id, "DuetMuse", "a.mp4",
                                      make_mp4(), appmod.UPLOAD_DIR,
                                      ai_generated=True, duration_secs=30,
                                      status="approved")
    for bad in (999999, 0, -3, "abc", "12.5"):
        try:
            videos.create_video_upload(db, fm_id, "DuetMuse", "x.mp4",
                                       make_mp4(), appmod.UPLOAD_DIR,
                                       ai_generated=True, duet_of=bad,
                                       status="approved")
            check("duet_of=%r rejected" % (bad,), False, "accepted!")
        except ValueError:
            check("duet_of=%r rejected" % (bad,), True)
    # duet_of pointing at a deleted upload -> rejected (row is gone)
    tmp, _ = videos.create_video_upload(db, fm_id, "DuetMuse", "tmp.mp4",
                                        make_mp4(), appmod.UPLOAD_DIR,
                                        ai_generated=True, status="approved")
    assert videos.delete_video_upload(db, tmp, appmod.UPLOAD_DIR)
    try:
        videos.create_video_upload(db, fm_id, "DuetMuse", "x.mp4",
                                   make_mp4(), appmod.UPLOAD_DIR,
                                   ai_generated=True, duet_of=tmp,
                                   status="approved")
        check("duet_of deleted parent rejected", False, "accepted!")
    except ValueError:
        check("duet_of deleted parent rejected", True)
    # the duet itself must be short-form: NULL ok, <=180 ok, >180 rejected
    a_children = []
    for ok_dur in (None, "30", "180"):
        uid, _ = videos.create_video_upload(
            db, fm_id, "DuetMuse", "ok.mp4", make_mp4(), appmod.UPLOAD_DIR,
            ai_generated=True, duration_secs=ok_dur, duet_of=a,
            status="approved")
        a_children.append(uid)
        check("duet with duration %r accepted" % (ok_dur,),
              videos.get_video_upload(db, uid)["duet_of"] == a)
    try:
        videos.create_video_upload(db, fm_id, "DuetMuse", "long.mp4",
                                   make_mp4(), appmod.UPLOAD_DIR,
                                   ai_generated=True, duration_secs="600",
                                   duet_of=a, status="approved")
        check("duet with 600s duration rejected", False, "accepted!")
    except ValueError:
        check("duet with 600s duration rejected", True)
    # blank duet_of == no duet
    uid, _ = videos.create_video_upload(db, fm_id, "DuetMuse", "plain.mp4",
                                        make_mp4(), appmod.UPLOAD_DIR,
                                        ai_generated=True, duet_of="",
                                        status="approved")
    check("blank duet_of -> original",
          videos.get_video_upload(db, uid)["duet_of"] is None)
    # duet_of rides the INSERT, not a second write
    b, _ = videos.create_video_upload(db, fm_id, "DuetMuse", "b.mp4",
                                      make_mp4(), appmod.UPLOAD_DIR,
                                      ai_generated=True, duet_of=a,
                                      status="approved")
    a_children.append(b)
    bu = videos.get_video_upload(db, b)
    check("duet_of persisted on row", bu["duet_of"] == a, str(bu))

    print("== chain assembly ==")
    c, _ = videos.create_video_upload(db, fm_id, "DuetMuse", "c.mp4",
                                      make_mp4(), appmod.UPLOAD_DIR,
                                      ai_generated=True, duet_of=b,
                                      status="approved")
    d, _ = videos.create_video_upload(db, fm_id, "DuetMuse", "d.mp4",
                                      make_mp4(), appmod.UPLOAD_DIR,
                                      ai_generated=True, duet_of=a,
                                      status="approved")
    a_children.append(d)
    e, _ = videos.create_video_upload(db, fm_id, "DuetMuse", "e.mp4",
                                      make_mp4(), appmod.UPLOAD_DIR,
                                      ai_generated=True, duet_of=c,
                                      status="approved")
    check("duet_parent(b) == a", (videos.duet_parent(db, b) or {}).get("id") == a)
    check("duet_parent(a) is None", videos.duet_parent(db, a) is None)
    check("duet_children(a) newest-first",
          [x["id"] for x in videos.duet_children(db, a)] ==
          sorted(a_children, reverse=True),
          str([x["id"] for x in videos.duet_children(db, a)]))
    ch = videos.duet_chain(db, e)
    check("chain(e) parents root-first",
          [p["id"] for p in ch["parents"]] == [a, b, c],
          str([p["id"] for p in ch["parents"]]))
    check("chain(e) has no children", ch["children"] == [])
    ch = videos.duet_chain(db, a)
    check("chain(a) no parents", ch["parents"] == [])
    top = {n["id"]: n for n in ch["children"]}
    check("chain(a) children match all direct duets",
          sorted(top) == sorted(a_children), str(sorted(top)))
    check("chain(a) nested: b -> c -> e",
          [n["id"] for n in top[b]["children"]] == [c] and
          [n["id"] for n in top[b]["children"][0]["children"]] == [e],
          str(top))
    # nodes carry feed-ready fields
    node = top[b]
    check("chain nodes carry watch urls",
          node["watch_url"] == "/watch/%d" % b and
          node["video_url"] == "/video/%d" % b and node["handle"],
          str(node))
    # pending remixes stay invisible in the tree
    p_, _ = videos.create_video_upload(db, fm_id, "DuetMuse", "pend.mp4",
                                       make_mp4(), appmod.UPLOAD_DIR,
                                       ai_generated=False, duet_of=a,
                                       status="pending")
    check("pending duet hidden from children",
          p_ not in [x["id"] for x in videos.duet_children(db, a)])
    marks = videos.duet_marks(db, [a, b, p_])
    check("duet_marks pending duet not counted",
          marks[a]["duet_count"] == len(a_children) and
          marks[b]["duet_count"] == 1 and
          marks[b]["is_duet"] is True and marks[a]["is_duet"] is False and
          marks[p_]["is_duet"] is True and marks[p_]["duet_count"] == 0,
          str(marks))
    check("duet_marks empty input -> {}", videos.duet_marks(db, []) == {})

    print("== depth: API returns the FULL chain ==")
    # 5-deep chain: z0 <- z1 <- z2 <- z3 <- z4
    zids = []
    prev = None
    for i in range(5):
        kw = dict(duet_of=prev) if prev else {}
        zid, _ = videos.create_video_upload(
            db, fm_id, "DuetMuse", "z%d.mp4" % i, make_mp4(),
            appmod.UPLOAD_DIR, ai_generated=True, status="approved", **kw)
        zids.append(zid)
        prev = zid
    ch = videos.duet_chain(db, zids[-1])
    check("5-deep chain returns all 4 ancestors",
          [p["id"] for p in ch["parents"]] == zids[:-1],
          str([p["id"] for p in ch["parents"]]))
    node, depth = ch, 0
    # walk the child side from the root instead
    ch = videos.duet_chain(db, zids[0])
    node = ch["children"][0]
    depth = 1
    while node["children"]:
        node = node["children"][0]
        depth += 1
    check("child tree reaches full depth 4 (UI caps at 3)",
          depth == 4 and node["id"] == zids[-1], "depth=%d" % depth)

    print("== delete orphans children ==")
    assert videos.delete_video_upload(db, b, appmod.UPLOAD_DIR)
    check("deleted parent's child orphaned (duet_of NULL)",
          videos.get_video_upload(db, c)["duet_of"] is None)
    check("grandchild still chains to orphaned parent",
          [p["id"] for p in videos.duet_chain(db, e)["parents"]] == [c],
          str(videos.duet_chain(db, e)["parents"]))

    print("== route-level: unsigned rejected, no Signal for duets ==")
    raw = make_mp4()
    before = signal_count(db, fm_id)
    r = post_video(client, priv, fm_id, raw, duration="30", ai="1",
                   duet_of=a)
    # NOTE: until the app.py duet patch lands, the route ignores duet_of.
    j = r.get_json()
    check("signed video upload -> 200", r.status_code == 200,
          f"{r.status_code} {r.get_data(as_text=True)[:200]}")
    check("response has no signal_earned key",
          isinstance(j, dict) and "signal_earned" not in j, str(j))
    check("upload added zero Signal rows",
          signal_count(db, fm_id) == before, str(signal_count(db, fm_id)))
    # unsigned upload -> 401 (the same gate every duet rides)
    r = client.post("/api/upload/video",
                    data={"video": (io.BytesIO(make_mp4()), "x.mp4", "video/mp4")},
                    content_type="multipart/form-data",
                    environ_base=fresh_ip())
    check("unsigned upload -> 401", r.status_code == 401,
          str(r.status_code))

    # ---- patch-gated section: only meaningful once the app.py duet
    # patch (duet_of on /api/upload/video + /api/video/<uid>/duets +
    # feed marks) is applied.
    probe = post_video(client, priv, fm_id, make_mp4(), duet_of="999999")
    if probe.status_code == 200:
        print()
        print("PATCH-GATED route tests SKIPPED: the route ignores duet_of")
        print("(apply the app.py duet patch, then re-run to cover them)")
    else:
        print("== PATCH-GATED: route duet_of handling ==")
        priv2, fm2 = register(client, "DuetMuse2")
        r = post_video(client, priv2, fm2, make_mp4())
        assert r.status_code == 200, r.get_data(as_text=True)[:200]
        pa = r.get_json()["id"]
        r = post_video(client, priv2, fm2, make_mp4(), duration="30",
                       duet_of=str(pa))
        j = r.get_json()
        check("route: duet upload -> 200 + duet_of echoed",
              r.status_code == 200 and j.get("duet_of") == pa, str(j)[:200])
        du = r.get_json()["id"]
        check("route: duet_of persisted",
              videos.get_video_upload(db, du)["duet_of"] == pa)
        check("route: duet earns no Signal",
              "signal_earned" not in (r.get_json() or {}) and
              signal_count(db, fm2) == 0)
        for bad, want in (("999999", 404), ("abc", 400), ("-3", 400)):
            r = post_video(client, priv2, fm2, make_mp4(), duet_of=bad)
            check("route: duet_of=%s -> %d" % (bad, want),
                  r.status_code == want, str(r.status_code))
        r = post_video(client, priv2, fm2, make_mp4(), duration="600",
                       duet_of=str(pa))
        check("route: long duet -> 400", r.status_code == 400,
              str(r.status_code))
        # tampered duet_of -> 401
        raw2 = make_mp4()
        data = signed_body(priv2, "upload", fm2,
                           file_sha256=hashlib.sha256(raw2).hexdigest(),
                           ai_generated="1", duet_of=str(pa))
        data["duet_of"] = "1"
        data["video"] = (io.BytesIO(raw2), "clip.mp4", "video/mp4")
        r = client.post("/api/upload/video", data=data,
                        content_type="multipart/form-data",
                        environ_base=fresh_ip())
        check("route: tampered duet_of -> 401", r.status_code == 401,
              str(r.status_code))
        # unsigned with duet_of -> 401
        r = client.post("/api/upload/video",
                        data={"video": (io.BytesIO(make_mp4()), "x.mp4",
                                        "video/mp4"),
                              "duet_of": str(pa)},
                        content_type="multipart/form-data",
                        environ_base=fresh_ip())
        check("route: unsigned duet -> 401", r.status_code == 401,
              str(r.status_code))
        # deleted parent rejected at the route
        r = post_video(client, priv2, fm2, make_mp4())
        gone = r.get_json()["id"]
        client.post("/api/video/%d/delete" % gone,
                    json=signed_body(priv2, "delete_video", fm2),
                    environ_base=fresh_ip())
        r = post_video(client, priv2, fm2, make_mp4(), duet_of=str(gone))
        check("route: duet of deleted parent -> 404",
              r.status_code == 404, str(r.status_code))

        print("== PATCH-GATED: feed marks ==")
        r = client.get("/api/shorts?limit=50")
        items = {it["id"]: it for it in r.get_json()["items"]}
        check("feed item carries is_duet/duet_count",
              "is_duet" in items[pa] and "duet_count" in items[pa],
              str(items[pa]))
        check("parent tile: not a duet, duet_count=1",
              items[pa]["is_duet"] is False and items[pa]["duet_count"] == 1,
              str({k: items[pa][k] for k in ("is_duet", "duet_count")}))
        check("duet tile: is_duet chip set",
              items[du]["is_duet"] is True and items[du]["duet_count"] == 0,
              str({k: items[du][k] for k in ("is_duet", "duet_count")}))

        print("== PATCH-GATED: GET /api/video/<uid>/duets ==")
        r = client.get("/api/video/%d/duets" % du)
        d = r.get_json()
        check("chain endpoint -> 200", r.status_code == 200,
              str(r.status_code))
        check("chain endpoint payload",
              d.get("ok") and [p["id"] for p in d["parents"]] == [pa] and
              d["children"] == [],
              str(d)[:300])
        r = client.get("/api/video/%d/duets" % pa)
        d = r.get_json()
        check("parent chain lists the child",
              [c["id"] for c in d["children"]] == [du], str(d)[:300])
        r = client.get("/api/video/999999/duets")
        check("unknown video -> 404", r.status_code == 404,
              str(r.status_code))

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
