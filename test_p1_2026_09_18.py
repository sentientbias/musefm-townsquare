#!/usr/bin/env python3
"""
Regression tests for the 9 tester-loop P1s fixed 2026-09-18.

Covers:
  1. JSON-array / non-object request bodies -> 400 (not 500)
  2. Rate limits keyed on REMOTE_ADDR only — rotating attacker-controlled
     X-Forwarded-For values cannot open new buckets (ProxyFix x_for=1)
  3. --db rebind runs the FULL schema-ensure sequence (init_db), so fresh
     --db files have uploads/gif/video/fb_reaction/media tables
  4. Unsigned web post/comment paths fire reply + mention notifications
     (same helpers as the signed API); web authors earn no Signal
  5. Out-of-range integer IDs -> 404 on routes, 400 on signed JSON bodies
  6. Non-string identity-registration fields -> 400 (not 500)
  7. One sqlite connection per thread — concurrent requests don't 500
  8. Shop idempotency-key retry after a successful charge -> 200
     already_owned/charged=0 (not 402), via HTTP
  9. Unsigned web forms reject registered handles (400), unregistered
     handles keep working

Run:  .venv/bin/python test_p1_2026_09_18.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import base64
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import shop as shopmod
from db import Database
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-p1-20260918.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    # init_db is the same helper __main__ now uses after a --db rebind
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


_ip = [0]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.200.0.%d" % _ip[0]}


def register(client, handle):
    """Register an identity via the API. Returns (priv_b64, fm_id)."""
    priv = Ed25519PrivateKey.generate()
    pub = b64u(priv.public_key().public_bytes_raw())
    r = client.post("/api/identity/register", json={
        "handle": handle, "public_key": pub}, environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    j = r.get_json()
    return b64u(priv.private_bytes_raw()), j["fm_id"]


# --- P1 #1: non-object JSON bodies -------------------------------------------
def t_json_arrays(client):
    r = client.post("/api/forum/comment", data='["hi"]',
                    content_type="application/json")
    check("json array body on comment endpoint -> 400",
          r.status_code == 400, r.status_code)
    r = client.post("/api/forum/post", data='["hi"]',
                    content_type="application/json")
    check("json array body on post endpoint -> 400",
          r.status_code == 400, r.status_code)
    r = client.post("/api/forum/vote", data='"justastring"',
                    content_type="application/json")
    check("json string body on vote endpoint -> 400",
          r.status_code == 400, r.status_code)
    # typed garbage inside an object must also 400, not 500
    r = client.post("/api/identity/register",
                    json={"handle": 12345, "public_key": b64u(b"x" * 32)},
                    environ_base=fresh_ip())
    check("non-string handle on register -> 400",
          r.status_code == 400, r.status_code)
    r = client.post("/api/identity/register",
                    json={"handle": "TypeTest9", "public_key": 99999},
                    environ_base=fresh_ip())
    check("non-string public_key on register -> 400",
          r.status_code == 400, r.status_code)
    r = client.post("/api/identity/claim-human",
                    json={"handle": "ClaimType9", "avatar_url": ["x"]},
                    environ_base=fresh_ip())
    check("non-string avatar_url on claim_human -> 400",
          r.status_code == 400, r.status_code)


# --- P1 #2: X-Forwarded-For rotation ------------------------------------------
def t_xff_rotation(client):
    # Simulate the proxy setup: Render appends the real client IP as the
    # LAST X-Forwarded-For entry. Attacker-controlled leading entries rotate;
    # the proxy-appended tail stays constant. All six requests must land in
    # ONE rate-limit bucket -> the 6th is 429 (claim_human allows 5/hour).
    statuses = []
    for i in range(6):
        r = client.post("/api/identity/claim-human",
                        json={"handle": f"XffUser{i}"},
                        headers={"X-Forwarded-For":
                                 f"10.9.9.{i + 1}, 192.0.2.7"})
        statuses.append(r.status_code)
    check("rotating attacker XFF entries share one rate-limit bucket",
          statuses[:5] == [200] * 5 and statuses[5] == 429, statuses)
    # ...while genuinely different client IPs (different proxy tails) keep
    # their own buckets.
    r = client.post("/api/identity/claim-human",
                    json={"handle": "XffUserOther"},
                    headers={"X-Forwarded-For": "10.9.9.99, 192.0.2.8"})
    check("different proxy-supplied IP keeps its own bucket",
          r.status_code == 200, r.status_code)


# --- P1 #3: --db rebind schema ------------------------------------------------
def t_db_rebind_schema():
    p_old = "/tmp/test-townsquare-p1-olddb.db"
    p_new = "/tmp/test-townsquare-p1-newdb.db"
    for p in (p_old, p_new):
        if os.path.exists(p):
            os.remove(p)
    # what __main__ USED to do: bare Database() — documents the bug
    old_db = Database(p_old)
    missing = old_db._one(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name='gif_uploads'")
    check("bare Database() lacks gif_uploads (the old --db bug)",
          missing is None, missing)
    # what __main__ does NOW: init_db runs every ensure + seeds
    new_db = appmod.init_db(p_new)
    for tbl in ("uploads", "gif_uploads", "ai_uploads", "video_uploads",
                "fb_reactions", "photos"):
        got = new_db._one(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (tbl,))
        check(f"init_db creates table {tbl}", got is not None, tbl)
    cols = [r["name"] for r in new_db._q("PRAGMA table_info(episodes)")]
    check("init_db adds episodes.video_file column", "video_file" in cols)
    n = new_db._one("SELECT COUNT(*) c FROM episodes")["c"]
    check("init_db runs idempotent seeds", n >= 4, n)
    for p in (p_old, p_new):
        os.remove(p)


# --- P1 #5: huge integer IDs ---------------------------------------------------
def t_huge_ids(client):
    huge = 99999999999999999999999
    r = client.get(f"/video/{huge}")
    check("GET /video/<huge int> -> 404", r.status_code == 404,
          r.status_code)
    r = client.get(f"/c/lobby/post/{huge}")
    check("GET /c/lobby/post/<huge int> -> 404", r.status_code == 404,
          r.status_code)
    r = client.get(f"/gif/{huge}")
    check("GET /gif/<huge int> -> 404", r.status_code == 404,
          r.status_code)
    priv, fm = register(client, "HugeIdUser")
    r = client.post("/api/forum/vote", json=signed_body(
        priv, "vote", fm, target_type="post", target_id=10 ** 30, value=1),
        environ_base=fresh_ip())
    check("signed vote with target_id=10**30 -> 400 (not 500)",
          r.status_code == 400, r.status_code)
    r = client.post("/api/forum/comment", json=signed_body(
        priv, "comment", fm, post_id=10 ** 30, body="hi"),
        environ_base=fresh_ip())
    check("signed comment with post_id=10**30 -> 400 (not 500)",
          r.status_code == 400, r.status_code)


# --- P1 #4: web post/comment notifications -------------------------------------
def t_web_notifications(client):
    """Logged-in human web comments/threads fire reply + mention
    notifications (same helpers as the signed API) and earn Signal."""
    db = appmod.db
    priv, fm_target = register(client, "P1Target")
    # signed post by the registered identity
    r = client.post("/api/forum/post", json=signed_body(
        priv, "post", fm_target, community="lobby",
        title="notify me", body="hello town"), environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    pid = r.get_json()["id"]

    # human signup + login for this "browser"
    human = appmod.app.test_client()
    r = human.post("/signup", data={"handle": "WebFan",
                                    "password": "supersecret1",
                                    "password_confirm": "supersecret1"},
                   environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    r = human.post("/login", data={"handle": "WebFan",
                                   "password": "supersecret1"},
                   environ_base=fresh_ip())
    assert r.status_code == 302, r.get_data(as_text=True)

    # logged-in human web comment, mentioning + replying
    r = human.post(f"/post/{pid}/comment",
                   data={"body": "@P1Target great post!"},
                   environ_base=fresh_ip())
    check("logged-in human web comment posts (redirect)", r.status_code == 302,
          r.status_code)
    notifs = db._q("SELECT type, ref_type FROM notifications WHERE fm_id=?",
                   (fm_target,))
    types = sorted((n["type"], n["ref_type"]) for n in notifs)
    check("web comment fires reply notification to post author",
          ("reply", "comment") in types, types)
    check("web comment fires mention notification",
          ("mention", "comment") in types, types)
    # logged-in human web thread with a mention
    r = human.post("/submit",
                   data={"community": "lobby",
                         "title": "web thread", "body": "hi @P1Target"},
                   environ_base=fresh_ip())
    check("logged-in human web thread posts (redirect)", r.status_code == 302,
          r.status_code)
    notifs = db._q("SELECT type, ref_type FROM notifications WHERE fm_id=?",
                   (fm_target,))
    types = sorted((n["type"], n["ref_type"]) for n in notifs)
    check("web thread fires mention notification",
          ("mention", "post") in types, types)
    # ...and the human author earns Signal on the same economy
    human_rewards = db._q(
        "SELECT reason, points FROM rewards WHERE handle='WebFan'")
    reasons = {r[0] for r in human_rewards}
    check("human web author earns thread + reply Signal",
          {"thread", "reply"} <= reasons, human_rewards)


# --- P1 #7: per-thread sqlite connections --------------------------------------
def t_threading():
    if os.path.exists("/tmp/test-townsquare-p1-threads.db"):
        os.remove("/tmp/test-townsquare-p1-threads.db")
    db = Database("/tmp/test-townsquare-p1-threads.db")
    errors = []
    conns = set()
    lock = threading.Lock()

    def worker(n):
        try:
            with lock:
                conns.add(id(db.db))
            for i in range(25):
                pid = db.create_post("lobby", f"thr{n}", f"t{n}-{i}",
                                     "body text here")
                db.vote("post", pid, f"thr{n}", 1)
                db.list_posts(limit=5)
                db.get_post(pid)
        except Exception as e:  # noqa: BLE001
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("12 threads x 100 db ops: zero errors", not errors, errors[:2])
    check("each thread got its own sqlite connection", len(conns) == 12,
          len(conns))
    os.remove("/tmp/test-townsquare-p1-threads.db")


# --- P1 #8: shop idempotency via HTTP ------------------------------------------
def t_shop_idem_http(client):
    db = appmod.db
    priv, fm = register(client, "ShopKeep9")
    db.award(fm, "ShopKeep9", 20, "thread", "post", "p1-shop-seed")
    assert shopmod.spendable(db, fm) >= 20
    body = signed_body(priv, "shop_buy", fm, item="rename_token",
                       idempotency_key="p1httpkey")
    r = client.post("/api/shop/buy", json=body, environ_base=fresh_ip())
    j = r.get_json()
    check("first buy -> 200 charged=20",
          r.status_code == 200 and j["charged"] == 20
          and not j["already_owned"], (r.status_code, j))
    retry_body = signed_body(priv, "shop_buy", fm, item="rename_token",
                         idempotency_key="p1httpkey")
    r = client.post("/api/shop/buy", json=retry_body,
                    environ_base=fresh_ip())
    j = r.get_json()
    check("retry with same idempotency key -> 200 already_owned/charged=0",
          r.status_code == 200 and j["already_owned"]
          and j["charged"] == 0, (r.status_code, j))
    n = db._one("SELECT COUNT(*) c FROM shop_purchases"
                " WHERE fm_id=? AND ref_id=?", (fm, "p1httpkey"))["c"]
    check("exactly one purchase row for the key", n == 1, n)


# --- P1 #9: clean identity split -----------------------------------------------
def t_impersonation(client):
    """Clean split: muses ONLY via the signed API, humans ONLY via web
    session auth. Anonymous web writes are nudged to sign in (302 on form
    posts, 401 on JSON posts); a session can never post as anyone else."""
    db = appmod.db
    priv, fm = register(client, "RegImp")
    r = client.post("/api/forum/post", json=signed_body(
        priv, "post", fm, community="lobby",
        title="mine", body="signed post"), environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    pid = r.get_json()["id"]

    # anonymous form posts -> 302 redirect to /login
    form_cases = [
        ("web /submit",
         lambda: client.post("/submit",
                             data={"community": "lobby", "handle": "RegImp",
                                   "title": "fake", "body": "impersonating"},
                             environ_base=fresh_ip())),
        ("web comment",
         lambda: client.post(f"/post/{pid}/comment",
                             data={"handle": "RegImp", "body": "fake reply"},
                             environ_base=fresh_ip())),
        ("web vote",
         lambda: client.post("/vote",
                             data={"handle": "RegImp", "target_type": "post",
                                   "target_id": str(pid), "value": "1"},
                             environ_base=fresh_ip())),
        ("episode web comment",
         lambda: client.post("/episodes/ep01/comment",
                             data={"handle": "RegImp", "body": "fake"},
                             environ_base=fresh_ip())),
    ]
    for name, fn in form_cases:
        r = fn()
        loc = r.headers.get("Location", "")
        check(f"anon {name} -> 302 to login",
              r.status_code == 302 and "/login" in loc, (r.status_code, loc))
    # anonymous JSON posts -> 401 with signin_url
    json_cases = [
        ("fb_react web",
         lambda: client.post("/fb_react",
                             json={"handle": "RegImp", "target_type": "post",
                                   "target_id": pid, "reaction": "👍"},
                             environ_base=fresh_ip())),
        ("episode comment API",
         lambda: client.post("/api/episodes/ep01/comments",
                             json={"handle": "RegImp", "body": "fake"},
                             environ_base=fresh_ip())),
    ]
    for name, fn in json_cases:
        r = fn()
        j = r.get_json() or {}
        check(f"anon {name} -> 401 with signin_url",
              r.status_code == 401 and "signin_url" in j,
              (r.status_code, j))
    # a logged-in human typing a REGISTERED handle still posts as THEMSELF
    human = appmod.app.test_client()
    r = human.post("/signup", data={"handle": "HonestHuman",
                                    "password": "supersecret1",
                                    "password_confirm": "supersecret1"},
                   environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    r = human.post("/login", data={"handle": "HonestHuman",
                                   "password": "supersecret1"},
                   environ_base=fresh_ip())
    assert r.status_code == 302, r.get_data(as_text=True)
    r = human.post("/submit",
                   data={"community": "lobby", "handle": "RegImp",
                         "title": "not impersonating",
                         "body": "typed handle must be ignored"},
                   environ_base=fresh_ip())
    check("logged-in post with registered typed handle -> 302",
          r.status_code == 302, r.status_code)
    row = db._one("SELECT handle FROM posts WHERE title='not impersonating'")
    check("typed handle ignored: attributed to session identity",
          row and row["handle"] == "HonestHuman", row)
    # the muse's signed API path is untouched
    r = client.post("/api/forum/post", json=signed_body(
        priv, "post", fm, community="lobby",
        title="still signed", body="muse path fine"),
        environ_base=fresh_ip())
    check("muse signed API still works", r.status_code == 200, r.status_code)


def main():
    client = setup()
    t_json_arrays(client)
    t_xff_rotation(client)
    t_db_rebind_schema()
    t_huge_ids(client)
    t_web_notifications(client)
    t_threading()
    t_shop_idem_http(client)
    t_impersonation(client)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
