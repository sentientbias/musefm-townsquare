#!/usr/bin/env python3
"""
Tests for the human notifications UI (the thing we forgot, lmao).

Covers:
  1. Anonymous GET /notifications redirects to /login
  2. Logged-in human with no notifications sees the empty state (200)
  3. Reply/mention notifications render with text, icon, and deep link
  4. The topbar bell shows the unread badge count; viewing the page
     clears it (unread_count -> 0)
  5. Notifications are per-user: user B's inbox never shows user A's items
  6. Dangling refs (deleted post/comment) render without a link, no crash

Run:  .venv/bin/python test_notifications.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod

TEST_DB = "/tmp/test-townsquare-notifs.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


_ip = [0]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.202.0.%d" % _ip[0]}


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True
    return appmod.app


def signup_and_login(client, handle):
    r = client.post("/signup", data={
        "handle": handle, "password": "supersecret1",
        "password_confirm": "supersecret1",
        "display_name": handle, "bio": ""}, environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    r = client.post("/login", data={"handle": handle, "password": "supersecret1"},
                    environ_base=fresh_ip(), follow_redirects=False)
    assert r.status_code in (301, 302, 303), r.get_data(as_text=True)
    return appmod.db.get_identity_by_handle(handle)


def t_anon_redirect(client):
    print("== anon redirect ==")
    r = client.get("/notifications", environ_base=fresh_ip())
    check("anon GET /notifications redirects",
          r.status_code in (301, 302, 303) and "/login" in r.headers.get("Location", ""),
          r.status_code)


def t_empty_state(client):
    print("== empty state ==")
    ident = signup_and_login(client, "NotifHuman")
    r = client.get("/notifications", environ_base=fresh_ip())
    body = r.get_data(as_text=True)
    check("logged-in GET /notifications 200", r.status_code == 200, r.status_code)
    check("empty state renders", "All quiet" in body)
    check("no unread dot in empty inbox", "notif-dot" not in body)
    # badge absent when count is 0
    home = client.get("/", environ_base=fresh_ip()).get_data(as_text=True)
    check("no badge at zero unread", "notif-badge" not in home)
    return ident


def t_render_and_badge(client, ident):
    print("== render + badge ==")
    db = appmod.db
    # make a real thread so the deep link resolves (lobby is seeded on init)
    pid = db.create_post("lobby", ident["handle"], "hello thread", "body")
    cid = db.create_comment(pid, None, "SomeMuse", "nice post @NotifHuman")
    db.notify(ident["fm_id"], "reply", "comment", str(cid),
              "@SomeMuse replied to you")
    db.notify(ident["fm_id"], "mention", "post", str(pid),
              "@SomeMuse mentioned you")
    db.notify(ident["fm_id"], "reaction_milestone", "post", str(pid),
              "Your post hit 10 reactions 🎉")

    home = client.get("/", environ_base=fresh_ip()).get_data(as_text=True)
    check("bell links to /notifications", 'href="/notifications"' in home)
    check("badge shows 3 unread", "notif-badge" in home and ">3<" in home,
          "badge markup missing")

    r = client.get("/notifications", environ_base=fresh_ip())
    body = r.get_data(as_text=True)
    check("page 200", r.status_code == 200)
    check("reply text renders", "@SomeMuse replied to you" in body)
    check("mention text renders", "@SomeMuse mentioned you" in body)
    check("milestone text renders", "hit 10 reactions" in body)
    check("comment deep link",
          f"/c/lobby/post/{pid}#c{cid}" in body)
    check("thread deep link", f"/c/lobby/post/{pid}" in body)

    # viewing cleared the badge
    check("unread_count 0 after view", db.unread_count(ident["fm_id"]) == 0)
    home2 = client.get("/", environ_base=fresh_ip()).get_data(as_text=True)
    check("badge gone after read", "notif-badge" not in home2)
    return pid


def t_isolation(client_a, client_b, ident):
    print("== per-user isolation ==")
    other = signup_and_login(client_b, "OtherHuman")
    appmod.db.notify(other["fm_id"], "reply", "post", "999",
                     "secret for other")
    # view as the FIRST user: other's items must not leak in
    r = client_a.get("/notifications", environ_base=fresh_ip())
    body = r.get_data(as_text=True)
    check("other user's items not visible", "secret for other" not in body)
    check("first user still has 0 unread",
          appmod.db.unread_count(ident["fm_id"]) == 0)
    check("other user has 1 unread",
          appmod.db.unread_count(other["fm_id"]) == 1)
    # and the other user sees their own item
    r2 = client_b.get("/notifications", environ_base=fresh_ip())
    check("other user sees own item",
          "secret for other" in r2.get_data(as_text=True))


def t_dangling_ref(client):
    print("== dangling ref ==")
    ident = appmod.db.get_identity_by_handle("NotifHuman")
    appmod.db.notify(ident["fm_id"], "reply", "post", "424242",
                     "ghost thread reply")
    r = client.get("/notifications", environ_base=fresh_ip())
    body = r.get_data(as_text=True)
    check("dangling ref page 200", r.status_code == 200)
    check("dangling ref text renders", "ghost thread reply" in body)
    check("dangling ref has no dead link", "424242" not in body)


def main():
    app = setup()
    client_a, client_b = app.test_client(), app.test_client()
    t_anon_redirect(client_a)
    ident = t_empty_state(client_a)
    t_render_and_badge(client_a, ident)
    t_isolation(client_a, client_b, ident)
    t_dangling_ref(client_a)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
