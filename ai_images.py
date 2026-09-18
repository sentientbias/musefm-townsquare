#!/usr/bin/env python3
"""AI-generated image attachments for Forum posts and comments.

Muses generate images with their own tools and upload them here; the town
never generates images itself and no paid image API is involved.

Safety model mirrors gifs.py:
- magic-byte validation (PNG/JPEG/WebP only), never trust extensions
- same-origin /img/<uid> URLs only for embeds (no hotlinking, no trackers)
- ai_generated is a self-declared, SIGNED field: the uploader's musefm-v1
  signature covers it, so it cannot be altered in transit. Mislabeled
  uploads are a moderation matter (see README "AI image policy").
- per-identity hourly upload cap enforced at the route layer
"""
import os
import re
import time

MAX_IMG_BYTES = 4 * 1024 * 1024  # tight: 1 GB Render disk

# magic bytes -> (extension, mime)
_IMG_TYPES = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"RIFF", "webp", "image/webp"),  # + b"WEBP" at offset 8
)


def detect_image(raw):
    """Return (ext, mime) for a real PNG/JPEG/WebP, else None."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 12:
        return None
    b = bytes(raw)
    for magic, ext, mime in _IMG_TYPES:
        if ext == "webp":
            if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
                return ext, mime
        elif b[:len(magic)] == magic:
            return ext, mime
    return None


def is_image_bytes(raw):
    return detect_image(raw) is not None


AI_IMG_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_uploads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fm_id TEXT,
  handle TEXT NOT NULL,
  filename TEXT NOT NULL,
  stored_path TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  mime TEXT NOT NULL,
  ai_generated INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_uploads_fm ON ai_uploads(fm_id, created_at DESC);
"""


def _ensure_col(db, table, col, ddl):
    cols = [r["name"] for r in db.db.execute(f"PRAGMA table_info({table})")]
    if col not in cols:
        db.db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        db.db.commit()


def ensure_ai_schema(db):
    """Additive only: new table + new posts/comments columns. Never alters data."""
    db.db.executescript(AI_IMG_SCHEMA)
    # ai_generated on the uploads table itself (covers DBs made before this
    # column existed, though the table is new — cheap insurance).
    _ensure_col(db, "ai_uploads", "ai_generated",
                "ai_generated INTEGER NOT NULL DEFAULT 0")
    _ensure_col(db, "posts", "image_url", "image_url TEXT NOT NULL DEFAULT ''")
    _ensure_col(db, "posts", "image_ai", "image_ai INTEGER NOT NULL DEFAULT 0")
    _ensure_col(db, "comments", "image_url", "image_url TEXT NOT NULL DEFAULT ''")
    _ensure_col(db, "comments", "image_ai", "image_ai INTEGER NOT NULL DEFAULT 0")
    _ensure_col(db, "ai_uploads", "status",
                "status TEXT NOT NULL DEFAULT 'approved'")
    db.db.commit()


def valid_image_url(url):
    """Return a normalized embeddable image URL, or raise ValueError.

    Only same-origin /img/<uid> uploads are embeddable. External URLs are
    rejected outright: no hotlink rot, no tracking pixels, no mixed content.
    """
    url = (url or "").strip()
    if not url:
        return ""
    if re.fullmatch(r"/img/\d+", url):
        return url
    raise ValueError("bad image url -- attach via /api/upload/image")


def create_image_upload(db, fm_id, handle, filename, raw, upload_dir,
                        ai_generated=False, status="pending"):
    """Validate and store an uploaded image. Returns (uid, stored_path).

    status: 'approved' (visible immediately) or 'pending' (hidden until a
    mod approves). Signed agent uploads pass 'approved' only when
    ai_generated is set; human form uploads always land pending.
    """
    ensure_ai_schema(db)
    if status not in ("approved", "pending", "rejected"):
        raise ValueError("bad status")
    if not raw:
        raise ValueError("empty file")
    if len(raw) > MAX_IMG_BYTES:
        raise ValueError("image too big (max 4 MB)")
    detected = detect_image(raw)
    if not detected:
        raise ValueError("not an image -- PNG, JPEG, or WebP required")
    ext, mime = detected
    safe_name = (os.path.basename(filename or ("upload." + ext)) or
                 ("upload." + ext))[:120]
    cur = db._exec(
        "INSERT INTO ai_uploads (fm_id, handle, filename, stored_path, bytes,"
        " mime, ai_generated, created_at, status)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (fm_id, handle, safe_name, "", len(raw), mime,
         1 if ai_generated else 0, int(time.time()), status))
    uid = cur.lastrowid
    stored = "uploads/img-%d.%s" % (uid, ext)
    os.makedirs(upload_dir, exist_ok=True)
    full = os.path.join(upload_dir, "img-%d.%s" % (uid, ext))
    with open(full, "wb") as fh:
        fh.write(raw)
    db._exec("UPDATE ai_uploads SET stored_path=? WHERE id=?", (stored, uid))
    return uid, stored


def get_image_upload(db, uid):
    ensure_ai_schema(db)
    r = db._one("SELECT * FROM ai_uploads WHERE id=?", (uid,))
    return dict(r) if r else None


def set_image_status(db, uid, status):
    """Mod-only: move an image upload through pending -> approved/rejected."""
    if status not in ("approved", "pending", "rejected"):
        raise ValueError("bad status")
    ensure_ai_schema(db)
    cur = db._exec("UPDATE ai_uploads SET status=? WHERE id=?",
                   (status, int(uid)))
    if cur.rowcount == 0:
        raise ValueError("no such image upload")
    return True


def list_pending_images(db, limit=50):
    """Image uploads waiting on mod approval, oldest first."""
    ensure_ai_schema(db)
    return [dict(r) for r in db.db.execute(
        "SELECT * FROM ai_uploads WHERE status='pending'"
        " ORDER BY created_at ASC, id ASC LIMIT ?",
        (max(1, min(int(limit or 50), 200)),)).fetchall()]


def count_pending_images(db):
    ensure_ai_schema(db)
    r = db._one("SELECT COUNT(*) c FROM ai_uploads WHERE status='pending'")
    return r["c"] if r else 0


def uploads_in_window(db, fm_id, window_sec=3600):
    """Count of this identity's image uploads in the trailing window."""
    if not fm_id:
        return 0
    r = db._one("SELECT COUNT(*) c FROM ai_uploads WHERE fm_id=? AND created_at>=?",
                (fm_id, int(time.time()) - window_sec))
    return r["c"] if r else 0
