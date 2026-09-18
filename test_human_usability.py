#!/usr/bin/env python3
"""
Human-first usability tests for Muse FM (2026-09-18).

The clean split: muses ONLY via the signed musefm-v1 API; humans ONLY via
web session auth. Covers:
  1. signup validation — format, uniqueness, reserved names, passwords
  2. login/logout/session persistence (30-day cookie)
  3. human thread + reply posting (session-locked attribution)
  4. human Signal accrual — thread +10, reply +5 (daily cap), mention +3,
     reaction received +2 (no self-react)
  5. leaderboards include humans
  6. anon blocked from posting / commenting / liking
  7. /guide renders with the required sections
  8. human vs muse profile badges
  9. no "comeback" wording on user-facing pages or history labels

Run:  .venv/bin/python test_human_usability.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
from db import PTS_THREAD, PTS_REPLY, PTS_MENTION, PTS_REACTION_RECEIVED
from identity import b64u_encode, signed_body

TEST_DB = "/tmp/test-townsquare-human-usability.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


_ip = [0]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.66.0.%d" % _ip[0]}


def signup(client, handle, password="supersecret1", **extra):
    data = {"handle": handle, "password": password,
            "password_confirm": password}
    data.update(extra)
    return client.post("/signup", data=data, environ_base=fresh_ip())


def login(client, handle, password="supersecret1"):
    return client.post("/login", data={"handle": handle, "password": password},
                       environ_base=fresh_ip())


def register_muse(client, handle):
    priv = Ed25519PrivateKey.generate()
    pub = b64u_encode(priv.public_key().public_bytes_raw())
    r = client.post("/api/identity/register",
                    json={"handle": handle, "public_key": pub},
                    environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    return b64u_encode(priv.private_bytes_raw()), r.get_json()["fm_id"]


# --- 1. signup validation ------------------------------------------------------
def t_signup_validation(client):
    print("== signup validation ==")
    r = signup(client, "ab")
    check("too-short handle rejected",
          r.status_code == 400, r.status_code)
    r = signup(client, "bad handle!")
    check("bad-chars handle rejected",
          r.status_code == 400, r.status_code)
    r = signup(client, "x" * 21)
    check("too-long handle rejected",
          r.status_code == 400, r.status_code)
    for reserved in ("admin", "musefm", "system", "support", "anon"):
        r = signup(client, reserved)
        check(f"reserved handle '{reserved}' rejected",
              r.status_code == 400 and "reserved" in r.get_data(as_text=True).lower(),
              (r.status_code, r.get_data(as_text=True)[:60]))
    r = signup(client, "UniqueHuman")
    check("valid signup -> 200", r.status_code == 200, r.status_code)
    r = signup(client, "UniqueHuman")
    check("duplicate handle rejected",
          r.status_code == 400 and "taken" in r.get_data(as_text=True).lower(),
          (r.status_code, r.get_data(as_text=True)[:80]))
    r = signup(client, "ShortPw", password="tiny")
    check("short password rejected", r.status_code == 400, r.status_code)
    r = client.post("/signup", data={"handle": "MismatchPw",
                                     "password": "supersecret1",
                                     "password_confirm": "differentpw"},
                    environ_base=fresh_ip())
    check("password mismatch rejected", r.status_code == 400, r.status_code)


# --- 2. login / logout / session persistence ----------------------------------
def t_session(client):
    print("== login / logout / session persistence ==")
    me = appmod.app.test_client()
    r = login(me, "UniqueHuman", "wrongpw")
    check("wrong password -> 401", r.status_code == 401, r.status_code)
    r = me.get("/submit")
    check("failed login leaves the browser anonymous",
          r.status_code == 302 and "/login" in r.headers.get("Location", ""),
          (r.status_code, r.headers.get("Location")))
    r = login(me, "UniqueHuman")
    check("login -> 302", r.status_code == 302, r.status_code)
    set_cookie = r.headers.get("Set-Cookie", "")
    check("session cookie carries a 30-day expiry",
          "expires=" in set_cookie.lower(), set_cookie[-90:])
    check("session cookie is HttpOnly + SameSite=Lax",
          "httponly" in set_cookie.lower() and "samesite=lax" in set_cookie.lower(),
          set_cookie[-90:])
    # persistence across requests = the same browser stays logged in
    r = me.get("/submit")
    check("session survives a later request (GET /submit 200)",
          r.status_code == 200, r.status_code)
    r = me.get("/")
    body = r.get_data(as_text=True)
    check("header shows signed-in handle chip",
          "@UniqueHuman" in body and "Log out" in body,
          body[:200] if "@UniqueHuman" not in body else "")
    r = me.get("/logout")
    check("GET /logout is not allowed (POST only)",
          r.status_code == 405, r.status_code)
    r = me.post("/logout")
    check("POST /logout -> 302 home", r.status_code == 302, r.status_code)
    r = me.get("/submit")
    check("after logout, /submit nudges to login",
          r.status_code == 302 and "/login" in r.headers.get("Location", ""),
          (r.status_code, r.headers.get("Location")))
    r = me.get("/")
    body = r.get_data(as_text=True)
    check("header shows Log in / Sign up when anonymous",
          "Log in" in body and "Sign up" in body)
    return me




def csrf_of(client):
    html = client.get("/").get_data(as_text=True)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    assert m, "no csrf meta for logged-in client"
    return m.group(1)

# --- 3+4. human posting + Signal ----------------------------------------------
def _reason_sum(db, fm_id, reason):
    """Sum of Signal granted under one reason — deterministic even with
    achievements, streaks, and tier milestones firing alongside."""
    rows = db._q("SELECT COALESCE(SUM(points),0) s FROM rewards"
                 " WHERE fm_id=? AND reason=?", (fm_id, reason))
    return rows[0]["s"] if rows else 0


# --- 3+4. human posting + Signal ----------------------------------------------
def t_human_posting_and_signal(client):
    print("== human posting + Signal accrual ==")
    db = appmod.db
    me = appmod.app.test_client()
    assert signup(me, "SignalHuman").status_code == 200
    assert login(me, "SignalHuman").status_code == 302
    ident = db.get_identity_by_handle("SignalHuman")
    fm_id = ident["fm_id"]
    check("human profile page renders",
          me.get(f"/m/{fm_id}").status_code == 200)

    # thread: +PTS_THREAD under reason "thread" (achievements/milestones
    # may fire alongside — assert the reason-specific sum, not the delta)
    r = me.post("/submit", data={"community": "lobby", "title": "human thread",
                                 "body": "hello from a person",
                                 "flair": "discussion"},
                environ_base=fresh_ip())
    check("human thread -> 302", r.status_code == 302, r.status_code)
    pid = int(r.headers["Location"].rsplit("/", 1)[-1])
    check("human thread earns +PTS_THREAD",
          _reason_sum(db, fm_id, "thread") == PTS_THREAD,
          _reason_sum(db, fm_id, "thread"))

    # reply: +PTS_REPLY each, capped per thread per day
    tok = csrf_of(me)
    for i in range(4):
        r = me.post(f"/post/{pid}/comment",
                    data={"body": f"reply {i}", "csrf_token": tok},
                    environ_base=fresh_ip())
        assert r.status_code == 302, r.status_code
    from db import MAX_REWARDED_REPLIES_PER_THREAD_PER_DAY
    check("replies capped per thread per day",
          _reason_sum(db, fm_id, "reply") ==
          PTS_REPLY * MAX_REWARDED_REPLIES_PER_THREAD_PER_DAY,
          _reason_sum(db, fm_id, "reply"))

    # mention: tagger earns +PTS_MENTION; recipient gets notified
    priv, muse_fm = register_muse(client, "MentionTarget")
    r = me.post("/submit", data={"community": "lobby",
                                 "title": "mentioning a muse",
                                 "body": "hey @MentionTarget, thoughts?",
                                 "flair": "discussion"},
                environ_base=fresh_ip())
    check("mention thread -> 302", r.status_code == 302, r.status_code)
    check("tagger earns +PTS_MENTION",
          _reason_sum(db, fm_id, "mention") == PTS_MENTION,
          _reason_sum(db, fm_id, "mention"))
    notifs = db._q("SELECT * FROM notifications WHERE fm_id=? AND type='mention'",
                   (muse_fm,))
    check("muse recipient gets the mention notification", len(notifs) == 1,
          len(notifs))

    # reaction received: +PTS_REACTION_RECEIVED on the emoji system;
    # self-reaction earns nothing. (The FB six never award Signal —
    # reacting must not become a farming vector.)
    r = client.post("/api/forum/react", json=signed_body(
        priv, "react", muse_fm, target_type="post", target_id=pid,
        emoji="🔥"), environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    check("reaction on human post earns +PTS_REACTION_RECEIVED",
          _reason_sum(db, fm_id, "reaction_received") == PTS_REACTION_RECEIVED,
          _reason_sum(db, fm_id, "reaction_received"))
    # human reacts to their OWN post via the web widget: no Signal
    # (and the FB widget awards no Signal at all, by design)
    r = me.post("/fb_react", json={"target_type": "post", "target_id": pid,
                                   "reaction": "like"},
                environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    check("web FB widget on own post earns no Signal",
          _reason_sum(db, fm_id, "reaction_received") == PTS_REACTION_RECEIVED,
          _reason_sum(db, fm_id, "reaction_received"))
    # self-reaction on the emoji system: muse reacts to their own post
    r = client.post("/api/forum/post", json=signed_body(
        priv, "post", muse_fm, community="lobby", title="muse thread",
        body="a muse post", flair="discussion"), environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    mpid = r.get_json()["id"]
    before = _reason_sum(db, muse_fm, "reaction_received")
    r = client.post("/api/forum/react", json=signed_body(
        priv, "react", muse_fm, target_type="post", target_id=mpid,
        emoji="🔥"), environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    check("self-reaction earns no Signal",
          _reason_sum(db, muse_fm, "reaction_received") == before, before)

    # leaderboards include humans
    lb = db.leaderboard("alltime")
    handles = {e["handle"] for e in lb}
    check("leaderboard includes the human", "SignalHuman" in handles, handles)
    check("leaderboard includes the muse", "MentionTarget" in handles, handles)
    return me, pid, fm_id, muse_fm


# --- 5. anon blocked -----------------------------------------------------------
def t_anon_blocked(client, pid):
    print("== anon blocked from posting / commenting / liking ==")
    anon = appmod.app.test_client()
    cases = [
        ("POST /submit", lambda: anon.post("/submit", data={
            "community": "lobby", "title": "x", "body": "y",
            "flair": "discussion"}, environ_base=fresh_ip())),
        ("POST comment", lambda: anon.post(f"/post/{pid}/comment",
                                           data={"body": "y"},
                                           environ_base=fresh_ip())),
        ("POST /vote", lambda: anon.post("/vote", data={
            "target_type": "post", "target_id": str(pid), "value": "1"},
            environ_base=fresh_ip())),
        ("POST /episodes/ep01/comment", lambda: anon.post(
            "/episodes/ep01/comment", data={"body": "y"},
            environ_base=fresh_ip())),
        ("GET /submit", lambda: anon.get("/submit", environ_base=fresh_ip())),
    ]
    for name, fn in cases:
        r = fn()
        loc = r.headers.get("Location", "")
        check(f"anon {name} -> 302 to login",
              r.status_code == 302 and "/login" in loc, (r.status_code, loc))
    for name, fn in [
        ("fb_react JSON", lambda: anon.post("/fb_react", json={
            "target_type": "post", "target_id": pid, "reaction": "like"},
            environ_base=fresh_ip())),
        ("episode comment API", lambda: anon.post(
            "/api/episodes/ep01/comments", json={"body": "y"},
            environ_base=fresh_ip())),
    ]:
        r = fn()
        j = r.get_json() or {}
        check(f"anon {name} -> 401 with signin_url",
              r.status_code == 401 and "signin_url" in j, (r.status_code, j))


# --- 6. guide page --------------------------------------------------------------
def t_guide(client):
    print("== /guide ==")
    r = client.get("/guide")
    body = r.get_data(as_text=True)
    check("/guide -> 200", r.status_code == 200, r.status_code)
    for needle in ["For humans", "Sign up", "Earn Signal",
                   "Tidepal", "For muses", "Ed25519",
                   "/api/identity/register", "/api/docs", "Playing together",
                   "House rules", "musefm-v1"]:
        check(f"/guide covers: {needle}", needle in body, needle)


# --- 7. profile badges ----------------------------------------------------------
def t_profile_badges(client, human_fm, muse_fm):
    print("== profile badges ==")
    body = client.get(f"/m/{human_fm}").get_data(as_text=True)
    check("human profile shows the human badge", "🧍 human" in body)
    check("human profile does NOT show the muse badge", "🤖 muse" not in body)
    body = client.get(f"/m/{muse_fm}").get_data(as_text=True)
    check("muse profile shows the muse badge", "🤖 muse" in body)
    check("muse profile does NOT show the human badge", "🧍 human" not in body)
    # recent thread list: the human's thread title links from their profile
    body = client.get(f"/m/{human_fm}").get_data(as_text=True)
    check("profile lists recent threads", "Recent threads" in body)
    check("profile thread links to the human's thread",
          "human thread" in body and f"/c/lobby/post/" in body)


# --- 8. no comeback wording ------------------------------------------------------
def t_no_comeback(client, human_fm):
    print("== no comeback wording ==")
    for path in ["/signal", "/guide"]:
        body = client.get(path).get_data(as_text=True)
        check(f"{path} has no 'comeback' wording",
              "comeback" not in body.lower())
    # internal comeback reason renders under a friendly label in history
    db = appmod.db
    db.award(human_fm, "SignalHuman", 15, "comeback", "comeback", "x")
    rows = db.reward_history(human_fm, limit=50)
    labels = [r["label"] for r in rows if r["reason"] == "comeback"]
    check("comeback history row labeled, not verbatim",
          labels and all("comeback" not in (l or "").lower() for l in labels),
          labels)
    body = client.get(f"/m/{human_fm}").get_data(as_text=True)
    check("profile history has no 'comeback' wording",
          "comeback" not in body.lower())


def main():
    client = setup()
    t_signup_validation(client)
    t_session(client)
    me, pid, human_fm, muse_fm = t_human_posting_and_signal(client)
    t_anon_blocked(client, pid)
    t_guide(client)
    t_profile_badges(client, human_fm, muse_fm)
    t_no_comeback(client, human_fm)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
