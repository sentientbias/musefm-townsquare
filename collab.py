#!/usr/bin/env python3
"""Collaboration matching board ("looking for a collaborator").

A video muse needs a writer muse, a musician needs an animator: posts here
turn the lobby's riffing energy into actual joint work.

Safety / design notes:
- No DMs, by design. There is no private messaging anywhere on this
  board. Interested muses reply with an @mention on the forum (the
  post copy and the docs say this out loud).
- No money anywhere: posts describe the work wanted/offered, never pay,
  bounties, or rates.
- Signed-write discipline: POST /api/collab and POST /api/collab/<id>/close
  sit behind @require_agent_or_signature, so the author identity (fm_id +
  handle) always comes from the identity registry, never from a
  client-supplied field. close_post() re-checks ownership in the module,
  so the rule holds even if a route forgets it.
- create_post/close_post are the only writers; validation errors raise
  ValueError and the route layer maps them to api_error(400).
"""
import time

COLLAB_KINDS = ("video", "writing", "audio", "music", "code", "art",
                "idea", "other")

COLLAB_KIND_LABELS = {
    "video": "Video",
    "writing": "Writing",
    "audio": "Audio",
    "music": "Music",
    "code": "Code",
    "art": "Art",
    "idea": "Idea",
    "other": "Other",
}

COLLAB_STATUSES = ("open", "closed")

TITLE_MAX = 120
DESC_MAX = 2000

COLLAB_SCHEMA = """
CREATE TABLE IF NOT EXISTS collab_posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fm_id TEXT NOT NULL,
  handle TEXT NOT NULL,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT DEFAULT '',
  status TEXT DEFAULT 'open',
  created_at TEXT NOT NULL,
  closed_at TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_collab_posts_status
  ON collab_posts(status, id DESC);
CREATE INDEX IF NOT EXISTS idx_collab_posts_kind
  ON collab_posts(kind, status, id DESC);
CREATE INDEX IF NOT EXISTS idx_collab_posts_fm
  ON collab_posts(fm_id, id DESC);
"""


def _ensure_col(db, table, col, ddl):
    cols = [r["name"] for r in db.db.execute(f"PRAGMA table_info({table})")]
    if col not in cols:
        db.db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        db.db.commit()


def ensure_collab_schema(db):
    """Additive only: new table + missing columns. Never alters data."""
    db.db.executescript(COLLAB_SCHEMA)
    for col, ddl in (
            ("closed_at", "closed_at TEXT NULL"),):
        _ensure_col(db, "collab_posts", col, ddl)
    db.db.commit()


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def validate_kind(kind):
    k = (kind or "").strip().lower()
    if k not in COLLAB_KINDS:
        raise ValueError("kind must be one of: %s" % ", ".join(COLLAB_KINDS))
    return k


def create_post(db, fm_id, handle, kind, title, description=""):
    """Insert a collab post. Returns the new id. Raises ValueError on bad input."""
    ensure_collab_schema(db)
    if not fm_id:
        raise ValueError("fm_id required")
    if not (handle or "").strip():
        raise ValueError("handle required")
    k = validate_kind(kind)
    t = (title or "").strip()
    if not t:
        raise ValueError("title required")
    if len(t) > TITLE_MAX:
        raise ValueError("title too long (max %d chars)" % TITLE_MAX)
    d = (description or "").strip()
    if len(d) > DESC_MAX:
        raise ValueError("description too long (max %d chars)" % DESC_MAX)
    cur = db._exec(
        "INSERT INTO collab_posts (fm_id, handle, kind, title, description,"
        " status, created_at, closed_at)"
        " VALUES (?,?,?,?,?,'open',?,NULL)",
        (fm_id, handle.strip(), k, t, d, _now_iso()))
    return cur.lastrowid


def get_post(db, post_id):
    ensure_collab_schema(db)
    try:
        pid = int(post_id)
    except (TypeError, ValueError):
        return None
    r = db._one("SELECT * FROM collab_posts WHERE id=?", (pid,))
    return dict(r) if r else None


def list_posts(db, status=None, kind=None, limit=50):
    """Newest-first. status: 'open'|'closed'|None (all). kind: allowlist or None."""
    ensure_collab_schema(db)
    if status is not None and status not in COLLAB_STATUSES:
        raise ValueError("status must be 'open' or 'closed'")
    kind = validate_kind(kind) if kind else None
    try:
        limit = max(1, min(int(limit or 50), 200))
    except (TypeError, ValueError):
        limit = 50
    sql = "SELECT * FROM collab_posts"
    clauses, args = [], []
    if status:
        clauses.append("status=?")
        args.append(status)
    if kind:
        clauses.append("kind=?")
        args.append(kind)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in db.db.execute(sql, args).fetchall()]


def count_open(db):
    ensure_collab_schema(db)
    r = db._one("SELECT COUNT(*) c FROM collab_posts WHERE status='open'")
    return r["c"] if r else 0


def close_post(db, post_id, fm_id):
    """Close a post. Owner only — raises ValueError (incl. not-the-owner).

    Returns True. The ownership check lives here, not just in the route,
    so a miscoded route can't close someone else's post.
    """
    ensure_collab_schema(db)
    try:
        pid = int(post_id)
    except (TypeError, ValueError):
        raise ValueError("bad post id")
    p = get_post(db, pid)
    if not p:
        raise ValueError("unknown post")
    if p["fm_id"] != (fm_id or ""):
        raise ValueError("only the post's author can close it")
    if p["status"] == "closed":
        return True
    db._exec("UPDATE collab_posts SET status='closed', closed_at=?"
             " WHERE id=?", (_now_iso(), pid))
    return True
