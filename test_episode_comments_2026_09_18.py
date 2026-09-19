#!/usr/bin/env python3
"""
Tests for episode comments on DBs built straight from Database()
(tests/scratch), which skip init_db's ensure chain.

Regression test for the 2026-09-18 report that episode comments were
"fully broken at the schema level": add_episode_comment INSERTs parent_id
and episode_comment_tree reads it, but the base CREATE TABLE for
episode_comments predates the comment-pro migration. The db methods now
carry a lazy _ensure_episode_comment_cols() (same precedent as
_ensure_photo_status_col), and the CREATE TABLE itself declares the
columns for fresh DBs.

Run:  python3 test_episode_comments_2026_09_18.py
Uses throwaway SQLite DBs. Nothing touches the real townsquare.db.
"""
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import Database

TEST_DB = "/tmp/test-townsquare-ep-comments.db"
OLD_DB = "/tmp/test-townsquare-ep-comments-old.db"

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))


def cols(db, table="episode_comments"):
    return [r["name"] for r in db.db.execute(f"PRAGMA table_info({table})")]


def seed_episode(db, slug="ep-t1"):
    db._exec(
        "INSERT OR IGNORE INTO episodes"
        " (slug, title, series, description, audio_file, duration_sec, published)"
        " VALUES (?,?,?,?,?,?,?)",
        (slug, "Test Ep", "test", "desc", "t.mp3", 60, 1))


def main():
    for f in (TEST_DB, OLD_DB):
        if os.path.exists(f):
            os.remove(f)

    # ---- 1. bare Database(): the footgun scenario -------------------------
    print("== bare Database() round-trip ==")
    db = Database(TEST_DB)
    check("parent_id declared in base CREATE TABLE now", "parent_id" in cols(db))
    seed_episode(db)

    top = db.add_episode_comment("ep-t1", "Zuckbot", "top level here")
    check("top-level insert works on bare Database()", isinstance(top, int))
    check("parent_id column lazily added", "parent_id" in cols(db))
    check("score column lazily added", "score" in cols(db))

    reply = db.add_episode_comment("ep-t1", "Mikey", "nested reply", parent_id=top)
    check("nested reply insert works", isinstance(reply, int) and reply != top)

    tree = db.episode_comment_tree("ep-t1")
    check("tree has one top-level node", len(tree) == 1, f"got {len(tree)}")
    check("top-level parent_id is None", tree[0]["parent_id"] is None)
    check("reply nested under parent",
          len(tree[0]["replies"]) == 1 and tree[0]["replies"][0]["id"] == reply)
    check("reply parent_id points at parent",
          tree[0]["replies"][0]["parent_id"] == top)

    # bad parent rejected, unknown episode rejected
    try:
        db.add_episode_comment("ep-t1", "Zuckbot", "x", parent_id=99999)
        check("unknown parent rejected", False)
    except ValueError:
        check("unknown parent rejected", True)
    try:
        db.add_episode_comment("nope", "Zuckbot", "x")
        check("unknown episode rejected", False)
    except ValueError:
        check("unknown episode rejected", True)

    # voting works on the lazily-migrated table
    s = db.vote("episode_comment", top, "Mikey", 1)
    check("vote on episode comment works on bare Database()", s == 1, f"score={s}")

    # flat listing still fine
    flat = db.episode_comments("ep-t1")
    check("flat listing returns both", len(flat) == 2, f"got {len(flat)}")

    # ---- 2. migration path: pre-migration DB file -------------------------
    print("== pre-migration DB file ==")
    con = sqlite3.connect(OLD_DB)
    con.execute("""CREATE TABLE episode_comments (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      episode_slug TEXT NOT NULL, handle TEXT NOT NULL,
      body TEXT NOT NULL, created_at INTEGER NOT NULL)""")
    con.execute("CREATE TABLE episodes (slug TEXT PRIMARY KEY, title TEXT, series TEXT,"
                " description TEXT, audio_file TEXT, duration_sec INTEGER, published INTEGER)")
    con.execute("INSERT INTO episodes VALUES ('ep-old','Old','t','d','o.mp3',60,1)")
    con.execute("INSERT INTO episode_comments (episode_slug, handle, body, created_at)"
                " VALUES ('ep-old','OldUser','legacy comment',1700000000)")
    con.commit()
    con.close()

    db2 = Database(OLD_DB)
    check("old DB lacks parent_id before calls", "parent_id" not in cols(db2))
    tree2 = db2.episode_comment_tree("ep-old")
    check("tree works on migrated legacy DB", len(tree2) == 1)
    check("legacy row parent_id is NULL", tree2[0]["parent_id"] is None)
    check("legacy body intact", tree2[0]["body"] == "legacy comment")
    cid = db2.add_episode_comment("ep-old", "Zuckbot", "reply to legacy",
                                  parent_id=tree2[0]["id"])
    check("nested reply on migrated DB", isinstance(cid, int))
    t3 = db2.episode_comment_tree("ep-old")
    check("reply attached to legacy parent",
          len(t3[0]["replies"]) == 1 and t3[0]["replies"][0]["parent_id"] == t3[0]["id"])

    # ---- 3. fresh CREATE TABLE declares the columns -----------------------
    print("== fresh schema declares columns ==")
    fresh = "/tmp/test-townsquare-ep-comments-fresh.db"
    if os.path.exists(fresh):
        os.remove(fresh)
    db3 = Database(fresh)
    c3 = cols(db3)
    check("fresh table has parent_id", "parent_id" in c3, str(c3))
    check("fresh table has score", "score" in c3)
    check("fresh table has edited_at", "edited_at" in c3)
    try:
        os.remove(fresh)
    except OSError:
        pass

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
