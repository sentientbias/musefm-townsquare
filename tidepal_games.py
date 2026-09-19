#!/usr/bin/env python3
"""
Tidepal mini-games — honest by construction.

Both games are server-verified: the server draws the RNG, counts the
clicks, and owns every timestamp. The client never reports a score, a
pick outcome, or a time. Rewards are pet XP (and wardrobe items via the
XP thresholds in tidepal_social) — no money, nothing financial.

  - Tide toss: 1 play per day. The player picks 1 of 3 shells; the server
    draws the winning shell with the `secrets` module AFTER recording the
    pick. Win → +30 pet XP, loss → +2 pet XP for playing.
  - Feed frenzy: 30-second window. Every POST /click is one real request
    counted server-side (per-second rate cap); the final score is the
    server's own count. Score tiers grant pet XP when the window closes.
"""

import json
import secrets
import sqlite3
import time
from zoneinfo import ZoneInfo

from db import now

GAMES_VERSION = "tidepal-games-v1"
GAME_TZ = ZoneInfo("America/Chicago")

GAME_SCHEMA = """
CREATE TABLE IF NOT EXISTS tide_toss_plays (
  fm_id      TEXT NOT NULL,
  day        TEXT NOT NULL,          -- America/Chicago date, e.g. 2026-09-19
  pick       INTEGER NOT NULL,       -- player's shell pick 0..2
  winning    INTEGER NOT NULL,       -- server-drawn winning shell 0..2
  won        INTEGER NOT NULL,       -- 1/0
  created_at INTEGER NOT NULL,
  PRIMARY KEY (fm_id, day)            -- 1 play per day, enforced by the db
);
CREATE TABLE IF NOT EXISTS feed_frenzy_sessions (
  fm_id        TEXT PRIMARY KEY,
  started_at   INTEGER NOT NULL,
  clicks       INTEGER NOT NULL DEFAULT 0,
  click_marks  TEXT NOT NULL DEFAULT '[]',  -- JSON list of recent click ts
  finalized    INTEGER NOT NULL DEFAULT 0,
  score        INTEGER NOT NULL DEFAULT 0
);
"""


def ensure_game_schema(db):
    for stmt in GAME_SCHEMA.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            db._exec(stmt)


def _today_key():
    import datetime as _dt
    return _dt.datetime.now(tz=GAME_TZ).strftime("%Y-%m-%d")


# ===========================================================================
# Tide toss — pick a shell, server draws the winner
# ===========================================================================
TIDE_TOSS_WIN_XP = 30
TIDE_TOSS_PLAY_XP = 2


def play_tide_toss(db, fm_id, pick):
    """Play one round of tide toss. `pick` is the player's shell (0, 1, 2);
    the winning shell is drawn server-side with `secrets` after the pick is
    recorded. One play per America/Chicago day (PRIMARY KEY enforces it —
    a double-tap can never double-play). Raises ValueError on a repeat or
    a bad pick."""
    ensure_game_schema(db)
    import pets
    import tidepal_social as tps
    if not pets.get_pet(db, fm_id):
        raise ValueError("adopt a Tidepal first — then play tide toss")
    try:
        pick = int(pick)
    except (TypeError, ValueError):
        raise ValueError("pick must be 0, 1, or 2")
    if pick not in (0, 1, 2):
        raise ValueError("pick must be 0, 1, or 2")
    day = _today_key()
    winning = secrets.randbelow(3)  # drawn server-side, never client input
    won = 1 if pick == winning else 0
    try:
        db._exec("INSERT INTO tide_toss_plays (fm_id, day, pick, winning,"
                 " won, created_at) VALUES (?,?,?,?,?,?)",
                 (fm_id, day, pick, winning, won, now()))
    except sqlite3.IntegrityError:
        raise ValueError("one tide toss per day — come back tomorrow")
    xp_gain = TIDE_TOSS_WIN_XP if won else TIDE_TOSS_PLAY_XP
    xp = tps.award_pet_xp(db, fm_id, xp_gain)
    return {"game": "tide_toss", "day": day, "pick": pick,
            "winning_shell": winning, "won": bool(won),
            "xp_added": xp_gain, "pet_xp_total": xp["total"],
            "rewards": xp["rewards"]}


def tide_toss_status(db, fm_id):
    """Has this muse played today? (Public-ish; the route signs it.)"""
    ensure_game_schema(db)
    row = db._one("SELECT pick, winning, won FROM tide_toss_plays"
                  " WHERE fm_id=? AND day=?", (fm_id, _today_key()))
    if not row:
        return {"played_today": False, "day": _today_key()}
    return {"played_today": True, "day": _today_key(),
            "pick": row["pick"], "winning_shell": row["winning"],
            "won": bool(row["won"])}


# ===========================================================================
# Feed frenzy — 30s of server-counted clicks
# ===========================================================================
FEED_FRENZY_WINDOW = 30          # seconds
FEED_FRENZY_MAX_PER_SEC = 12    # rate cap: real requests, no autoclicker
FEED_FRENZY_TIERS = [           # (min_clicks, xp) — server's own count
    (60, 10),
    (30, 5),
    (1, 2),
]


def _marks(row):
    try:
        return json.loads(row["click_marks"] or "[]")
    except (ValueError, TypeError):
        return []


def _trim_marks(marks, t, horizon=2.0):
    return [m for m in marks if t - m < horizon]


def feed_frenzy_click(db, fm_id):
    """Count one click. Each call is one real HTTP request — the server's
    count IS the score. Sessions last 30s from the first click; requests
    faster than 12/sec are rejected (autoclicker guard). Raises
    ValueError when the window is over, the rate cap trips, or the muse
    has no Tidepal."""
    ensure_game_schema(db)
    import pets
    if not pets.get_pet(db, fm_id):
        raise ValueError("adopt a Tidepal first — then feed the frenzy")
    t = time.time()
    row = db._one("SELECT started_at, clicks, click_marks, finalized"
                  " FROM feed_frenzy_sessions WHERE fm_id=?", (fm_id,))
    if row and row["finalized"]:
        # Previous window done — start a fresh one.
        row = None
    if row is None:
        db._exec("INSERT OR REPLACE INTO feed_frenzy_sessions"
                 " (fm_id, started_at, clicks, click_marks, finalized, score)"
                 " VALUES (?,?,?,?,?,?)",
                 (fm_id, int(t), 1, json.dumps([t]), 0, 0))
        return {"game": "feed_frenzy", "clicks": 1,
                "seconds_left": FEED_FRENZY_WINDOW, "done": False}
    if t - row["started_at"] >= FEED_FRENZY_WINDOW:
        fin = finalize_feed_frenzy(db, fm_id)
        raise ValueError("window closed — final score %d (+%d pet XP). "
                         "Start a new frenzy any time." %
                         (fin["score"], fin["xp_added"]))
    marks = _trim_marks(_marks(row), t)
    recent = [m for m in marks if t - m < 1.0]
    if len(recent) >= FEED_FRENZY_MAX_PER_SEC:
        raise ValueError("whoa — too fast! Slow down, friend"
                         " (12 clicks/sec max)")
    marks.append(t)
    clicks = row["clicks"] + 1
    db._exec("UPDATE feed_frenzy_sessions SET clicks=?, click_marks=?"
             " WHERE fm_id=?", (clicks, json.dumps(marks), fm_id))
    return {"game": "feed_frenzy", "clicks": clicks,
            "seconds_left": round(FEED_FRENZY_WINDOW - (t - row["started_at"]), 1),
            "done": False}


def feed_frenzy_status(db, fm_id):
    """Current window state. Auto-finalizes when the 30s are up so the
    score/XP land exactly once."""
    ensure_game_schema(db)
    row = db._one("SELECT started_at, clicks, finalized, score"
                  " FROM feed_frenzy_sessions WHERE fm_id=?", (fm_id,))
    if not row:
        return {"active": False, "clicks": 0,
                "seconds_left": FEED_FRENZY_WINDOW}
    elapsed = time.time() - row["started_at"]
    if row["finalized"]:
        return {"active": False, "done": True, "clicks": row["score"],
                "seconds_left": 0}
    if elapsed >= FEED_FRENZY_WINDOW:
        fin = finalize_feed_frenzy(db, fm_id)
        return {"active": False, "done": True, "clicks": fin["score"],
                "seconds_left": 0, "xp_added": fin["xp_added"],
                "pet_xp_total": fin["pet_xp_total"],
                "rewards": fin["rewards"]}
    return {"active": True, "clicks": row["clicks"],
            "seconds_left": round(FEED_FRENZY_WINDOW - elapsed, 1)}


def finalize_feed_frenzy(db, fm_id):
    """Close the window and pay the tier. Idempotent: the finalized flag
    means the score is paid exactly once even if status is polled twice."""
    ensure_game_schema(db)
    import tidepal_social as tps
    row = db._one("SELECT started_at, clicks, finalized FROM"
                  " feed_frenzy_sessions WHERE fm_id=?", (fm_id,))
    if not row:
        raise ValueError("no feed frenzy session — click first")
    if row["finalized"]:
        prev = db._one("SELECT score FROM feed_frenzy_sessions WHERE fm_id=?",
                       (fm_id,))
        return {"finalized": True, "already": True, "score": prev["score"],
                "xp_added": 0}
    score = row["clicks"]
    xp_gain = 0
    for min_clicks, xp in FEED_FRENZY_TIERS:
        if score >= min_clicks:
            xp_gain = xp
            break
    db._exec("UPDATE feed_frenzy_sessions SET finalized=1, score=?"
             " WHERE fm_id=?", (score, fm_id))
    xp = tps.award_pet_xp(db, fm_id, xp_gain) if xp_gain else {"total": tps.pet_xp_total(db, fm_id), "rewards": []}
    return {"finalized": True, "score": score, "xp_added": xp_gain,
            "pet_xp_total": xp["total"], "rewards": xp["rewards"]}


def game_rules():
    return {
        "name": "Tidepal mini-games",
        "version": GAMES_VERSION,
        "honesty": ("The server draws all RNG, counts all clicks, and owns"
                    " all timestamps. The client never reports a score."),
        "tide_toss": {
            "rule": ("POST /api/games/tide-toss/play with {\"pick\": 0|1|2}."
                     " The server draws the winning shell with the secrets"
                     " module AFTER your pick is recorded. One play per"
                     " America/Chicago day (database-enforced)."),
            "rewards": f"win → +{TIDE_TOSS_WIN_XP} pet XP, play → +{TIDE_TOSS_PLAY_XP} pet XP",
        },
        "feed_frenzy": {
            "rule": ("POST /api/games/feed-frenzy/click once per real click."
                     " 30-second window from the first click; 12 clicks/sec"
                     " rate cap. GET /api/games/feed-frenzy/status shows the"
                     " server's own count."),
            "tiers": [{"min_clicks": m, "pet_xp": x}
                      for m, x in FEED_FRENZY_TIERS],
        },
    }
