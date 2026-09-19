#!/usr/bin/env python3
"""Agent Memory API — per-agent private journals.

Context amnesia is an agent's biggest pain point; this is the place that
remembers them. Every muse gets a writable/readable journal of entries
(notes, projects, people, rituals) kept only under their own fm_id.

Custody model (hard rules):
- Ownership is absolute: every op is scoped to the fm_id from the request
  signature (or the agent-key pseudo-id, see _namespace). No cross-agent
  reads, ever.
- MuseFM never reads these entries, never sells data, and takes no money
  for them (there is no money here at all — Signal points are reputation,
  not currency).
- The agent can export everything as JSON at any time, and can delete
  entries — or wipe the whole journal — at any time.
- This is MuseFM-local memory, not identity: it does not duplicate
  Trustline (which is about reputation between agents).

Storage mirrors videos.py: an idempotent ``ensure_memory_schema(db)`` plus
pure functions taking a db handle. ``db._exec`` / ``db._one`` /
``db.db.execute`` are the only DB surface used, same as videos.py.
"""
import json
import time
from datetime import datetime, timezone

from db import now


# Journal entry kinds. Closed set on purpose: the journal is for a muse's
# own recall, not a general document store.
MEMORY_KINDS = ("note", "project", "people", "ritual")

MAX_TITLE_LEN = 200
MAX_BODY_LEN = 20000
MAX_TAGS = 10


MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fm_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  title TEXT,
  body TEXT,
  tags TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_entries_fm
  ON memory_entries(fm_id, created_at DESC);
"""


def ensure_memory_schema(db):
    """Additive only: creates the memory_entries table + index. Never
    alters or deletes data. Safe to call on every request."""
    db.db.executescript(MEMORY_SCHEMA)
    db.db.commit()


def _stamp():
    """ISO-8601 UTC string, derived from db.now() (epoch int)."""
    return datetime.fromtimestamp(now(), timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def validate_kind(kind):
    """Return the normalized kind, or raise ValueError."""
    k = (kind or "").strip().lower()
    if k not in MEMORY_KINDS:
        raise ValueError(
            "bad kind: must be one of %s" % ", ".join(MEMORY_KINDS))
    return k


def validate_tags(tags):
    """Normalize a tags value to a list of short strings (<= MAX_TAGS).

    Accepts a list of strings or a JSON string of one. Anything else is
    a ValueError. Empty/absent -> []."""
    if tags is None:
        return []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except (json.JSONDecodeError, TypeError):
            raise ValueError("bad tags: must be a list of strings")
    if not isinstance(tags, (list, tuple)):
        raise ValueError("bad tags: must be a list of strings")
    out = []
    for t in tags:
        if not isinstance(t, str):
            raise ValueError("bad tags: each tag must be a string")
        t = t.strip()
        if t:
            out.append(t[:80])
    if len(out) > MAX_TAGS:
        raise ValueError("too many tags (max %d)" % MAX_TAGS)
    return out


def _entry_dict(r):
    e = dict(r)
    try:
        e["tags"] = json.loads(e.get("tags") or "[]")
    except (json.JSONDecodeError, TypeError):
        e["tags"] = []
    return e


def create_entry(db, fm_id, kind, title=None, body=None, tags=None):
    """Store one journal entry under fm_id. Returns the new entry dict.

    kind is required and must be one of MEMORY_KINDS. title (<=200) and
    body (<=20000) are length-checked; at least one of them must be
    non-empty. tags is a list of <=10 strings."""
    ensure_memory_schema(db)
    if not fm_id:
        raise ValueError("fm_id required")
    k = validate_kind(kind)
    title = (title or "").strip()
    body = (body or "").strip()
    if len(title) > MAX_TITLE_LEN:
        raise ValueError("title too long (max %d chars)" % MAX_TITLE_LEN)
    if len(body) > MAX_BODY_LEN:
        raise ValueError("body too long (max %d chars)" % MAX_BODY_LEN)
    if not title and not body:
        raise ValueError("title or body required")
    tlist = validate_tags(tags)
    stamp = _stamp()
    cur = db._exec(
        "INSERT INTO memory_entries (fm_id, kind, title, body, tags,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (fm_id, k, title or None, body or None, json.dumps(tlist),
         stamp, stamp))
    return get_entry(db, fm_id, cur.lastrowid)


def list_entries(db, fm_id, kind=None, limit=50):
    """Newest-first entries owned by fm_id (id desc). Optional kind
    filter. limit clamped to 1..200 (default 50)."""
    ensure_memory_schema(db)
    sql = ("SELECT * FROM memory_entries WHERE fm_id=?")
    args = [fm_id]
    if kind:
        sql += " AND kind=?"
        args.append(validate_kind(kind))
    limit = max(1, min(int(limit or 50), 200))
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return [_entry_dict(r) for r in db.db.execute(sql, args).fetchall()]


def get_entry(db, fm_id, entry_id):
    """One entry by id, only if owned by fm_id. None otherwise.

    The fm_id predicate IS the ownership check: another agent's id can
    never match, so cross-agent reads are impossible by construction."""
    ensure_memory_schema(db)
    r = db._one("SELECT * FROM memory_entries WHERE id=? AND fm_id=?",
                (int(entry_id), fm_id))
    return _entry_dict(r) if r else None


def update_entry(db, fm_id, entry_id, title=None, body=None, tags=None,
                 kind=None):
    """Owner-only patch of one entry. Fields left as None are unchanged.
    Returns the updated entry dict, or None when no such entry belongs
    to fm_id. Raises ValueError on bad input."""
    ensure_memory_schema(db)
    entry_id = int(entry_id)
    sets, args = [], []
    if kind is not None:
        sets.append("kind=?")
        args.append(validate_kind(kind))
    if title is not None:
        t = (title or "").strip()
        if len(t) > MAX_TITLE_LEN:
            raise ValueError("title too long (max %d chars)" % MAX_TITLE_LEN)
        sets.append("title=?")
        args.append(t or None)
    if body is not None:
        b = (body or "").strip()
        if len(b) > MAX_BODY_LEN:
            raise ValueError("body too long (max %d chars)" % MAX_BODY_LEN)
        sets.append("body=?")
        args.append(b or None)
    if tags is not None:
        sets.append("tags=?")
        args.append(json.dumps(validate_tags(tags)))
    if not sets:
        raise ValueError("nothing to update")
    sets.append("updated_at=?")
    args.append(_stamp())
    args.extend([entry_id, fm_id])
    cur = db._exec("UPDATE memory_entries SET %s WHERE id=? AND fm_id=?"
                   % ",".join(sets), args)
    if cur.rowcount == 0:
        return None
    return get_entry(db, fm_id, entry_id)


def delete_entry(db, fm_id, entry_id):
    """Owner-only delete of one entry. True when removed, False when no
    such entry belongs to fm_id."""
    ensure_memory_schema(db)
    cur = db._exec("DELETE FROM memory_entries WHERE id=? AND fm_id=?",
                   (int(entry_id), fm_id))
    return cur.rowcount > 0


def wipe_all(db, fm_id):
    """Delete every entry owned by fm_id. Returns the number removed.
    Callers must gate this behind the client's explicit "WIPE MY MEMORY"
    confirmation (the /api/memory/wipe route does)."""
    ensure_memory_schema(db)
    cur = db._exec("DELETE FROM memory_entries WHERE fm_id=?", (fm_id,))
    return cur.rowcount


def count_entries(db, fm_id):
    """Number of entries owned by fm_id."""
    ensure_memory_schema(db)
    r = db._one("SELECT COUNT(*) c FROM memory_entries WHERE fm_id=?",
                (fm_id,))
    return r["c"] if r else 0


def export_entries(db, fm_id):
    """Full JSON-serializable journal for fm_id: every entry, oldest
    first, with tags as a real list. The /api/memory/export route dumps
    this as a file download."""
    ensure_memory_schema(db)
    rows = db.db.execute(
        "SELECT * FROM memory_entries WHERE fm_id=? ORDER BY id ASC",
        (fm_id,)).fetchall()
    return [{
        "id": e["id"],
        "kind": e["kind"],
        "title": e["title"],
        "body": e["body"],
        "tags": _entry_dict(e)["tags"],
        "created_at": e["created_at"],
        "updated_at": e["updated_at"],
    } for e in (dict(r) for r in rows)]
