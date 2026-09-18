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
_HEXSEG_RE = re.compile(r"\b[0-9a-f]{8,}\b", re.I)
_BATCHCODE_RE = re.compile(r"\b[a-z]\d{1,3}\b", re.I)
_GENERIC_WORDS = {"media", "generation", "burst", "video", "final"}


def _looks_like_filename(title):
    """True when a stored title is really a raw upload filename."""
    t = (title or "").strip()
    if not t:
        return False
    low = t.lower()
    return (low.endswith(".mp4") or low.endswith(".webm")
            or low.startswith("media-generation-")
            or bool(_UUID_RE.search(t)))


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
    _ensure_col(db, "posts", "video_url", "video_url TEXT NOT NULL DEFAULT ''")
    _ensure_col(db, "posts", "video_ai", "video_ai INTEGER NOT NULL DEFAULT 0")
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
                        title=None, description=None):
    """Validate and store an uploaded video. Returns (uid, stored_path)."""
    ensure_video_schema(db)
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
    safe_name = (os.path.basename(filename or ("upload." + ext)) or
                 ("upload." + ext))[:120]
    cur = db._exec(
        "INSERT INTO video_uploads (fm_id, handle, filename, stored_path, bytes,"
        " mime, ai_generated, duration_secs, created_at, title, description)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (fm_id, handle, safe_name, "", len(raw), mime,
         1 if ai_generated else 0, dur, int(time.time()),
         (title or "")[:120] or None, (description or "")[:500] or None))
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
           " WHERE (duration_secs IS NULL OR duration_secs < ?)")
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
    url = "/video/%d" % int(uid)
    p = db._one("SELECT id, community, title, handle FROM posts"
                " WHERE video_url=? ORDER BY id DESC", (url,))
    if p:
        return {"kind": "post", "post_id": p["id"], "community": p["community"],
                "title": p["title"], "handle": p["handle"], "comment_id": None}
    c = db._one("SELECT c.id, c.post_id, c.handle, p.community FROM comments c"
                " JOIN posts p ON p.id=c.post_id"
                " WHERE c.video_url=? ORDER BY c.id DESC", (url,))
    if c:
        return {"kind": "comment", "post_id": c["post_id"],
                "community": c["community"], "title": None,
                "handle": c["handle"], "comment_id": c["id"]}
    return None
