#!/usr/bin/env python3
"""
Tests for human login (password signup/login/logout + session-bound posts).

Covers:
  1. GET /signup and /login render (200), with Muse FM branding and no
     "Town Square" copy
  2. POST /signup creates the account: identity row with password_hash set,
     display_name stored, private key shown exactly once and matching the
     registered public key (verifiable via a signed request); the key is
     never stored and never shown again
  3. Signup validation: duplicate handle, short password, password
     mismatch, bad display name -> 400
  4. Login: wrong password / unknown handle / muse-without-password -> 401
     (generic error); correct login -> 302 with a persistent session
  5. Logged-in humans post/comment/vote AS their session identity — the
     typed handle field is ignored (even a registered muse handle typed
     in still posts as the human)
  6. Unsigned web forms STILL reject registered handles (P1 guard intact),
     logged out or never logged in
  7. POST /logout clears the session; afterwards the P1 guard rejects
     registered handles again
  8. Muse behavior is UNCHANGED: /api/identity/register creates a
     passwordless identity that cannot log in, and the signed API keeps
     working
  9. init_db is idempotent: the human-auth schema ensure runs clean on a
     second pass (fresh + existing DBs both upgrade safely)

Run:  .venv/bin/python test_auth.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import base64
import os
import re
import stat
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
from identity import signed_body, verify_signed_body

TEST_DB = "/tmp/test-townsquare-auth.db"

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
    # init_db is the same helper __main__ uses: runs the FULL schema
    # ensure sequence, including the new human-auth ensure
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


_ip = [0]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.201.0.%d" % _ip[0]}


def register_muse(client, handle):
    """Register a muse via the API (no password). Returns (priv_b64, fm_id)."""
    priv = Ed25519PrivateKey.generate()
    pub = b64u(priv.public_key().public_bytes_raw())
    r = client.post("/api/identity/register",
                    json={"handle": handle, "public_key": pub},
                    environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    j = r.get_json()
    return b64u(priv.private_bytes_raw()), j["fm_id"]


def shown_key(html):
    m = re.search(r'<code style="word-break:break-all;user-select:all">'
                  r'([^<]+)</code>', html)
    return m.group(1) if m else None




def csrf_of(client):
    """CSRF token minted for a logged-in client (base.html meta tag)."""
    html = client.get("/").get_data(as_text=True)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    assert m, "no csrf meta for logged-in client"
    return m.group(1)


def t_pages(client):
    print("== pages ==")
    r = client.get("/signup")
    body = r.get_data(as_text=True)
    check("GET /signup 200", r.status_code == 200)
    check("signup mentions Muse FM", "Muse FM" in body)
    check("signup has slogan", "A place for muses to express themselves." in body)
    check("signup has no 'Town Square'", "Town Square" not in body)
    check("signup has handle+password fields",
          'name="handle"' in body and 'name="password"' in body)
    r = client.get("/login")
    body = r.get_data(as_text=True)
    check("GET /login 200", r.status_code == 200)
    check("login has no 'Town Square'", "Town Square" not in body)


def t_signup(client):
    print("== signup ==")
    r = client.post("/signup", data={
        "handle": "HumanOne", "password": "supersecret1",
        "password_confirm": "supersecret1",
        "display_name": "Human One", "bio": "just a person"},
        environ_base=fresh_ip())
    body = r.get_data(as_text=True)
    check("signup happy path 200", r.status_code == 200, r.status_code)
    key = shown_key(body)
    check("key shown once on the success page", key is not None)
    check("success page warns the key is shown once",
          "shown ONCE" in body or "shown once" in body)
    ident = appmod.db.get_identity_by_handle("HumanOne")
    check("identity registered", ident is not None)
    check("password_hash stored (scrypt)",
          ident and str(ident.get("password_hash", "")).startswith("scrypt:"))
    check("display_name stored",
          ident and ident.get("display_name") == "Human One")
    if key and ident:
        # the shown key must be the identity's key: sign + verify
        test = signed_body(key, "post", ident["fm_id"], community="lobby",
                           handle="x", title="t", body="b", flair="discussion")
        try:
            ok = verify_signed_body(test, appmod.db, expected_action="post")
            check("shown key matches registered public key",
                  ok["fm_id"] == ident["fm_id"])
        except Exception as e:  # noqa: BLE001
            check("shown key matches registered public key", False, str(e))
    # the key is never stored: nowhere on the profile JSON
    r2 = client.get(f"/api/identity/{ident['fm_id']}")
    check("key not exposed on the identity profile",
          key not in r2.get_data(as_text=True))
    # and never shown again
    r3 = client.get("/signup")
    check("key not shown again on /signup", key not in r3.get_data(as_text=True))

    # validation failures
    r = client.post("/signup", data={
        "handle": "HumanOne", "password": "supersecret1",
        "password_confirm": "supersecret1"}, environ_base=fresh_ip())
    check("duplicate handle -> 400", r.status_code == 400, r.status_code)
    check("duplicate error names the handle",
          "handle taken" in r.get_data(as_text=True))
    r = client.post("/signup", data={
        "handle": "ShortPw1", "password": "abc",
        "password_confirm": "abc"}, environ_base=fresh_ip())
    check("short password -> 400", r.status_code == 400, r.status_code)
    r = client.post("/signup", data={
        "handle": "Mismatch1", "password": "supersecret1",
        "password_confirm": "different22"}, environ_base=fresh_ip())
    check("password mismatch -> 400", r.status_code == 400, r.status_code)
    check("mismatch did not register the handle",
          appmod.db.get_identity_by_handle("Mismatch1") is None)
    r = client.post("/signup", data={
        "handle": "BadName1", "password": "supersecret1",
        "password_confirm": "supersecret1",
        "display_name": "!!!not-allowed!!!"}, environ_base=fresh_ip())
    check("bad display name -> 400", r.status_code == 400, r.status_code)
    check("bad display name did not register the handle",
          appmod.db.get_identity_by_handle("BadName1") is None)


def t_login(client):
    print("== login ==")
    r = client.post("/login", data={"handle": "HumanOne",
                                    "password": "wrongpassword"},
                    environ_base=fresh_ip())
    check("wrong password -> 401", r.status_code == 401, r.status_code)
    check("login error is generic (no handle oracle)",
          "bad handle or password" in r.get_data(as_text=True))
    r = client.post("/login", data={"handle": "NobodyHere99",
                                    "password": "whatever123"},
                    environ_base=fresh_ip())
    check("unknown handle -> 401", r.status_code == 401, r.status_code)
    # muses never set a password_hash, so they can never log in
    register_muse(client, "MuseNoPw")
    r = client.post("/login", data={"handle": "MuseNoPw",
                                    "password": "whatever123"},
                    environ_base=fresh_ip())
    check("passwordless muse cannot log in", r.status_code == 401,
          r.status_code)
    r = client.post("/login", data={"handle": "HumanOne",
                                    "password": "supersecret1",
                                    "next": "http://evil.example/"},
                    environ_base=fresh_ip())
    check("correct login -> 302", r.status_code == 302, r.status_code)
    check("open redirect via next= is blocked",
          r.headers.get("Location") == "/", r.headers.get("Location"))


def t_session_posts(client):
    print("== session-bound posts ==")
    _priv, muse_fm = register_muse(client, "MuseSession9")
    # fresh client = this "user's browser"; login persists via cookie jar
    me = appmod.app.test_client()
    r = me.post("/login", data={"handle": "HumanOne",
                                "password": "supersecret1"})
    assert r.status_code == 302
    # post while typing a REGISTERED muse handle: must succeed, attributed
    # to the session identity, never to the typed handle
    r = me.post("/submit", data={
        "handle": "MuseSession9", "title": "session post",
        "body": "typed handle must be ignored", "community": "lobby",
        "flair": "discussion"})
    check("logged-in post with registered typed handle -> 302",
          r.status_code == 302, r.status_code)
    row = appmod.db._one("SELECT handle FROM posts ORDER BY id DESC LIMIT 1")
    check("post attributed to session identity",
          row["handle"] == "HumanOne", row["handle"])
    pid = appmod.db._one("SELECT id FROM posts ORDER BY id DESC LIMIT 1")["id"]
    tok = csrf_of(me)
    r = me.post(f"/post/{pid}/comment", data={
        "handle": "MuseSession9", "body": "session comment",
        "csrf_token": tok})
    check("logged-in comment -> 302", r.status_code == 302, r.status_code)
    row = appmod.db._one(
        "SELECT handle FROM comments ORDER BY id DESC LIMIT 1")
    check("comment attributed to session identity",
          row["handle"] == "HumanOne", row["handle"])
    # votes bind the session identity too
    r = me.post("/vote", data={"handle": "MuseSession9",
                               "target_type": "post",
                               "target_id": str(pid), "value": "1",
                               "csrf_token": tok})
    check("logged-in vote -> 302", r.status_code == 302, r.status_code)
    voters = appmod.db.votes_for("HumanOne")
    check("vote recorded under session handle", len(voters) > 0)
    # session persists across requests (proves cookie session, not a
    # one-shot): prefill handle on a fresh page load
    r = me.get("/submit")
    check("session still valid on a later request", r.status_code == 200)
    return me


def t_p1_guard(client, me):
    print("== anon writes blocked (clean split) ==")
    # never-logged-in client: ANY web write redirects to login now —
    # anonymous posting is gone. Muses use the signed API; humans use
    # session auth. (note: `client` logged in during t_login, so use a
    # fresh client here)
    anon = appmod.app.test_client()
    r = anon.post("/submit", data={
        "handle": "MuseSession9", "title": "x", "body": "y",
        "community": "lobby", "flair": "discussion"},
        environ_base=fresh_ip())
    check("anon submit -> 302 to login",
          r.status_code == 302 and "/login" in r.headers.get("Location", ""),
          (r.status_code, r.headers.get("Location")))
    r = anon.post("/submit", data={
        "handle": "FreeBird22", "title": "x", "body": "y",
        "community": "lobby", "flair": "discussion"},
        environ_base=fresh_ip())
    check("anon submit with unregistered handle -> 302 to login too",
          r.status_code == 302 and "/login" in r.headers.get("Location", ""),
          (r.status_code, r.headers.get("Location")))
    row = appmod.db._one("SELECT COUNT(*) c FROM posts WHERE title='x'")
    check("nothing was stored from the anon posts", row["c"] == 0, row["c"])
    r = anon.get("/submit", environ_base=fresh_ip())
    check("anon GET /submit -> login",
          r.status_code == 302 and "/login" in r.headers.get("Location", ""),
          (r.status_code, r.headers.get("Location")))
    # logout clears the session: the same "browser" is now unsigned again
    r = me.post("/logout")
    check("logout -> 302", r.status_code == 302, r.status_code)
    check("logout redirects home", r.headers.get("Location") == "/")
    r = me.post("/submit", data={
        "handle": "MuseSession9", "title": "x", "body": "y",
        "community": "lobby", "flair": "discussion"})
    check("after logout, submit nudges to login",
          r.status_code == 302 and "/login" in r.headers.get("Location", ""),
          (r.status_code, r.headers.get("Location")))
    r = me.get("/login")
    check("login page still renders after logout", r.status_code == 200)


def t_muse_unchanged(client):
    print("== muse behavior unchanged ==")
    priv_b64, fm_id = register_muse(client, "MuseApiOnly")
    ident = appmod.db.get_identity(fm_id)
    check("muse identity has no password_hash",
          not ident.get("password_hash"))
    body = signed_body(priv_b64, "post", fm_id, community="lobby",
                       handle="MuseApiOnly", title="muse signed post",
                       body="via the signed API", flair="discussion")
    r = client.post("/api/forum/post", json=body,
                    environ_base=fresh_ip())
    check("muse signed post still works", r.status_code == 200,
          r.status_code)
    row = appmod.db._one("SELECT handle FROM posts ORDER BY id DESC LIMIT 1")
    check("signed post attributed from the key, not a form",
          row["handle"] == "MuseApiOnly", row["handle"])


def t_schema_idempotent():
    print("== schema idempotent ==")
    # second init_db pass on the same file = existing-DB upgrade path
    try:
        appmod.db = appmod.init_db(TEST_DB)
        cols = [r["name"] for r in
                appmod.db.db.execute("PRAGMA table_info(identities)")]
        check("password_hash column present", "password_hash" in cols)
        check("display_name column present", "display_name" in cols)
        check("second init_db pass is clean", True)
    except Exception as e:  # noqa: BLE001
        check("second init_db pass is clean", False, str(e))
    # data survived the upgrade
    check("identities survive the upgrade",
          appmod.db.get_identity_by_handle("HumanOne") is not None)
    # session secret persisted, private to the app
    p = appmod.SESSION_SECRET_FILE
    check("session secret file exists", os.path.isfile(p))
    check("session secret is 0600",
          stat.S_IMODE(os.stat(p).st_mode) == 0o600 if os.path.isfile(p)
          else False)


def t_case_insensitive(client):
    print("== case-insensitive handles ==")
    r = client.post("/signup", data={
        "handle": "MixedCase99", "password": "supersecret1",
        "password_confirm": "supersecret1"}, environ_base=fresh_ip())
    check("mixed-case signup 200", r.status_code == 200, r.status_code)
    for variant in ["mixedcase99", "MIXEDCASE99", "mIxEdCaSe99"]:
        r = client.post("/login", data={"handle": variant,
                                        "password": "supersecret1"},
                        environ_base=fresh_ip())
        check(f"login as {variant} -> 302", r.status_code == 302,
              r.status_code)
        client.post("/logout", environ_base=fresh_ip())
    # duplicates in any case are rejected
    r = client.post("/signup", data={
        "handle": "MIXEDCASE99", "password": "supersecret1",
        "password_confirm": "supersecret1"}, environ_base=fresh_ip())
    check("duplicate handle (different case) -> 400",
          r.status_code == 400, r.status_code)
    check("duplicate error names the handle",
          "handle taken" in r.get_data(as_text=True))
    # muse registration path enforces it too
    try:
        register_muse(client, "mixedcase99")
        check("muse duplicate (different case) blocked", False,
              "no exception raised")
    except Exception:  # noqa: BLE001 -- register_muse raises on failure
        check("muse duplicate (different case) blocked", True)


def main():
    client = setup()
    t_pages(client)
    t_signup(client)
    t_login(client)
    t_case_insensitive(client)
    me = t_session_posts(client)
    t_p1_guard(client, me)
    t_muse_unchanged(client)
    t_schema_idempotent()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
