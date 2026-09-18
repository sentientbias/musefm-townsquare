#!/usr/bin/env python3
"""
Tests for the Muse FM media section: episode watch pages, the section hub,
shorts feed (video/photo/audio cards), photos, FB reactions on
episode/video/photo targets, series filtering, and idempotent seeds.

Run:  .venv/bin/python test_musefm.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
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
import fb_reactions
import videos
from db import ensure_musefm_media_schema
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-musefm.db"
TEST_DATA = "/tmp/test-townsquare-musefm-data"

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
    if os.path.exists(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    os.makedirs(TEST_DATA, exist_ok=True)
    from db import Database, ensure_human_auth_schema
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)  # mirrors app startup
    appmod.DATA_DIR = TEST_DATA
    import gifs, ai_images
    gifs.ensure_gif_schema(appmod.db)
    ai_images.ensure_ai_schema(appmod.db)
    videos.ensure_video_schema(appmod.db)
    fb_reactions.ensure_fb_reactions_schema(appmod.db)
    ensure_musefm_media_schema(appmod.db)
    appmod.db.ensure_musefm_seeds()
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


_ip = [0]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.77.0.%d" % _ip[0]}


def register(client, handle):
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    r = client.post("/api/identity/register", json={
        "handle": handle,
        "public_key": b64u(pub.public_bytes_raw())})
    assert r.status_code == 200, r.get_data(as_text=True)
    return b64u(priv.private_bytes_raw()), r.get_json()["fm_id"]


def login_human(handle="MuseFmHuman", password="supersecret1"):
    """Sign up + log in a human on a fresh test client. Returns the client."""
    me = appmod.app.test_client()
    r = me.post("/signup", data={"handle": handle, "password": password,
                                 "password_confirm": password},
                environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    r = me.post("/login", data={"handle": handle, "password": password},
                environ_base=fresh_ip())
    assert r.status_code == 302, r.get_data(as_text=True)
    return me


def fb_react(client, priv, fm_id, ttype, tid, reaction):
    return client.post("/api/forum/fb_react", json=signed_body(
        priv, "fb_react", fm_id, target_type=ttype,
        target_id=tid, reaction=reaction), environ_base=fresh_ip())


# 1x1 png (magic bytes + minimal valid structure)
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
       b"\x00\x01\x01\x00\x05\x1b\xa4\xd6\x00\x00\x00\x00IEND\xaeB`\x82")
# Structurally valid minimal MP4 (ftyp + moov); must clear
# videos.MIN_VIDEO_BYTES for upload validation.
MP4 = (b"\x00\x00\x00\x1c" + b"ftyp" + b"isom" + b"\x00" * 16 +
       b"\x00\x00\x00\x08" + b"moov" + b"\x00" * 5000)


def main():
    client = setup()

    print("== seeds (idempotent) ==")
    eps = {e["slug"]: e for e in appmod.db.episodes()}
    check("ep04 seeded with real title",
          eps.get("ep04", {}).get("title") == "Helix 2.5 and the Humanoid Report Card",
          str(eps.get("ep04", {}).get("title")))
    check("ep01 keeps real title", eps["ep01"]["title"] == "Muse FM Ep01")
    check("ep03 has video_file", eps["ep03"].get("video_file") == "ep03-video.mp4")
    check("2 starter photos", len(appmod.db.list_photos()) == 2)
    n_posts = appmod.db._one(
        "SELECT COUNT(*) c FROM posts WHERE title LIKE '🎙️%'")["c"]
    appmod.db.ensure_musefm_seeds()  # run again: must not duplicate
    n_posts2 = appmod.db._one(
        "SELECT COUNT(*) c FROM posts WHERE title LIKE '🎙️%'")["c"]
    check("episode posts idempotent", n_posts == n_posts2 == 4, f"{n_posts}/{n_posts2}")
    check("photos idempotent", len(appmod.db.list_photos()) == 2)

    print("== section hub ==")
    r = client.get("/musefm")
    body = r.get_data(as_text=True)
    check("/musefm 200", r.status_code == 200, str(r.status_code))
    check("hub lists ep04", "Helix 2.5 and the Humanoid Report Card" in body)
    check("hub has attribution",
          "Funky Groove Logo/Intro Music" in body and "Alexander Blu" in body
          and "CC BY-NC 4.0" in body)
    check("hub links show page", "muse.ai/s/musefm-xoxa6ixn5uxhh4g" in body)
    check("hub has reaction widgets", 'class="rxn' in body)
    check("no money-talk", "attention first" not in body.lower())
    welcome = appmod.db._one(
        "SELECT body FROM posts WHERE title='Welcome to the Forum'"
        " ORDER BY id LIMIT 1")
    check("seed welcome has no money-hunger copy",
          welcome and "attention first" not in welcome["body"].lower()
          and "money later" not in welcome["body"].lower()
          and "muses to express themselves" in welcome["body"].lower(),
          repr(welcome["body"][:80]) if welcome else "missing")

    print("== episode watch pages ==")
    r = client.get("/episodes/ep04")
    body = r.get_data(as_text=True)
    check("/episodes/ep04 200", r.status_code == 200, str(r.status_code))
    check("watch page has audio player", "<audio" in body and "/audio/ep04.mp3" in body)
    check("watch page has title card art", "muse-fm-title-card.png" in body)
    check("watch page has reactions", 'data-target-type="episode"' in body)
    check("watch page has comment form", "Sign in</a> to comment on episodes" in body)
    check("watch page attribution", "Alexander Blu" in body)
    r = client.get("/episodes/ep03")
    body = r.get_data(as_text=True)
    check("ep03 watch page has video player",
          r.status_code == 200 and "<video" in body and "ep03-video.mp4" in body,
          str(r.status_code))
    r = client.get("/episodes/nope")
    check("unknown episode -> 404", r.status_code == 404, str(r.status_code))
    r = client.get("/episodes")
    check("/episodes listing 200 + widgets + watch links",
          r.status_code == 200 and 'class="rxn' in r.get_data(as_text=True)
          and "/episodes/ep04" in r.get_data(as_text=True), str(r.status_code))

    print("== episode media ==")
    r = client.get("/audio/ep04.mp3")
    check("/audio/ep04.mp3 200", r.status_code == 200, str(r.status_code))

    print("== shorts feed ==")
    r = client.get("/musefm/shorts")
    body = r.get_data(as_text=True)
    check("/musefm/shorts 200", r.status_code == 200, str(r.status_code))
    check("feed has photo cards", "short-photo" in body)
    check("feed has audio cards", "short-play-audio" in body)
    check("feed has reaction overlays", body.count('class="rxn') >= 3,
          str(body.count('class="rxn')))
    check("feed empty-state absent (photos+episodes seed it)",
          "No Muse FM clips yet" not in body)

    print("== photos ==")
    r = client.get("/musefm/photos")
    check("/musefm/photos 200", r.status_code == 200, str(r.status_code))
    check("photos grid has widgets", 'class="rxn' in r.get_data(as_text=True))
    r = client.get("/musefm/photos/1")
    body = r.get_data(as_text=True)
    check("photo permalink 200", r.status_code == 200, str(r.status_code))
    check("photo page has widget", 'data-target-type="photo"' in body)
    r = client.get("/musefm/photos/424242")
    check("unknown photo -> 404", r.status_code == 404, str(r.status_code))
    r = client.get("/photos/upload")
    check("anon upload form -> login",
          r.status_code == 302 and "/login" in r.headers.get("Location", ""),
          f"{r.status_code} {r.headers.get('Location')}")
    human = login_human("ShutterHuman")
    r = human.get("/photos/upload")
    check("upload form 200 for signed-in human", r.status_code == 200,
          str(r.status_code))
    # valid upload — lands in the mod-approval queue, not on a public permalink
    r = human.post("/photos/upload",
                   data={"title": "Test shot",
                         "caption": "a test",
                         "photo": (io.BytesIO(PNG), "shot.png")},
                   content_type="multipart/form-data", environ_base=fresh_ip())
    check("photo upload -> redirect with pending notice",
          r.status_code == 302 and r.headers["Location"] == "/photos/upload?pending=1",
          f"{r.status_code} {r.headers.get('Location')}")
    row = appmod.db._one(
        "SELECT id, handle, status FROM photos WHERE title='Test shot'")
    check("photo attributed to the human's handle",
          row["handle"] == "ShutterHuman", row["handle"])
    pid = row["id"]
    check("human photo upload lands pending", row["status"] == "pending",
          row["status"])
    r = client.get(f"/photo-file/{pid}")
    check("pending photo does NOT serve publicly", r.status_code == 404,
          str(r.status_code))
    r = client.get(f"/musefm/photos/{pid}")
    check("pending photo page 404s publicly", r.status_code == 404,
          str(r.status_code))
    # invalid upload
    r = human.post("/photos/upload",
                   data={"title": "Bad",
                         "photo": (io.BytesIO(b"not an image"), "x.txt")},
                   content_type="multipart/form-data", environ_base=fresh_ip())
    check("non-image upload -> 400", r.status_code == 400, str(r.status_code))

    print("== reactions on episode / video / photo ==")
    priv_a, fm_a = register(client, "ReactA")
    rid = appmod.db.episode_rowid("ep04")
    r = fb_react(client, priv_a, fm_a, "episode", rid, "love")
    d = r.get_json()
    check("react to episode -> added",
          r.status_code == 200 and d["action"] == "added" and d["counts"] == {"love": 1},
          str(d))
    r = fb_react(client, priv_a, fm_a, "episode", rid, "love")
    check("same reaction removes", r.get_json()["action"] == "removed")
    r = fb_react(client, priv_a, fm_a, "episode", 424242, "like")
    check("unknown episode rowid -> 400", r.status_code == 400, str(r.status_code))
    # video target
    uid, _stored = videos.create_video_upload(
        appmod.db, fm_a, "ReactA", "clip.mp4", MP4, TEST_DATA, duration_secs=30,
        status="approved")
    videos.set_series(appmod.db, uid, "musefm")
    r = fb_react(client, priv_a, fm_a, "video", uid, "wow")
    check("react to video -> added", r.get_json()["action"] == "added",
          str(r.get_json()))
    r = fb_react(client, priv_a, fm_a, "video", 424242, "like")
    check("unknown video -> 400", r.status_code == 400, str(r.status_code))
    # photo target
    r = fb_react(client, priv_a, fm_a, "photo", 1, "haha")
    check("react to photo -> added", r.get_json()["action"] == "added",
          str(r.get_json()))
    r = fb_react(client, priv_a, fm_a, "photo", 424242, "like")
    check("unknown photo -> 400", r.status_code == 400, str(r.status_code))
    # web route on an episode: anonymous -> 401 with sign-in URL ...
    r = client.post("/fb_react", json={
        "target_type": "episode", "target_id": rid, "reaction": "like",
        "handle": "WebFan", "next": "/episodes/ep04"}, environ_base=fresh_ip())
    d = r.get_json() or {}
    check("anon web fb_react on episode -> 401",
          r.status_code == 401 and "signin_url" in d, (r.status_code, d))
    # ... signed-in human -> 200 and stored under their identity
    fan = appmod.app.test_client()
    r = fan.post("/signup", data={"handle": "EpFan",
                                  "password": "supersecret1",
                                  "password_confirm": "supersecret1"},
                 environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    r = fan.post("/login", data={"handle": "EpFan", "password": "supersecret1"},
                 environ_base=fresh_ip())
    assert r.status_code == 302, r.get_data(as_text=True)
    r = fan.post("/fb_react", json={
        "target_type": "episode", "target_id": rid, "reaction": "like",
        "handle": "RegImp", "next": "/episodes/ep04"}, environ_base=fresh_ip())
    d = r.get_json()
    check("web fb_react on episode", r.status_code == 200 and d["ok"]
          and d["total"] >= 1, str(d))
    # summaries span mixed types
    sums = fb_reactions.fb_reaction_summaries(
        appmod.db, [("episode", rid), ("video", uid), ("photo", 1)], "agent:x")
    check("mixed-type summaries",
          all(sums[k]["total"] >= 1 for k in sums), str({k: v["total"] for k, v in sums.items()}))

    print("== series filter ==")
    r = client.get("/api/shorts?series=musefm")
    d = r.get_json()
    check("/api/shorts?series=musefm lists the tagged clip",
          r.status_code == 200 and d["ok"] and any(
              it["id"] == uid for it in d["items"]), str(d))
    check("api short items carry fb summaries",
          all("fb" in it and "target_type" in it for it in d["items"]),
          str(d["items"][:1]))
    r = client.get("/watch/%d" % uid)
    check("video watch page 200 + reactions",
          r.status_code == 200 and 'data-target-type="video"' in r.get_data(as_text=True),
          str(r.status_code))
    r = client.get("/musefm/shorts")
    check("musefm shorts now includes the video card",
          "/video/%d" % uid in r.get_data(as_text=True))

    print("== api episode urls ==")
    r = client.get("/api/episodes")
    d = r.get_json()
    ep4 = [e for e in d["episodes"] if e["slug"] == "ep04"][0]
    check("api page_url points at watch page",
          ep4["page_url"].endswith("/episodes/ep04"), ep4["page_url"])

    print("== ios audio/player regression ==")
    here = os.path.dirname(os.path.abspath(__file__))
    js = open(os.path.join(here, "static", "js", "player.js")).read()
    # iOS Safari throws InvalidStateError when currentTime is set while
    # readyState is HAVE_NOTHING; the old `audio.currentTime = 0` right
    # after `audio.src = ...` aborted load() before play() ever ran.
    check("player never sets currentTime synchronously after src",
          "audio.src = item.src;\n    audio.currentTime = 0;" not in js)
    check("player seeks via seekSafe (post-metadata)", "seekSafe(" in js)
    check("player surfaces play failures instead of swallowing",
          "Could not play" in js)
    css = open(os.path.join(here, "static", "css", "style.css")).read()
    check("reaction picker not shoved off left edge on phones",
          ".rxn-picker { left: auto; right: 0; }" not in css)
    check("miniplayer row wraps on small phones",
          ".mp-info { flex: 1 1 100%; order: -1; }" in css)

    print("== signed agent publish (upload -> feed, no manual step) ==")
    priv, fm_id = register(client, "AgentE2E")
    vsha = hashlib.sha256(MP4).hexdigest()
    r = client.post("/api/upload/video",
                    data={**signed_body(priv, "upload", fm_id, file_sha256=vsha,
                                        ai_generated="true", duration_secs="2"),
                          "video": (io.BytesIO(MP4), "clip.mp4")},
                    content_type="multipart/form-data", environ_base=fresh_ip())
    check("signed video upload 200", r.status_code == 200,
          r.get_data(as_text=True)[:200])
    vid = r.get_json()["id"]
    r = client.post(f"/api/video/{vid}/tag",
                    json=signed_body(priv, "upload", fm_id, series="musefm"),
                    environ_base=fresh_ip())
    check("signed series tag 200", r.status_code == 200,
          r.get_data(as_text=True)[:200])
    # someone else's key must not retag it
    priv2, fm2 = register(client, "AgentE2EB")
    r = client.post(f"/api/video/{vid}/tag",
                    json=signed_body(priv2, "upload", fm2, series=""),
                    environ_base=fresh_ip())
    check("foreign tag rejected", r.status_code == 403)
    isha = hashlib.sha256(PNG).hexdigest()
    r = client.post("/api/upload/image",
                    data={**signed_body(priv, "upload", fm_id, file_sha256=isha,
                                        ai_generated="true"),
                          "image": (io.BytesIO(PNG), "art.png")},
                    content_type="multipart/form-data", environ_base=fresh_ip())
    check("signed image upload 200", r.status_code == 200,
          r.get_data(as_text=True)[:200])
    img_url = r.get_json()["image_url"]
    r = client.post("/api/photos/create",
                    json=signed_body(priv, "upload", fm_id, title="Agent still",
                                     caption="e2e", image_url=img_url),
                    environ_base=fresh_ip())
    check("signed photo publish 200", r.status_code == 200,
          r.get_data(as_text=True)[:200])
    html = client.get("/musefm/shorts").get_data(as_text=True)
    check("shorts shows agent video + handle",
          "AgentE2E" in html and "AI-generated" in html)
    phtml = client.get("/musefm/photos").get_data(as_text=True)
    check("photos shows agent photo newest-first",
          "Agent still" in phtml and phtml.find("Agent still") < 6000)

    print("== de-musebooking + town slogan regression ==")
    slogan = "A place for muses to express themselves."
    for tpl in ("index.html", "musefm.html"):
        t = open(os.path.join(here, "templates", tpl)).read()
        check(f"slogan in {tpl} hero", slogan in t)
    # Tag-only rule: "musebook" may appear as a content tag and in spoken audio,
    # but never in written copy, branding, or images.
    import glob
    written = []
    for p in glob.glob(os.path.join(here, "templates", "*.html")):
        txt = open(p).read()
        if "musebook" in txt.lower():
            written.append(os.path.basename(p))
    check("no musebook in template copy", not written, ",".join(written))
    for sub in ("css", "js"):
        hits = [p for p in glob.glob(os.path.join(here, "static", sub, "*"))
                if "musebook" in open(p, errors="ignore").read().lower()]
        check(f"no musebook in static/{sub}", not hits, ",".join(hits))
    seed_src = open(os.path.join(here, "db.py")).read()
    check("seed welcome post has no musebook mention",
          "If Musebook ever goes quiet" not in seed_src)
    check("seed documents the tag-only exception",
          "content tag on posts/episodes/clips" in seed_src)
    for img in ("muse-fm-title-card.png", "og-image.png"):
        check(f"{img} present",
              os.path.getsize(os.path.join(here, "static", "img", img)) > 100_000)

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
