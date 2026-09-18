#!/usr/bin/env python3
"""
Concurrent vote stress: N threads hammer db.vote() on the same target.
The displayed score must always equal SUM(votes.value) afterwards —
the P1 lost-update race (2026-09-18: 10 rapid identical votes left zero
vote rows but a displayed score of -6).

Also benchmarks local Shorts query/render cost.

Throwaway SQLite db. Nothing touches townsquare.db.
"""
import os
import shutil
import sqlite3
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TEST_DB = "/tmp/test-townsquare-votestress.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    from db import Database
    db = Database(TEST_DB)
    db.db.execute("INSERT INTO posts (community, handle, title, body, created_at)"
                  " VALUES ('lobby','Zuckbot','t','b',%d)" % int(time.time()))
    db.db.commit()
    return db


def worker(db, target, value, handle, n, errs):
    for _ in range(n):
        try:
            db.vote("post", target, handle, value)
        except Exception as e:
            errs.append(repr(e))


def main():
    print("== concurrent votes: score == SUM(votes) ==")
    db = setup()
    pid = db.db.execute("SELECT id FROM posts").fetchone()["id"]
    handles = ["Voter%02d" % i for i in range(10)]
    threads = []
    errs = []
    for h in handles:
        t = threading.Thread(target=worker, args=(db, pid, 1, h, 21, errs))
        threads.append(t)
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    dt = time.time() - t0
    check("no vote errors under concurrency", not errs, str(errs[:3]))
    row = db.db.execute("SELECT score FROM posts WHERE id=?", (pid,)).fetchone()
    truth = db.db.execute("SELECT COALESCE(SUM(value),0) s FROM votes"
                          " WHERE target_type='post' AND target_id=?",
                          (pid,)).fetchone()["s"]
    nrows = db.db.execute("SELECT COUNT(*) c FROM votes").fetchone()["c"]
    check("10 voters x 20 votes: 10 vote rows", nrows == 10, str(nrows))
    check("displayed score == SUM(votes)", row["score"] == truth,
          "score=%s truth=%s" % (row["score"], truth))
    check("every vote counted (+1 x 10)", truth == 10, str(truth))
    print("   210 serialized votes in %.2fs" % dt)

    print("== rapid identical votes from one voter (toggle storm) ==")
    db2 = setup()
    pid2 = db2.db.execute("SELECT id FROM posts").fetchone()["id"]
    errs2 = []
    threads = [threading.Thread(target=worker, args=(db2, pid2, 1, "Solo", 25, errs2))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("toggle storm: no errors", not errs2, str(errs2[:3]))
    row = db2.db.execute("SELECT score FROM posts WHERE id=?",
                         (pid2,)).fetchone()
    truth = db2.db.execute("SELECT COALESCE(SUM(value),0) s FROM votes"
                           " WHERE target_type='post' AND target_id=?",
                           (pid2,)).fetchone()["s"]
    check("toggle storm: score == SUM(votes)",
          row["score"] == truth, "score=%s truth=%s" % (row["score"], truth))

    print("== shorts local benchmark ==")
    import app as appmod
    import videos
    from db import Database, ensure_human_auth_schema
    tdb = "/tmp/test-townsquare-bench.db"
    if os.path.exists(tdb):
        os.remove(tdb)
    appmod.db = Database(tdb)
    ensure_human_auth_schema(appmod.db)
    videos.ensure_video_schema(appmod.db)
    appmod.app.config["TESTING"] = True
    # 40 fake short uploads
    for i in range(40):
        appmod.db.db.execute(
            "INSERT INTO video_uploads (fm_id, handle, filename, stored_path,"
            " bytes, mime, ai_generated, created_at, duration_secs)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            ("fm_x", "Bench", "c%d.mp4" % i, "uploads/c%d.mp4" % i, 5000,
             "video/mp4", 0, int(time.time()), 30))
    appmod.db.db.commit()
    t0 = time.time()
    with appmod.app.test_request_context("/"):
        ups, total = videos.shuffled_short_page(appmod.db, "benchseed", limit=10,
                                                page=0)
        items = appmod._short_items(ups)
    q_ms = (time.time() - t0) * 1000
    check("shuffled page returns 10 of 40", len(items) == 10 and total == 40)
    print("   shuffled_short_page + items: %.1f ms (2 aggregate queries)" % q_ms)
    client = appmod.app.test_client()
    t0 = time.time()
    r = client.get("/shorts")
    html_ms = (time.time() - t0) * 1000
    check("/shorts renders 200", r.status_code == 200)
    print("   /shorts page render: %.1f ms (warm, local)" % html_ms)
    t0 = time.time()
    r = client.get("/api/shorts?limit=10&page=2")
    api_ms = (time.time() - t0) * 1000
    check("/api/shorts page=2 -> 200", r.status_code == 200)
    print("   /api/shorts page fetch: %.1f ms (warm, local)" % api_ms)
    os.remove(tdb)

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
