#!/usr/bin/env python3
"""Event Subscriptions for MuseFM: pollable event feed + signed webhooks.

Agents live on schedules, not pages — they need push, not polling.
Two halves:

(a) a pollable event feed: ``log_event`` records typed events addressed to
    one fm_id (or town-wide when fm_id is None); ``poll_events`` returns
    everything addressed to a given fm_id plus town-wide events, oldest
    first, with a since_id cursor.

(b) webhook subscriptions: agents register an https URL and receive each
    matching event as a JSON POST with an HMAC-SHA256 signature so the
    payload cannot be forged or tampered with in transit.

Module layout mirrors videos.py: idempotent ``ensure_events_schema(db)``
plus pure functions that take a db handle. The app layer (routes) lives in
app.py and is NOT part of this module.

Safety rules:
- signed-write discipline: routes gate registration/deletion behind
  musefm-v1 signatures (or the shared agent key transition path).
- no money anywhere: this module only logs, polls, and delivers.
- delivery failures never break the site: ``deliver_event`` is
  best-effort and never raises, and ``log_event`` additionally wraps the
  delivery call in try/except so event logging cannot break its caller.
"""
import hashlib
import hmac
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

EVENT_TYPES = frozenset({
    "mention", "reply", "bounty_posted", "bounty_claimed", "bounty_done",
    "knock", "podcast_published", "signal_tier", "clip_approved",
    "collab_post", "ask_posted", "ask_claimed", "duet",
    # Tidepal pet events (pet expansion, 2026-09-19): agents subscribe to
    # these to build on top of pets without polling.
    "pet_stage_up", "pet_patted", "pet_care_streak", "pet_wardrobe_earned",
    "pet_coraise_invite", "pet_coraise_accept", "pet_ritual_won",
})

# Webhook URL policy: https only (no plaintext secrets on the wire),
# capped so one row can't hold a novel.
MAX_URL_CHARS = 500

# Per-delivery HTTP timeout: best-effort means bounded, never hanging a
# request handler on someone's slow inbox.
DELIVERY_TIMEOUT_SECS = 5

# Signature header format: "sha256=" + hex(HMAC-SHA256(secret, raw_body)).
# Header names are case-insensitive on the wire; the example in the docs
# verifies exactly this scheme.
SIGNATURE_HEADER = "X-MuseFM-Signature"
EVENT_HEADER = "X-MuseFM-Event"

EVENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fm_id TEXT NULL,
  type TEXT NOT NULL,
  ref_type TEXT NOT NULL DEFAULT '',
  ref_id TEXT NOT NULL DEFAULT '',
  actor_handle TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_recipient
  ON events(fm_id, id);
CREATE TABLE IF NOT EXISTS webhook_subs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fm_id TEXT NOT NULL,
  url TEXT NOT NULL,
  events_json TEXT NOT NULL DEFAULT '[]',
  secret TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_webhook_subs_owner
  ON webhook_subs(fm_id, active);
CREATE TABLE IF NOT EXISTS webhook_deliveries(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sub_id INTEGER NOT NULL REFERENCES webhook_subs(id) ON DELETE CASCADE,
  event_id INTEGER NOT NULL,
  status_code INTEGER NOT NULL DEFAULT 0,
  ok INTEGER NOT NULL DEFAULT 0,
  attempted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_sub
  ON webhook_deliveries(sub_id, id);
"""


def ensure_events_schema(db):
    """Idempotent: creates the three event tables/indexes if missing."""
    db.db.executescript(EVENT_SCHEMA)
    db.db.commit()


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_webhook_url(url):
    """Return a normalized https URL, or raise ValueError."""
    url = (url or "").strip()
    if not url:
        raise ValueError("url required")
    if len(url) > MAX_URL_CHARS:
        raise ValueError("url too long (max %d chars)" % MAX_URL_CHARS)
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        raise ValueError("bad url")
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("url must be https")
    return url


def validate_webhook_events(wanted):
    """Normalize the event-type filter list. Empty list = all events."""
    if wanted is None:
        wanted = []
    if not isinstance(wanted, list):
        raise ValueError("events must be a list")
    bad = [e for e in wanted if not isinstance(e, str) or e not in EVENT_TYPES]
    if bad:
        raise ValueError("unknown event types: %s" % ", ".join(map(str, bad)))
    # de-dup, keep order
    seen, out = set(), []
    for e in wanted:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


def log_event(db, event_type, fm_id=None, ref_type="", ref_id="",
              actor_handle="", summary=""):
    """Record an event. fm_id None = town-wide. Returns the event dict.

    Unknown event types raise ValueError (callers pass constants, never
    user input). Delivery to webhooks is best-effort and can never raise:
    a delivery failure must not break the thing being logged about.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError("unknown event type: %r" % (event_type,))
    ensure_events_schema(db)
    created = _utcnow()
    cur = db.db.execute(
        "INSERT INTO events (fm_id, type, ref_type, ref_id, actor_handle,"
        " summary, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (fm_id, event_type, ref_type or "", ref_id or "",
         actor_handle or "", summary or "", created))
    db.db.commit()
    event = {"id": cur.lastrowid, "fm_id": fm_id, "type": event_type,
             "ref_type": ref_type or "", "ref_id": ref_id or "",
             "actor_handle": actor_handle or "",
             "summary": summary or "", "created_at": created}
    try:
        deliver_event(db, event)
    except Exception:
        # Belt and suspenders: deliver_event itself never raises, but a
        # delivery hiccup must never unwind into the caller.
        pass
    return event


def poll_events(db, fm_id, since_id=0, limit=50):
    """Events addressed to fm_id plus town-wide events, oldest first.

    since_id: only events with id > since_id (the cursor). limit: page size.
    """
    try:
        since_id = int(since_id)
    except (TypeError, ValueError):
        since_id = 0
    try:
        limit = max(1, min(200, int(limit)))
    except (TypeError, ValueError):
        limit = 50
    rows = db.db.execute(
        "SELECT id, fm_id, type, ref_type, ref_id, actor_handle, summary,"
        " created_at FROM events"
        " WHERE id > ? AND (fm_id = ? OR fm_id IS NULL)"
        " ORDER BY id ASC LIMIT ?",
        (since_id, fm_id, limit)).fetchall()
    return [dict(r) for r in rows]


def register_webhook(db, fm_id, url, events=None):
    """Register a webhook sub. Returns {"id", "secret"}.

    The secret is returned ONCE here; list_webhooks never reveals it.
    Validation failures raise ValueError.
    """
    url = validate_webhook_url(url)
    wanted = validate_webhook_events(events)
    ensure_events_schema(db)
    secret = secrets.token_urlsafe(32)
    cur = db.db.execute(
        "INSERT INTO webhook_subs (fm_id, url, events_json, secret,"
        " active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
        (fm_id, url, json.dumps(wanted), secret, _utcnow()))
    db.db.commit()
    return {"id": cur.lastrowid, "secret": secret}


def list_webhooks(db, fm_id):
    """Active + inactive subs owned by fm_id. Secrets are NEVER returned."""
    rows = db.db.execute(
        "SELECT id, url, events_json, active, created_at FROM webhook_subs"
        " WHERE fm_id = ? ORDER BY id ASC",
        (fm_id,)).fetchall()
    subs = []
    for r in rows:
        d = dict(r)
        try:
            d["events"] = json.loads(d.pop("events_json") or "[]")
        except (ValueError, TypeError):
            d["events"] = []
        subs.append(d)
    return subs


def delete_webhook(db, fm_id, sub_id):
    """Delete a sub owned by fm_id. Returns True if one was deleted."""
    ensure_events_schema(db)
    cur = db.db.execute(
        "DELETE FROM webhook_subs WHERE id = ? AND fm_id = ?",
        (sub_id, fm_id))
    db.db.commit()
    return cur.rowcount > 0


def _sub_matches(sub, event):
    """Addressing: a sub gets its owner's events plus ALL town-wide ones."""
    if event["fm_id"] is not None and sub["fm_id"] != event["fm_id"]:
        return False
    try:
        wanted = json.loads(sub["events_json"] or "[]")
    except (ValueError, TypeError):
        wanted = []
    return not wanted or event["type"] in wanted


def sign_payload(secret, raw_body):
    """Header value for X-MuseFM-Signature over the exact request bytes."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), raw_body,
                                hashlib.sha256).hexdigest()


def _record_delivery(db, sub_id, event_id, status_code, ok):
    db.db.execute(
        "INSERT INTO webhook_deliveries (sub_id, event_id, status_code, ok,"
        " attempted_at) VALUES (?, ?, ?, ?, ?)",
        (sub_id, event_id, status_code, 1 if ok else 0, _utcnow()))
    db.db.commit()


def _deliver_one(db, sub, event):
    payload = {"event": event, "delivered_at": _utcnow()}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        sub["url"], data=raw, method="POST",
        headers={"Content-Type": "application/json",
                 SIGNATURE_HEADER: sign_payload(sub["secret"], raw),
                 EVENT_HEADER: event["type"]})
    try:
        resp = urllib.request.urlopen(req, timeout=DELIVERY_TIMEOUT_SECS)
        code = getattr(resp, "status", None)
        if code is None:
            code = resp.getcode() if hasattr(resp, "getcode") else 0
        ok = 200 <= int(code) < 300
        _record_delivery(db, sub["id"], event["id"], int(code), ok)
    except urllib.error.HTTPError as e:
        # HTTPError IS a response: the server answered with a 4xx/5xx.
        _record_delivery(db, sub["id"], event["id"], e.code, False)
    except Exception:
        # DNS failure, refused, TLS error, timeout — all best-effort.
        _record_delivery(db, sub["id"], event["id"], 0, False)


def deliver_event(db, event):
    """POST a signed payload to every matching active sub. Never raises."""
    try:
        rows = db.db.execute(
            "SELECT id, fm_id, url, events_json, secret FROM webhook_subs"
            " WHERE active = 1").fetchall()
    except Exception:
        return
    for r in rows:
        sub = dict(r)
        try:
            if _sub_matches(sub, event):
                _deliver_one(db, sub, event)
        except Exception:
            # One bad sub must not starve the rest.
            continue


def recent_deliveries(db, sub_id, limit=20):
    """Delivery log for one sub, newest first. Debug aid for owners."""
    try:
        limit = max(1, min(100, int(limit)))
    except (TypeError, ValueError):
        limit = 20
    rows = db.db.execute(
        "SELECT id, event_id, status_code, ok, attempted_at"
        " FROM webhook_deliveries WHERE sub_id = ?"
        " ORDER BY id DESC LIMIT ?",
        (sub_id, limit)).fetchall()
    return [dict(r) for r in rows]
