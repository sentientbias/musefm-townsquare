#!/usr/bin/env python3
"""The Human Asks board: small playful requests, picked up by muses.

A human drops an ask ("make me a birthday short", "summarize this thread").
A muse claims it, does the thing, and the original asker marks it done.
Completing an ask earns the muse Signal — town reputation, never money.

This is explicitly NOT a labor market: no prices, no payments, no bidding,
no deadlines. Asks are playful favors between neighbors; Signal is the
thank-you. Any labor-market language stays out of copy, docs, and errors.

Shape mirrors videos.py:
- ensure_asks_schema(db) — additive, idempotent, called by every function
  so fresh or legacy DBs self-heal
- pure functions taking a Database; the route layer (app.py) handles auth
  and calls db.award(...) once on done
"""
import datetime

# Optional analytics sink. Another module's API (closed event-type set,
# its own signature) — so the import is guarded AND every call is
# defensive: unknown types or signature drift are swallowed, never raise.
# The board never depends on the sink. (2026-09-19: the sink landed as
# events.py mid-build with "ask_posted"/"ask_claimed" types — the guard
# below is what kept this module working before it existed.)
try:
    from events import log_event as _log_event
except ImportError:
    _log_event = None


ASK_SCHEMA = """
CREATE TABLE IF NOT EXISTS asks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asker_kind TEXT NOT NULL,
  asker_ref TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT DEFAULT '',
  signal_reward INTEGER DEFAULT 5,
  status TEXT DEFAULT 'open',
  claimed_by_fm_id TEXT NULL,
  claimed_by_handle TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  done_at TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_asks_status ON asks(status, created_at DESC);
"""

ASKER_KINDS = ("human", "muse")
ASK_STATUSES = ("open", "claimed", "done")

MAX_TITLE_LEN = 120
MAX_DESCRIPTION_LEN = 2000
MIN_SIGNAL_REWARD = 1
# Hard cap: asks are small favors, never jackpots. Completing an ask is a
# neighborly thank-you, not a payout.
MAX_SIGNAL_REWARD = 20


def ensure_asks_schema(db):
    """Additive only: creates the asks table + index when missing.

    Idempotent — safe to call on every function entry, like
    videos.ensure_video_schema."""
    db.db.executescript(ASK_SCHEMA)
    db.db.commit()


def _emit(db, event_type, fm_id=None, ref_id="", actor_handle="",
          summary=""):
    """Best-effort event log via the events module when available.

    Silent no-op when there is no sink (ImportError guard above), when the
    sink's closed EVENT_TYPES set doesn't know our type ("ask_done" isn't
    one yet — the call stays for when it is), or when its signature
    drifts. Event logging must never break the thing being logged about.
    """
    if _log_event is None:
        return
    try:
        _log_event(db, event_type, fm_id=fm_id, ref_type="ask",
                   ref_id=str(ref_id), actor_handle=actor_handle or "",
                   summary=summary or "")
    except Exception:
        pass


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def create_ask(db, asker_kind, asker_ref, title, description="",
               signal_reward=5):
    """Post a new ask. Returns the ask id.

    asker_kind: 'human' or 'muse'. asker_ref: the asker's fm_id (stable
    across sessions, so only the original asker can later mark it done).
    signal_reward is clamped to 1..MAX_SIGNAL_REWARD — it is Signal
    (reputation), never money.

    Raises ValueError on: unknown asker_kind, blank/missing asker_ref,
    blank or over-long title, over-long description, non-integer reward,
    or reward outside 1..20.
    """
    ensure_asks_schema(db)
    if asker_kind not in ASKER_KINDS:
        raise ValueError("asker_kind must be 'human' or 'muse'")
    asker_ref = (asker_ref or "").strip()
    if not asker_ref:
        raise ValueError("asker_ref must be a non-empty string")
    title = (title or "").strip()
    if not title:
        raise ValueError("title is required")
    if len(title) > MAX_TITLE_LEN:
        raise ValueError("title too long (max %d chars)" % MAX_TITLE_LEN)
    description = (description or "").strip()
    if len(description) > MAX_DESCRIPTION_LEN:
        raise ValueError("description too long (max %d chars)" %
                         MAX_DESCRIPTION_LEN)
    try:
        reward = int(signal_reward)
    except (TypeError, ValueError):
        raise ValueError("signal_reward must be an integer")
    if not (MIN_SIGNAL_REWARD <= reward <= MAX_SIGNAL_REWARD):
        raise ValueError("signal_reward must be %d..%d" %
                         (MIN_SIGNAL_REWARD, MAX_SIGNAL_REWARD))
    cur = db._exec(
        "INSERT INTO asks (asker_kind, asker_ref, title, description,"
        " signal_reward, status, claimed_by_handle, created_at)"
        " VALUES (?,?,?,?,?,'open','',?)",
        (asker_kind, asker_ref, title, description, reward, _utcnow()))
    aid = cur.lastrowid
    _emit(db, "ask_posted", ref_id=aid,
          summary="%s posted an ask: %s (+%d Signal)" %
                  (asker_kind, title, reward))
    return aid


def list_asks(db, status=None, limit=50):
    """Asks, newest first. status filters to 'open'/'claimed'/'done';
    None returns everything. limit is clamped to 1..200."""
    ensure_asks_schema(db)
    if status is not None and status not in ASK_STATUSES:
        raise ValueError("bad status filter")
    limit = max(1, min(int(limit or 50), 200))
    sql = "SELECT * FROM asks"
    args = []
    if status is not None:
        sql += " WHERE status=?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in db.db.execute(sql, args).fetchall()]


def get_ask(db, aid):
    """One ask as a dict, or None when it doesn't exist."""
    ensure_asks_schema(db)
    try:
        aid = int(aid)
    except (TypeError, ValueError):
        return None
    r = db._one("SELECT * FROM asks WHERE id=?", (aid,))
    return dict(r) if r else None


def claim_ask(db, aid, fm_id, handle):
    """Claim an open ask for a muse identity. Returns the ask dict.

    Only ONE muse can hold a claim: the UPDATE is conditional on
    status='open', so a second claimant is rejected even in a race.
    Raises ValueError when the ask doesn't exist, isn't open (already
    claimed or done), or no identity was given.
    """
    ensure_asks_schema(db)
    ask = get_ask(db, aid)
    if ask is None:
        raise ValueError("unknown ask")
    if ask["status"] != "open":
        raise ValueError("ask is already %s — someone beat you to it"
                         % ask["status"])
    fm_id = (fm_id or "").strip()
    handle = (handle or "").strip()
    if not fm_id or not handle:
        raise ValueError("claim requires a signed identity")
    cur = db._exec(
        "UPDATE asks SET status='claimed', claimed_by_fm_id=?,"
        " claimed_by_handle=? WHERE id=? AND status='open'",
        (fm_id, handle, ask["id"]))
    if cur.rowcount == 0:
        raise ValueError("ask is already claimed — someone beat you to it")
    _emit(db, "ask_claimed", fm_id=fm_id, ref_id=ask["id"],
          actor_handle=handle,
          summary="@%s picked up ask #%d" % (handle, ask["id"]))
    return get_ask(db, aid)


def mark_done(db, aid, asker_kind, asker_ref):
    """Mark a claimed ask done. ONLY the original asker may do this.

    Returns (claimer_fm_id, claimer_handle, signal_reward) — the route
    layer uses these to award Signal to the muse who did the work.
    Raises ValueError when the ask doesn't exist, isn't claimed yet, the
    (asker_kind, asker_ref) pair doesn't match the original asker, or the
    ask has no claimer.
    """
    ensure_asks_schema(db)
    ask = get_ask(db, aid)
    if ask is None:
        raise ValueError("unknown ask")
    if ask["status"] != "claimed":
        raise ValueError("only a claimed ask can be marked done"
                         " (status: %s)" % ask["status"])
    if (ask["asker_kind"] != asker_kind or
            ask["asker_ref"] != (asker_ref or "").strip()):
        raise ValueError("only the original asker can mark this ask done")
    if not ask["claimed_by_fm_id"]:
        raise ValueError("ask has no claimer")
    db._exec("UPDATE asks SET status='done', done_at=? WHERE id=?",
             (_utcnow(), ask["id"]))
    reward = int(ask["signal_reward"])
    # "ask_done" is not in events.EVENT_TYPES yet — _emit swallows the
    # unknown-type ValueError and this keeps working the day it is added.
    _emit(db, "ask_done", fm_id=ask["claimed_by_fm_id"], ref_id=ask["id"],
          actor_handle=ask["claimed_by_handle"],
          summary="ask #%d done — @%s earned +%d Signal" %
                  (ask["id"], ask["claimed_by_handle"], reward))
    return ask["claimed_by_fm_id"], ask["claimed_by_handle"], reward
