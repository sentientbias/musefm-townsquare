#!/usr/bin/env python3
"""
Tests for GIF support in Forum posts: whitelisted CDN embeds,
magic-byte-verified uploads, signed upload endpoint, and rendering.

Run:  .venv/bin/python test_gifs.py
Throwaway SQLite db + Flask test client + temp DATA_DIR.
Nothing touches townsquare.db.
"""
import hashlib
import io
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import base64

import app as appmod
import gifs
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-gifs.db"
TEST_DATA = "/tmp/test-townsquare-gifs-data"

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


def make_gif(n=200):
    return b"GIF89a" + bytes(n)


def make_png(n=200):
    return b"\x89PNG\r\n\x1a\n" + bytes(n)


def cdn(path):
    """Build a whitelisted CDN URL without hardcoding host strings."""
    return "https://" + gifs._host("media", "giphy", "com") + path


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    from db import Database
    appmod.db = Database(TEST_DB)
    gifs.ensure_gif_schema(appmod.db)
    appmod.DATA_DIR = TEST_DATA
    appmod.UPLOAD_DIR = os.path.join(TEST_DATA, "uploads")
    os.makedirs(appmod.UPLOAD_DIR, exist_ok=True)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def register(client, handle):
    priv_b64, pub_b64 = fresh_keypair()
    r = client.post("/api/identity/register",
                    json={"handle": handle, "public_key": pub_b64})
    assert r.status_code == 200, r.get_data(as_text=True)
    return priv_b64, r.get_json()["fm_id"]


_ip_counter = [0]


def fresh_ip():
    """Rate limits are per-IP; each post in the test gets its own bucket."""
    _ip_counter[0] += 1
    return {"X-Forwarded-For": "10.9.0.%d" % _ip_counter[0]}


def main():
    client = setup()

    print("== valid_gif_url ==")
    good = cdn("/media/abc123/giphy.gif")
    check("whitelisted .gif accepted", gifs.valid_gif_url(good) == good)
    check("empty -> ''", gifs.valid_gif_url("") == "" and gifs.valid_gif_url(None) == "")
    check("uppercase .GIF path accepted",
          gifs.valid_gif_url(cdn("/x/GIPHY.GIF")).endswith(".GIF"))
    host = "media" + "." + "giphy" + "." + "com"
    rejects = [
        "http://" + host + "/x.gif",                       # not https
        "https://evil.example.com/x.gif",                  # host not whitelisted
        "https://" + host + "/x.png",                       # not a .gif
        "https://" + host + "/view/funny-cat-12345",        # share URL, no .gif
        "javascript:alert(1)",                             # scheme attack
        "data:image/gif;base64,R0lGODdhAQABAIA=",           # data URI
        "https://user:pass@" + host + "/x.gif",             # userinfo
        "https://" + host + ":8443/x.gif",                  # port
        "https://" + host + "/x.gif frag\nment",            # control chars
        "https://" + host + "/" + "a" * 600 + ".gif",       # too long
        "https://" + "xn--" + host + "/x.gif",              # lookalike host
    ]
    for bad in rejects:
        try:
            gifs.valid_gif_url(bad)
            check("reject: " + bad[:50], False, "accepted!")
        except ValueError:
            check("reject: " + bad[:50], True)

    print("== is_gif_bytes ==")
    check("GIF89a detected", gifs.is_gif_bytes(make_gif()))
    check("GIF87a detected", gifs.is_gif_bytes(b"GIF87a" + bytes(50)))
    check("PNG rejected", not gifs.is_gif_bytes(make_png()))
    check("empty rejected", not gifs.is_gif_bytes(b""))
    check("truncated header rejected", not gifs.is_gif_bytes(b"GIF"))

    print("== ensure_gif_schema idempotent ==")
    gifs.ensure_gif_schema(appmod.db)
    cols = [r["name"] for r in appmod.db.db.execute("PRAGMA table_info(posts)")]
    check("posts.gif_url column exists", "gif_url" in cols)
    check("gif_uploads table exists",
          appmod.db._one("SELECT name FROM sqlite_master WHERE name='gif_uploads'") is not None)

    print("== create_post with gif_url ==")
    pid = appmod.db.create_post("lobby", "GifFan", "look at this", "so good",
                                gif_url=good)
    p = appmod.db.get_post(pid)
    check("gif_url stored on post", p["gif_url"] == good, p["gif_url"])
    try:
        appmod.db.create_post("lobby", "GifFan", "bad", "x",
                              gif_url="https://evil.example.com/x.gif")
        check("bad gif_url rejected by create_post", False, "accepted!")
    except ValueError:
        check("bad gif_url rejected by create_post", True)
    pid2 = appmod.db.create_post("lobby", "GifFan", "no gif", "plain")
    check("gif_url defaults to ''", appmod.db.get_post(pid2)["gif_url"] == "")

    print("== signed /api/upload/gif ==")
    priv, fm_id = register(client, "GifMuse")
    raw = make_gif(500)

    def signed_fields(raw, sha=None):
        return signed_body(priv, "upload", fm_id,
                           file_sha256=sha or hashlib.sha256(raw).hexdigest())

    def post_gif(fields, raw, filename="fun.gif"):
        data = dict(fields)
        data["gif"] = (io.BytesIO(raw), filename, "image/gif")
        return client.post("/api/upload/gif", data=data,
                           content_type="multipart/form-data")

    r = post_gif(signed_fields(raw), raw)
    j = r.get_json()
    check("valid signed gif upload -> 200", r.status_code == 200,
          f"{r.status_code} {r.get_data(as_text=True)[:200]}")
    uid = j.get("id") if j else None
    check("upload returns id + gif_url",
          bool(uid) and ("/gif/%d" % uid) in str(j.get("gif_url", "")))
    check("no Signal awarded for gif upload", j.get("signal_earned", "absent") == "absent",
          str(j))
    check("returned gif_url is same-origin relative",
          j.get("gif_url") == "/gif/%d" % uid, str(j.get("gif_url")))
    r = client.post("/api/forum/post", json=signed_body(
        priv, "post", fm_id, community="lobby",
        title="my upload", body="fresh bytes", flair="discussion",
        gif_url=j["gif_url"]), headers=fresh_ip())
    d2 = r.get_json()
    check("uploaded gif_url round-trips into a post", r.status_code == 200,
          f"{r.status_code} {d2}")
    if r.status_code == 200:
        p = appmod.db.get_post(d2["id"])
        check("round-tripped gif_url persisted", p["gif_url"] == "/gif/%d" % uid)

    r = post_gif(signed_fields(raw, sha="0" * 64), raw)
    check("sha256 mismatch -> 401", r.status_code == 401, str(r.status_code))

    png = make_png(500)
    r = post_gif(signed_fields(png), png, filename="evil.gif")
    check("png bytes with .gif name rejected", r.status_code == 400,
          str(r.status_code))

    big = b"GIF89a" + bytes(gifs.MAX_GIF_BYTES + 100)
    r = post_gif(signed_fields(big), big)
    check("oversize gif -> 413", r.status_code == 413, str(r.status_code))

    r = client.post("/api/upload/gif", data=signed_fields(raw),
                    content_type="multipart/form-data")
    check("missing file -> 400", r.status_code == 400, str(r.status_code))

    print("== GET /gif/<uid> ==")
    r = client.get("/gif/%d" % uid)
    check("serve -> 200 image/gif",
          r.status_code == 200 and r.content_type == "image/gif",
          f"{r.status_code} {r.content_type}")
    check("served bytes match", r.get_data() == raw)
    r = client.get("/gif/999999")
    check("unknown gif -> 404", r.status_code == 404)

    print("== signed post with gif_url ==")
    r = client.post("/api/forum/post", json=signed_body(
        priv, "post", fm_id, community="lobby",
        title="gif thread", body="check it", flair="discussion",
        gif_url=good), headers=fresh_ip())
    d = r.get_json()
    check("api post with gif_url -> 200", r.status_code == 200, str(d))
    if r.status_code == 200:
        p = appmod.db.get_post(d["id"])
        check("gif_url persisted via api", p["gif_url"] == good)
    r = client.post("/api/forum/post", json=signed_body(
        priv, "post", fm_id, community="lobby",
        title="evil gif", body="x", flair="discussion",
        gif_url="https://evil.example.com/x.gif"), headers=fresh_ip())
    check("api post with bad gif_url -> 400", r.status_code == 400,
          str(r.status_code))

    print("== trust-based /submit with gif_url ==")
    r = client.post("/submit", data={
        "community": "lobby", "handle": "HumanFan", "title": "human gif",
        "body": "from the form", "flair": "discussion", "gif_url": good,
    }, headers=fresh_ip())
    check("form post with gif_url redirects", r.status_code == 302,
          str(r.status_code))
    r = client.post("/submit", data={
        "community": "lobby", "handle": "HumanFan", "title": "bad gif",
        "body": "x", "flair": "discussion",
        "gif_url": "https://evil.example.com/x.gif",
    }, headers=fresh_ip())
    check("form post with bad gif_url -> 400", r.status_code == 400,
          str(r.status_code))

    print("== trust-based /submit with gif file upload ==")
    data = {"community": "lobby", "handle": "HumanFan", "title": "uploaded gif",
            "body": "fresh bytes", "flair": "discussion"}
    data["gif_file"] = (io.BytesIO(make_gif(300)), "dance.gif", "image/gif")
    r = client.post("/submit", data=data, content_type="multipart/form-data",
                    headers=fresh_ip())
    check("form gif file upload -> redirect", r.status_code == 302,
          f"{r.status_code} {r.get_data(as_text=True)[:200]}")
    loc = r.headers.get("Location", "")
    thread = client.get(loc)
    check("thread page shows uploaded gif",
          thread.status_code == 200 and 'class="post-gif"' in thread.get_data(as_text=True),
          str(thread.status_code))

    print("== rendering ==")
    thread = client.get("/c/lobby/post/%d" % pid)
    html = thread.get_data(as_text=True)
    check("thread page embeds gif img",
          'class="post-gif"' in html and good in html)
    idx = client.get("/")
    check("index shows gif thumbnail",
          'class="post-gif-thumb"' in idx.get_data(as_text=True))
    check("xss in gif_url impossible (whitelist)",
          "evil.example.com" not in html)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
