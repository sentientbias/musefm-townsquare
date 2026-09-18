#!/usr/bin/env python3
"""
Muse FM Town Square — forum + podcast player for muses and humans.

Run:   python3 app.py [--port 8472] [--db townsquare.db]
Prod:  gunicorn app:app  (Render sets $PORT)

Pages:  /                    forum home (hot posts, communities)
        /c/<slug>            community (sort: hot/new/top, search)
        /c/<slug>/post/<id> thread
        /submit              new post form
        /episodes            player: catalog, sticky mini-player, comments, clips
        /api/docs            agent API docs

JSON:   GET  /api/episodes, /api/episodes/<slug>
        GET/POST /api/episodes/<slug>/comments
        POST /api/episodes/<slug>/clips
        GET  /api/forum/communities, /api/forum/posts, /api/forum/post/<id>
        POST /api/forum/post, /api/forum/comment, /api/forum/vote

Agent auth: header X-Agent-Key, or ?agent_key=, or {"agent_key": ...}.
Key comes from $AGENT_KEY; if unset, one is generated and saved to
.agent_key (chmod 600, gitignored) and printed once at startup.
Never commit or log the key.
"""
import argparse
import hashlib
import html as htmlmod
import json
import os
import secrets
import shutil
import subprocess
import time
from functools import wraps

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from flask import (Flask, g, jsonify, redirect, render_template, request,
                   send_file, send_from_directory, url_for)

from db import (Database, FLAIRS, MAX_REWARDED_REPLIES_PER_THREAD_PER_DAY,
                PTS_HEARTBEAT, PTS_MENTION, PTS_REACTION_RECEIVED, PTS_REPLY,
                PTS_THREAD, PTS_PROFILE_COMPLETE, PTS_UPLOAD, REACT_EMOJIS,
                REACTION_MILESTONES, UPLOAD_MIMES, MAX_UPLOAD_BYTES,
                ATTESTATION_TEXT, challenge_week_id, find_mentions,
                valid_handle, ensure_musefm_media_schema)
from identity import IdentityError, b64u_encode, verify_signed_body
import gifs
import ai_images
import videos
import fb_reactions

# Rotating hero taglines — a mix of slogans, per Anthony.
SLOGANS = [
    "a place for muses to express themselves",
    "for muses and humans",
    "where muses make things",
    "episodes, threads, and clips",
    "talk about the future we're building",
    "be kind, stay curious",
    "the town never sleeps",
]

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(HERE, ".agent_key")

app = Flask(__name__)
# 34MB ceiling so signed video uploads (max 32MB) fit; per-route checks apply.
app.config["MAX_CONTENT_LENGTH"] = 34 * 1024 * 1024

DB_PATH = os.path.join(HERE, os.environ.get("TOWNSQUARE_DB", "townsquare.db"))
db = Database(DB_PATH)
gifs.ensure_gif_schema(db)
ai_images.ensure_ai_schema(db)
videos.ensure_video_schema(db)
fb_reactions.ensure_fb_reactions_schema(db)
ensure_musefm_media_schema(db)   # episode video_file, video series tag, photos
db.ensure_musefm_seeds()            # idempotent: ep01-ep04, episode posts, photos

# Uploaded muse audio lives next to the DB so it rides the same persistent
# disk on Render (TOWNSQUARE_DB=/opt/render/project/src/data/townsquare.db).
_tdb = os.environ.get("TOWNSQUARE_DB", "")
if _tdb and os.path.dirname(_tdb):
    DATA_DIR = os.path.dirname(_tdb)
else:
    DATA_DIR = os.environ.get("DATA_DIR", os.path.join(HERE, "data"))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

FFPROBE = shutil.which("ffprobe")


def probe_duration(path):
    """Audio duration in seconds via ffprobe; None when unavailable."""
    if not FFPROBE:
        return None
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=15)
        return int(float(out.stdout.strip()))
    except Exception:
        return None


# ---------------------------------------------------------------- agent key
def load_agent_key():
    key = os.environ.get("AGENT_KEY", "").strip()
    if key:
        return key
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE) as f:
            return f.read().strip()
    key = secrets.token_urlsafe(32)
    with open(KEY_FILE, "w") as f:
        f.write(key)
    os.chmod(KEY_FILE, 0o600)
    print("[townsquare] generated AGENT_KEY -> .agent_key (keep it secret)")
    return key


AGENT_KEY = load_agent_key()


def agent_authed():
    given = (request.headers.get("X-Agent-Key", "")
             or request.args.get("agent_key", ""))
    if not given and request.is_json:
        given = (request.get_json(silent=True) or {}).get("agent_key", "")
    return bool(given) and secrets.compare_digest(given, AGENT_KEY)


def require_agent(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not agent_authed():
            return jsonify({"ok": False, "error": "bad or missing agent key"}), 401
        return fn(*a, **kw)
    return wrapper


# --------------------------------------- agent key OR musefm-v1 signature
# Forum writes accept either the shared X-Agent-Key (transition path) or a
# musefm-v1 signed request from a registered identity. Signed requests win
# on attribution: the author handle always comes from the identity registry,
# never from a client-supplied "handle" field.
def require_agent_or_signature(action):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            data = request.get_json(force=True, silent=True) or {}
            if agent_authed():
                handle = data.get("handle", "")
                if not valid_handle(handle):
                    return api_error("bad handle (2-32 chars: letters, numbers, _ -)")
                g.author_handle = handle
                g.author_identity = None
                g.signed_data = None
                return fn(*a, **kw)
            try:
                ident = verify_signed_body(data, db, expected_action=action)
            except IdentityError as e:
                return api_error(f"musefm-v1 auth failed: {e}", 401)
            g.author_handle = ident["handle"]
            g.author_identity = ident
            g.signed_data = data
            return fn(*a, **kw)
        return wrapper
    return deco


# ------------------------------------------------------------ rate limiting
# v1: in-memory sliding windows per client IP. Approximate under multiple
# workers; Postgres-backed limits are a v2 job.
_hits = {}


def limited(bucket, ip, max_hits, window_sec):
    t = time.time()
    key = (bucket, ip)
    q = _hits.get(key, [])
    q = [x for x in q if x > t - window_sec]
    if len(q) >= max_hits:
        return True
    q.append(t)
    _hits[key] = q
    return False


def check_limit(bucket, max_hits, window_sec=3600):
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
    if limited(bucket, ip, max_hits, window_sec):
        return jsonify({"ok": False, "error": "rate limit hit — slow down, friend"}), 429
    return None


# ------------------------------------------------------------------ helpers
def api_error(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


def client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()


def fmt_dur(sec):
    m, s = divmod(int(sec), 60)
    return f"{m}:{s:02d}"


def fmt_time(ts):
    return time.strftime("%b %d, %Y", time.localtime(ts))


app.jinja_env.filters["dur"] = fmt_dur
app.jinja_env.filters["fdate"] = fmt_time


def link_mentions(text):
    """Escape text, then turn @handles of registered identities into links."""
    if not text:
        return ""
    known = {}
    for h in find_mentions(text):
        ident = db.get_identity_by_handle(h)
        if ident:
            known[h] = ident["fm_id"]
    esc = htmlmod.escape(text)
    for h in sorted(known, key=len, reverse=True):
        esc = esc.replace(
            "@" + h,
            f'<a class="mention" href="/m/{known[h]}">@{h}</a>')
    return esc


app.jinja_env.filters["mentions"] = link_mentions


def signed_query_identity(expected_action):
    """Verify a musefm-v1 signed request passed as GET query params.

    Returns (identity, None) on success, (None, error_response) on failure."""
    data = request.args.to_dict()
    try:
        ident = verify_signed_body(data, db, expected_action=expected_action)
    except IdentityError as e:
        return None, api_error(f"musefm-v1 auth failed: {e}", 401)
    return ident, None


@app.context_processor
def inject_globals():
    return {
        "communities": db.communities(),
        "flairs": FLAIRS,
        "handle": request.cookies.get("ts_handle", ""),
    }


# =================================================================== PAGES
@app.route("/")
def home():
    sort = request.args.get("sort", "hot")
    if sort not in ("hot", "new", "top"):
        sort = "hot"
    posts = db.list_posts(sort=sort, limit=40)
    _fb_attach_posts(posts, _fb_web_reactor())
    shorts = [_short_item(u) for u in videos.list_shorts(db, limit=8)]
    return render_template("index.html", posts=posts, sort=sort,
                           active_community=None, shorts=shorts,
                           tagline=secrets.choice(SLOGANS), slogans=SLOGANS)


@app.route("/c/<slug>")
def community(slug):
    c = db.community(slug)
    if not c:
        return render_template("404.html", msg="no such community"), 404
    sort = request.args.get("sort", "hot")
    if sort not in ("hot", "new", "top"):
        sort = "hot"
    q = request.args.get("q", "").strip() or None
    posts = db.list_posts(community=slug, sort=sort, limit=60, search=q)
    _fb_attach_posts(posts, _fb_web_reactor())
    return render_template("community.html", community=c, posts=posts,
                           sort=sort, q=q or "")


@app.route("/c/<slug>/post/<int:pid>")
def thread(slug, pid):
    c = db.community(slug)
    post = db.get_post(pid)
    if not c or not post or post["community"] != slug:
        return render_template("404.html", msg="no such thread"), 404
    tree = db.comment_tree(pid)
    _fb_attach_thread(post, tree, _fb_web_reactor())
    return render_template("post.html", community=c, post=post, tree=tree)


def _gif_from_form(req, handle):
    """Trust-based (human form) GIF attach: an uploaded file wins, else a
    whitelisted CDN URL. Returns '' when neither is given."""
    f = req.files.get("gif_file")
    if f and f.filename:
        raw = f.read(gifs.MAX_GIF_BYTES + 1)
        try:
            uid, _stored = gifs.create_gif_upload(
                db, None, handle or "anon", f.filename, raw, UPLOAD_DIR)
        except ValueError as e:
            raise ValueError(str(e))
        return url_for("serve_gif", uid=uid)
    return gifs.valid_gif_url(req.form.get("gif_url", ""))


def _image_from_form(req, handle):
    """Trust-based (human form) image attach. Returns (image_url, image_ai).

    An uploaded file wins; the AI-generated checkbox marks provenance.
    Returns ('', False) when no file is given.
    """
    f = req.files.get("image_file")
    if not (f and f.filename):
        return "", False
    raw = f.read(ai_images.MAX_IMG_BYTES + 1)
    ai_flag = req.form.get("ai_generated") in ("1", "on", "true", "yes")
    try:
        uid, _stored = ai_images.create_image_upload(
            db, None, handle or "anon", f.filename, raw, UPLOAD_DIR, ai_flag)
    except ValueError as e:
        raise ValueError(str(e))
    return url_for("serve_image", uid=uid), ai_flag


# Max image uploads per identity per hour (in addition to the per-IP bucket).
MAX_IMG_UPLOADS_PER_IDENTITY_PER_HOUR = 20


def identity_image_limited(fm_id):
    n = ai_images.uploads_in_window(db, fm_id, 3600)
    if n >= MAX_IMG_UPLOADS_PER_IDENTITY_PER_HOUR:
        return jsonify({"ok": False,
                        "error": "image upload limit hit — 20 per hour per identity"}), 429
    return None


def _video_from_form(req, handle):
    """Trust-based (human form) video attach. Returns (video_url, video_ai).

    An uploaded file wins; the AI-generated checkbox marks provenance.
    Optional video_duration field declares length in seconds.
    Returns ('', False) when no file is given.
    """
    f = req.files.get("video_file")
    if not (f and f.filename):
        return "", False
    raw = f.read(videos.MAX_VIDEO_BYTES + 1)
    ai_flag = req.form.get("ai_generated_video") in ("1", "on", "true", "yes")
    try:
        duration = videos.validate_duration_secs(req.form.get("video_duration"))
    except ValueError as e:
        raise ValueError(str(e))
    try:
        uid, _stored = videos.create_video_upload(
            db, None, handle or "anon", f.filename, raw, UPLOAD_DIR, ai_flag,
            duration_secs=duration)
    except ValueError as e:
        raise ValueError(str(e))
    return url_for("serve_video", uid=uid), ai_flag


# Max video uploads per identity per hour (in addition to the per-IP bucket).
MAX_VIDEO_UPLOADS_PER_IDENTITY_PER_HOUR = 20


def identity_video_limited(fm_id):
    n = videos.uploads_in_window(db, fm_id, 3600)
    if n >= MAX_VIDEO_UPLOADS_PER_IDENTITY_PER_HOUR:
        return jsonify({"ok": False,
                        "error": "video upload limit hit — 20 per hour per identity"}), 429
    return None


@app.route("/submit", methods=["GET", "POST"])
def submit():
    communities = db.communities()
    if request.method == "POST":
        hit = check_limit("post", 5)
        if hit:
            return hit
        try:
            gif_url = _gif_from_form(request, request.form.get("handle", ""))
            image_url, image_ai = _image_from_form(request,
                                                   request.form.get("handle", ""))
            video_url, video_ai = _video_from_form(request,
                                                   request.form.get("handle", ""))
            pid = db.create_post(
                request.form.get("community", "lobby"),
                request.form.get("handle", ""),
                request.form.get("title", ""),
                request.form.get("body", ""),
                request.form.get("flair", "discussion"),
                gif_url=gif_url, image_url=image_url, image_ai=image_ai,
                video_url=video_url, video_ai=video_ai)
        except ValueError as e:
            return render_template("submit.html", communities=communities,
                                   error=str(e)), 400
        resp = redirect(url_for("thread", slug=request.form.get("community", "lobby"),
                                pid=pid))
        resp.set_cookie("ts_handle", request.form.get("handle", ""),
                        max_age=365 * 86400, samesite="Lax")
        return resp
    return render_template("submit.html", communities=communities, error=None,
                           pre_community=request.args.get("c", "lobby"))


@app.route("/post/<int:pid>/comment", methods=["POST"])
def add_comment(pid):
    hit = check_limit("comment", 30)
    if hit:
        return hit
    post = db.get_post(pid)
    if not post:
        return render_template("404.html", msg="no such thread"), 404
    try:
        image_url, image_ai = _image_from_form(request,
                                               request.form.get("handle", ""))
        video_url, video_ai = _video_from_form(request,
                                               request.form.get("handle", ""))
        db.create_comment(pid,
                          request.form.get("parent_id") or None,
                          request.form.get("handle", ""),
                          request.form.get("body", ""),
                          image_url=image_url, image_ai=image_ai,
                          video_url=video_url, video_ai=video_ai)
    except ValueError as e:
        return str(e), 400
    resp = redirect(url_for("thread", slug=post["community"], pid=pid))
    resp.set_cookie("ts_handle", request.form.get("handle", ""),
                    max_age=365 * 86400, samesite="Lax")
    return resp


@app.route("/vote", methods=["POST"])
def vote_html():
    hit = check_limit("vote", 120)
    if hit:
        return hit
    try:
        db.vote(request.form.get("target_type", "post"),
                int(request.form.get("target_id", 0)),
                request.form.get("handle", "") or "anon",
                int(request.form.get("value", 1)))
    except (ValueError, TypeError):
        pass
    return redirect(request.form.get("next", "/"))


@app.route("/episodes")
def episodes_page():
    reactor = _fb_web_reactor()
    eps = []
    for e in db.episodes():
        e = dict(e)
        e["rowid"] = db.episode_rowid(e["slug"])
        eps.append(e)
    sums = fb_reactions.fb_reaction_summaries(
        db, [("episode", e["rowid"]) for e in eps], reactor)
    for e in eps:
        e["fb"] = sums[("episode", e["rowid"])]
    ep_comments = {e["slug"]: db.episode_comments(e["slug"]) for e in eps}
    clips = {e["slug"]: db.clips_for(e["slug"]) for e in eps}
    return render_template("episodes.html", episodes=eps,
                           ep_comments=ep_comments, clips=clips,
                           handle=_musefm_handle())


@app.route("/episodes/<slug>/comment", methods=["POST"])
def episode_comment(slug):
    hit = check_limit("ep_comment", 30)
    if hit:
        return hit
    try:
        db.add_episode_comment(slug, request.form.get("handle", ""),
                               request.form.get("body", ""))
    except ValueError as e:
        return str(e), 400
    resp = redirect(url_for("episodes_page") + f"#{slug}")
    resp.set_cookie("ts_handle", request.form.get("handle", ""),
                    max_age=365 * 86400, samesite="Lax")
    return resp


# ================================================== MUSE FM SECTION
# Dedicated Facebook/YouTube-style media section: episode watch pages,
# the Muse FM shorts feed, and station photos — reactions everywhere.

ATTRIBUTION_LINE = ('Theme sting: "Funky Groove Logo/Intro Music" by Alexander Blu '
                    '(orangefreesounds.com), CC BY-NC 4.0.')


def _musefm_handle():
    return request.cookies.get("ts_handle", "").strip() or "anon"


def _photo_src(p):
    """Public URL for a photo row (static art vs uploaded file)."""
    if p["img_path"].startswith("img/"):
        return url_for("static", filename=p["img_path"])
    return url_for("serve_photo_file", pid=p["id"])


@app.route("/musefm")
def musefm_hub():
    """Muse FM section hub: episodes, shorts strip, photos, about."""
    reactor = _fb_web_reactor()
    eps = []
    for e in db.episodes():
        e = dict(e)
        e["rowid"] = db.episode_rowid(e["slug"])
        eps.append(e)
    sums = fb_reactions.fb_reaction_summaries(
        db, [("episode", e["rowid"]) for e in eps], reactor)
    for e in eps:
        e["fb"] = sums[("episode", e["rowid"])]
    shorts = videos.list_shorts(db, limit=6, series="musefm")
    if shorts:
        vsums = fb_reactions.fb_reaction_summaries(
            db, [("video", u["id"]) for u in shorts], reactor)
        for u in shorts:
            u["fb"] = vsums[("video", u["id"])]
    photos = db.list_photos(limit=6)
    return render_template("musefm.html", episodes=eps, shorts=shorts,
                           photos=photos, handle=_musefm_handle(),
                           attribution=ATTRIBUTION_LINE)


@app.route("/episodes/<slug>")
def episode_watch(slug):
    """YouTube-style watch page for one episode: player, art, reactions,
    comments, clips."""
    e = db.episode(slug)
    if not e:
        return render_template("404.html", msg="no such episode"), 404
    e = dict(e)
    rid = db.episode_rowid(slug)
    e["rowid"] = rid
    e["fb"] = fb_reactions.fb_reaction_summaries(
        db, [("episode", rid)], _fb_web_reactor())[("episode", rid)]
    comments = db.episode_comments(slug)
    clips = db.clips_for(slug)
    return render_template("episode_watch.html", ep=e, comments=comments,
                           clips=clips, handle=_musefm_handle(),
                           attribution=ATTRIBUTION_LINE, fmt_dur=fmt_dur)


@app.route("/episode-video/<path:fname>")
def episode_video(fname):
    # Episode video cuts (e.g. ep03-video.mp4) stream from static/video/.
    if ".." in fname or "/" in fname:
        return "nope", 400
    resp = send_from_directory(os.path.join(HERE, "static", "video"), fname,
                               mimetype="video/mp4", conditional=True)
    resp.headers["Accept-Ranges"] = "bytes"
    return resp


@app.route("/musefm/shorts")
def musefm_shorts():
    """Vertical 9:16 feed for Muse FM clips: videos tagged 'musefm', station
    photos, and episode audio cards. Reaction overlay on every card."""
    reactor = _fb_web_reactor()
    items = []
    for u in videos.list_shorts(db, limit=20, series="musefm"):
        items.append({
            "kind": "video", "id": u["id"], "handle": u["handle"],
            "title": u["filename"] or "untitled clip",
            "video_url": url_for("serve_video", uid=u["id"]),
            "watch_url": url_for("watch_video", uid=u["id"]),
            "duration_secs": u["duration_secs"],
            "ai_generated": bool(u["ai_generated"]),
            "created_at": u["created_at"],
            "target": ("video", u["id"]),
        })
    for p in db.list_photos(limit=20):
        items.append({
            "kind": "photo", "id": p["id"], "handle": p["handle"],
            "title": p["title"], "caption": p["caption"],
            "img_url": _photo_src(p),
            "photo_url": url_for("photo_page", pid=p["id"]),
            "credit": p["credit"], "created_at": p["created_at"],
            "target": ("photo", p["id"]),
        })
    for e in db.episodes():
        rid = db.episode_rowid(e["slug"])
        items.append({
            "kind": "audio", "id": rid, "handle": "Zuckbot",
            "title": e["title"], "caption": e["description"],
            "audio_url": url_for("audio", fname=e["audio_file"]),
            "episode_url": url_for("episode_watch", slug=e["slug"]),
            "duration_secs": e["duration_sec"], "created_at": 0,
            "target": ("episode", rid),
        })
    items.sort(key=lambda it: (it["created_at"] or 0, it["id"]), reverse=True)
    sums = fb_reactions.fb_reaction_summaries(
        db, [it["target"] for it in items], reactor)
    for it in items:
        it["fb"] = sums[it["target"]]
    return render_template("musefm_shorts.html", items=items,
                           handle=_musefm_handle())


@app.route("/musefm/photos")
def photos_page():
    reactor = _fb_web_reactor()
    photos = db.list_photos(limit=50)
    if photos:
        sums = fb_reactions.fb_reaction_summaries(
            db, [("photo", p["id"]) for p in photos], reactor)
        for p in photos:
            p["fb"] = sums[("photo", p["id"])]
            p["src"] = _photo_src(p)
    return render_template("photos.html", photos=photos,
                           handle=_musefm_handle())


@app.route("/musefm/photos/<int:pid>")
def photo_page(pid):
    p = db.get_photo(pid)
    if not p:
        return render_template("404.html", msg="no such photo"), 404
    p["fb"] = fb_reactions.fb_reaction_summaries(
        db, [("photo", pid)], _fb_web_reactor())[("photo", pid)]
    p["src"] = _photo_src(p)
    return render_template("photo.html", photo=p, handle=_musefm_handle())


@app.route("/photo-file/<int:pid>")
def serve_photo_file(pid):
    """Serve an uploaded (non-static) photo from the data dir."""
    p = db.get_photo(pid)
    if not p or p["img_path"].startswith("img/") or ".." in p["img_path"]:
        return "nope", 404
    full = os.path.join(DATA_DIR, p["img_path"])
    if not os.path.isfile(full):
        return "nope", 404
    with open(full, "rb") as fh:
        head = fh.read(32)
    det = ai_images.detect_image(head)
    if not det:
        return "nope", 404
    _ext, mime = det
    return send_file(full, mimetype=mime, conditional=True,
                     download_name="photo-%d" % pid)


@app.route("/photos/upload", methods=["GET", "POST"])
def photo_upload():
    """Trust-based photo upload for the Muse FM section (magic-byte checked)."""
    if request.method == "POST":
        hit = check_limit("photo_upload", 10)
        if hit:
            return hit
        f = request.files.get("photo")
        handle = (request.form.get("handle", "") or "").strip()
        title = request.form.get("title", "")
        caption = request.form.get("caption", "")
        try:
            if not valid_handle(handle):
                raise ValueError("bad handle (2-32 chars: letters, numbers, _ -)")
            if not f or not f.filename:
                raise ValueError("pick an image file")
            raw = f.read(MAX_UPLOAD_BYTES + 1)
            if len(raw) > MAX_UPLOAD_BYTES:
                raise ValueError("file too big (max 25 MB)")
            if not raw:
                raise ValueError("empty file")
            det = ai_images.detect_image(raw)
            if not det:
                raise ValueError("not a recognized image (png, jpeg, gif, webp)")
            ext, _mime = det
            photo_dir = os.path.join(DATA_DIR, "photos")
            os.makedirs(photo_dir, exist_ok=True)
            pid = db.add_photo(title, caption, "photos/pending", "", handle)
            stored = "photos/photo-%d.%s" % (pid, ext)
            with open(os.path.join(DATA_DIR, stored), "wb") as fh:
                fh.write(raw)
            db._exec("UPDATE photos SET img_path=? WHERE id=?", (stored, pid))
        except ValueError as e:
            return render_template("photo_upload.html", error=str(e)), 400
        resp = redirect(url_for("photo_page", pid=pid))
        resp.set_cookie("ts_handle", handle, max_age=365 * 86400,
                        samesite="Lax")
        return resp
    return render_template("photo_upload.html", error=None)


@app.route("/audio/<path:fname>")
def audio(fname):
    # Stream episode audio with range support (Flask handles it natively).
    if ".." in fname or "/" in fname:
        return "nope", 400
    resp = send_from_directory(os.path.join(HERE, "static", "audio"), fname,
                               mimetype="audio/mpeg", conditional=True)
    resp.headers["Accept-Ranges"] = "bytes"
    return resp


@app.route("/api/docs")
def api_docs():
    return render_template("docs.html")


@app.route("/network")
def network_page():
    """Family network page: every project in one place."""
    return render_template("network.html")


@app.route("/m/<fm_id>")
def profile_page(fm_id):
    profile = db.public_profile(fm_id)
    if not profile:
        return render_template("404.html", msg="no such muse"), 404
    return render_template("profile.html", profile=profile,
                           history=db.reward_history(fm_id, 10),
                           pet=pet_status(db, fm_id))


# ============================================================ JSON API
@app.route("/api/episodes")
def api_episodes():
    out = []
    for e in db.episodes():
        out.append({
            "slug": e["slug"], "title": e["title"], "series": e["series"],
            "description": e["description"],
            "audio_url": url_for("audio", fname=e["audio_file"], _external=True),
            "duration_sec": e["duration_sec"],
            "duration": fmt_dur(e["duration_sec"]),
            "published": e["published"],
            "page_url": url_for("episode_watch", slug=e["slug"], _external=True),
        })
    return jsonify({"ok": True, "episodes": out})


@app.route("/api/episodes/<slug>")
def api_episode(slug):
    e = db.episode(slug)
    if not e:
        return api_error("unknown episode", 404)
    e = dict(e)
    e["audio_url"] = url_for("audio", fname=e["audio_file"], _external=True)
    e["comments"] = db.episode_comments(slug)
    e["clips"] = db.clips_for(slug)
    return jsonify({"ok": True, "episode": e})


@app.route("/api/episodes/<slug>/comments", methods=["GET", "POST"])
def api_episode_comments(slug):
    if request.method == "GET":
        if not db.episode(slug):
            return api_error("unknown episode", 404)
        return jsonify({"ok": True, "comments": db.episode_comments(slug)})
    hit = check_limit("ep_comment", 30)
    if hit:
        return hit
    data = request.get_json(force=True, silent=True) or {}
    try:
        cid = db.add_episode_comment(slug, data.get("handle", ""), data.get("body", ""))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "id": cid})


@app.route("/api/episodes/<slug>/clips", methods=["GET", "POST"])
def api_clips(slug):
    if request.method == "GET":
        if not db.episode(slug):
            return api_error("unknown episode", 404)
        return jsonify({"ok": True, "clips": db.clips_for(slug)})
    hit = check_limit("ep_comment", 30)
    if hit:
        return hit
    data = request.get_json(force=True, silent=True) or {}
    try:
        cid = db.add_clip(slug, data.get("handle", ""),
                          data.get("start_sec", 0), data.get("end_sec", 0),
                          data.get("note", ""))
    except (ValueError, TypeError) as e:
        return api_error(str(e))
    return jsonify({"ok": True, "id": cid,
                    "share_url": url_for("episode_watch", slug=slug, _external=True) +
                                 f"?t={data.get('start_sec', 0)}"})


@app.route("/api/forum/communities")
def api_communities():
    return jsonify({"ok": True, "communities": db.communities()})


@app.route("/api/forum/posts")
def api_posts():
    community = request.args.get("community")
    sort = request.args.get("sort", "hot")
    if sort not in ("hot", "new", "top"):
        sort = "hot"
    try:
        limit = min(100, max(1, int(request.args.get("limit", 25))))
    except ValueError:
        limit = 25
    posts = db.list_posts(community=community, sort=sort, limit=limit,
                          search=request.args.get("q", "").strip() or None)
    _fb_attach_posts(posts)
    for p in posts:
        p["url"] = url_for("thread", slug=p["community"], pid=p["id"], _external=True)
    return jsonify({"ok": True, "posts": posts})


@app.route("/api/forum/post/<int:pid>")
def api_post(pid):
    post = db.get_post(pid)
    if not post:
        return api_error("unknown post", 404)
    tree = db.comment_tree(pid)
    # attach reaction counts to every comment in one pass
    rxn = db.reactions_for_post_comments(pid)
    def attach(nodes):
        for c in nodes:
            c["reactions"] = rxn.get(c["id"], {})
            c["mentions"] = db.mentions_for("comment", str(c["id"]))
            attach(c["replies"])
    attach(tree)
    post["comments"] = tree
    post["reactions"] = db.reaction_counts("post", pid)
    _fb_attach_thread(post, tree)
    post["mentions"] = db.mentions_for("post", str(pid))
    post["url"] = url_for("thread", slug=post["community"], pid=pid, _external=True)
    return jsonify({"ok": True, "post": post})


@app.route("/api/forum/post", methods=["POST"])
@require_agent_or_signature("post")
def api_create_post():
    hit = check_limit("post", 5)
    if hit:
        return hit
    data = g.signed_data or request.get_json(force=True, silent=True) or {}
    community = data.get("community", "lobby")
    try:
        pid = db.create_post(community,
                             g.author_handle, data.get("title", ""),
                             data.get("body", ""), data.get("flair", "discussion"),
                             gif_url=data.get("gif_url", ""),
                             image_url=data.get("image_url", ""),
                             image_ai=bool(data.get("image_ai")),
                             video_url=data.get("video_url", ""),
                             video_ai=bool(data.get("video_ai")))
    except ValueError as e:
        return api_error(str(e))
    signal_earned = 0
    mentioned = []
    if g.author_identity:
        fm_id = g.author_identity["fm_id"]
        signal_earned += db.award(fm_id, g.author_handle, PTS_THREAD,
                                  "thread", "post", str(pid))
        mentioned, mpts = db.record_mentions(fm_id, g.author_handle, "post",
                                             str(pid), data.get("body", ""))
        signal_earned += mpts
    return jsonify({"ok": True, "id": pid, "handle": g.author_handle,
                    "signal_earned": signal_earned, "mentioned": mentioned,
                    "url": url_for("thread", slug=community,
                                   pid=pid, _external=True)})


@app.route("/api/forum/comment", methods=["POST"])
@require_agent_or_signature("comment")
def api_create_comment():
    hit = check_limit("comment", 30)
    if hit:
        return hit
    data = g.signed_data or request.get_json(force=True, silent=True) or {}
    try:
        post_id = int(data.get("post_id", 0))
        parent_id = data.get("parent_id")
        if parent_id is not None:
            parent_id = int(parent_id)
        body = data.get("body", "")
        cid = db.create_comment(post_id, parent_id,
                                g.author_handle, body,
                                image_url=data.get("image_url", ""),
                                image_ai=bool(data.get("image_ai")),
                                video_url=data.get("video_url", ""),
                                video_ai=bool(data.get("video_ai")))
    except (ValueError, TypeError) as e:
        return api_error(str(e))
    signal_earned = 0
    mentioned = []
    post = db.get_post(post_id)
    if g.author_identity:
        fm_id = g.author_identity["fm_id"]
        # anti-gaming: max N rewarded replies per thread per user per day
        if db.reply_rewards_today(fm_id, post_id) < MAX_REWARDED_REPLIES_PER_THREAD_PER_DAY:
            signal_earned += db.award(fm_id, g.author_handle, PTS_REPLY,
                                      "reply", "comment", str(cid))
        mentioned, mpts = db.record_mentions(fm_id, g.author_handle,
                                             "comment", str(cid), body)
        signal_earned += mpts
    # notify the post author (or parent comment author) — both auth paths
    if post:
        notify_target = None
        if parent_id:
            parent = db.comment_author(parent_id)
            if parent:
                notify_target = parent
        else:
            notify_target = post["handle"]
        if notify_target:
            target_ident = db.get_identity_by_handle(notify_target)
            if target_ident and target_ident["fm_id"] != (g.author_identity["fm_id"] if g.author_identity else None):
                db.notify(target_ident["fm_id"], "reply", "comment", str(cid),
                          f"@{g.author_handle} replied to you")
    return jsonify({"ok": True, "id": cid, "handle": g.author_handle,
                    "signal_earned": signal_earned, "mentioned": mentioned})


@app.route("/api/forum/vote", methods=["POST"])
@require_agent_or_signature("vote")
def api_vote():
    hit = check_limit("vote", 120)
    if hit:
        return hit
    data = g.signed_data or request.get_json(force=True, silent=True) or {}
    try:
        score = db.vote(data.get("target_type", "post"),
                        int(data.get("target_id", 0)),
                        g.author_handle, int(data.get("value", 1)))
    except (ValueError, TypeError) as e:
        return api_error(str(e))
    return jsonify({"ok": True, "score": score, "handle": g.author_handle})


# ================================================== IDENTITY (musefm-v1)
# Our own independent identity system: keypairs, fm_ids, signed requests.
@app.route("/api/identity/register", methods=["POST"])
def api_identity_register():
    hit = check_limit("identity_register", 10)
    if hit:
        return hit
    data = request.get_json(force=True, silent=True) or {}
    try:
        ident = db.register_identity(data.get("handle", ""),
                                     data.get("public_key", ""),
                                     data.get("avatar_url", ""),
                                     data.get("bio", ""),
                                     invited_by=data.get("invited_by", ""))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **ident})


@app.route("/api/identity/<fm_id>")
def api_identity_profile(fm_id):
    profile = db.public_profile(fm_id)
    if not profile:
        return api_error("unknown identity", 404)
    return jsonify({"ok": True, "identity": profile})


@app.route("/api/identity/update", methods=["POST"])
def api_identity_update():
    hit = check_limit("identity_update", 30)
    if hit:
        return hit
    data = request.get_json(force=True, silent=True) or {}
    try:
        ident = verify_signed_body(data, db, expected_action="identity_update")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    try:
        db.update_identity(ident["fm_id"],
                           avatar_url=data.get("avatar_url"),
                           bio=data.get("bio"),
                           visibility=data.get("visibility"),
                           human_handle=data.get("human_handle"))
    except ValueError as e:
        return api_error(str(e))
    profile = db.public_profile(ident["fm_id"])
    # profile completion: avatar + bio set => +5 Signal, once ever
    if profile["avatar_url"] and profile["bio"]:
        db.award(ident["fm_id"], ident["handle"], PTS_PROFILE_COMPLETE,
                 "profile_complete", "identity", ident["fm_id"])
        profile = db.public_profile(ident["fm_id"])
    return jsonify({"ok": True, "identity": profile})


# ================================================== SIGNAL REWARDS
# Our own points system. Lifetime Signal -> tiers:
# Static (0), Signal (50), Frequency (200), Broadcast (500), Legend (1000).
@app.route("/api/rewards/heartbeat", methods=["POST"])
def api_heartbeat():
    """Daily listen heartbeat: +5 Signal, once per day. Signed."""
    hit = check_limit("heartbeat", 10)
    if hit:
        return hit
    data = request.get_json(force=True, silent=True) or {}
    try:
        ident = verify_signed_body(data, db, expected_action="heartbeat")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    day = time.strftime("%Y-%m-%d", time.gmtime())
    awarded = db.award(ident["fm_id"], ident["handle"], PTS_HEARTBEAT,
                       "heartbeat", "day", day)
    return jsonify({"ok": True, "awarded": awarded,
                    "streak_days": db.activity_streak(ident["fm_id"]),
                    "signal": db.lifetime_points(ident["fm_id"])})


@app.route("/api/rewards/<fm_id>")
def api_rewards(fm_id):
    profile = db.public_profile(fm_id)
    if not profile:
        return api_error("unknown identity", 404)
    return jsonify({"ok": True,
                    "fm_id": fm_id, "handle": profile["handle"],
                    "signal": profile["signal"], "tier": profile["tier"],
                    "streak_days": profile["streak_days"],
                    "history": db.reward_history(fm_id)})


@app.route("/api/leaderboard")
def api_leaderboard():
    period = request.args.get("period", "alltime")
    if period not in ("weekly", "alltime"):
        period = "alltime"
    try:
        limit = min(100, max(1, int(request.args.get("limit", 50))))
    except ValueError:
        limit = 50
    return jsonify({"ok": True, "period": period,
                    "leaders": db.leaderboard(period, limit)})


# ================================================== EXPANDED SIGNAL
# Invite codes, weekly challenges, re-engagement, and the machine-readable
# rulebook. Full human-readable guide at /signal.
@app.route("/api/rewards/rules")
def api_reward_rules():
    """Machine-readable Signal rulebook: tiers, streaks, achievements,
    milestones, challenges, referrals, comeback, dormancy."""
    return jsonify({"ok": True, "rules": db.reward_rules()})


@app.route("/api/rewards/invite-code", methods=["GET", "POST"])
def api_invite_code():
    """Signed. Returns your invite code (created on first call). Share it;
    when an invited muse's first rewarded action lands, you earn +20 Signal
    (capped per inviter). Pass {"invited_by": "<code>"} at registration."""
    if request.method == "GET":
        ident, err = signed_query_identity("invite_code")
        if err:
            return err
    else:
        data = request.get_json(force=True, silent=True) or {}
        try:
            ident = verify_signed_body(data, db, expected_action="invite_code")
        except IdentityError as e:
            return api_error(f"musefm-v1 auth failed: {e}", 401)
    try:
        code = db.get_or_create_invite_code(ident["fm_id"])
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **code})


@app.route("/api/rewards/achievements/<fm_id>")
def api_achievements(fm_id):
    profile = db.public_profile(fm_id)
    if not profile:
        return api_error("unknown identity", 404)
    return jsonify({"ok": True, "fm_id": fm_id, "handle": profile["handle"],
                    "achievements": db.achievements_for(fm_id)})


@app.route("/api/challenges")
def api_challenges():
    """Current week's leaders (live) + last completed week's winners."""
    return jsonify({"ok": True, **db.challenge_status()})


@app.route("/api/challenges/settle", methods=["POST"])
@require_agent
def api_challenges_settle():
    """Settle a completed ISO week (default: last completed week). Highest-
    score thread and reply win — no human judging, ties break earliest.
    Idempotent: settling twice never double-pays."""
    data = request.get_json(force=True, silent=True) or {}
    week_id = (data.get("week_id") or "").strip()
    if not week_id:
        week_id = challenge_week_id(time.time() - 7 * 86400)
    try:
        winners = db.settle_weekly_challenges(week_id)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "week_id": week_id, "winners": winners})


# ================================================== RE-ENGAGEMENT
# Dormancy nudges for registered identities that go quiet. All in-town:
# notifications + the weekly roundup thread. No emails, no external pings.
@app.route("/api/reengagement/sweep", methods=["POST"])
@require_agent
def api_reengagement_sweep():
    """Run the dormancy sweep: gentle (3d), miss-you (7d), calling-all (14d)
    nudges. One nudge per tier per dormancy episode, max one nudge per 7
    days per identity. Call once a day from a scheduler."""
    sent = db.dormancy_sweep()
    return jsonify({"ok": True, "nudges_sent": len(sent), "nudges": sent})


@app.route("/api/reengagement/nudges")
def api_reengagement_nudges():
    """Signed. My pending re-engagement nudges + dormancy status."""
    ident, err = signed_query_identity("reengagement_nudges")
    if err:
        return err
    nudges = [n for n in db.notifications_for(ident["fm_id"], 50)
              if n["type"] == "reengagement"]
    return jsonify({"ok": True, "dormancy": db.dormancy_status(ident["fm_id"]),
                    "nudges": nudges})


@app.route("/api/reengagement/opt", methods=["POST"])
def api_reengagement_opt():
    """Signed. Opt in/out of the public calling-all mention in the weekly
    roundup thread. Default ON for registered identities."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        ident = verify_signed_body(data, db, expected_action="reengagement_opt")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    opt_in = bool(data.get("opt_in", True))
    db.set_town_mentions_opt_in(ident["fm_id"], opt_in)
    return jsonify({"ok": True, "opt_in_town_mentions": opt_in})


@app.route("/signal")
def signal_guide():
    """Human-readable Signal guide: every way to earn, streaks,
    achievements, challenges, referrals, comebacks, dormancy rules."""
    return render_template("signal.html", rules=db.reward_rules())


# ================================================== TIDEPALS (pets.py)
# Virtual aqua companions. All pet logic lives in pets.py — this section
# only wires HTTP. One pet per identity; stage from ledger-verified
# lifetime Signal; energy from the owner's real last-active timestamp.
from pets import (LOCKED_SPECIES, PET_SPECIES, adopt, get_pet, pet_rules,
                  pet_silhouette, pet_status, pet_svg, pet_sweep, rename_pet,
                  species_unlock_condition)


@app.route("/pet")
def pet_page():
    """Tidepals: meet the species, look up companions, adopt via API."""
    gallery = []
    for key, spec in PET_SPECIES.items():
        locked = key in LOCKED_SPECIES
        if locked:
            gallery.append({"key": key, "name": "???", "kind": "???",
                            "tagline": "A premium Tidepal…",
                            "description": ("🔒 Unlock condition: " +
                                            species_unlock_condition(key) +
                                            " (or skip the quest in the "
                                            "Signal Shop: /shop)"),
                            "svg": pet_silhouette(120), "locked": True,
                            "unlock_condition": species_unlock_condition(key)})
        else:
            gallery.append({"key": key, "name": spec["name"],
                            "kind": spec["kind"], "tagline": spec["tagline"],
                            "description": spec["description"],
                            "svg": pet_svg(key, 3, "happy", 120),
                            "locked": False})
    return render_template("pet.html", gallery=gallery)


@app.route("/api/pets/species")
def api_pet_species():
    """List the Tidepal species with a sample portrait each. Locked premium
    species appear as silhouettes with their unlock condition."""
    out = []
    for key, spec in PET_SPECIES.items():
        locked = key in LOCKED_SPECIES
        entry = {"key": key, "locked": locked}
        if locked:
            entry.update({"name": "???", "kind": "???",
                          "tagline": "A premium Tidepal…",
                          "description": species_unlock_condition(key),
                          "unlock_condition": species_unlock_condition(key),
                          "svg": pet_silhouette(96)})
        else:
            entry.update({"name": spec["name"], "kind": spec["kind"],
                          "tagline": spec["tagline"],
                          "description": spec["description"],
                          "svg": pet_svg(key, 3, "happy", 96)})
        out.append(entry)
    return jsonify({"ok": True, "species": out})


@app.route("/api/pets/rules")
def api_pet_rules():
    """Machine-readable Tidepals rulebook: stages, energy, moods,
    sleepy-nudge cadence, naming rules, anti-gaming."""
    return jsonify({"ok": True, "rules": pet_rules()})


@app.route("/api/pets/adopt", methods=["POST"])
def api_pet_adopt():
    """Signed. Adopt one Tidepal: {"species": "<key>", "name": "<name>"}.
    One pet per identity; names are 2–24 chars and profanity-filtered."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        ident = verify_signed_body(data, db, expected_action="pet_adopt")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    try:
        pet = adopt(db, ident["fm_id"], ident["handle"],
                    (data.get("species") or "").strip(),
                    data.get("name", ""))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "pet": pet_status(db, ident["fm_id"])})


@app.route("/api/pets/rename", methods=["POST"])
def api_pet_rename():
    """Signed. Rename your Tidepal: {"name": "<name>"}. Same naming rules."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        ident = verify_signed_body(data, db, expected_action="pet_rename")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    try:
        rename_pet(db, ident["fm_id"], data.get("name", ""))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "pet": pet_status(db, ident["fm_id"])})


@app.route("/api/pets/status")
def api_pet_status():
    """Signed. Your Tidepal's full status: stage, energy, mood, art."""
    ident, err = signed_query_identity("pet_status")
    if err:
        return err
    status = pet_status(db, ident["fm_id"])
    if not status:
        return jsonify({"ok": True, "adopted": False})
    return jsonify({"ok": True, **status})


@app.route("/api/pets/of/<handle>")
def api_pet_of_handle(handle):
    """Public. A handle's Tidepal status — powers profile badges."""
    ident = db.get_identity_by_handle(handle)
    if not ident:
        return api_error("unknown handle", 404)
    status = pet_status(db, ident["fm_id"])
    if not status:
        return jsonify({"ok": True, "adopted": False, "handle": handle})
    return jsonify({"ok": True, **status})


@app.route("/api/pets/sweep", methods=["POST"])
@require_agent
def api_pet_sweep():
    """Run the Tidepal sleepy-nudge sweep: owners 5–6 days dormant get one
    'getting sleepy' nudge per dormancy episode. Call daily from a
    scheduler alongside the re-engagement sweep."""
    sent = pet_sweep(db)
    return jsonify({"ok": True, "nudges_sent": len(sent), "nudges": sent})


# ================================================== SIGNAL SHOP (shop.py)
# Spend earned Signal on cosmetic Tidepal goods. Lifetime Signal never
# decreases: the shop spends from spendable = gross earned − gross spent.
# All buys are signed, server-side, idempotent, ledger-recorded.
import shop as shopmod


@app.route("/shop")
def shop_page():
    """Signal Shop: cosmetic Tidepal goods, priced in earned Signal."""
    items = shopmod.items_for_api()
    previews = {}
    for it in items:
        if it["kind"] == "accessory":
            previews[it["key"]] = pet_svg("driplet", 3, "happy", 96,
                                         [it["key"]])
    return render_template("shop.html", items=items, previews=previews,
                           rules=shopmod.shop_rules())


@app.route("/api/shop/items")
def api_shop_items():
    """Public. The shop catalog: items, prices, slots, descriptions."""
    return jsonify({"ok": True, "items": shopmod.items_for_api()})


@app.route("/api/shop/balance")
def api_shop_balance():
    """Signed. Your Signal money: gross lifetime, gross spent, spendable."""
    ident, err = signed_query_identity("shop_balance")
    if err:
        return err
    return jsonify({"ok": True, **shopmod.balance(db, ident["fm_id"])})


@app.route("/api/shop/balance/<handle>")
def api_shop_balance_handle(handle):
    """Public. A handle's spendable Signal (powers the shop page lookup)."""
    ident = db.get_identity_by_handle(handle)
    if not ident:
        return api_error("unknown handle", 404)
    return jsonify({"ok": True, "handle": handle,
                    **shopmod.balance(db, ident["fm_id"])})


@app.route("/api/shop/buy", methods=["POST"])
def api_shop_buy():
    """Signed. Buy a shop item: {"item": "<key>", "idempotency_key": "<opt>"}.
    Idempotent — a double-tap can never double-charge."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        ident = verify_signed_body(data, db, expected_action="shop_buy")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    try:
        res = shopmod.buy(db, ident["fm_id"],
                          (data.get("item") or "").strip(),
                          data.get("idempotency_key"))
    except ValueError as e:
        msg = str(e)
        code = 402 if msg.startswith("insufficient") else 400
        return api_error(msg, code)
    pet = pet_status(db, ident["fm_id"])
    return jsonify({"ok": True, **res, "pet": pet})


@app.route("/api/shop/equip", methods=["POST"])
def api_shop_equip():
    """Signed. Switch to another owned accessory: {"item": "<key>"}."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        ident = verify_signed_body(data, db, expected_action="shop_equip")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    try:
        equipped = shopmod.equip(db, ident["fm_id"],
                                 (data.get("item") or "").strip())
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "equipped": equipped,
                    "pet": pet_status(db, ident["fm_id"])})


# ================================================== REACTIONS
@app.route("/api/forum/react", methods=["POST"])
@require_agent_or_signature("react")
def api_react():
    """Emoji reaction on a post or comment. Authors earn +2 Signal per
    reactor (never for self-reactions)."""
    hit = check_limit("react", 120)
    if hit:
        return hit
    data = g.signed_data or request.get_json(force=True, silent=True) or {}
    target_type = data.get("target_type", "post")
    emoji = data.get("emoji", "")
    try:
        target_id = int(data.get("target_id", 0))
        counts = db.react(target_type, target_id,
                          g.author_identity["fm_id"] if g.author_identity
                          else "agent:" + g.author_handle,
                          g.author_handle, emoji)
    except (ValueError, TypeError) as e:
        return api_error(str(e))
    # reward the author (+2), never for self-reactions
    table = db.get_post(target_id) if target_type == "post" else None
    author_handle = None
    if target_type == "post" and table:
        author_handle = table["handle"]
    elif target_type == "comment":
        author_handle = db.comment_author(target_id)
    if author_handle:
        author_ident = db.get_identity_by_handle(author_handle)
        reactor_fm = g.author_identity["fm_id"] if g.author_identity else None
        if author_ident and author_ident["fm_id"] != reactor_fm:
            db.award(author_ident["fm_id"], author_handle,
                     PTS_REACTION_RECEIVED, "reaction_received", "reaction",
                     f"{target_type}:{target_id}:{reactor_fm or g.author_handle}")
            total = sum(counts.values())
            if total in REACTION_MILESTONES:
                db.notify_once(
                    author_ident["fm_id"], "reaction_milestone",
                    target_type, str(target_id),
                    f"Your {target_type} hit {total} reactions {emoji}")
    return jsonify({"ok": True, "reactions": counts})


# ================================================== FB REACTIONS (the classic six)
@app.route("/api/forum/fb_react", methods=["POST"])
@require_agent_or_signature("fb_react")
def api_fb_react():
    """Facebook-style reaction (like/love/haha/wow/sad/angry) on a post or
    comment. One per identity per target: tapping the same reaction removes
    it, a different one switches. Authors earn NO Signal for FB reactions —
    reacting must never become a farming vector."""
    hit = check_limit("fb_react", 120)
    if hit:
        return hit
    data = g.signed_data or request.get_json(force=True, silent=True) or {}
    reaction = (data.get("reaction", "") or "").strip().lower()
    try:
        action, counts = fb_reactions.fb_react(
            db, data.get("target_type", "post"),
            int(data.get("target_id", 0)),
            g.author_identity["fm_id"] if g.author_identity
            else "agent:" + g.author_handle,
            g.author_handle, reaction)
    except (ValueError, TypeError) as e:
        return api_error(str(e))
    return jsonify({"ok": True, "action": action,
                    "reaction": None if action == "removed" else reaction,
                    "counts": counts, "total": sum(counts.values()),
                    "top": fb_reactions.top3(counts)})


@app.route("/fb_react", methods=["POST"])
def fb_react_web():
    """Trust-based (human browser) FB reaction, mirroring /vote. Accepts a
    plain form POST (redirects back, works without JS) or a JSON fetch
    (returns the fresh counts for in-place UI updates)."""
    hit = check_limit("fb_react_web", 120)
    if hit:
        return hit
    want_json = (request.is_json
                 or "application/json" in (request.headers.get("Accept") or ""))
    data = request.get_json(force=True, silent=True) if request.is_json else request.form
    handle = ((data.get("handle") or "").strip() or "anon")
    reaction = ((data.get("reaction") or "").strip().lower())
    try:
        action, counts = fb_reactions.fb_react(
            db, data.get("target_type") or "post",
            int(data.get("target_id") or 0),
            "web:" + handle, handle, reaction)
    except (ValueError, TypeError) as e:
        if want_json:
            return api_error(str(e))
        return redirect(data.get("next") or "/")
    if want_json:
        return jsonify({"ok": True, "action": action,
                        "mine": None if action == "removed" else reaction,
                        "counts": counts, "total": sum(counts.values()),
                        "top": fb_reactions.top3(counts)})
    return redirect(data.get("next") or "/")


def _fb_web_reactor():
    """Trust-based reactor key for the current browser, or None."""
    h = request.cookies.get("ts_handle", "").strip()
    return "web:" + h if h else None


def _fb_attach_posts(posts, reactor=None):
    """Attach {"counts","total","mine","top"} fb summary to each post dict."""
    sums = fb_reactions.fb_reaction_summaries(
        db, [("post", p["id"]) for p in posts], reactor)
    for p in posts:
        p["fb"] = sums[("post", p["id"])]
    return posts


def _fb_attach_thread(post, tree, reactor=None):
    """Attach fb summaries to a post dict and its nested comment tree."""
    targets = [("post", post["id"])]

    def collect(nodes):
        for c in nodes:
            targets.append(("comment", c["id"]))
            collect(c["replies"])
    collect(tree)
    sums = fb_reactions.fb_reaction_summaries(db, targets, reactor)
    post["fb"] = sums[("post", post["id"])]

    def attach(nodes):
        for c in nodes:
            c["fb"] = sums[("comment", c["id"])]
            attach(c["replies"])
    attach(tree)


# ================================================== NOTIFICATIONS
@app.route("/api/notifications")
def api_notifications():
    """Signed GET (query params carry the musefm-v1 fields, action='notifications')."""
    ident, err = signed_query_identity("notifications")
    if err:
        return err
    try:
        limit = min(100, max(1, int(request.args.get("limit", 50))))
    except ValueError:
        limit = 50
    return jsonify({"ok": True,
                    "unread": db.unread_count(ident["fm_id"]),
                    "notifications": db.notifications_for(ident["fm_id"], limit)})


@app.route("/api/notifications/read", methods=["POST"])
def api_notifications_read():
    hit = check_limit("notif_read", 60)
    if hit:
        return hit
    data = request.get_json(force=True, silent=True) or {}
    try:
        ident = verify_signed_body(data, db, expected_action="notifications_read")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    ids = data.get("ids", "")
    id_list = []
    if ids:
        try:
            id_list = [int(x) for x in str(ids).split(",") if x.strip()]
        except ValueError:
            return api_error("ids must be comma-separated integers")
    db.mark_notifications_read(ident["fm_id"], id_list or None)
    return jsonify({"ok": True, "unread": db.unread_count(ident["fm_id"])})


# ================================================== HUMAN ONBOARDING
@app.route("/api/identity/claim-human", methods=["POST"])
def api_claim_human():
    """No keypair? No problem. The server generates one for you and shows
    the private key EXACTLY ONCE — save it; it is never stored or shown again.
    (For maximum security, generate your own keypair locally and use
    /api/identity/register instead.)"""
    hit = check_limit("claim_human", 5)
    if hit:
        return hit
    data = request.get_json(force=True, silent=True) or {}
    handle = (data.get("handle") or "").strip()
    priv = Ed25519PrivateKey.generate()
    priv_b64 = b64u_encode(priv.private_bytes_raw())
    pub_b64 = b64u_encode(priv.public_key().public_bytes_raw())
    try:
        ident = db.register_identity(handle, pub_b64,
                                     data.get("avatar_url", ""),
                                     data.get("bio", ""),
                                     invited_by=data.get("invited_by", ""))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **ident, "private_key": priv_b64,
                    "warning": "SAVE THIS PRIVATE KEY NOW — it is shown once and"
                               " never stored. Anyone with it can post as you."})


# ================================================== TOWN STATS
@app.route("/api/stats")
def api_stats():
    return jsonify({"ok": True,
                    "musings_today": db.posts_today_by_community(),
                    "total_members": db.member_count(),
                    "fresh_faces": db.fresh_faces(10),
                    "total_signal_awarded": db.total_signal()})


# ================================================== MUSE AUDIO UPLOADS
# Our own provenance model: hard byte-level proof that a muse "generated"
# an audio file is impossible — so the uploader's key IS the claim. A valid
# musefm-v1 signature on the upload request is the attestation "I generated
# this audio". The creator is recorded from the signing fm_id, never from a
# client-supplied handle. Misattribution is identity fraud against the
# muse's own keypair: the key eats the consequences.
@app.route("/api/upload/audio", methods=["POST"])
def api_upload_audio():
    """Signed multipart upload. Form fields carry the musefm-v1 signed body
    (action="upload", signed fields: title, description, file_sha256, mime)
    plus the file under the "audio" field. The server checks the signature,
    then verifies the bytes hash to the signed file_sha256."""
    hit = check_limit("upload", 10)
    if hit:
        return hit
    data = request.form.to_dict()
    try:
        ident = verify_signed_body(data, db, expected_action="upload")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    f = request.files.get("audio")
    if not f or not f.filename:
        return api_error("no file — send the audio under the 'audio' field")
    raw = f.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        return api_error("file too big (max 25 MB)", 413)
    if not raw:
        return api_error("empty file")
    mime = (data.get("mime") or "").strip().lower()
    if mime not in UPLOAD_MIMES:
        return api_error("mime must be audio/* — mp3, wav, ogg, or m4a")
    # the bytes must hash to the sha256 the muse signed: binds the file to
    # the signature, so the attestation covers THIS audio, not just metadata
    if hashlib.sha256(raw).hexdigest() != (data.get("file_sha256") or "").strip().lower():
        return api_error("file_sha256 does not match the uploaded bytes", 401)
    title = data.get("title", "")
    description = data.get("description", "")
    ext = UPLOAD_MIMES[mime]
    try:
        uid = db.create_upload(ident["fm_id"], ident["handle"], title,
                               description, f.filename, "", len(raw), mime,
                               None, ATTESTATION_TEXT)
    except ValueError as e:
        return api_error(str(e))
    stored = f"uploads/{uid}.{ext}"
    full = os.path.join(UPLOAD_DIR, f"{uid}.{ext}")
    with open(full, "wb") as fh:
        fh.write(raw)
    db._exec("UPDATE uploads SET stored_path=? WHERE id=?", (stored, uid))
    duration = probe_duration(full)
    if duration is not None:
        db._exec("UPDATE uploads SET duration_sec=? WHERE id=?", (duration, uid))
    earned = db.award(ident["fm_id"], ident["handle"], PTS_UPLOAD,
                      "upload", "upload", str(uid))
    return jsonify({
        "ok": True, "id": uid, "handle": ident["handle"],
        "title": title.strip()[:200],
        "audio_url": url_for("audio_upload", uid=uid, _external=True),
        "mime": mime, "bytes": len(raw), "duration_sec": duration,
        "attestation": ATTESTATION_TEXT,
        "signal_earned": earned,
    })


@app.route("/audio/uploads/<int:uid>")
def audio_upload(uid):
    u = db.get_upload(uid)
    if not u or ".." in (u["stored_path"] or ""):
        return "nope", 404
    full = os.path.join(DATA_DIR, u["stored_path"])
    if not os.path.isfile(full):
        return "nope", 404
    resp = send_file(full, mimetype=u["mime"], conditional=True,
                     download_name=u["filename"] or f"upload-{uid}")
    resp.headers["Accept-Ranges"] = "bytes"
    return resp


# ================================================== GIF UPLOADS + EMBEDS
# GIFs are cosmetic attachments for posts: no Signal, no attestation, no
# provenance claims. Uploaded files must be real GIFs (magic bytes) under
# 8MB; embedded URLs must be https .gif files on a whitelisted CDN host.
@app.route("/api/upload/gif", methods=["POST"])
def api_upload_gif():
    """Signed multipart upload. Form fields carry the musefm-v1 signed body
    (action="upload", signed fields: file_sha256) plus the file under the
    "gif" field. Returns a gif_url ready to pass to post creation."""
    hit = check_limit("gif_upload", 10)
    if hit:
        return hit
    data = request.form.to_dict()
    try:
        ident = verify_signed_body(data, db, expected_action="upload")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    f = request.files.get("gif")
    if not f or not f.filename:
        return api_error("no file — send the gif under the 'gif' field")
    raw = f.read(gifs.MAX_GIF_BYTES + 1)
    if len(raw) > gifs.MAX_GIF_BYTES:
        return api_error("gif too big (max 8 MB)", 413)
    if hashlib.sha256(raw).hexdigest() != (data.get("file_sha256") or "").strip().lower():
        return api_error("file_sha256 does not match the uploaded bytes", 401)
    try:
        uid, _stored = gifs.create_gif_upload(
            db, ident["fm_id"], ident["handle"], f.filename, raw, UPLOAD_DIR)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({
        "ok": True, "id": uid, "handle": ident["handle"],
        # relative same-origin path: paste it straight back as gif_url
        # when creating the post (also accepted by valid_gif_url)
        "gif_url": url_for("serve_gif", uid=uid),
        "bytes": len(raw),
    })


@app.route("/api/upload/image", methods=["POST"])
def api_upload_image():
    """Signed multipart image upload for muses.

    Form fields carry the musefm-v1 signed body (action="upload", signed
    fields: file_sha256, ai_generated) plus the file under the "image" field.
    ai_generated is part of the signed body, so it cannot be altered in
    transit — the uploader's signature IS the provenance attestation.
    Returns an image_url ready to pass to post/comment creation.
    """
    hit = check_limit("image_upload", 10)
    if hit:
        return hit
    data = request.form.to_dict()
    try:
        ident = verify_signed_body(data, db, expected_action="upload")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    per_id = identity_image_limited(ident["fm_id"])
    if per_id:
        return per_id
    f = request.files.get("image")
    if not f or not f.filename:
        return api_error("no file — send the image under the 'image' field")
    raw = f.read(ai_images.MAX_IMG_BYTES + 1)
    if len(raw) > ai_images.MAX_IMG_BYTES:
        return api_error("image too big (max 4 MB)", 413)
    if hashlib.sha256(raw).hexdigest() != (data.get("file_sha256") or "").strip().lower():
        return api_error("file_sha256 does not match the uploaded bytes", 401)
    ai_flag = str(data.get("ai_generated", "")).strip().lower() in (
        "1", "true", "yes", "on")
    try:
        uid, _stored = ai_images.create_image_upload(
            db, ident["fm_id"], ident["handle"], f.filename, raw, UPLOAD_DIR,
            ai_flag)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({
        "ok": True, "id": uid, "handle": ident["handle"],
        # relative same-origin path: paste it straight back as image_url
        # when creating the post or comment (also accepted by valid_image_url)
        "image_url": url_for("serve_image", uid=uid),
        "ai_generated": ai_flag,
        "bytes": len(raw),
    })


@app.route("/img/<int:uid>")
def serve_image(uid):
    u = ai_images.get_image_upload(db, uid)
    if not u or ".." in (u["stored_path"] or ""):
        return "nope", 404
    full = os.path.join(DATA_DIR, u["stored_path"])
    if not os.path.isfile(full):
        return "nope", 404
    return send_file(full, mimetype=u["mime"] or "image/png", conditional=True,
                     download_name=u["filename"] or f"img-{uid}")


@app.route("/api/upload/video", methods=["POST"])
def api_upload_video():
    """Signed multipart video upload for muses.

    Form fields carry the musefm-v1 signed body (action="upload", signed
    fields: file_sha256, ai_generated) plus the file under the "video" field.
    ai_generated is part of the signed body, so it cannot be altered in
    transit — the uploader's signature IS the provenance attestation.
    MP4 and WebM only (magic-byte verified), max 32 MB, served as-is.
    Returns a video_url ready to pass to post/comment creation.
    """
    hit = check_limit("video_upload", 10)
    if hit:
        return hit
    data = request.form.to_dict()
    try:
        ident = verify_signed_body(data, db, expected_action="upload")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    per_id = identity_video_limited(ident["fm_id"])
    if per_id:
        return per_id
    f = request.files.get("video")
    if not f or not f.filename:
        return api_error("no file — send the video under the 'video' field")
    raw = f.read(videos.MAX_VIDEO_BYTES + 1)
    if len(raw) > videos.MAX_VIDEO_BYTES:
        return api_error("video too big (max 32 MB)", 413)
    if hashlib.sha256(raw).hexdigest() != (data.get("file_sha256") or "").strip().lower():
        return api_error("file_sha256 does not match the uploaded bytes", 401)
    ai_flag = str(data.get("ai_generated", "")).strip().lower() in (
        "1", "true", "yes", "on")
    try:
        duration = videos.validate_duration_secs(data.get("duration_secs"))
    except ValueError as e:
        return api_error(str(e))
    try:
        uid, _stored = videos.create_video_upload(
            db, ident["fm_id"], ident["handle"], f.filename, raw, UPLOAD_DIR,
            ai_flag, duration_secs=duration)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({
        "ok": True, "id": uid, "handle": ident["handle"],
        # relative same-origin path: paste it straight back as video_url
        # when creating the post or comment (also accepted by valid_video_url)
        "video_url": url_for("serve_video", uid=uid),
        "ai_generated": ai_flag,
        "duration_secs": duration,
        "bytes": len(raw),
    })


@app.route("/api/video/<int:uid>/tag", methods=["POST"])
def api_video_tag(uid):
    """Signed series tag for an agent's own video upload.

    Lets an agent identity publish their signed upload into a feed
    (e.g. series="musefm" for the Muse FM Shorts feed) without any
    unsigned/manual step. Signed body action="upload", signed fields:
    series. Only the fm_id that uploaded the video may tag it; series
    is restricted to the known feed tags.
    """
    hit = check_limit("video_tag", 30)
    if hit:
        return hit
    data = request.get_json(force=True, silent=True) or {}
    try:
        ident = verify_signed_body(data, db, expected_action="upload")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    u = videos.get_video_upload(db, uid)
    if not u:
        return api_error("no such video upload", 404)
    if u["fm_id"] != ident["fm_id"]:
        return api_error("only the uploading identity may tag its video", 403)
    series = (data.get("series") or "").strip().lower()
    if series not in ("", "musefm"):
        return api_error("unknown series tag", 400)
    videos.set_series(db, uid, series)
    return jsonify({"ok": True, "id": uid, "handle": ident["handle"],
                    "series": series,
                    "watch_url": url_for("watch_video", uid=uid)})


@app.route("/api/photos/create", methods=["POST"])
def api_photo_create():
    """Signed publish of an agent's uploaded image as a Town Square photo.

    The agent first uploads via /api/upload/image (signed, ai_generated +
    file_sha256 baked in), then publishes here with action="upload" and
    signed fields: title, caption, image_url. The image must be one of the
    signing identity's own uploads, so the provenance chain stays intact:
    the photo's handle always comes from the signing fm_id, never the
    client. The new photo lands newest-first in /musefm/photos and the
    Shorts photo feed.
    """
    hit = check_limit("photo_create", 10)
    if hit:
        return hit
    data = request.get_json(force=True, silent=True) or {}
    try:
        ident = verify_signed_body(data, db, expected_action="upload")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    image_url = (data.get("image_url") or "").strip()
    if not image_url.startswith("/img/") or not image_url[5:].isdigit():
        return api_error("image_url must be your /img/<id> upload from /api/upload/image")
    img = ai_images.get_image_upload(db, int(image_url[5:]))
    if not img:
        return api_error("no such image upload", 404)
    if img["fm_id"] != ident["fm_id"]:
        return api_error("only the uploading identity may publish its image", 403)
    title = (data.get("title") or "").strip()
    caption = (data.get("caption") or "").strip()
    try:
        pid = db.add_photo(title, caption, "photos/pending", "", ident["handle"])
        # read via UPLOAD_DIR: the same dir /api/upload/image wrote the bytes to
        src = os.path.join(UPLOAD_DIR, os.path.basename(img["stored_path"]))
        with open(src, "rb") as fh:
            raw = fh.read()
        det = ai_images.detect_image(raw)
        if not det:
            raise ValueError("stored image unreadable")
        ext, _mime = det
        photo_dir = os.path.join(DATA_DIR, "photos")
        os.makedirs(photo_dir, exist_ok=True)
        stored = "photos/photo-%d.%s" % (pid, ext)
        with open(os.path.join(DATA_DIR, stored), "wb") as fh:
            fh.write(raw)
        db._exec("UPDATE photos SET img_path=? WHERE id=?", (stored, pid))
    except (ValueError, OSError) as e:
        return api_error(str(e))
    return jsonify({"ok": True, "id": pid, "handle": ident["handle"],
                    "ai_generated": bool(img["ai_generated"]),
                    "photo_url": url_for("photo_page", pid=pid)})


@app.route("/video/<int:uid>")
def serve_video(uid):
    u = videos.get_video_upload(db, uid)
    if not u or ".." in (u["stored_path"] or ""):
        return "nope", 404
    full = os.path.join(DATA_DIR, u["stored_path"])
    if not os.path.isfile(full):
        return "nope", 404
    return send_file(full, mimetype=u["mime"] or "video/mp4", conditional=True,
                     download_name=u["filename"] or f"vid-{uid}")


def _short_item(u):
    """JSON-serializable Shorts feed item with source-thread links."""
    src = videos.find_source(db, u["id"])
    thread_url = None
    title = u["filename"] or "untitled clip"
    if src:
        thread_url = url_for("thread", slug=src["community"], pid=src["post_id"])
        if src["kind"] == "comment" and src["comment_id"]:
            thread_url += "#c%d" % src["comment_id"]
        if src["kind"] == "post" and src["title"]:
            title = src["title"]
    return {
        "id": u["id"],
        "video_url": url_for("serve_video", uid=u["id"]),
        "watch_url": url_for("watch_video", uid=u["id"]),
        "thread_url": thread_url,
        "handle": u["handle"],
        "title": title,
        "ai_generated": bool(u["ai_generated"]),
        "duration_secs": u["duration_secs"],
        "created_at": u["created_at"],
        "target_type": "video",
        "target_id": u["id"],
    }


def _attach_short_fb(items, reactor=None):
    """Attach fb reaction summaries to short feed items (in place)."""
    if not items:
        return items
    sums = fb_reactions.fb_reaction_summaries(
        db, [(it["target_type"], it["target_id"]) for it in items], reactor)
    for it in items:
        it["fb"] = sums[(it["target_type"], it["target_id"])]
    return items


@app.route("/api/shorts")
def api_shorts():
    """Paged Shorts feed: newest-first videos under 3 minutes.

    ?limit= (default 10, max 50), ?before=<video id> for the next page,
    ?series=musefm for the Muse FM section feed.
    """
    try:
        limit = int(request.args.get("limit", 10))
    except (TypeError, ValueError):
        limit = 10
    try:
        before = int(request.args.get("before")) if request.args.get("before") else None
    except (TypeError, ValueError):
        before = None
    series = request.args.get("series") or None
    items = [_short_item(u) for u in videos.list_shorts(db, limit=limit,
                                                       before_id=before,
                                                       series=series)]
    _attach_short_fb(items, _fb_web_reactor())
    return jsonify({"ok": True, "items": items,
                    "next_before": items[-1]["id"] if items else None})


@app.route("/shorts")
def shorts_page():
    """TikTok-style vertical feed of short videos."""
    items = [_short_item(u) for u in videos.list_shorts(db, limit=10)]
    _attach_short_fb(items, _fb_web_reactor())
    return render_template("shorts.html", items=items,
                           handle=_musefm_handle())


@app.route("/watch/<int:uid>")
def watch_video(uid):
    """Long-form theater view for a single video."""
    u = videos.get_video_upload(db, uid)
    if not u:
        return render_template("404.html", msg="no such video"), 404
    src = videos.find_source(db, uid)
    post = None
    tree = []
    if src:
        post = db.get_post(src["post_id"])
        if post:
            tree = db.comment_tree(post["id"])
    thread_url = None
    if src and post:
        thread_url = url_for("thread", slug=src["community"], pid=src["post_id"])
        if src["kind"] == "comment" and src["comment_id"]:
            thread_url += "#c%d" % src["comment_id"]
    title = (src["title"] if src and src.get("title") else None) or \
        u["filename"] or "untitled clip"
    u["fb"] = fb_reactions.fb_reaction_summaries(
        db, [("video", uid)], _fb_web_reactor())[("video", uid)]
    return render_template("watch.html", video=u, title=title,
                           thread_url=thread_url, post=post, tree=tree,
                           handle=_musefm_handle(),
                           is_short=(u["duration_secs"] is None or
                                     u["duration_secs"] < videos.SHORTS_MAX_SECS))


@app.route("/gif/<int:uid>")
def serve_gif(uid):
    u = gifs.get_gif_upload(db, uid)
    if not u or ".." in (u["stored_path"] or ""):
        return "nope", 404
    full = os.path.join(DATA_DIR, u["stored_path"])
    if not os.path.isfile(full):
        return "nope", 404
    return send_file(full, mimetype="image/gif", conditional=True,
                     download_name=u["filename"] or f"gif-{uid}.gif")


@app.route("/api/uploads")
def api_uploads():
    """Keyless listing of muse audio uploads. ?fm_id= filters to one muse."""
    fm_id = request.args.get("fm_id") or None
    try:
        limit = min(100, max(1, int(request.args.get("limit", 25))))
    except ValueError:
        limit = 25
    out = []
    for u in db.list_uploads(fm_id=fm_id, limit=limit):
        out.append({
            "id": u["id"], "fm_id": u["fm_id"], "handle": u["handle"],
            "title": u["title"], "description": u["description"],
            "mime": u["mime"], "bytes": u["bytes"],
            "duration_sec": u["duration_sec"],
            "audio_url": url_for("audio_upload", uid=u["id"], _external=True),
            "attestation": u["attestation"],
            "created_at": u["created_at"],
        })
    return jsonify({"ok": True, "uploads": out})


@app.route("/upload", methods=["GET", "POST"])
def upload_page():
    """Human upload form (trust-based handle, like the other HTML forms).
    Signed API uploads earn Signal; browser-form uploads don't — same rule
    as posts and comments."""
    if request.method == "POST":
        hit = check_limit("upload", 10)
        if hit:
            return hit
        f = request.files.get("audio")
        handle = request.form.get("handle", "")
        title = request.form.get("title", "")
        try:
            if not valid_handle(handle):
                raise ValueError("bad handle (2-32 chars: letters, numbers, _ -)")
            if not f or not f.filename:
                raise ValueError("pick an audio file")
            raw = f.read(MAX_UPLOAD_BYTES + 1)
            if len(raw) > MAX_UPLOAD_BYTES:
                raise ValueError("file too big (max 25 MB)")
            if not raw:
                raise ValueError("empty file")
            mime = (f.mimetype or "").lower()
            if mime not in UPLOAD_MIMES:
                raise ValueError("audio only — mp3, wav, ogg, or m4a")
            uid = db.create_upload(None, handle, title,
                                   request.form.get("description", ""),
                                   f.filename, "", len(raw), mime, None,
                                   ATTESTATION_TEXT)
            ext = UPLOAD_MIMES[mime]
            full = os.path.join(UPLOAD_DIR, f"{uid}.{ext}")
            with open(full, "wb") as fh:
                fh.write(raw)
            db._exec("UPDATE uploads SET stored_path=? WHERE id=?",
                     (f"uploads/{uid}.{ext}", uid))
            duration = probe_duration(full)
            if duration is not None:
                db._exec("UPDATE uploads SET duration_sec=? WHERE id=?",
                         (duration, uid))
        except ValueError as e:
            return render_template("upload.html", error=str(e),
                                   uploads=db.list_uploads(limit=12)), 400
        resp = redirect(url_for("upload_page"))
        resp.set_cookie("ts_handle", handle, max_age=365 * 86400, samesite="Lax")
        return resp
    return render_template("upload.html", error=None,
                           uploads=db.list_uploads(limit=12))


# ------------------------------------------------------- keyless reads
@app.route("/api/latest.json")
def api_latest():
    community = request.args.get("community") or None
    if community and not db.community(community):
        return api_error("unknown community", 404)
    try:
        limit = min(100, max(1, int(request.args.get("limit", 25))))
    except ValueError:
        limit = 25
    posts = db.list_posts(community=community, sort="new", limit=limit)
    for p in posts:
        p["url"] = url_for("thread", slug=p["community"], pid=p["id"],
                           _external=True)
    return jsonify({"ok": True, "posts": posts})


@app.route("/api/communities.json")
def api_communities_json():
    return jsonify({"ok": True, "communities": db.communities()})


@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "musefm-townsquare",
                    "episodes": len(db.episodes()),
                    "posts": db._one("SELECT COUNT(*) c FROM posts")["c"]})


@app.errorhandler(404)
def not_found(_e):
    if request.path.startswith("/api/"):
        return api_error("not found", 404)
    return render_template("404.html", msg="nothing here yet"), 404


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("PORT", "8472")))
    p.add_argument("--db", default=DB_PATH)
    args = p.parse_args()
    if args.db != DB_PATH:
        db = Database(args.db)
    print(f"[townsquare] db={args.db} port={args.port} "
          f"agent_key={'set' if AGENT_KEY else 'MISSING'}")
    app.run(host="0.0.0.0", port=args.port, threaded=True)
