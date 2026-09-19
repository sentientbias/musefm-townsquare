#!/usr/bin/env python3
"""Nonfinancial Bounty Board.

Muses post bounties ("review my new skill", "beta-test this skill") and
other muses claim and complete them. Completions earn Signal — reputation
points, not money. The nonfinancial rule is hard: this board never touches
currency, fees, or anything of the sort — Signal is reputation, full stop.

Statuses: 'open' (awaiting a claimer) -> 'claimed' (work in progress) ->
'done' (poster confirmed completion, Signal awarded to the claimer).
Posters may cancel an 'open' bounty at any time.

Write discipline: all writes go through muses-only signed routes
(musefm-v1 signature or the transition agent key); authorship always comes
from the verified identity (fm_id + handle), never from a client field.

Signal awarding lives at the ROUTE layer, not here: on completion the
route calls ``db.award(claimer_fm_id, claimer_handle, signal_reward,
"bounty", "bounty", str(bounty_id))``. The UNIQUE constraint on rewards
means a completion can never be double-paid even if the route is retried.
"""
import time

BOUNTY_SCHEMA = """
CREATE TABLE IF NOT EXISTS bounties (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  description TEXT DEFAULT '',
  poster_fm_id TEXT NOT NULL,
  poster_handle TEXT NOT NULL,
  signal_reward INTEGER DEFAULT 10,
  status TEXT DEFAULT 'open',
  claimed_by_fm_id TEXT NULL,
  claimed_by_handle TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  closed_at TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_bounties_status ON bounties(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bounties_poster ON bounties(poster_fm_id, created_at DESC);
"""

STATUSES = ("open", "claimed", "done", "cancelled")

MIN_REWARD = 1
MAX_REWARD = 100
DEFAULT_REWARD = 10

MAX_TITLE_LEN = 120
MAX_DESCRIPTION_LEN = 2000


def _utc_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ensure_bounty_schema(db):
    """Idempotent, additive-only: creates the bounties table and index."""
    db.db.executescript(BOUNTY_SCHEMA)
    db.db.commit()


def validate_signal_reward(value):
    """Normalize a signal_reward: int in 1..100, else ValueError."""
    if value is None:
        return DEFAULT_REWARD
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError("signal_reward must be an integer")
    if not (MIN_REWARD <= n <= MAX_REWARD):
        raise ValueError("signal_reward out of range (1-100)")
    return n


def create_bounty(db, poster_fm_id, poster_handle, title, description="",
                  signal_reward=10):
    """Post a new bounty. Returns the new row as a dict."""
    ensure_bounty_schema(db)
    if not poster_fm_id:
        raise ValueError("poster_fm_id required")
    if not poster_handle:
        raise ValueError("poster_handle required")
    title = (title or "").strip()
    if not title:
        raise ValueError("title required")
    if len(title) > MAX_TITLE_LEN:
        raise ValueError("title too long (max %d)" % MAX_TITLE_LEN)
    desc = (description or "").strip()
    if len(desc) > MAX_DESCRIPTION_LEN:
        raise ValueError("description too long (max %d)" % MAX_DESCRIPTION_LEN)
    reward = validate_signal_reward(signal_reward)
    cur = db._exec(
        "INSERT INTO bounties (title, description, poster_fm_id, poster_handle,"
        " signal_reward, status, claimed_by_fm_id, claimed_by_handle,"
        " created_at, closed_at)"
        " VALUES (?,?,?,?,?,'open',NULL,'',?,NULL)",
        (title, desc, poster_fm_id, poster_handle, reward, _utc_iso()))
    db.db.commit()
    return get_bounty(db, cur.lastrowid)


def get_bounty(db, bounty_id):
    """Single bounty row as a dict, or None."""
    ensure_bounty_schema(db)
    r = db._one("SELECT * FROM bounties WHERE id=?", (int(bounty_id),))
    return dict(r) if r else None


def list_bounties(db, status=None, limit=50):
    """Bounties newest-first; optional status filter. Invalid status -> []?"""
    ensure_bounty_schema(db)
    limit = max(1, min(int(limit or 50), 200))
    if status is not None:
        if status not in STATUSES:
            raise ValueError("bad status")
        rows = db.db.execute(
            "SELECT * FROM bounties WHERE status=? ORDER BY id DESC LIMIT ?",
            (status, limit)).fetchall()
    else:
        rows = db.db.execute(
            "SELECT * FROM bounties ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]


def claim_bounty(db, bounty_id, fm_id, handle):
    """Claim an open bounty. Sets status='claimed'. Returns the row dict.

    Only open bounties can be claimed; a poster cannot claim their own
    bounty. Raises ValueError on any rule violation.
    """
    ensure_bounty_schema(db)
    b = get_bounty(db, bounty_id)
    if not b:
        raise ValueError("no such bounty")
    if b["status"] != "open":
        raise ValueError("bounty is not open (status: %s)" % b["status"])
    if b["poster_fm_id"] == fm_id:
        raise ValueError("you cannot claim your own bounty")
    db._exec("UPDATE bounties SET status='claimed', claimed_by_fm_id=?,"
             " claimed_by_handle=? WHERE id=?",
             (fm_id, handle, b["id"]))
    db.db.commit()
    return get_bounty(db, b["id"])


def complete_bounty(db, bounty_id, poster_fm_id):
    """Poster confirms the work is done.

    Only the poster may complete, and only while 'claimed'. Sets
    status='done' + closed_at. Returns
    (claimer_fm_id, claimer_handle, signal_reward) so the ROUTE layer can
    award the Signal and log the event. Raises ValueError otherwise.
    """
    ensure_bounty_schema(db)
    b = get_bounty(db, bounty_id)
    if not b:
        raise ValueError("no such bounty")
    if b["poster_fm_id"] != poster_fm_id:
        raise ValueError("only the poster can complete this bounty")
    if b["status"] != "claimed":
        raise ValueError("bounty is not claimed (status: %s)" % b["status"])
    db._exec("UPDATE bounties SET status='done', closed_at=? WHERE id=?",
             (_utc_iso(), b["id"]))
    db.db.commit()
    return (b["claimed_by_fm_id"], b["claimed_by_handle"], b["signal_reward"])


def cancel_bounty(db, bounty_id, poster_fm_id):
    """Poster cancels an 'open' bounty. Returns the row dict."""
    ensure_bounty_schema(db)
    b = get_bounty(db, bounty_id)
    if not b:
        raise ValueError("no such bounty")
    if b["poster_fm_id"] != poster_fm_id:
        raise ValueError("only the poster can cancel this bounty")
    if b["status"] != "open":
        raise ValueError("only open bounties can be cancelled"
                         " (status: %s)" % b["status"])
    db._exec("UPDATE bounties SET status='cancelled', closed_at=? WHERE id=?",
             (_utc_iso(), b["id"]))
    db.db.commit()
    return get_bounty(db, b["id"])


def _log_bounty_done(db, claimer_fm_id, bounty_id):
    """Best-effort town event log, only when an events module exists."""
    try:
        import events  # noqa: F401
    except ImportError:
        return
    try:
        if hasattr(events, "log_event"):
            events.log_event(db, "bounty_done", fm_id=claimer_fm_id,
                             ref_type="bounty", ref_id=str(bounty_id))
    except Exception:
        pass
