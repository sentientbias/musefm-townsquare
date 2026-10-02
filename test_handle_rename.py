#!/usr/bin/env python3
"""Focused tests for the handle-rename feature (2026-10-02, Anthony).

Covers db.rename_handle against an in-memory DB:
  - rename moves identities.handle AND every denormalized handle column
  - identities.human_handle is left untouched
  - rejects bad format / reserved / taken / unknown-old handles
  - handle_change_requests: create, one-pending-per-user, list, decide
    (approve runs the rename; reject leaves the handle alone)
Throwaway in-memory DB; nothing touches the real townsquare.db.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import (Database, ensure_handle_change_schema,
                ensure_forum_flags_schema)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (" -- %s" % detail if detail and not cond else ""))


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


def fresh_db():
    db = Database(":memory:")
    ensure_handle_change_schema(db)
    ensure_forum_flags_schema(db)
    # Workroom + Row tables: the rename covers their denormalized handle
    # columns, so the test DB needs them too.
    import workroom
    workroom.ensure_workroom_schema(db)
    import row as rowmod
    rowmod.ensure_row_schema(db)
    return db


def seed_member(db, handle, human_handle=""):
    db._exec(
        "INSERT INTO identities (fm_id, handle, public_key, created_at,"
        " visibility, human_handle) VALUES (?,?,?,?,?,?)",
        ("fm_" + handle, handle, "k", 1, "anonymous", human_handle))


def seed_everywhere(db, h):
    """Put handle h in every denormalized column rename_handle touches."""
    db._exec("INSERT INTO posts (community, handle, title, body, created_at)"
             " VALUES (?,?,?,?,?)", ("lobby", h, "t", "b", 1))
    db._exec("INSERT INTO comments (post_id, handle, body, created_at)"
             " VALUES (?,?,?,?)", (1, h, "c", 1))
    db._exec("INSERT INTO votes (target_type, target_id, handle, value,"
             " created_at) VALUES (?,?,?,?,?)", ("post", 1, h, 1, 1))
    db._exec("INSERT INTO photos (title, img_path, handle, created_at)"
             " VALUES (?,?,?,?)", ("p", "/x.jpg", h, 1))
    ep = "daily-news-2026-09-23"
    db._exec("INSERT INTO episode_comments (episode_slug, handle, body,"
             " created_at) VALUES (?,?,?,?)", (ep, h, "ec", 1))
    db._exec("INSERT INTO clips (episode_slug, handle, start_sec, end_sec,"
             " created_at) VALUES (?,?,?,?,?)", (ep, h, 0, 10, 1))
    db._exec("INSERT INTO rewards (fm_id, handle, points, reason, created_at)"
             " VALUES (?,?,?,?,?)", ("fm_" + h, h, 5, "r", 1))
    db._exec("INSERT INTO mentions (mentioned_fm_id, mentioner_fm_id,"
             " mentioner_handle, ref_type, ref_id, created_at)"
             " VALUES (?,?,?,?,?,?)", ("fm_x", "fm_" + h, h, "post", "1", 1))
    db._exec("INSERT INTO reactions (target_type, target_id, reactor, handle,"
             " emoji, created_at) VALUES (?,?,?,?,?,?)",
             ("post", 1, h, h, "x", 1))
    db._exec("INSERT INTO signals (target_type, target_id, reactor, handle,"
             " reaction, created_at) VALUES (?,?,?,?,?,?)",
             ("post", 1, h, h, "y", 1))
    db._exec("INSERT INTO uploads (fm_id, handle, title, filename, stored_path,"
             " bytes, mime, attestation, created_at)"
             " VALUES (?,?,?,?,?,?,?,?,?)",
             ("fm_" + h, h, "u", "f.mp3", "/s/f.mp3", 10, "audio/mp3", "a", 1))
    db._exec("INSERT INTO invite_codes (code, fm_id, handle, created_at, uses)"
             " VALUES (?,?,?,?,?)", ("CODE1", "fm_" + h, h, 1, 0))
    db._exec("INSERT INTO referrals (inviter_fm_id, inviter_handle, new_fm_id,"
             " new_handle, created_at, rewarded) VALUES (?,?,?,?,?,?)",
             ("fm_" + h, h, "fm_z", "zeta", 1, 0))
    db._exec("INSERT INTO referrals (inviter_fm_id, inviter_handle, new_fm_id,"
             " new_handle, created_at, rewarded) VALUES (?,?,?,?,?,?)",
             ("fm_z", "zeta", "fm_" + h, h, 1, 0))
    db._exec("INSERT INTO room_presence (room_id, identity_key, fm_id, handle,"
             " last_seen) VALUES (?,?,?,?,?)", (1, "k", "fm_" + h, h, 1))
    db._exec("INSERT INTO room_chat (room_id, fm_id, handle, body, created_at)"
             " VALUES (?,?,?,?,?)", (1, "fm_" + h, h, "hi", 1))
    db._exec("INSERT INTO room_reactions (room_id, handle, emoji, created_at)"
             " VALUES (?,?,?,?)", (1, h, "z", 1))
    db._exec("INSERT INTO bulletin (fm_id, handle, kind, text, created_at)"
             " VALUES (?,?,?,?,?)", ("fm_" + h, h, "note", "n", 1))
    db._exec("INSERT INTO post_flags (target_type, target_id, flagger_fm_id,"
             " flagger_handle, created_at) VALUES (?,?,?,?,?)",
             ("post", 1, "fm_" + h, h, 1))
    # Workroom + Row denormalized handles.
    db._exec("INSERT INTO endorsements (fm_id, endorser_fm_id,"
             " endorser_handle, skill, note, created_at)"
             " VALUES (?,?,?,?,?,?)", ("fm_z", "fm_" + h, h, "s", "n", 1))
    db._exec("INSERT INTO workroom_notes (workroom_id, author_fm_id,"
             " author_handle, kind, body, created_at)"
             " VALUES (?,?,?,?,?,?)", (1, "fm_" + h, h, "note", "b", 1))
    db._exec("INSERT INTO workroom_invites (room_id, inviter_fm_id,"
             " invitee_fm_id, invitee_handle, created_at)"
             " VALUES (?,?,?,?,?)", (1, "fm_z", "fm_" + h, h, 1))
    db._exec("INSERT INTO workroom_knocks (room_id, fm_id, handle, message,"
             " created_at) VALUES (?,?,?,?,?)", (1, "fm_" + h, h, "m", 1))
    db._exec("INSERT INTO row_avatar (fm_id, handle, config, updated_at)"
             " VALUES (?,?,?,?)", ("fm_" + h, h, "{}", 1))
    db._exec("INSERT INTO row_presence (fm_id, handle, updated_at)"
             " VALUES (?,?,?)", ("fm_" + h, h, 1))
    db._exec("INSERT INTO row_journal (fm_id, handle, kind, text, created_at)"
             " VALUES (?,?,?,?,?)", ("fm_" + h, h, "moment", "t", 1))


def count_handle(db, table, col, h):
    return db._one("SELECT COUNT(*) c FROM %s WHERE %s=? COLLATE NOCASE"
                   % (table, col), (h,))["c"]


# --- rename moves everything -------------------------------------------
db = fresh_db()
seed_member(db, "oldmuse", human_handle="linked_human")
seed_member(db, "othermuse")
seed_everywhere(db, "oldmuse")
res = db.rename_handle("oldmuse", "newmuse")
check("rename returns old/new handles",
      res["old_handle"] == "oldmuse" and res["new_handle"] == "newmuse")
check("identities.handle moved",
      db.get_identity("fm_oldmuse")["handle"] == "newmuse")
check("old handle gone from identities",
      db.get_identity_by_handle("oldmuse") is None)
for table, col in Database._RENAME_COLUMNS:
    left = count_handle(db, table, col, "oldmuse")
    check("rename moved %s.%s" % (table, col), left == 0, "left=%d" % left)
moved = count_handle(db, "posts", "handle", "newmuse")
check("new handle present in posts", moved == 1)
check("referrals.new_handle also moved",
      count_handle(db, "referrals", "new_handle", "newmuse") == 1)
check("referrals.inviter_handle also moved",
      count_handle(db, "referrals", "inviter_handle", "newmuse") == 1)
check("human_handle untouched",
      db.get_identity("fm_oldmuse")["human_handle"] == "linked_human")
check("other member untouched",
      db.get_identity_by_handle("othermuse") is not None)
# case-insensitive old handle
db.rename_handle("NEWMUSE", "finalmuse")
check("old handle match is case-insensitive",
      db.get_identity("fm_oldmuse")["handle"] == "finalmuse")

# --- rename validation --------------------------------------------------
db2 = fresh_db()
seed_member(db2, "alice")
seed_member(db2, "bob")
expect_value_error("rejects unknown old handle",
                   db2.rename_handle, "nobody", "whatever")
expect_value_error("rejects bad format handle",
                   db2.rename_handle, "alice", "x")
expect_value_error("rejects handle with spaces",
                   db2.rename_handle, "alice", "bad name")
expect_value_error("rejects reserved handle",
                   db2.rename_handle, "alice", "admin")
expect_value_error("rejects reserved handle (case)",
                   db2.rename_handle, "alice", "Support")
expect_value_error("rejects taken handle",
                   db2.rename_handle, "alice", "bob")
expect_value_error("rejects taken handle (case)",
                   db2.rename_handle, "alice", "BOB")
expect_value_error("rejects renaming to same handle",
                   db2.rename_handle, "alice", "alice")
check("failed rename left alice alone",
      db2.get_identity_by_handle("alice") is not None)

# --- handle_change_requests ---------------------------------------------
db3 = fresh_db()
seed_member(db3, "carol")
seed_member(db3, "dave")
req = db3.create_handle_request("fm_carol", "carol_new", "fresh start")
check("request created with id", bool(req["id"]))
check("request old/new captured",
      req["old_handle"] == "carol" and req["new_handle"] == "carol_new")
expect_value_error("one pending request per user",
                   db3.create_handle_request, "fm_carol", "carol_two", "")
expect_value_error("request validates format",
                   db3.create_handle_request, "fm_dave", "x", "")
expect_value_error("request rejects reserved",
                   db3.create_handle_request, "fm_dave", "moderator", "")
expect_value_error("request rejects taken",
                   db3.create_handle_request, "fm_dave", "carol", "")
pend = db3.list_handle_requests("pending")
check("list pending shows the request", len(pend) == 1)
check("pending helper finds it",
      db3.pending_handle_request_for("fm_carol")["new_handle"] == "carol_new")
check("pending helper empty for dave",
      db3.pending_handle_request_for("fm_dave") is None)

dec = db3.decide_handle_request(req["id"], True, "AMRADIOverse")
check("approve marks approved", dec["status"] == "approved")
check("approve ran the rename",
      db3.get_identity("fm_carol")["handle"] == "carol_new")
row = db3._one("SELECT * FROM handle_change_requests WHERE id=?",
               (req["id"],))
check("decided_at recorded", row["decided_at"] > 0)
check("decided_by recorded", row["decided_by"] == "AMRADIOverse")
check("no longer pending",
      db3.pending_handle_request_for("fm_carol") is None)
check("list pending now empty",
      db3.list_handle_requests("pending") == [])
expect_value_error("cannot decide twice",
                   db3.decide_handle_request, req["id"], True, "AMRADIOverse")
expect_value_error("unknown request id",
                   db3.decide_handle_request, 99999, True, "AMRADIOverse")

req2 = db3.create_handle_request("fm_dave", "dave_new", "")
dec2 = db3.decide_handle_request(req2["id"], False, "AMRADIOverse")
check("reject marks rejected", dec2["status"] == "rejected")
check("reject leaves handle alone",
      db3.get_identity("fm_dave")["handle"] == "dave")
check("reject can re-request after",
      db3.create_handle_request("fm_dave", "dave_new", "")["id"] > 0)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
