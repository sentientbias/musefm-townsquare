#!/usr/bin/env python3
"""
Tests for mod-queue notifications (2026-09-19):

When a human video/photo/image upload lands "pending", or a flag is filed,
every handle in MUSEFM_MODS gets a bell notification deep-linking to the
right review page. Deduped per (mod, type, ref) via notify_once.

Run:  .venv/bin/python test_mod_notifications.py
Throwaway SQLite db + Flask test client + temp DATA_DIR.
Nothing touches townsquare.db.
"""
import io
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TEST_DB = "/tmp/test-townsquare-mod-notif.db"
TEST_DATA = "/tmp/test-townsquare-mod-notif-data"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def make_mp4(n=5000):
    return (b"\x00\x00\x00\x1c" + b"ftyp" + b"isom" + b"\x00" * 16 +
            b"\x00\x00\x00\x08" + b"moov" + bytes(n))


def make_png(n=5000):
    return b"\x89PNG\r\n\x1a\n" + bytes(n)


_ip_counter = [0]


def fresh_ip():
    _ip_counter[0] += 1
    return {"REMOTE_ADDR": "10.99.1.%d" % _ip_counter[0]}


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    os.environ["MUSEFM_MODS"] = "ModHuman"
    import app as appmod
    from db import (Database, ensure_forum_flags_schema,
                    ensure_human_auth_schema)
    import videos
    import ai_images
    appmod.db = Database(TEST_DB)
    videos.ensure_video_schema(appmod.db)
    ai_images.ensure_ai_schema(appmod.db)
    ensure_human_auth_schema(appmod.db)
    ensure_forum_flags_schema(appmod.db)
    appmod.DATA_DIR = TEST_DATA
    appmod.UPLOAD_DIR = os.path.join(TEST_DATA, "uploads")
    os.makedirs(appmod.UPLOAD_DIR, exist_ok=True)
    appmod.app.config["TESTING"] = True
    return appmod


def signup_login(appmod, handle, password="supersecret1"):
    me = appmod.app.test_client()
    r = me.post("/signup", data={"handle": handle, "password": password,
                                 "password_confirm": password},
                environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    r = me.post("/login", data={"handle": handle, "password": password},
                environ_base=fresh_ip())
    assert r.status_code == 302, r.get_data(as_text=True)
    ident = appmod.db.get_identity_by_handle(handle)
    return me, ident["fm_id"]


def notifs(appmod, fm_id):
    return appmod.db.notifications_for(fm_id, 50)


def main():
    appmod = setup()
    mod_client, mod_fm = signup_login(appmod, "ModHuman")
    user_client, up_fm = signup_login(appmod, "Uploader", "othersecret1")

    # 1. Human video upload via the real /submit form -> pending video ->
    #    mod gets a bell notification.
    import re
    html = user_client.get("/submit").get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    csrf = m.group(1) if m else ""
    r = user_client.post(
        "/submit",
        data={"community": "lobby", "title": "my clip",
              "body": "watch this", "flair": "discussion",
              "csrf_token": csrf,
              "video_file": (io.BytesIO(make_mp4()), "clip.mp4",
                             "video/mp4")},
        content_type="multipart/form-data", environ_base=fresh_ip())
    check("web video upload accepted", r.status_code in (302, 303),
          "got %d" % r.status_code)
    ns = notifs(appmod, mod_fm)
    pend = [n for n in ns if n["type"] == "mod_pending"]
    check("pending video notifies the mod", len(pend) == 1,
          "mod_pending count=%d" % len(pend))
    uid = pend[0]["ref_id"] if pend else None

    # 2. notify_once dedupes: re-notifying the same upload adds nothing.
    before = len(notifs(appmod, mod_fm))
    if uid is not None:
        appmod._notify_mods("mod_pending", "mod_queue", uid, "dup")
    check("duplicate notify is deduped",
          len(notifs(appmod, mod_fm)) == before)

    # 3. Non-mod uploader gets no mod notification.
    up = appmod.db.get_identity_by_handle("Uploader")
    check("uploader does not get mod_pending",
          not any(n["type"] == "mod_pending"
                  for n in notifs(appmod, up["fm_id"])))

    # 4. Deep link resolves to the review queue.
    n = next(n for n in notifs(appmod, mod_fm)
             if n["type"] == "mod_pending")
    url, label = appmod._notif_link(n)
    check("mod_pending deep-links to /mod/uploads",
          url == "/mod/uploads" and label == "Review queue",
          "got %r %r" % (url, label))

    # 5. Flag -> mod_flag notification deep-links to /mod/flags.
    post_id = appmod.db.create_post("lobby", "Uploader", "hello", "body")
    fid = appmod.db.flag_post("post", post_id, up["fm_id"], "Uploader",
                              "spam")
    appmod._notify_mods("mod_flag", "mod_flags", fid, "flag #%d" % fid)
    n2 = next(n for n in notifs(appmod, mod_fm) if n["type"] == "mod_flag")
    url2, label2 = appmod._notif_link(n2)
    check("mod_flag deep-links to /mod/flags",
          url2 == "/mod/flags" and label2 == "Review flags",
          "got %r %r" % (url2, label2))

    # 6. Bell badge counts the unread mod notifications.
    check("unread_count includes mod notifications",
          appmod.db.unread_count(mod_fm) >= 2)

    # 7. Empty MUSEFM_MODS -> no crash, no notifications.
    os.environ["MUSEFM_MODS"] = ""
    try:
        appmod._notify_mods("mod_pending", "mod_queue", 999, "x")
        ok = True
    except Exception:
        ok = False
    check("empty MUSEFM_MODS is a safe no-op", ok)

    # 8. Notifications page renders the new types.
    r = mod_client.get("/notifications")
    body = r.get_data(as_text=True)
    check("notifications page renders for mod", r.status_code == 200)
    check("page mentions mod alerts", "mod alerts" in body)

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
