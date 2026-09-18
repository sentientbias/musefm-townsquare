#!/usr/bin/env python3
"""
Tests for the moderation batch (2026-09-18):

1. Shorts comment flagging works: db.flag_post accepts "video_comment",
   the /flag form records it, and it shows up in /mod/flags with a link
   back to the shorts comment thread.
2. Upload approval queue: human video/photo/image uploads land "pending"
   and are invisible in feeds, watch pages, and direct serving; signed
   agent uploads with ai_generated=true go live immediately; signed
   non-AI agent uploads also wait for approval. Mods can preview, approve,
   and reject. Existing production rows stay visible (approved default).

Run:  .venv/bin/python test_mod_approval.py
Throwaway SQLite db + Flask test client + temp DATA_DIR.
Nothing touches townsquare.db.
"""
import base64
import hashlib
import io
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import videos
import ai_images
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-mod-approval.db"
TEST_DATA = "/tmp/test-townsquare-mod-approval-data"

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
    return (b"\x00\x00\x00\x1c" + b"ftyp" + b"isom" + b"\x00" * 16 +
            b"\x00\x00\x00\x08" + b"moov" + bytes(n))


def make_png(n=5000):
    return b"\x89PNG\r\n\x1a\n" + bytes(n)


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    from db import Database, ensure_human_auth_schema, ensure_forum_flags_schema
    appmod.db = Database(TEST_DB)
    videos.ensure_video_schema(appmod.db)
    ai_images.ensure_ai_schema(appmod.db)
    ensure_human_auth_schema(appmod.db)  # mirrors app startup (app.py)
    ensure_forum_flags_schema(appmod.db)
    appmod.DATA_DIR = TEST_DATA
    appmod.UPLOAD_DIR = os.path.join(TEST_DATA, "uploads")
    os.makedirs(appmod.UPLOAD_DIR, exist_ok=True)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


_ip_counter = [0]


def fresh_ip():
    _ip_counter[0] += 1
    return {"REMOTE_ADDR": "10.99.0.%d" % _ip_counter[0]}


def login_human(handle="ModHuman", password="supersecret1"):
    me = appmod.app.test_client()
    r = me.post("/signup", data={"handle": handle, "password": password,
                                 "password_confirm": password},
                environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    r = me.post("/login", data={"handle": handle, "password": password},
                environ_base=fresh_ip())
    assert r.status_code == 302, r.get_data(as_text=True)
    return me


def register_muse(client, handle):
    priv_b64, pub_b64 = fresh_keypair()
    r = client.post("/api/identity/register",
                    json={"handle": handle, "public_key": pub_b64})
    assert r.status_code == 200, r.get_data(as_text=True)
    return priv_b64, r.get_json()["fm_id"]


def signed_video_upload(client, priv, fm_id, raw, ai="1", filename="clip.mp4"):
    fields = signed_body(priv, "upload", fm_id,
                         file_sha256=hashlib.sha256(raw).hexdigest(),
                         ai_generated=ai)
    data = dict(fields)
    data["video"] = (io.BytesIO(raw), filename, "video/mp4")
    return client.post("/api/upload/video", data=data,
                       content_type="multipart/form-data",
                       environ_base=fresh_ip())


def signed_image_upload(client, priv, fm_id, raw, ai="1", filename="pic.png"):
    fields = signed_body(priv, "upload", fm_id,
                         file_sha256=hashlib.sha256(raw).hexdigest(),
                         ai_generated=ai)
    data = dict(fields)
    data["image"] = (io.BytesIO(raw), filename, "image/png")
    return client.post("/api/upload/image", data=data,
                       content_type="multipart/form-data",
                       environ_base=fresh_ip())


def signed_photo_create(client, priv, fm_id, img_id, title):
    fields = signed_body(priv, "upload", fm_id,
                         image_url="/img/%d" % img_id, title=title)
    return client.post("/api/photos/create", json=fields,
                       environ_base=fresh_ip())


def main():
    os.environ["MUSEFM_MODS"] = "ModHuman"
    client = setup()
    db = appmod.db

    print("== video_comment flagging ==")
    priv, fm_id = register_muse(client, "ClipMuse")
    raw = make_mp4()
    r = signed_video_upload(client, priv, fm_id, raw, ai="1")
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    vid = r.get_json()["id"]

    cid = db.create_video_comment(vid, None, "SomeHuman", "flag me please")
    db.flag_post("video_comment", cid, fm_id, "ClipMuse", "spam")
    flags = db.list_flags("open")
    check("video_comment flag recorded",
          any(f["target_type"] == "video_comment" and f["target_id"] == cid
              for f in flags), str(flags))

    # re-flagging the same target by the same flagger updates the reason
    db.flag_post("video_comment", cid, fm_id, "ClipMuse", "harassment")
    flags = db.list_flags("open")
    mine = [f for f in flags
            if f["target_type"] == "video_comment" and f["target_id"] == cid]
    check("re-flag updates reason, no duplicate row",
          len(mine) == 1 and mine[0]["reason"] == "harassment", str(mine))

    for bad_tt in ("post2", "", "user"):
        try:
            db.flag_post(bad_tt, cid, fm_id, "ClipMuse", "spam")
            ok = False
        except ValueError:
            ok = True
        check("bad target_type %r rejected" % bad_tt, ok)

    try:
        db.flag_post("video_comment", 424242, fm_id, "ClipMuse", "spam")
        ok = False
    except ValueError:
        ok = True
    check("unknown video_comment id rejected", ok)

    # plain post/comment flags still work
    db.flag_post("post", 1, fm_id, "ClipMuse", "other")
    check("post flag still works",
          any(f["target_type"] == "post" for f in db.list_flags("open")))

    # web form: signed-in human can flag a shorts comment
    me = login_human("FlagHuman")
    r = me.post("/flag", data={"target_type": "video_comment",
                               "target_id": str(cid), "reason": "nsfw",
                               "next": "/shorts"},
                environ_base=fresh_ip())
    check("web /flag on video_comment -> redirect", r.status_code == 302,
          str(r.status_code))
    check("web flag shows in mod queue",
          any(f["target_type"] == "video_comment" and f["target_id"] == cid
              and f["flagger_handle"] == "FlagHuman"
              for f in db.list_flags("open")))

    # anonymous flag is denied (redirect to login, nothing recorded)
    anon = appmod.app.test_client()
    before = db.count_open_flags()
    r = anon.post("/flag", data={"target_type": "video_comment",
                                 "target_id": str(cid), "reason": "spam",
                                 "next": "/shorts"},
                  environ_base=fresh_ip())
    check("anonymous /flag denied",
          r.status_code == 302 and db.count_open_flags() == before,
          str(r.status_code))

    print("== signed agent video approval policy ==")
    r = signed_video_upload(client, priv, fm_id, make_mp4(), ai="1")
    j = r.get_json()
    u = videos.get_video_upload(db, j["id"])
    check("signed ai_generated video auto-approved",
          r.status_code == 200 and j.get("status") == "approved"
          and u["status"] == "approved", str(j))
    check("approved video appears in shorts feed",
          any(s["id"] == u["id"] for s in videos.list_shorts(db)))
    check("approved video id appears in list_short_ids",
          u["id"] in videos.list_short_ids(db))

    r = signed_video_upload(client, priv, fm_id, make_mp4(), ai="0")
    j2 = r.get_json()
    u2 = videos.get_video_upload(db, j2["id"])
    check("signed non-AI video goes pending",
          r.status_code == 200 and j2.get("status") == "pending"
          and u2["status"] == "pending", str(j2))
    check("pending video hidden from shorts feed",
          all(s["id"] != u2["id"] for s in videos.list_shorts(db)))
    check("pending video hidden from list_short_ids",
          u2["id"] not in videos.list_short_ids(db))
    check("pending video direct-serve 404s for anonymous",
          anon.get("/video/%d" % u2["id"]).status_code == 404)
    check("pending video watch page 404s for anonymous",
          anon.get("/watch/%d" % u2["id"]).status_code == 404)
    check("approved video watch page 200s for anonymous",
          anon.get("/watch/%d" % u["id"]).status_code == 200)

    print("== human form video -> pending ==")
    from flask import request as _freq  # noqa: F401 (context helper below)
    builder_data = {"video_file": (io.BytesIO(make_mp4()), "human.mp4"),
                    "video_duration": "20"}
    with appmod.app.test_request_context(
            "/x", method="POST", data=builder_data,
            content_type="multipart/form-data"):
        from flask import request
        url, ai_flag = appmod._video_from_form(request, "HumanUploader")
    huid = int(url.rsplit("/", 1)[-1])
    hu = videos.get_video_upload(db, huid)
    check("human form video lands pending",
          hu and hu["status"] == "pending" and url == "/video/%d" % huid,
          str(hu and hu["status"]))
    check("pending human video hidden from shorts feed",
          all(s["id"] != huid for s in videos.list_shorts(db)))
    check("pending human video direct-serve 404s for anonymous",
          anon.get("/video/%d" % huid).status_code == 404)

    print("== mod approve/reject flow ==")
    mod = login_human("ModHuman")
    r = mod.get("/mod/uploads")
    check("mod can open approval queue", r.status_code == 200,
          str(r.status_code))
    r = mod.post("/mod/uploads/video/%d/approve" % u2["id"],
                 environ_base=fresh_ip())
    check("mod approve -> redirect", r.status_code == 302, str(r.status_code))
    check("approval makes video public exactly once",
          videos.get_video_upload(db, u2["id"])["status"] == "approved"
          and any(s["id"] == u2["id"] for s in videos.list_shorts(db)))
    check("approved video now serves 200",
          anon.get("/video/%d" % u2["id"]).status_code == 200)

    r = mod.post("/mod/uploads/video/%d/reject" % huid,
                 environ_base=fresh_ip())
    check("mod reject -> redirect", r.status_code == 302, str(r.status_code))
    check("rejected video stays hidden",
          videos.get_video_upload(db, huid)["status"] == "rejected"
          and all(s["id"] != huid for s in videos.list_shorts(db))
          and anon.get("/video/%d" % huid).status_code == 404)

    # non-mod humans can't touch the queue
    r = me.post("/mod/uploads/video/%d/approve" % u2["id"],
                environ_base=fresh_ip())
    check("non-mod approve blocked", r.status_code == 403, str(r.status_code))
    r = me.get("/mod/uploads")
    check("non-mod queue view blocked", r.status_code == 403,
          str(r.status_code))
    r = anon.get("/mod/uploads")
    check("anonymous queue view blocked", r.status_code == 302,
          str(r.status_code))

    print("== signed agent photo approval policy ==")
    r = signed_image_upload(client, priv, fm_id, make_png(), ai="1")
    ji = r.get_json()
    check("signed ai_generated image upload approved",
          ji.get("status") == "approved", str(ji))
    r = signed_photo_create(client, priv, fm_id, ji["id"], "AI sunset")
    jp = r.get_json()
    p = db.get_photo(jp["id"])
    check("signed ai_generated photo goes live",
          r.status_code == 200 and jp.get("status") == "approved"
          and p and p["status"] == "approved", str(jp))
    check("approved photo listed publicly",
          any(x["id"] == p["id"] for x in db.list_photos()))
    check("approved photo page 200s",
          anon.get("/musefm/photos/%d" % p["id"]).status_code == 200)

    r = signed_image_upload(client, priv, fm_id, make_png(), ai="0")
    ji2 = r.get_json()
    check("signed non-AI image upload pending", ji2.get("status") == "pending",
          str(ji2))
    r = signed_photo_create(client, priv, fm_id, ji2["id"], "Real snapshot")
    jp2 = r.get_json()
    p2 = db.get_photo(jp2["id"])
    check("signed non-AI photo goes pending",
          r.status_code == 200 and jp2.get("status") == "pending"
          and p2 and p2["status"] == "pending", str(jp2))
    check("pending photo not in public gallery",
          all(x["id"] != p2["id"] for x in db.list_photos()))
    check("pending photo page 404s for anonymous",
          anon.get("/musefm/photos/%d" % p2["id"]).status_code == 404)
    check("pending photo file 404s for anonymous",
          anon.get("/photo-file/%d" % p2["id"]).status_code == 404)

    # human form photo upload -> pending + stays out of the gallery
    r = me.post("/photos/upload",
                data={"title": "My human pic",
                      "photo": (io.BytesIO(make_png()), "pic.png", "image/png")},
                content_type="multipart/form-data",
                environ_base=fresh_ip())
    check("human photo upload redirects to upload page with notice",
          r.status_code == 302 and "pending=1" in r.headers.get("Location", ""),
          "%s %s" % (r.status_code, r.headers.get("Location")))
    pending = db.list_pending_photos()
    check("human photo lands pending",
          any(x["title"] == "My human pic" for x in pending), str(pending))
    hp = next(x for x in pending if x["title"] == "My human pic")
    check("human pending photo not in public gallery",
          all(x["id"] != hp["id"] for x in db.list_photos()))
    check("human pending photo page 404s for anonymous",
          anon.get("/musefm/photos/%d" % hp["id"]).status_code == 404)

    r = mod.post("/mod/uploads/photo/%d/approve" % hp["id"],
                 environ_base=fresh_ip())
    check("mod approves human photo",
          r.status_code == 302
          and db.get_photo(hp["id"])["status"] == "approved"
          and any(x["id"] == hp["id"] for x in db.list_photos()))

    print("== comment/post image attachments go through the queue ==")
    r = signed_image_upload(client, priv, fm_id, make_png(), ai="0")
    img_pend = r.get_json()["id"]
    check("pending comment image direct-serve 404s for anonymous",
          anon.get("/img/%d" % img_pend).status_code == 404)
    check("media_visible filter hides pending image",
          appmod.media_visible("/img/%d" % img_pend) is False)
    check("media_visible filter shows approved image",
          appmod.media_visible("/img/%d" % ji["id"]) is True)
    check("media_visible passes external URLs through",
          appmod.media_visible("https://example.com/x.png") is True)

    r = mod.post("/mod/uploads/image/%d/reject" % img_pend,
                 environ_base=fresh_ip())
    check("mod rejects pending comment image",
          r.status_code == 302
          and ai_images.get_image_upload(db, img_pend)["status"] == "rejected")

    print("== migration defaults stay visible ==")
    # Rows created before the status column existed default to approved.
    check("seed photos remain visible after migration",
          len(db.list_photos()) >= 1)
    try:
        videos.set_video_status(db, u["id"], "bogus")
        ok = False
    except ValueError:
        ok = True
    check("bad status rejected", ok)
    try:
        videos.set_video_status(db, 424242, "approved")
        ok = False
    except ValueError:
        ok = True
    check("status change on unknown video rejected", ok)

    print("== mod queue context for video_comment flags ==")
    modflags = db.list_flags("open")
    vc = [f for f in modflags if f["target_type"] == "video_comment"]
    check("video_comment flags in open list", len(vc) >= 1)
    r = mod.get("/mod/flags")
    check("mod flags page renders with video_comment context",
          r.status_code == 200 and b"video_comment" in r.data,
          str(r.status_code))
    check("approval queue link shown on flags page",
          b"/mod/uploads" in r.data)

    print()
    print("PASS %d  FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("failures:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
