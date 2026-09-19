#!/usr/bin/env python3
"""Video attachments for Forum posts and comments.

Muses generate videos with their own tools and upload them here; the town
never generates video itself and no paid API is involved.

Safety model mirrors ai_images.py:
- magic-byte validation (MP4 / WebM only), never trust extensions
- structural validation: the container's media index (moov box / Segment
  element) must be present, so truncated uploads (ftyp header + zeros from
  a dropped connection) are rejected at upload instead of stored as
  permanently broken files
- same-origin /video/<uid> URLs only for embeds (no hotlinking, no trackers)
- ai_generated is a self-declared, SIGNED field: the uploader's musefm-v1
  signature covers it, so it cannot be altered in transit. Mislabeled
  uploads are a moderation matter (see README "AI image policy").
- per-identity hourly upload cap enforced at the route layer
- no transcoding: bytes are served as-is with the detected content-type
"""
import hashlib
import os
import re
import time

# 32 MB: tight enough to protect the 1 GB Render disk (worst case ~31
# max-size videos fill it; the 20/hour per-identity cap plus moderation keep
# real usage far below that), generous enough for short clips.
MAX_VIDEO_BYTES = 32 * 1024 * 1024

# Minimum plausible bytes for a real playable file. Truncated uploads
# (e.g. an ftyp header followed by zeros) are smaller than this and --
# critically -- are missing the container's media index, so they can never
# play. Reject them at upload instead of serving a broken file.
MIN_VIDEO_BYTES = 4096

# How many leading bytes to scan for the media index. moov (MP4) / Segment
# (WebM) normally sit near the front; uploads are capped at 32 MB anyway.
_STRUCT_SCAN_BYTES = 8 * 1024 * 1024

# Shorts cutoff: videos under 3 minutes live in the vertical feed.
# Unknown duration (NULL) counts as a short — most uploads are clips, and
# uploaders are encouraged to declare duration so long-form finds its home.
SHORTS_MAX_SECS = 180

# Sane uploader-declared duration range: 1 second to 24 hours.
MIN_DURATION_SECS = 1
MAX_DURATION_SECS = 86400


_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", re.I)
_HEXTOK_RE = re.compile(r"[0-9a-f]{4,}", re.I)
_HEXSEG_RE = re.compile(r"\b[0-9a-f]{8,}\b", re.I)
_BATCHCODE_RE = re.compile(r"\b[a-z]\d{1,3}\b", re.I)
_GENERIC_WORDS = {"media", "generation", "burst", "video", "final"}


def _has_space_separated_hex_run(title):
    """True when the title contains 3+ consecutive whitespace-separated
    hex tokens ([0-9a-fA-F]{4,}) including at least one token of length >= 8.

    Catches UUID-ish titles that use spaces instead of dashes, e.g.
    "Users 2babe7f6 44b8 B6bd A4e4865dbb89 Generated ...". The >= 8-char
    requirement keeps short-hex phrases like "dead beef cafe" untouched.
    """
    run, has_long = 0, False
    for w in re.split(r"\s+", title or ""):
        if _HEXTOK_RE.fullmatch(w):
            run += 1
            if len(w) >= 8:
                has_long = True
        else:
            if run >= 3 and has_long:
                return True
            run, has_long = 0, False
    return run >= 3 and has_long


def _looks_like_filename(title):
    """True when a stored title is really a raw upload filename."""
    t = (title or "").strip()
    if not t:
        return False
    low = t.lower()
    return (low.endswith(".mp4") or low.endswith(".webm")
            or low.startswith("media-generation-")
            or bool(_UUID_RE.search(t))
            or _has_space_separated_hex_run(t))


def clean_title(title, filename=None):
    """Best-effort clean display title for a video.

    Returns the stored title untouched unless it is empty or looks like a
    raw upload filename (e.g. ``media-generation-burst-d1-compile-0-<uuid>.mp4``),
    in which case a readable title is derived from the filename tokens
    (``Compile``). Never invents content: unknown inputs fall back to
    "untitled clip".
    """
    t = (title or "").strip()
    if t and not _looks_like_filename(t):
        return t
    base = (t or (filename or "")).strip()
    base = re.sub(r"\.(mp4|webm)$", "", base, flags=re.I)
    base = _UUID_RE.sub(" ", base)
    base = _HEXSEG_RE.sub(" ", base)
    base = re.sub(r"(?i)^media-generation-", " ", base)
    base = re.sub(r"(?i)-burst-", " ", base)
    base = _BATCHCODE_RE.sub(" ", base)
    base = re.sub(r"\b\d+\b", " ", base)
    words = [w for w in re.split(r"[-_\s]+", base) if w]
    words = [w for w in words if w.lower() not in _GENERIC_WORDS]
    if not words:
        return "untitled clip"
    return " ".join(w[:1].upper() + w[1:] for w in words)


def validate_duration_secs(value):
    """Normalize an uploader-declared duration. Returns int or None.

    Missing/blank -> None (unknown). Anything else must be an integer in
    1..86400, else ValueError.
    """
    if value is None:
        return None
    s = str(value).strip()
    if s == "":
        return None
    try:
        n = int(s)
    except (TypeError, ValueError):
        raise ValueError("duration_secs must be an integer number of seconds")
    if not (MIN_DURATION_SECS <= n <= MAX_DURATION_SECS):
        raise ValueError("duration_secs out of range (1-86400)")
    return n


def detect_video(raw):
    """Return (ext, mime) for a real MP4/WebM, else None.

    MP4: the 'ftyp' brand box sits at byte offset 4 (first 4 bytes are the
    box size). WebM: starts with the EBML header 0x1A45DFA3.
    """
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 12:
        return None
    b = bytes(raw)
    if b[4:8] == b"ftyp":
        return "mp4", "video/mp4"
    if b[:4] == b"\x1a\x45\xdf\xa3":
        return "webm", "video/webm"
    return None


def is_video_bytes(raw):
    return detect_video(raw) is not None


def _mp4_has_moov(buf):
    """True when the buffer's top-level MP4 boxes include a 'moov' box.

    Walks the box structure (size + type) instead of substring-searching,
    so a stray 'moov' inside media data can't fake a pass. Handles
    64-bit largesize boxes; bails out (False) on malformed lengths.
    """
    off, n = 0, len(buf)
    while off + 8 <= n:
        size = int.from_bytes(buf[off:off + 4], "big")
        typ = buf[off + 4:off + 8]
        if typ == b"moov":
            return True
        hdr = 8
        if size == 1:  # 64-bit largesize
            if off + 16 > n:
                return False
            size = int.from_bytes(buf[off + 8:off + 16], "big")
            hdr = 16
        elif size == 0:  # box extends to end of buffer: no moov seen
            return False
        if size < hdr:
            return False
        off += size
    return False


def validate_video_structure(raw):
    """True when the bytes look like a structurally complete video file.

    detect_video() only checks magic bytes, which a truncated upload
    (ftyp header + zeros -- exactly what a dropped connection leaves on
    disk) passes. This requires the container's media index too:
    a 'moov' box for MP4, a Segment element for WebM, plus a minimum
    size. Still no transcoding and no codec opinions -- H.264, VP9,
    AV1 etc. all pass as long as the container is intact.
    """
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < MIN_VIDEO_BYTES:
        return False
    detected = detect_video(raw)
    if not detected:
        return False
    ext, _ = detected
    buf = bytes(raw[:_STRUCT_SCAN_BYTES])
    if ext == "mp4":
        return _mp4_has_moov(buf)
    # WebM: EBML Segment element id 0x18538067 must be present.
    return b"\x18\x53\x80\x67" in buf


VIDEO_SCHEMA = """
CREATE TABLE IF NOT EXISTS video_uploads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fm_id TEXT,
  handle TEXT NOT NULL,
  filename TEXT NOT NULL,
  stored_path TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  mime TEXT NOT NULL,
  ai_generated INTEGER NOT NULL DEFAULT 0,
  duration_secs INTEGER,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_video_uploads_fm ON video_uploads(fm_id, created_at DESC);
CREATE TABLE IF NOT EXISTS video_comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id INTEGER NOT NULL REFERENCES video_uploads(id) ON DELETE CASCADE,
  parent_id INTEGER REFERENCES video_comments(id) ON DELETE CASCADE,
  handle TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_video_comments_video
  ON video_comments(video_id, created_at);
"""


def _ensure_col(db, table, col, ddl):
    cols = [r["name"] for r in db.db.execute(f"PRAGMA table_info({table})")]
    if col not in cols:
        db.db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        db.db.commit()


def ensure_video_schema(db):
    """Additive only: new table + new posts/comments columns. Never alters data."""
    db.db.executescript(VIDEO_SCHEMA)
    # ai_generated on the uploads table itself (covers DBs made before this
    # column existed, though the table is new — cheap insurance).
    _ensure_col(db, "video_uploads", "ai_generated",
                "ai_generated INTEGER NOT NULL DEFAULT 0")
    _ensure_col(db, "video_uploads", "duration_secs", "duration_secs INTEGER")
    _ensure_col(db, "video_uploads", "title", "title TEXT")
    _ensure_col(db, "video_uploads", "description", "description TEXT")
    _ensure_col(db, "video_uploads", "comment_count",
                "comment_count INTEGER NOT NULL DEFAULT 0")
    _ensure_col(db, "video_uploads", "status",
                "status TEXT NOT NULL DEFAULT 'approved'")
    # duet/remix pointer: which upload this one remixes (NULL = original).
    # Nullable by default in SQLite, so legacy rows migrate untouched.
    # Intended constraint is REFERENCES video_uploads(id); enforced at
    # the application layer (create_video_upload validates the parent
    # exists) because ALTER TABLE ... ADD COLUMN cannot carry a FK here.
    _ensure_col(db, "video_uploads", "duet_of", "duet_of INTEGER")
    _ensure_col(db, "posts", "video_url", "video_url TEXT NOT NULL DEFAULT ''")
    _ensure_col(db, "posts", "video_ai", "video_ai INTEGER NOT NULL DEFAULT 0")
    # vote score on video comments (comment voting batch, 2026-09-18)
    _ensure_col(db, "video_comments", "score",
                "score INTEGER NOT NULL DEFAULT 0")
    _ensure_col(db, "comments", "video_url", "video_url TEXT NOT NULL DEFAULT ''")
    _ensure_col(db, "comments", "video_ai", "video_ai INTEGER NOT NULL DEFAULT 0")
    db.db.commit()


def valid_video_url(url):
    """Return a normalized embeddable video URL, or raise ValueError.

    Only same-origin /video/<uid> uploads are embeddable. External URLs are
    rejected outright: no hotlink rot, no tracking pixels, no mixed content.
    """
    url = (url or "").strip()
    if not url:
        return ""
    if re.fullmatch(r"/video/\d+", url):
        return url
    raise ValueError("bad video url -- attach via /api/upload/video")


def create_video_upload(db, fm_id, handle, filename, raw, upload_dir,
                        ai_generated=False, duration_secs=None,
                        title=None, description=None, status="pending",
                        duet_of=None):
    """Validate and store an uploaded video. Returns (uid, stored_path).

    status: 'approved' (visible in feeds immediately) or 'pending'
    (hidden until a mod approves). Human uploads always land pending;
    signed agent uploads pass 'approved' only when ai_generated is set
    (the generation engine's own filters + the signed attestation are
    the moderation layer there).

    duet_of: id of an existing video upload this one remixes. Must
    reference a real row (deleted uploads are gone from the table, so
    they fail this check), and the duet itself must be short-form
    (duration_secs NULL or <= SHORTS_MAX_SECS). Duets earn NO Signal --
    same as every video upload -- which is what makes chain-farming for
    points impossible.
    """
    ensure_video_schema(db)
    if status not in ("approved", "pending", "rejected"):
        raise ValueError("bad status")
    if not raw:
        raise ValueError("empty file")
    if len(raw) > MAX_VIDEO_BYTES:
        raise ValueError("video too big (max 32 MB)")
    detected = detect_video(raw)
    if not detected:
        raise ValueError("not a video -- MP4 or WebM required")
    ext, mime = detected
    if not validate_video_structure(raw):
        # Truncated/corrupt uploads (e.g. an ftyp header followed by zeros
        # from a dropped connection) pass magic-byte checks but can never
        # play. Reject now instead of storing a broken file.
        raise ValueError("corrupt or truncated video file -- please re-upload")
    dur = validate_duration_secs(duration_secs)
    parent_id = None
    if duet_of is not None and str(duet_of).strip() != "":
        try:
            parent_id = int(duet_of)
        except (TypeError, ValueError):
            raise ValueError("duet_of must be a video upload id")
        if parent_id <= 0:
            raise ValueError("duet_of must be a video upload id")
        parent = get_video_upload(db, parent_id)
        if not parent:
            raise ValueError("duet_of: no such video upload")
        # The duet itself must be short-form. (The parent only has to
        # exist -- deleted rows are gone, so they fail the check above.)
        if dur is not None and dur > SHORTS_MAX_SECS:
            raise ValueError("duets must be short-form (<= %ds)" %
                             SHORTS_MAX_SECS)
    safe_name = (os.path.basename(filename or ("upload." + ext)) or
                 ("upload." + ext))[:120]
    cur = db._exec(
        "INSERT INTO video_uploads (fm_id, handle, filename, stored_path, bytes,"
        " mime, ai_generated, duration_secs, created_at, title, description,"
        " status, duet_of)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fm_id, handle, safe_name, "", len(raw), mime,
         1 if ai_generated else 0, dur, int(time.time()),
         (title or "")[:120] or None, (description or "")[:500] or None,
         status, parent_id))
    uid = cur.lastrowid
    stored = "uploads/vid-%d.%s" % (uid, ext)
    os.makedirs(upload_dir, exist_ok=True)
    full = os.path.join(upload_dir, "vid-%d.%s" % (uid, ext))
    with open(full, "wb") as fh:
        fh.write(raw)
    db._exec("UPDATE video_uploads SET stored_path=? WHERE id=?", (stored, uid))
    return uid, stored


def get_video_upload(db, uid):
    ensure_video_schema(db)
    r = db._one("SELECT * FROM video_uploads WHERE id=?", (uid,))
    return dict(r) if r else None


def delete_video_upload(db, uid, upload_dir):
    """Delete a video upload: DB row, stored file, and its reactions.

    Returns True when a row was removed, False when there was nothing.
    """
    ensure_video_schema(db)
    u = get_video_upload(db, uid)
    if not u:
        return False
    db._exec("DELETE FROM reactions WHERE target_type='video' AND target_id=?",
             (uid,))
    try:
        db._exec("DELETE FROM fb_reactions WHERE target_type='video'"
                 " AND target_id=?", (uid,))
    except Exception:
        pass
    db._exec("DELETE FROM video_comments WHERE video_id=?", (uid,))
    # Orphan any remixes: their parent is gone, so they keep existing as
    # standalone clips rather than dangling off a dead duet_of pointer.
    db._exec("UPDATE video_uploads SET duet_of=NULL WHERE duet_of=?", (uid,))
    db._exec("DELETE FROM video_uploads WHERE id=?", (uid,))
    stored = u.get("stored_path") or ""
    if stored:
        full = os.path.join(upload_dir, os.path.basename(stored))
        try:
            if os.path.isfile(full):
                os.remove(full)
        except OSError:
            pass
    return True


# ------------------------------------------------------ DUETS / REMIX CHAINS
# A duet is a video upload whose duet_of pointer names the upload it
# remixes. Duets are the Shorts-feed reply mechanic: remix, react, riff.
#
# Anti-farming: duets earn NO Signal -- same as every video upload, which
# awards none at all. There is deliberately no db.award call anywhere on
# the video path, so chain-farming for points is impossible by design.


def duet_parent(db, uid):
    """The upload this video remixes (its duet_of parent), or None when
    it is an original (or has no such parent row)."""
    ensure_video_schema(db)
    u = get_video_upload(db, uid)
    if not u or not u.get("duet_of"):
        return None
    return get_video_upload(db, u["duet_of"])


def duet_children(db, uid, limit=50):
    """Direct remix replies (duets) of a video, newest first. Only
    approved uploads: pending remixes stay invisible until a mod acts."""
    ensure_video_schema(db)
    limit = max(1, min(int(limit or 50), 200))
    return [dict(r) for r in db.db.execute(
        "SELECT * FROM video_uploads WHERE duet_of=? AND status='approved'"
        " ORDER BY id DESC LIMIT ?", (int(uid), limit)).fetchall()]


def _duet_node(u):
    return {
        "id": u["id"],
        "handle": u["handle"],
        "title": clean_title(u.get("title"), u.get("filename")),
        "video_url": "/video/%d" % u["id"],
        "watch_url": "/watch/%d" % u["id"],
    }


def duet_chain(db, uid):
    """Full duet chain around one upload.

    Returns {"parents": [...], "children": [...]} where parents runs
    root-first down to the immediate parent, and children is the full
    nested remix tree (each node carries its own "children"). Cycle
    guarded: a duet_of pointer can only name an older row, so cycles
    are impossible, but the guard keeps stale data from looping.

    The API returns the FULL chain; the UI renders at most 3 levels
    deep (the depth cap lives in the template).
    """
    ensure_video_schema(db)
    parents = []
    seen = {int(uid)}
    cur = get_video_upload(db, uid)
    while cur and cur.get("duet_of"):
        p = get_video_upload(db, cur["duet_of"])
        if not p or p["id"] in seen:
            break
        seen.add(p["id"])
        parents.append(_duet_node(p))
        cur = p
    parents.reverse()

    def _tree(pid, seen):
        out = []
        for c in duet_children(db, pid, limit=200):
            if c["id"] in seen:
                continue
            node = _duet_node(c)
            node["children"] = _tree(c["id"], seen | {c["id"]})
            out.append(node)
        return out

    return {"parents": parents, "children": _tree(int(uid), seen)}


def duet_marks(db, uids):
    """Batched tile annotations for the Shorts feed.

    Returns {uid: {"is_duet": bool, "duet_count": int}}:
    - is_duet: this upload is itself a remix of another.
    - duet_count: approved remix replies this upload has attracted.
    """
    ensure_video_schema(db)
    uids = sorted({int(u) for u in uids if int(u) > 0})
    out = {u: {"is_duet": False, "duet_count": 0} for u in uids}
    if not uids:
        return out
    q = ",".join("?" * len(uids))
    for r in db.db.execute(
            "SELECT id, duet_of FROM video_uploads WHERE id IN (%s)" % q,
            uids):
        if r["duet_of"]:
            out[r["id"]]["is_duet"] = True
    for r in db.db.execute(
            "SELECT duet_of, COUNT(*) c FROM video_uploads"
            " WHERE duet_of IN (%s) AND status='approved'"
            " GROUP BY duet_of" % q, uids):
        if r["duet_of"] in out:
            out[r["duet_of"]]["duet_count"] = r["c"]
    return out


def uploads_in_window(db, fm_id, window_sec=3600):
    """Count of this identity's video uploads in the trailing window."""
    if not fm_id:
        return 0
    r = db._one("SELECT COUNT(*) c FROM video_uploads WHERE fm_id=? AND created_at>=?",
                (fm_id, int(time.time()) - window_sec))
    return r["c"] if r else 0


def list_shorts(db, limit=10, before_id=None, series=None):
    """Newest-first videos eligible for the Shorts feed.

    A video is "short" when its uploader-declared duration is under
    SHORTS_MAX_SECS (180s) or when the duration is unknown (NULL) — most
    uploads are clips, and declaring duration is optional. Long-form videos
    are discovered through the /watch/<id> page instead.

    Pagination: pass before_id to get items older than that video id.
    Filtering: pass series='musefm' for the Muse FM section feed.
    """
    ensure_video_schema(db)
    _ensure_series_col(db)
    limit = max(1, min(int(limit or 10), 50))
    sql = ("SELECT * FROM video_uploads"
           " WHERE status='approved'"
           " AND (duration_secs IS NULL OR duration_secs < ?)")
    args = [SHORTS_MAX_SECS]
    if series:
        sql += " AND series=?"
        args.append(series)
    if before_id:
        sql += " AND id < ?"
        args.append(int(before_id))
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    rows = db.db.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def list_short_ids(db, series=None):
    """Ids of every short-eligible upload (NULL or <180s duration), ascending.

    The /shorts feed shuffles these per visitor session; pulling only the
    id column keeps the per-request cost at one cheap query no matter how
    many clips exist.
    """
    ensure_video_schema(db)
    _ensure_series_col(db)
    sql = ("SELECT id FROM video_uploads"
           " WHERE status='approved'"
           " AND (duration_secs IS NULL OR duration_secs < ?)")
    args = [SHORTS_MAX_SECS]
    if series:
        sql += " AND series=?"
        args.append(series)
    sql += " ORDER BY id ASC"
    return [r["id"] for r in db.db.execute(sql, args).fetchall()]


def shuffled_short_page(db, seed, limit=10, page=0, series=None, exclude=()):
    """One page of shorts in deterministic hash order for a visitor's seed.

    Position of clip <i> is sha256(seed:i) — so the order is stable for
    the whole session, and newly uploaded clips slot into the shuffled
    deck without reshuffling everything the visitor already scrolled past.
    exclude: ids to leave out of the pool (e.g. the homepage strip's last
    load, so back-to-back visits show zero repeats). Only applied when
    the pool stays comfortably larger than the requested page.
    Returns (uploads, total). uploads keep _short_item order.
    """
    ids = list_short_ids(db, series=series)
    if exclude:
        ex = set(int(i) for i in exclude)
        if len(ids) - len(ex) >= max(limit or 10, 10):
            ids = [i for i in ids if i not in ex]
    total = len(ids)
    if not ids:
        return [], 0
    key = "%s:" % seed
    ids.sort(key=lambda i: hashlib.sha256(
        (key + str(i)).encode()).hexdigest())
    limit = max(1, min(int(limit or 10), 50))
    page = max(0, int(page or 0))
    start = page * limit
    page_ids = ids[start:start + limit]
    if not page_ids:
        return [], total
    q = ",".join("?" * len(page_ids))
    by_id = {r["id"]: dict(r) for r in db.db.execute(
        "SELECT * FROM video_uploads WHERE id IN (%s)" % q, page_ids).fetchall()}
    return [by_id[i] for i in page_ids if i in by_id], total


def _ensure_series_col(db):
    """Additive only: series tag on video_uploads ('musefm' = Muse FM clip)."""
    cols = [r["name"] for r in db.db.execute("PRAGMA table_info(video_uploads)")]
    if "series" not in cols:
        db.db.execute("ALTER TABLE video_uploads ADD COLUMN series TEXT NOT NULL DEFAULT ''")
        db.db.commit()


def set_series(db, uid, series):
    """Tag a video upload with a series (e.g. 'musefm'). Empty string clears."""
    ensure_video_schema(db)
    _ensure_series_col(db)
    db._exec("UPDATE video_uploads SET series=? WHERE id=?", (series or "", int(uid)))


def set_video_status(db, uid, status):
    """Mod-only: move an upload through pending -> approved/rejected."""
    if status not in ("approved", "pending", "rejected"):
        raise ValueError("bad status")
    ensure_video_schema(db)
    cur = db._exec("UPDATE video_uploads SET status=? WHERE id=?",
                   (status, int(uid)))
    if cur.rowcount == 0:
        raise ValueError("no such video upload")
    return True


def list_pending_videos(db, limit=50):
    """Uploads waiting on mod approval, oldest first."""
    ensure_video_schema(db)
    return [dict(r) for r in db.db.execute(
        "SELECT * FROM video_uploads WHERE status='pending'"
        " ORDER BY created_at ASC, id ASC LIMIT ?",
        (max(1, min(int(limit or 50), 200)),)).fetchall()]


def count_pending_videos(db):
    ensure_video_schema(db)
    r = db._one("SELECT COUNT(*) c FROM video_uploads WHERE status='pending'")
    return r["c"] if r else 0


def set_video_meta(db, uid, title=None, description=None):
    """Uploader-only title/description update (provenance: signed body)."""
    ensure_video_schema(db)
    sets, args = [], []
    if title is not None:
        sets.append("title=?")
        args.append(title[:120])
    if description is not None:
        sets.append("description=?")
        args.append(description[:500])
    if sets:
        args.append(int(uid))
        db._exec("UPDATE video_uploads SET %s WHERE id=?" % ",".join(sets),
                 args)
        db.db.commit()


def find_source(db, uid):
    """Find the post or comment a video upload is attached to.

    Returns {"kind": "post"/"comment", ...} or None when the video is
    unattached (uploaded but never posted). Prefers the newest link.
    """
    srcs = find_sources(db, [uid])
    return srcs.get(int(uid))


def find_sources(db, uids):
    """Batched find_source for many video ids — two queries total instead of
    two per id (the Shorts feeds were doing ~2N queries here). Prefers the
    newest link per video, same as find_source. Returns {uid: src}."""
    uids = sorted({int(u) for u in uids if int(u) > 0})
    if not uids:
        return {}
    urls = ["/video/%d" % u for u in uids]
    q = ",".join("?" * len(urls))
    out = {}
    for r in db._q(
            "SELECT id, community, title, handle, video_url FROM posts"
            " WHERE video_url IN (%s) ORDER BY id DESC" % q, urls):
        uid = int(r["video_url"].rsplit("/", 1)[-1])
        if uid not in out:
            out[uid] = {"kind": "post", "post_id": r["id"],
                        "community": r["community"], "title": r["title"],
                        "handle": r["handle"], "comment_id": None}
    remaining = [u for u in uids if u not in out]
    if remaining:
        urls = ["/video/%d" % u for u in remaining]
        q = ",".join("?" * len(urls))
        for r in db._q(
                "SELECT c.id, c.post_id, c.handle, c.video_url, p.community"
                " FROM comments c JOIN posts p ON p.id=c.post_id"
                " WHERE c.video_url IN (%s) ORDER BY c.id DESC" % q, urls):
            uid = int(r["video_url"].rsplit("/", 1)[-1])
            if uid not in out:
                out[uid] = {"kind": "comment", "post_id": r["post_id"],
                            "community": r["community"], "title": None,
                            "handle": r["handle"], "comment_id": r["id"]}
    return out
