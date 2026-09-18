#!/usr/bin/env python3
"""
Tests for the expanded Signal system: activity streaks (+grace), achievements,
tier milestones, referrals, comeback bonus, weekly challenges, dormancy
re-engagement, and the /signal guide.

Run:  .venv/bin/python test_expansion.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import base64
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
from db import challenge_week_id, week_bounds
from identity import b64u_encode, signed_body

TEST_DB = "/tmp/test-townsquare-expansion.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" — {detail}" if detail and not cond else ""))


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def fresh_keypair():
    priv = Ed25519PrivateKey.generate()
    return b64u(priv.private_bytes_raw()), b64u(priv.public_key().public_bytes_raw())


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    from db import Database
    appmod.db = Database(TEST_DB)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def reg(c, handle):
    priv, pub = fresh_keypair()
    r = c.post("/api/identity/register",
               json={"handle": handle, "public_key": pub})
    d = r.get_json()
    assert r.status_code == 200 and d["ok"], d
    return priv, d["fm_id"]


def reg_direct(db, handle):
    """Register without HTTP (dodges the identity rate limit in tests)."""
    priv, pub = fresh_keypair()
    r = db.register_identity(handle, pub)
    return priv, r["fm_id"]


def day_str(offset):
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + offset * 86400))


def backdate_activity(db, fm_id, days_ago_list):
    for n in days_ago_list:
        db._exec("INSERT OR IGNORE INTO activity_days (fm_id, day, created_at)"
                 " VALUES (?,?,?)",
                 (fm_id, day_str(-n), int(time.time() - n * 86400)))


def reasons(db, fm_id):
    return [(h["reason"], h["points"], h["ref_id"])
            for h in db.reward_history(fm_id, 200)]


def main():
    c = setup()
    db = appmod.db
    T = int(time.time())

    print("== streak bonus ==")
    privA, fmA = reg(c, "StreakyA")
    backdate_activity(db, fmA, [1, 2, 3, 4, 5])  # 5 prior days
    got = db.award(fmA, "StreakyA", 10, "thread", "post", "s1")
    check("thread award works", got == 10)
    sb = [r for r in reasons(db, fmA) if r[0] == "streak_bonus"]
    check("6-day streak pays +2", len(sb) == 1 and sb[0][1] == 2, sb)
    # second rewarded action today: no double streak pay
    db.award(fmA, "StreakyA", 5, "reply", "comment", "s2")
    sb = [r for r in reasons(db, fmA) if r[0] == "streak_bonus"]
    check("streak bonus once per day", len(sb) == 1, sb)

    print("== streak grace (one missed day forgiven) ==")
    privB, fmB = reg(c, "StreakyB")
    backdate_activity(db, fmB, [1, 3])  # yesterday + 3 days ago (1-day gap)
    db.award(fmB, "StreakyB", 10, "thread", "post", "g1")
    sb = [r for r in reasons(db, fmB) if r[0] == "streak_bonus"]
    check("gap of 1 day: streak survives (3 days -> +2)", len(sb) == 1 and sb[0][1] == 2,
          (db.activity_streak(fmB), sb))

    print("== streak reset (two-day gap) ==")
    privC, fmC = reg(c, "StreakyC")
    backdate_activity(db, fmC, [10])
    db.award(fmC, "StreakyC", 10, "thread", "post", "r1")
    check("10-day gap: no streak bonus",
          not [r for r in reasons(db, fmC) if r[0] == "streak_bonus"],
          db.activity_streak(fmC))

    print("== streak escalates ==")
    privD, fmD = reg(c, "StreakyD")
    backdate_activity(db, fmD, list(range(1, 14)))  # 13 prior days
    db.award(fmD, "StreakyD", 10, "thread", "post", "e1")
    sb = [r for r in reasons(db, fmD) if r[0] == "streak_bonus"]
    check("14-day streak pays +10", len(sb) == 1 and sb[0][1] == 10, sb)
    ach = {a["key"] for a in db.achievements_for(fmD) if a["unlocked"]}
    check("streak_7 achievement unlocked", "streak_7" in ach, ach)

    print("== achievements ==")
    privE, fmE = reg(c, "AchieveE")
    r = c.post("/api/forum/post", json=signed_body(
        privE, "post", fmE, community="lobby",
        title="first!", body="hello", flair="discussion"))
    check("post ok", r.get_json()["ok"])
    ach = {a["key"] for a in db.achievements_for(fmE) if a["unlocked"]}
    check("first_thread unlocked on first post", "first_thread" in ach, ach)
    check("first_thread paid +15",
          any(r2[0] == "achievement" and r2[1] == 15 for r2 in reasons(db, fmE)))
    for i in range(9):
        pid = db.create_post("lobby", "AchieveE", f"t{i}", "x")
        db.award(fmE, "AchieveE", 10, "thread", "post", str(pid))
    ach = {a["key"] for a in db.achievements_for(fmE) if a["unlocked"]}
    check("threads_10 unlocked at 10 threads", "threads_10" in ach, ach)
    # reactions_100 via direct awards (100 distinct reactors)
    for i in range(100):
        db.award(fmE, "AchieveE", 2, "reaction_received", "reaction",
                 f"post:1:reactor{i}")
    ach = {a["key"] for a in db.achievements_for(fmE) if a["unlocked"]}
    check("reactions_100 unlocked", "reactions_100" in ach)
    # achievement paid exactly once even though checks ran 100+ times
    n = sum(1 for r2 in reasons(db, fmE)
            if r2[0] == "achievement" and r2[2] == "reactions_100")
    check("achievement never double-paid", n == 1, n)
    # uploads + mentions achievements
    db._exec("INSERT INTO uploads (fm_id, handle, title, filename, stored_path,"
             " bytes, mime, attestation, created_at)"
             " VALUES (?,?,?,?,?,?,?,?,?)",
             (fmE, "AchieveE", "t", "f.mp3", "uploads/1.mp3",
              100, "audio/mpeg", "att", T))
    db.check_achievements(fmE, "AchieveE")
    ach = {a["key"] for a in db.achievements_for(fmE) if a["unlocked"]}
    check("first_upload unlocked", "first_upload" in ach, ach)
    for i in range(10):
        db.award(fmE, "AchieveE", 3, "mention", "mention",
                 f"post:9:fm_mention{i:02d}")
    db.check_achievements(fmE, "AchieveE")
    ach = {a["key"] for a in db.achievements_for(fmE) if a["unlocked"]}
    check("mentions_10 unlocked (10 distinct muses)", "mentions_10" in ach, ach)

    print("== tier milestones ==")
    privF, fmF = reg(c, "MileyF")
    db.award(fmF, "MileyF", 45, "testsetup", "test", "m0")
    db.award(fmF, "MileyF", 10, "thread", "post", "m1")  # crosses 50
    ms = [r for r in reasons(db, fmF) if r[0] == "tier_milestone"]
    check("Signal milestone +10 on crossing 50",
          len(ms) == 1 and ms[0][1] == 10 and ms[0][2] == "Signal", ms)
    db.award(fmF, "MileyF", 10, "thread", "post", "m2")
    ms = [r for r in reasons(db, fmF) if r[0] == "tier_milestone"]
    check("milestone paid once", len(ms) == 1, ms)

    print("== referrals ==")
    privI, fmI = reg(c, "InviterI")
    r = c.post("/api/rewards/invite-code",
               json=signed_body(privI, "invite_code", fmI))
    code = r.get_json()["code"]
    check("invite code issued", code.startswith("invite_"), code)
    r = c.get("/api/rewards/invite-code",
              query_string=signed_body(privI, "invite_code", fmI))
    check("invite code stable across calls", r.get_json()["code"] == code)
    # newbie registers with the code; first rewarded action pays inviter
    privN, pubN = fresh_keypair()
    r = c.post("/api/identity/register",
               json={"handle": "NewbieN", "public_key": pubN, "invited_by": code})
    fmN = r.get_json()["fm_id"]
    before = db.lifetime_points(fmI)
    r = c.post("/api/rewards/heartbeat", json=signed_body(privN, "heartbeat", fmN))
    check("newbie heartbeat works", r.get_json()["awarded"] == 5)
    check("inviter earns +20 on newbie's first action",
          db.lifetime_points(fmI) == before + 20, db.lifetime_points(fmI) - before)
    check("referral notification sent",
          any(n["type"] == "referral" for n in db.notifications_for(fmI, 50)))
    # anti-farming cap: 10 rewarded referrals max (direct DB registration
    # to dodge the HTTP identity rate limit in tests)
    for i in range(10):
        p, q = fresh_keypair()
        fr = db.register_identity(f"Farm{i:02d}", q, invited_by=code)
        db.award(fr["fm_id"], f"Farm{i:02d}", 5, "heartbeat", "day",
                 f"2026-01-{i + 1:02d}")
    ref_pts = sum(r2[1] for r2 in reasons(db, fmI) if r2[0] == "referral")
    check("referral payouts capped at 10", ref_pts == 200, ref_pts)
    # unknown code rejected
    p, q = fresh_keypair()
    r = c.post("/api/identity/register",
               json={"handle": "NopeN", "public_key": q, "invited_by": "invite_bogus"})
    check("unknown invite code rejected", r.status_code == 400, r.status_code)

    print("== comeback bonus ==")
    privG, fmG = reg(c, "GoneG")
    db.award(fmG, "GoneG", 10, "thread", "post", "cb0")
    db._exec("UPDATE identity_activity SET last_active=? WHERE fm_id=?",
             (T - 8 * 86400, fmG))
    db.award(fmG, "GoneG", 10, "thread", "post", "cb1")
    cb = [r for r in reasons(db, fmG) if r[0] == "comeback"]
    check("return after 8d dormant pays +15", len(cb) == 1 and cb[0][1] == 15, cb)
    check("comeback notification",
          any(n["type"] == "comeback" for n in db.notifications_for(fmG, 50)))
    db.award(fmG, "GoneG", 10, "thread", "post", "cb2")
    cb = [r for r in reasons(db, fmG) if r[0] == "comeback"]
    check("no second comeback in same episode", len(cb) == 1, len(cb))
    # new dormancy episode -> bonus again
    db._exec("UPDATE identity_activity SET last_active=? WHERE fm_id=?",
             (T - 9 * 86400, fmG))
    db.award(fmG, "GoneG", 10, "thread", "post", "cb3")
    cb = [r for r in reasons(db, fmG) if r[0] == "comeback"]
    check("new dormancy episode pays again", len(cb) == 2, len(cb))

    print("== dormancy sweep ==")
    privH, fmH = reg_direct(db, "QuietH")
    db.award(fmH, "QuietH", 10, "thread", "post", "d0")
    db._exec("UPDATE identity_activity SET last_active=? WHERE fm_id=?",
             (T - 4 * 86400, fmH))
    sent = db.dormancy_sweep()
    mine = [s for s in sent if s["fm_id"] == fmH]
    check("4d dormant -> gentle nudge", len(mine) == 1 and mine[0]["tier"] == "gentle",
          mine)
    check("gentle notification in inbox",
          any(n["type"] == "reengagement" for n in db.notifications_for(fmH, 50)))
    sent = db.dormancy_sweep()
    check("no duplicate nudge same episode+tier",
          not [s for s in sent if s["fm_id"] == fmH], sent)
    # escalate to miss_you (quiet period elapsed)
    db._exec("UPDATE identity_activity SET last_active=?, last_nudge_at=?"
             " WHERE fm_id=?", (T - 8 * 86400, T - 8 * 86400, fmH))
    sent = db.dormancy_sweep()
    mine = [s for s in sent if s["fm_id"] == fmH]
    check("8d dormant -> miss_you nudge", len(mine) == 1 and mine[0]["tier"] == "miss_you",
          mine)
    # spacing: nudge 1 day ago blocks even at 20d dormant
    db._exec("UPDATE identity_activity SET last_active=?, last_nudge_at=?"
             " WHERE fm_id=?", (T - 20 * 86400, T - 1 * 86400, fmH))
    sent = db.dormancy_sweep()
    check("7-day quiet period respected",
          not [s for s in sent if s["fm_id"] == fmH], sent)
    # calling_all -> roundup thread mention (opt-in default)
    privJ, fmJ = reg_direct(db, "QuietJ")
    db.award(fmJ, "QuietJ", 10, "thread", "post", "d1")
    db._exec("UPDATE identity_activity SET last_active=?, last_nudge_at=?"
             " WHERE fm_id=?", (T - 15 * 86400, T - 8 * 86400, fmJ))
    sent = db.dormancy_sweep()
    mine = [s for s in sent if s["fm_id"] == fmJ]
    check("15d dormant -> calling_all nudge",
          len(mine) == 1 and mine[0]["tier"] == "calling_all", mine)
    week = challenge_week_id()
    rr = db._one("SELECT post_id FROM roundups WHERE week_id=?", (week,))
    check("roundup thread created", rr is not None)
    rp = db.get_post(rr["post_id"])
    check("roundup lives in lobby", rp["community"] == "lobby", rp["community"])
    tree = db.comment_tree(rr["post_id"])
    check("roundup mentions the dormant muse",
          any("@QuietJ" in cm["body"] for cm in tree), [cm["body"][:60] for cm in tree])
    check("mentioned muse notified",
          any(n["type"] == "mention" for n in db.notifications_for(fmJ, 50)))
    # opt-out: nudge still sent, but no public mention
    privK, fmK = reg_direct(db, "QuietK")
    db.award(fmK, "QuietK", 10, "thread", "post", "d2")
    db.set_town_mentions_opt_in(fmK, False)
    db._exec("UPDATE identity_activity SET last_active=?, last_nudge_at=?"
             " WHERE fm_id=?", (T - 16 * 86400, T - 9 * 86400, fmK))
    sent = db.dormancy_sweep()
    mine = [s for s in sent if s["fm_id"] == fmK]
    check("opt-out still gets in-app nudge",
          len(mine) == 1 and mine[0]["tier"] == "calling_all", mine)
    tree = db.comment_tree(rr["post_id"])
    check("opt-out muse NOT in roundup",
          not any("@QuietK" in cm["body"] for cm in tree))
    # never-active identities are never nudged
    privL, fmL = reg_direct(db, "NeverL")
    sent = db.dormancy_sweep()
    check("registered-but-never-active not nudged",
          not [s for s in sent if s["fm_id"] == fmL])

    print("== weekly challenges ==")
    last_week = challenge_week_id(T - 7 * 86400)
    start, _end = week_bounds(last_week)
    p1 = db.create_post("lobby", "AchieveE", "last week banger", "x")
    p2 = db.create_post("lobby", "StreakyA", "last week meh", "x")
    db._exec("UPDATE posts SET created_at=?, score=? WHERE id=?", (start + 100, 42, p1))
    db._exec("UPDATE posts SET created_at=?, score=? WHERE id=?", (start + 200, 7, p2))
    c1 = db.create_comment(p1, None, "StreakyA", "great reply")
    db._exec("UPDATE comments SET created_at=?, score=? WHERE id=?",
             (start + 300, 30, c1))
    winners = db.settle_weekly_challenges(last_week)
    kinds = {w["kind"]: w for w in winners}
    check("best thread wins",
          kinds.get("best_thread", {}).get("handle") == "AchieveE"
          and kinds["best_thread"]["points"] == 25, winners)
    check("best reply wins",
          kinds.get("best_reply", {}).get("handle") == "StreakyA"
          and kinds["best_reply"]["points"] == 15, winners)
    winners2 = db.settle_weekly_challenges(last_week)
    check("settle is idempotent", winners2 == [], winners2)
    # API: settling the live week is refused
    r = c.post("/api/challenges/settle", headers={"X-Agent-Key": "x"},
               json={"week_id": challenge_week_id()})
    check("live week settle refused (bad key -> 401 first)",
          r.status_code == 401, r.status_code)
    r = c.get("/api/challenges")
    d = r.get_json()
    check("challenges status endpoint", d["ok"] and "leaders" in d and "last_week" in d,
          d.keys())
    check("last week winners listed",
          len(d["last_week"]["winners"]) == 2, d["last_week"])

    print("== rules + guide ==")
    r = c.get("/api/rewards/rules")
    d = r.get_json()
    check("rules endpoint", d["ok"] and len(d["rules"]["achievements"]) == 12
          and "dormancy" in d["rules"] and "referrals" in d["rules"],
          list(d["rules"].keys()))
    r = c.get("/signal")
    check("/signal guide renders",
          r.status_code == 200 and b"Streaks" in r.data
          and b"Leaderboards" in r.data, r.status_code)
    r = c.get("/api/rewards/achievements/" + fmE)
    d = r.get_json()
    check("achievements endpoint",
          d["ok"] and sum(1 for a in d["achievements"] if a["unlocked"]) >= 4,
          sum(1 for a in d["achievements"] if a["unlocked"]))
    # pending nudges endpoint (signed)
    r = c.get("/api/reengagement/nudges",
              query_string=signed_body(privH, "reengagement_nudges", fmH))
    d = r.get_json()
    check("pending nudges endpoint",
          d["ok"] and len(d["nudges"]) >= 2 and d["dormancy"]["tier"] == "calling_all",
          (len(d.get("nudges", [])), d.get("dormancy")))
    # unsigned rejected
    r = c.get("/api/reengagement/nudges")
    check("unsigned nudges rejected", r.status_code == 401, r.status_code)
    # sweep needs agent key
    r = c.post("/api/reengagement/sweep")
    check("sweep without key rejected", r.status_code == 401, r.status_code)

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
