#!/usr/bin/env python3
"""GIF support for Forum posts."""
import os
import re
import time
from urllib.parse import urlparse
MAX_GIF_BYTES = 8 * 1024 * 1024
MAX_GIF_URL_LEN = 500

def _host(*labels):
    return ".".join(labels)

# Direct media hosts of the two big GIF CDNs. Share/page URLs are not
# embeddable and are rejected by valid_gif_url below.
GIF_HOSTS = {
    _host("giphy", "com"),
    _host("www", "giphy", "com"),
    _host("media", "giphy", "com"),
    _host("i", "giphy", "com"),
    _host("tenor", "com"),
    _host("www", "tenor", "com"),
    _host("media", "tenor", "com"),
    _host("c", "tenor", "com"),
}

GIF_SCHEMA = """
CREATE TABLE IF NOT EXISTS gif_uploads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fm_id TEXT,
  handle TEXT NOT NULL,
  filename TEXT NOT NULL,
  stored_path TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gif_uploads_fm ON gif_uploads(fm_id, created_at DESC);
"""

def ensure_gif_schema(db):
    """Additive only: new table + new posts.gif_url column. Never alters data."""
    db.db.executescript(GIF_SCHEMA)
    cols = [r["name"] for r in db.db.execute("PRAGMA table_info(posts)")]
    if "gif_url" not in cols:
        db.db.execute("ALTER TABLE posts ADD COLUMN gif_url TEXT NOT NULL DEFAULT ''")
        db.db.commit()


def valid_gif_url(url):
    """Return a normalized embeddable GIF URL, or raise ValueError.

    Rules: https only, host on the GIF_HOSTS whitelist, no userinfo/port,
    path ending in .gif, sane length. Everything else is rejected so a
    malicious or mistyped URL can never become an <img> on the town.
    """
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("/"):
        # same-origin upload served by this app -- only /gif/<uid> exists
        if re.fullmatch(r"/gif/\d+", url):
            return url
        raise ValueError("bad gif url")
    if len(url) > MAX_GIF_URL_LEN:
        raise ValueError("gif url too long")
    if re.search(r"[\x00-\x20\x7f]", url):
        raise ValueError("bad gif url")
    try:
        p = urlparse(url)
    except Exception:
        raise ValueError("bad gif url")
    if p.scheme != "https":
        raise ValueError("gif url must be https")
    if p.username or p.password or p.port:
        raise ValueError("bad gif url")
    host = (p.hostname or "").lower()
    if host not in GIF_HOSTS:
        raise ValueError("gif host not allowed -- use a Giphy or Tenor media link")
    if not p.path.lower().endswith(".gif"):
        raise ValueError("gif url must point at a .gif file")
    clean = "https://" + host + p.path
    if p.query:
        clean += "?" + p.query
    return clean


def is_gif_bytes(raw):
    """True only for real GIF files (magic bytes), never trust extensions."""
    return isinstance(raw, (bytes, bytearray)) and bytes(raw[:6]) in (b"GIF87a", b"GIF89a")


def create_gif_upload(db, fm_id, handle, filename, raw, upload_dir):
    """Validate and store an uploaded GIF. Returns (uid, stored_path)."""
    if not raw:
        raise ValueError("empty file")
    if len(raw) > MAX_GIF_BYTES:
        raise ValueError("gif too big (max 8 MB)")
    if not is_gif_bytes(raw):
        raise ValueError("not a gif -- magic bytes do not match")
    safe_name = (os.path.basename(filename or "upload.gif") or "upload.gif")[:120]
    cur = db._exec(
        "INSERT INTO gif_uploads (fm_id, handle, filename, stored_path, bytes, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (fm_id, handle, safe_name, "", len(raw), int(time.time())))
    uid = cur.lastrowid
    stored = "uploads/gif-%d.gif" % uid
    os.makedirs(upload_dir, exist_ok=True)
    full = os.path.join(upload_dir, "gif-%d.gif" % uid)
    with open(full, "wb") as fh:
        fh.write(raw)
    db._exec("UPDATE gif_uploads SET stored_path=? WHERE id=?", (stored, uid))
    return uid, stored


def get_gif_upload(db, uid):
    ensure_gif_schema(db)
    r = db._one("SELECT * FROM gif_uploads WHERE id=?", (uid,))
    return dict(r) if r else None

