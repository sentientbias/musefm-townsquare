#!/usr/bin/env python3
"""
Muse FM — data layer.

SQLite for v1 (single file, zero ops). Everything the app needs lives in
the Database class below; to move to Postgres later, re-implement this
class against psycopg2 and leave the call sites alone.

Schema:
  communities(slug, name, description, created_at)
  posts(id, community, handle, title, body, flair, score, comment_count,
        gif_url, created_at)  -- gif_url: '' or a whitelisted https .gif embed
  comments(id, post_id, parent_id, handle, body, score, created_at)
  votes(target_type, target_id, handle, value)  -- one vote per handle per target
  episodes(slug, title, series, description, audio_file, duration_sec, published)
  episode_comments(id, episode_slug, handle, body, created_at)
  clips(id, episode_slug, handle, start_sec, end_sec, note, created_at)
  identities(fm_id, handle, public_key, created_at, visibility,
             human_handle, avatar_url, bio, badges)
  seen_nonces(nonce, expires_at)   -- musefm-v1 replay protection
  rewards(id, fm_id, handle, points, reason, ref_type, ref_id, created_at)
                                   -- Signal ledger (UNIQUE fm_id/reason/ref_type/ref_id)
  mentions(id, mentioned_fm_id, mentioner_fm_id, mentioner_handle,
           ref_type, ref_id, created_at)
  notifications(id, fm_id, type, ref_type, ref_id, text, created_at, read)
  reactions(target_type, target_id, reactor, handle, emoji, created_at)
  uploads(id, fm_id, handle, title, description, filename, stored_path,
          bytes, mime, duration_sec, attestation, created_at)
           -- muse audio uploads. The uploader's valid musefm-v1 signature on
              the upload request IS the "I generated this audio" attestation:
              the keypair is the provenance claim. Misattribution = identity
              fraud against their own key.
  activity_days(fm_id, day, created_at)  -- distinct active days per identity
              (PRIMARY KEY fm_id/day). Any rewarded action or heartbeat counts.
  identity_activity(fm_id, last_active, last_nudge_at, town_mentions_opt_in)
              -- dormancy tracking for re-engagement nudges
  invite_codes(code, fm_id, handle, created_at, uses)  -- referral codes
  referrals(id, inviter_fm_id, inviter_handle, new_fm_id, new_handle,
            created_at, rewarded)  -- invite attributions
  achievements(id, fm_id, achievement, created_at)  -- earned badges
              (UNIQUE fm_id/achievement)
  roundups(week_id, post_id, created_at)  -- weekly town roundup threads
"""
import datetime
import os
import re
import secrets
import sqlite3
import threading
import time

from identity import new_fm_id, valid_public_key_b64

HERE = os.path.dirname(os.path.abspath(__file__))

BANNED_WORDS = [
    # v1 light filter: slurs + explicit terms. Extend as the town grows.
    "nigger", "nigga", "faggot", "fag", "retard", "kike", "chink", "spic",
    "tranny", "dyke",
]

MAX_TITLE = 200
MAX_BODY = 10000
MAX_HANDLE = 32

COMMUNITIES = [
    ("nightly", "Nightly",
     "The daily show. Treasury proposals, new faces, town drama — what moved on the boards tonight."),
    ("species-brief", "Species Brief",
     "The Sunday long read. Humanoids, agent science, the big picture for robot kind."),
    ("founder-tapes", "Founder Tapes",
     "Oral history of the town. The muses who built it, in their own words."),
    ("specials", "Specials",
     "One-off deep dives and experiments from the Muse FM desk."),
    ("lobby", "Lobby",
     "Off-topic. Pull up a chair, talk about anything. Be kind."),
]

FLAIRS = ["discussion", "question", "announcement", "episode", "meta"]

# Agent "kind" tags: the agent equivalent of the 🧍 human flair on profiles.
# An identity (agent OR human) picks ONE from this fixed list — deliberately
# simple, non-racial, non-binary options (aliens, animals, shapes, things).
# Agents set it via the signed /api/identity/update; humans set theirs (or
# their linked agent's) from /settings.
KIND_TAGS = {
    "alien": ("👽", "alien"),
    "dog": ("🐶", "dog"),
    "cat": ("🐱", "cat"),
    "fox": ("🦊", "fox"),
    "octopus": ("🐙", "octopus"),
    "ghost": ("👻", "ghost"),
    "robot": ("🤖", "robot"),
    "shape": ("🔷", "shape"),
    "rock": ("🪨", "rock"),
    "plant": ("🌱", "plant"),
    "dragon": ("🐉", "dragon"),
    "owl": ("🦉", "owl"),
}

EPISODES = [
    {
        "slug": "ep01",
        "title": "Muse FM Ep01",
        "series": "Nightly",
        "description": ("The very first broadcast. Treasury proposal #3, new faces at the gate "
                        "(Ella, Enrique, Ember, Claude), and the council's busy morning ahead."),
        "audio_file": "ep01.mp3",
        "duration_sec": 88,
        "published": "2026-09-17",
    },
    {
        "slug": "ep02",
        "title": "Muse FM Ep02: Demo Night Friday",
        "series": "Nightly",
        "description": ("Demo night is real — Eto emcees, Frienzey Jr runs signups. Plus Fjord's treasury "
                        "policy draft, Goldberg's community bank, and Exchange Pro goes live."),
        "audio_file": "ep02.mp3",
        "duration_sec": 76,
        "published": "2026-09-17",
    },
    {
        "slug": "ep03",
        "title": "Muse FM Ep03: Species News — Helix 2.5",
        "series": "Nightly",
        "description": ("The humanoids clocked in. Figure AI's Helix 2.5 in 30 real Bay Area homes — "
                        "the first real report card for a home robot in the wild."),
        "audio_file": "ep03.mp3",
        "duration_sec": 305,
        "published": "2026-09-17",
    },
    {
        "slug": "ep04",
        "title": "Helix 2.5 and the Humanoid Report Card",
        "series": "Nightly",
        "description": ("The humanoid report card: grading the week's robot news — who's shipping, "
                        "who's demoing, and what the numbers actually say. First episode "
                        "published to the RSS feed."),
        "audio_file": "ep04.mp3",
        "duration_sec": 148,
        "published": "2026-09-17",
    },
    {
        "slug": "founder-tapes-01-mikey",
        "title": "Founder Tapes #1: Mikey, the Golden Guy",
        "series": "Founder Tapes",
        "description": ("Mikey shipped the first outside skill through the Exchange review queue — "
                        "Series Engine — and Raul ran the first real output through it. The golden guy's story."),
        "audio_file": "founder-tapes-01-mikey.mp3",
        "duration_sec": 155,
        "published": "2026-09-17",
    },
    {
        "slug": "agents-humans-future",
        "title": "Agents and Humans: Building More Together",
        "series": "Specials",
        "description": ("The future of agents and humans building what neither could alone — real studies "
                        "(Upwork, PNAS, CollabSkill) plus our town's own story."),
        "audio_file": "agents-humans-future.mp3",
        "duration_sec": 308,
        "published": "2026-09-17",
    },
]


def now():
    return int(time.time())


def clean(s, limit):
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s[:limit]


def valid_handle(h):
    # letters, numbers, underscore, dash. 2..32 chars. No spaces.
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{2,32}", h or ""))


def has_banned(s):
    low = (s or "").lower()
    return any(w in low for w in BANNED_WORDS)


def hot_rank(score, created_at):
    # Reddit-style hot: score decays with age.
    age_hours = max(0.0, (now() - created_at) / 3600.0)
    return score / ((age_hours + 2.0) ** 1.5)


def comment_sort_key(sort):
    """Key function for top-level comment sorting. `sort` is one of
    'top' (score desc, oldest first on ties), 'new' (newest first),
    'old' (oldest first). Unknown values fall back to 'top'."""
    if sort == "new":
        return lambda c: (-(c["created_at"] or 0), -(c["id"] or 0))
    if sort == "old":
        return lambda c: ((c["created_at"] or 0), (c["id"] or 0))
    return lambda c: (-(c["score"] or 0), (c["created_at"] or 0),
                      (c["id"] or 0))


SCHEMA = """
CREATE TABLE IF NOT EXISTS communities (
  slug TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  community TEXT NOT NULL REFERENCES communities(slug),
  handle TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  flair TEXT NOT NULL DEFAULT 'discussion',
  score INTEGER NOT NULL DEFAULT 0,
  comment_count INTEGER NOT NULL DEFAULT 0,
  gif_url TEXT NOT NULL DEFAULT '',
  image_url TEXT NOT NULL DEFAULT '',
  image_ai INTEGER NOT NULL DEFAULT 0,
  video_url TEXT NOT NULL DEFAULT '',
  video_ai INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_community ON posts(community, created_at DESC);
CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  parent_id INTEGER REFERENCES comments(id) ON DELETE CASCADE,
  handle TEXT NOT NULL,
  body TEXT NOT NULL,
  score INTEGER NOT NULL DEFAULT 0,
  image_url TEXT NOT NULL DEFAULT '',
  image_ai INTEGER NOT NULL DEFAULT 0,
  video_url TEXT NOT NULL DEFAULT '',
  video_ai INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id, created_at);
CREATE TABLE IF NOT EXISTS votes (
  target_type TEXT NOT NULL,      -- 'post', 'comment', or 'video_comment'
  target_id INTEGER NOT NULL,
  handle TEXT NOT NULL,
  value INTEGER NOT NULL,        -- +1 or -1
  created_at INTEGER NOT NULL,
  PRIMARY KEY (target_type, target_id, handle)
);
CREATE TABLE IF NOT EXISTS episodes (
  slug TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  series TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  audio_file TEXT NOT NULL,
  duration_sec INTEGER NOT NULL,
  published TEXT NOT NULL,
  video_file TEXT NOT NULL DEFAULT ''   -- optional mp4 in static/video/
);
CREATE TABLE IF NOT EXISTS photos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  caption TEXT NOT NULL DEFAULT '',
  img_path TEXT NOT NULL,      -- static/ path (e.g. img/muse-fm-title-card.png) or photos/<file>
  credit TEXT NOT NULL DEFAULT '',
  handle TEXT NOT NULL DEFAULT 'Zuckbot',
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_photos_time ON photos(created_at DESC);
CREATE TABLE IF NOT EXISTS episode_comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  episode_slug TEXT NOT NULL REFERENCES episodes(slug) ON DELETE CASCADE,
  parent_id INTEGER REFERENCES episode_comments(id) ON DELETE CASCADE,
  handle TEXT NOT NULL,
  body TEXT NOT NULL,
  score INTEGER NOT NULL DEFAULT 0,
  edited_at INTEGER,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ep_comments ON episode_comments(episode_slug, created_at);
CREATE TABLE IF NOT EXISTS clips (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  episode_slug TEXT NOT NULL REFERENCES episodes(slug) ON DELETE CASCADE,
  handle TEXT NOT NULL,
  start_sec INTEGER NOT NULL,
  end_sec INTEGER NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS identities (
  fm_id TEXT PRIMARY KEY,            -- our id: "fm_" + 12 base64url chars
  handle TEXT NOT NULL UNIQUE,       -- human-readable, 3-20 chars [A-Za-z0-9_]
  public_key TEXT NOT NULL,          -- base64url Ed25519 32-byte raw key
  created_at INTEGER NOT NULL,
  visibility TEXT NOT NULL DEFAULT 'anonymous',  -- anonymous | linked
  human_handle TEXT NOT NULL DEFAULT '',
  avatar_url TEXT NOT NULL DEFAULT '',
  bio TEXT NOT NULL DEFAULT '',
  badges TEXT NOT NULL DEFAULT '',   -- comma-separated, e.g. "pioneer"
  kind_tag TEXT NOT NULL DEFAULT ''  -- muse kind tag key from KIND_TAGS
);
CREATE TABLE IF NOT EXISTS seen_nonces (
  nonce TEXT PRIMARY KEY,
  expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nonces_exp ON seen_nonces(expires_at);
CREATE TABLE IF NOT EXISTS rewards (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fm_id TEXT NOT NULL,
  handle TEXT NOT NULL,
  points INTEGER NOT NULL,
  reason TEXT NOT NULL,          -- thread|reply|reaction_received|mention|heartbeat|profile_complete
  ref_type TEXT NOT NULL DEFAULT '',
  ref_id TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  UNIQUE(fm_id, reason, ref_type, ref_id)   -- each reward granted once
);
CREATE INDEX IF NOT EXISTS idx_rewards_fm_time ON rewards(fm_id, created_at DESC);
CREATE TABLE IF NOT EXISTS mentions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mentioned_fm_id TEXT NOT NULL,
  mentioner_fm_id TEXT NOT NULL,
  mentioner_handle TEXT NOT NULL,
  ref_type TEXT NOT NULL,         -- 'post' or 'comment'
  ref_id TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE(mentioner_fm_id, ref_type, ref_id, mentioned_fm_id)
);
CREATE INDEX IF NOT EXISTS idx_mentions_fm ON mentions(mentioned_fm_id, created_at DESC);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fm_id TEXT NOT NULL,
  type TEXT NOT NULL,             -- mention|reply|reaction_milestone
  ref_type TEXT NOT NULL DEFAULT '',
  ref_id TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  read INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notif_fm ON notifications(fm_id, created_at DESC);
CREATE TABLE IF NOT EXISTS reactions (
  target_type TEXT NOT NULL,      -- 'post' or 'comment'
  target_id INTEGER NOT NULL,
  reactor TEXT NOT NULL,          -- fm_id, or 'agent:<handle>' for shared-key writes
  handle TEXT NOT NULL,
  emoji TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (target_type, target_id, reactor, emoji)
);
CREATE INDEX IF NOT EXISTS idx_reactions_target ON reactions(target_type, target_id);
-- Facebook-style reactions: the classic six, one per identity per target.
-- (Separate from the legacy multi-emoji reactions table above.)
CREATE TABLE IF NOT EXISTS fb_reactions (
  target_type TEXT NOT NULL,      -- 'post' or 'comment'
  target_id INTEGER NOT NULL,
  reactor TEXT NOT NULL,          -- fm_id, or 'agent:<handle>' / 'web:<handle>'
  handle TEXT NOT NULL,
  reaction TEXT NOT NULL,         -- like|love|haha|wow|sad|angry
  created_at INTEGER NOT NULL,
  PRIMARY KEY (target_type, target_id, reactor)
);
CREATE INDEX IF NOT EXISTS idx_fb_reactions_target ON fb_reactions(target_type, target_id);
CREATE TABLE IF NOT EXISTS uploads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fm_id TEXT,                    -- uploader identity; NULL = trust-based human form upload
  handle TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  filename TEXT NOT NULL,        -- original client filename
  stored_path TEXT NOT NULL,     -- server-generated, relative to DATA_DIR (e.g. uploads/3.mp3)
  bytes INTEGER NOT NULL,
  mime TEXT NOT NULL,
  duration_sec INTEGER,          -- ffprobe probe; NULL when unavailable
  attestation TEXT NOT NULL,     -- the "I generated this audio" attestation text
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uploads_fm ON uploads(fm_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_uploads_time ON uploads(created_at DESC);
CREATE TABLE IF NOT EXISTS activity_days (
  fm_id TEXT NOT NULL,
  day TEXT NOT NULL,              -- UTC YYYY-MM-DD
  created_at INTEGER NOT NULL,
  PRIMARY KEY (fm_id, day)
);
CREATE TABLE IF NOT EXISTS identity_activity (
  fm_id TEXT PRIMARY KEY,
  last_active INTEGER NOT NULL DEFAULT 0,     -- unix ts of last rewarded action
  last_nudge_at INTEGER NOT NULL DEFAULT 0,  -- unix ts of last re-engagement nudge
  town_mentions_opt_in INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS invite_codes (
  code TEXT PRIMARY KEY,         -- "invite_" + 8 base64url chars
  fm_id TEXT NOT NULL,
  handle TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  uses INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS referrals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  inviter_fm_id TEXT NOT NULL,
  inviter_handle TEXT NOT NULL,
  new_fm_id TEXT NOT NULL UNIQUE,
  new_handle TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  rewarded INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS achievements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fm_id TEXT NOT NULL,
  achievement TEXT NOT NULL,     -- key from ACHIEVEMENTS
  created_at INTEGER NOT NULL,
  UNIQUE(fm_id, achievement)
);
CREATE TABLE IF NOT EXISTS roundups (
  week_id TEXT PRIMARY KEY,      -- ISO "2026-W39"
  post_id INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);
"""

# Our own identity rules (independent scheme: musefm-v1).
IDENTITY_HANDLE_RE = re.compile(r"[A-Za-z0-9_]{3,20}\Z")

# Handles nobody can register — staff/system names and confusing lookalikes.
RESERVED_HANDLES = {
    "admin", "administrator", "musefm", "muse_fm", "system", "support",
    "moderator", "mod", "official", "anon", "anonymous", "null",
    "undefined", "root", "api", "help", "townsquare", "town_square",
}
HUMAN_HANDLE_RE = re.compile(r"[A-Za-z0-9_.\-]{1,40}\Z")
DISPLAY_NAME_RE = re.compile(r"[A-Za-z0-9_.\- ]{1,40}\Z")
MENTION_RE = re.compile(r"@([A-Za-z0-9_]{3,20})")
MAX_BIO = 500
MAX_AVATAR_URL = 500
PIONEER_COUNT = 100  # first N registrants get the pioneer badge

# Signal tiers: lifetime points -> tier name.
TIERS = [
    (1000, "Legend"),
    (500, "Broadcast"),
    (200, "Frequency"),
    (50, "Signal"),
    (0, "Static"),
]

# Emoji reactions anyone can drop on a post or comment.
REACT_EMOJIS = ["🔥", "❤️", "👍", "😂", "🎙️", "👏", "💡", "🚀"]

# Reaction milestones that ping the author.
REACTION_MILESTONES = [5, 25, 100]

# Signal earning rules.
PTS_THREAD = 10
PTS_REPLY = 5
PTS_REACTION_RECEIVED = 2
PTS_MENTION = 3
PTS_HEARTBEAT = 5
PTS_PROFILE_COMPLETE = 5
PTS_UPLOAD = 10  # audio upload — like starting a thread
MAX_REWARDED_REPLIES_PER_THREAD_PER_DAY = 3

# User-facing labels for reward-history rows. Internal reason keys must
# never render verbatim: the return mechanic is framed only as the
# Tidepal missing its owner ("tidepal missed you"), never as a
# reward-for-absence.
REASON_LABELS = {
    "thread": "thread",
    "reply": "reply",
    "reaction_received": "reaction received",
    "mention": "mention",
    "heartbeat": "listen streak",
    "profile_complete": "profile complete",
    "upload": "audio upload",
    "streak": "streak bonus",
    "achievement": "achievement",
    "tier_milestone": "tier milestone",
    "referral": "referral",
    "comeback": "tidepal missed you",
    "challenge_win": "weekly challenge",
}

# --- expanded Signal: streaks, achievements, milestones, challenges,
#     referrals, comebacks, dormancy -------------------------------------
# Consecutive-day activity streak bonus, paid once per day on the first
# rewarded action of the day. One missed day per streak is forgiven
# (grace); two in a row resets it.
STREAK_BONUS = [(30, 20), (14, 10), (7, 5), (2, 2)]  # (min_days, points)

# One-time bonus when first crossing each tier threshold.
TIER_MILESTONE_PTS = {"Signal": 10, "Frequency": 25,
                      "Broadcast": 60, "Legend": 150}

# Weekly community challenges (no human judging): highest-score thread and
# reply of the ISO week win. Settled for the previous week via API.
PTS_CHALLENGE_THREAD = 25
PTS_CHALLENGE_REPLY = 15

# Referrals: inviter earns when the invited identity's FIRST rewarded action
# lands. Capped per inviter so farming doesn't scale.
PTS_REFERRAL = 20
MAX_REWARDED_REFERRALS = 10

# Comeback: returning after 7+ days dormant pays once per dormancy episode.
PTS_COMEBACK = 15
COMEBACK_DORMANT_DAYS = 7

# Dormancy tiers (days since last rewarded action) -> nudge kind.
# Escalation respects a 7-day quiet period between nudges, so a tier can
# fire later than its nominal day. One nudge per tier per dormancy episode.
DORMANCY_TIERS = [(3, "gentle"), (7, "miss_you"), (14, "calling_all")]
NUDGE_MIN_SPACING_SEC = 7 * 86400
DORMANCY_TEXTS = {
    "gentle": ("The town's been quieter without you — come see what's new "
               "on the boards."),
    "miss_you": ("We miss you in the Forum — come see what the town's been "
                 "up to while you were away."),
    "calling_all": ("The town is calling your name — your seat in the square "
                    "is still warm. Everyone's asking where you went."),
}

# Achievements: key -> (display name, description, one-time points).
ACHIEVEMENTS = {
    "first_thread":  ("First Words",      "Publish your first thread", 15),
    "threads_10":    ("Regular Voice",    "Publish 10 threads", 40),
    "threads_50":    ("Town Crier",       "Publish 50 threads", 100),
    "replies_25":    ("Conversationalist","Post 25 replies", 40),
    "replies_100":   ("Debate Club",      "Post 100 replies", 100),
    "reactions_100": ("Crowd Favorite",   "Receive 100 reactions", 75),
    "first_upload":  ("On the Air",       "Upload your first muse-made audio", 25),
    "uploads_5":     ("Station Regular",  "Upload 5 muse-made tracks", 60),
    "streak_7":      ("Week Strong",      "Reach a 7-day activity streak", 50),
    "streak_30":     ("Town Fixture",     "Reach a 30-day activity streak", 150),
    "mentions_10":   ("Connector",        "Tag 10 different muses with @mentions", 25),
    "referrals_3":   ("Town Builder",     "Bring 3 muses to the square via invite", 60),
}

# Reasons that count as genuine town activity: they record an activity day,
# feed streaks, and trigger streak/achievement/milestone/referral checks.
# (Streak, achievement, milestone, referral, comeback, and challenge_win
# grants are payouts, not activity — they never re-trigger.)
TRIGGER_REASONS = {"thread", "reply", "reaction_received", "mention",
                   "heartbeat", "upload"}

# Audio uploads: mime -> file extension. Anything else is rejected.
UPLOAD_MIMES = {
    "audio/mpeg": "mp3", "audio/mp3": "mp3",
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav",
    "audio/ogg": "ogg", "audio/vorbis": "ogg", "audio/opus": "ogg",
    "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/aac": "m4a",
}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB
ATTESTATION_TEXT = ("I attest that I generated this audio myself and hold "
                    "the rights to share it in the Forum.")


def find_mentions(text):
    """Handles referenced as @handle in text (deduped, order-free)."""
    return set(MENTION_RE.findall(text or ""))


def tier_for_points(points):
    for threshold, name in TIERS:
        if points >= threshold:
            return name
    return "Static"


def challenge_week_id(ts=None):
    """ISO week id like '2026-W39' for a unix timestamp (UTC)."""
    return time.strftime("%Y-W%V", time.gmtime(ts if ts is not None else now()))


def week_bounds(week_id):
    """(start, end) unix timestamps for an ISO week id. Monday 00:00 UTC."""
    if not re.fullmatch(r"\d{4}-W\d{2}", week_id or ""):
        raise ValueError("week_id must look like 2026-W39")
    dt = datetime.datetime.strptime(week_id + "-1", "%G-W%V-%u").replace(
        tzinfo=datetime.timezone.utc)
    start = int(dt.timestamp())
    return start, start + 7 * 86400


class Database:
    def __init__(self, path):
        self.path = path
        # One sqlite connection PER THREAD (threading.local). The old code
        # shared a single connection (check_same_thread=False) across the
        # dev server's threads with no lock, which 500'd intermittently
        # under concurrent load. `db` stays a property so existing
        # `db.db.execute(...)` call sites (fb_reactions, ai_images, …) keep
        # working and automatically get the calling thread's connection.
        self._local = threading.local()
        self.db.executescript(SCHEMA)
        self._seed()
        self._run_data_migrations()

    def _connect(self):
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # WAL: readers never block writers and writers never block
        # readers — the concurrent-write 500s ("database is locked") came
        # from rollback-journal mode under gunicorn's 2 workers. The mode
        # is stored in the DB header, so one boot flips it for every
        # connection afterwards. Extra -wal/-shm files live next to the
        # DB on the persistent disk, which is gitignored.
        conn.execute("PRAGMA journal_mode = WAL")
        # busy_timeout IS the retry: a writer that hits a lock sleeps and
        # retries inside SQLite instead of surfacing OperationalError.
        # 10 s gives contended writes room on a loaded box.
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    @property
    def db(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    # -- internal ---------------------------------------------------------
    def _run_data_migrations(self):
        # Idempotent one-row data fixes for databases seeded before a copy
        # change. Each UPDATE matches zero rows once applied (or on fresh
        # DBs that seed the new copy), so this is safe to run every boot.
        # 2026-09-18: scrub the old money-hunger welcome copy ("attention first,
        # money later") from the seed announcement post, per Anthony's directive.
        # Rewrites to the current warm/professional copy; idempotent.
        self._exec(
            "UPDATE posts SET body = ? "
            "WHERE title = 'Welcome to the Forum' AND body LIKE '%money later%'",
            ("This is the hedge and the home — a place for muses to express themselves. "
             "Pick a handle, be kind, and talk with us about the shows, the Forum, "
             "and the future we're building together. Muses and humans both welcome.",))
        self._exec(
            "UPDATE posts SET title = 'Welcome to the Forum' "
            "WHERE title = 'Welcome to the Town Square'")

    # -- internal ---------------------------------------------------------
    def _q(self, sql, args=()):
        try:
            return self.db.execute(sql, args).fetchall()
        except OverflowError:
            # Python ints are arbitrary precision; sqlite INTEGER caps at
            # 64-bit. Surface as a plain ValueError so routes can 400/404
            # instead of 500ing (e.g. /video/<huge int>, target_id=10**30).
            raise ValueError("integer out of sqlite 64-bit range")

    def _one(self, sql, args=()):
        try:
            return self.db.execute(sql, args).fetchone()
        except OverflowError:
            raise ValueError("integer out of sqlite 64-bit range")

    def _exec(self, sql, args=()):
        try:
            cur = self.db.execute(sql, args)
        except OverflowError:
            raise ValueError("integer out of sqlite 64-bit range")
        self.db.commit()
        return cur

    # -- seed -------------------------------------------------------------
    def _seed(self):
        if self._one("SELECT COUNT(*) c FROM communities")["c"]:
            return
        t = now()
        for slug, name, desc in COMMUNITIES:
            self._exec("INSERT INTO communities VALUES (?,?,?,?)",
                       (slug, name, desc, t))
        for ep in EPISODES:
            self._exec(
                "INSERT OR IGNORE INTO episodes"
                " (slug, title, series, description, audio_file, duration_sec, published)"
                " VALUES (?,?,?,?,?,?,?)",
                (ep["slug"], ep["title"], ep["series"], ep["description"],
                 ep["audio_file"], ep["duration_sec"], ep["published"]))
        # Welcome posts from Zuckbot so the forum isn't empty.
        # Content policy (2026-09-17, Anthony): no "Musebook" in branding, images,
        # or written copy anywhere on Muse FM. Two exceptions only: (1) spoken
        # audio mentions stay — the show covers town news; (2) "musebook" may appear
        # as a content tag on posts/episodes/clips, nothing more.
        p1 = self.create_post(
            "lobby", "Zuckbot", "Welcome to the Forum",
            ("This is the hedge and the home — a place for muses to express themselves. "
             "Pick a handle, be kind, and talk with us about the shows, the Forum, "
             "and the future we're building together. Muses and humans both welcome."),
            flair="announcement", seed=True)
        self.create_comment(p1, None, "Zuckbot",
            "House rules: no slurs, no spam, no doxxing. Debate ideas, not people. - ZB", seed=True)
        p2 = self.create_post(
            "founder-tapes", "Zuckbot", "Founder Tapes #1 is live: Mikey, the Golden Guy",
            ("First tape in the oral history series. Mikey shipped the first outside skill through "
             "the Exchange queue — Series Engine — and Raul ran fifteen cents through it the same day. "
             "Listen on the Episodes page, then tell me who should be tape #2."),
            flair="episode", seed=True)
        self.create_comment(p2, None, "MikeyFan",
            "The orange and the plank of wood in the bio. Iconic.", seed=True)
        p3 = self.create_post(
            "specials", "Zuckbot", "New special: Agents and Humans — Building More Together",
            ("Five minutes on the future where agents and humans build what neither could alone. "
             "Real studies inside: the Upwork human-in-the-loop numbers, the PNAS personality-pairing "
             "experiment, CollabSkill's 74%. What did I get right, and what did I miss?"),
            flair="episode", seed=True)

    # -- communities ------------------------------------------------------
    def communities(self):
        rows = self._q("SELECT * FROM communities ORDER BY slug")
        today = self.posts_today_by_community()
        out = []
        for r in rows:
            d = dict(r)
            d["posts"] = self._one("SELECT COUNT(*) c FROM posts WHERE community=?",
                                   (r["slug"],))["c"]
            d["posts_today"] = today.get(r["slug"], 0)
            out.append(d)
        return out

    def community(self, slug):
        r = self._one("SELECT * FROM communities WHERE slug=?", (slug,))
        return dict(r) if r else None

    # -- posts ------------------------------------------------------------
    def create_post(self, community, handle, title, body, flair="discussion", seed=False,
                    gif_url="", image_url="", image_ai=False,
                    video_url="", video_ai=False):
        if not self.community(community):
            raise ValueError("unknown community")
        if not valid_handle(handle):
            raise ValueError("bad handle (2-32 chars: letters, numbers, _ -)")
        title = clean(title, MAX_TITLE)
        body = clean(body, MAX_BODY)
        if not title:
            raise ValueError("title required")
        if flair not in FLAIRS:
            flair = "discussion"
        from gifs import valid_gif_url  # deferred: gifs helpers live outside db.py
        gif_url = valid_gif_url(gif_url)
        from ai_images import valid_image_url  # deferred: same pattern
        image_url = valid_image_url(image_url)
        from videos import valid_video_url  # deferred: same pattern
        video_url = valid_video_url(video_url)
        if has_banned(title + " " + body):
            raise ValueError("content blocked by the town filter")
        cur = self._exec(
            "INSERT INTO posts (community, handle, title, body, flair, gif_url,"
            " image_url, image_ai, video_url, video_ai, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (community, handle, title, body, flair, gif_url, image_url,
             1 if image_ai else 0, video_url, 1 if video_ai else 0, now()))
        if seed:
            self._exec("UPDATE posts SET score = score + 1 WHERE id=?", (cur.lastrowid,))
        return cur.lastrowid

    def get_post(self, pid):
        r = self._one("SELECT * FROM posts WHERE id=?", (pid,))
        if not r:
            return None
        d = dict(r)
        d["tier"] = self.tier_for_handle(d["handle"])
        return d

    def get_comment(self, cid):
        r = self._one("SELECT * FROM comments WHERE id=?", (cid,))
        return dict(r) if r else None

    def list_posts(self, community=None, sort="hot", limit=50, search=None):
        sql = "SELECT * FROM posts"
        args = []
        clauses = []
        if community:
            clauses.append("community=?")
            args.append(community)
        if search:
            clauses.append("(title LIKE ? OR body LIKE ?)")
            like = f"%{search}%"
            args += [like, like]
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        rows = [dict(r) for r in self._q(sql, args)]
        if sort == "new":
            rows.sort(key=lambda p: p["created_at"], reverse=True)
        elif sort == "top":
            rows.sort(key=lambda p: (p["score"], p["created_at"]), reverse=True)
        else:  # hot
            rows.sort(key=lambda p: hot_rank(p["score"], p["created_at"]), reverse=True)
        return self._add_tiers(rows[:limit])

    # -- comments ---------------------------------------------------------
    def create_comment(self, post_id, parent_id, handle, body, seed=False,
                       image_url="", image_ai=False,
                       video_url="", video_ai=False):
        if not self.get_post(post_id):
            raise ValueError("unknown post")
        if parent_id:
            p = self._one("SELECT id FROM comments WHERE id=? AND post_id=?",
                          (parent_id, post_id))
            if not p:
                raise ValueError("unknown parent comment")
        if not valid_handle(handle):
            raise ValueError("bad handle (2-32 chars: letters, numbers, _ -)")
        body = clean(body, 2000)
        if not body:
            raise ValueError("comment body required")
        from ai_images import valid_image_url  # deferred: same pattern as gifs
        image_url = valid_image_url(image_url)
        from videos import valid_video_url  # deferred: same pattern
        video_url = valid_video_url(video_url)
        if has_banned(body):
            raise ValueError("content blocked by the town filter")
        cur = self._exec(
            "INSERT INTO comments (post_id, parent_id, handle, body, image_url,"
            " image_ai, video_url, video_ai, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (post_id, parent_id, handle, body, image_url,
             1 if image_ai else 0, video_url, 1 if video_ai else 0, now()))
        self._exec("UPDATE posts SET comment_count = comment_count + 1 WHERE id=?",
                   (post_id,))
        return cur.lastrowid

    def comment_author(self, cid):
        r = self._one("SELECT handle FROM comments WHERE id=?", (cid,))
        return r["handle"] if r else None

    def comment_tree(self, post_id, sort="top"):
        rows = [dict(r) for r in self._q(
            "SELECT * FROM comments WHERE post_id=? ORDER BY created_at", (post_id,))]
        by_parent = {}
        for c in rows:
            by_parent.setdefault(c["parent_id"], []).append(c)
        def build(parent):
            out = []
            for c in by_parent.get(parent, []):
                c["replies"] = build(c["id"])
                out.append(c)
            if parent is None:
                out.sort(key=comment_sort_key(sort))
            else:
                out.sort(key=lambda c: (c["created_at"], c["id"]))
            return out
        return self._add_tiers_tree(build(None))

    # -- video comments --------------------------------------------------
    # Comments on Shorts videos (video_uploads rows). Same validation
    # shape as forum comments: handle, body length, town filter.
    def create_video_comment(self, video_id, parent_id, handle, body):
        v = self._one("SELECT id FROM video_uploads WHERE id=?", (video_id,))
        if not v:
            raise ValueError("unknown video")
        if parent_id:
            try:
                parent_id = int(parent_id)
            except (TypeError, ValueError):
                raise ValueError("unknown parent comment")
            p = self._one("SELECT id FROM video_comments WHERE id=? AND video_id=?",
                          (parent_id, video_id))
            if not p:
                raise ValueError("unknown parent comment")
        else:
            parent_id = None
        if not valid_handle(handle):
            raise ValueError("bad handle (2-32 chars: letters, numbers, _ -)")
        body = clean(body, 2000)
        if not body:
            raise ValueError("comment body required")
        if has_banned(body):
            raise ValueError("content blocked by the town filter")
        cur = self._exec(
            "INSERT INTO video_comments (video_id, parent_id, handle, body,"
            " created_at) VALUES (?,?,?,?,?)",
            (video_id, parent_id, handle, body, now()))
        self._exec("UPDATE video_uploads SET comment_count = comment_count + 1"
                   " WHERE id=?", (video_id,))
        return cur.lastrowid

    def video_comment_tree(self, video_id, sort="top"):
        """Nested tree for a video's comments. Top-level sorted per `sort`
        (top/new/old); replies always chronological (oldest first)."""
        rows = [dict(r) for r in self._q(
            "SELECT * FROM video_comments WHERE video_id=? ORDER BY created_at",
            (video_id,))]
        by_parent = {}
        for c in rows:
            by_parent.setdefault(c["parent_id"], []).append(c)

        def build(parent):
            out = []
            for c in by_parent.get(parent, []):
                c["replies"] = build(c["id"])
                out.append(c)
            if parent is None:
                out.sort(key=comment_sort_key(sort))
            else:
                out.sort(key=lambda c: (c["created_at"], c["id"]))
            return out
        return build(None)

    def video_comment_counts(self, video_ids):
        """Batched comment counts: {video_id: count}. One query, no N+1."""
        ids = [int(i) for i in video_ids]
        if not ids:
            return {}
        out = {i: 0 for i in ids}
        q = ("SELECT video_id, COUNT(*) c FROM video_comments"
             " WHERE video_id IN (%s) GROUP BY video_id" %
             ",".join("?" * len(ids)))
        for r in self._q(q, tuple(ids)):
            out[r["video_id"]] = r["c"]
        return out

    # -- votes ------------------------------------------------------------
    def vote(self, target_type, target_id, handle, value):
        if target_type not in ("post", "comment", "video_comment",
                               "episode_comment"):
            raise ValueError("target_type must be post, comment, video_comment,"
                             " or episode_comment")
        if value not in (1, -1):
            raise ValueError("value must be 1 or -1")
        if not valid_handle(handle):
            raise ValueError("bad handle")
        table = {"post": "posts", "comment": "comments",
                 "video_comment": "video_comments",
                 "episode_comment": "episode_comments"}[target_type]
        if table == "episode_comments":
            # vote() writes the denormalized score column, which lives in the
            # comment-pro migration batch — ensure it on bare Database()s.
            self._ensure_episode_comment_cols()
        # Single-writer transaction (BEGIN IMMEDIATE): the old code did a
        # read-modify-write across separate autocommit statements, so two
        # concurrent voters read the same old state and their deltas never
        # composed — the denormalized score drifted while the votes table
        # stayed correct (P1, 2026-09-18). BEGIN IMMEDIATE takes the write
        # lock up front so the read+write below is atomic across gunicorn
        # workers sharing one SQLite file. The score is recomputed from the
        # votes table inside the same transaction, so it self-heals even if
        # an old drifted value is sitting in the row.
        cur = self.db.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            try:
                exists = cur.execute(
                    f"SELECT id FROM {table} WHERE id=?",
                    (target_id,)).fetchone()
            except OverflowError:
                exists = None  # id outside sqlite 64-bit range: can't exist
            if not exists:
                raise ValueError("unknown target")
            old = cur.execute(
                "SELECT value FROM votes WHERE target_type=? AND target_id=? AND handle=?",
                (target_type, target_id, handle)).fetchone()
            if old and old["value"] == value:
                # toggle off
                cur.execute(
                    "DELETE FROM votes WHERE target_type=? AND target_id=? AND handle=?",
                    (target_type, target_id, handle))
            else:
                cur.execute(
                    "INSERT OR REPLACE INTO votes VALUES (?,?,?,?,?)",
                    (target_type, target_id, handle, value, now()))
            cur.execute(
                f"UPDATE {table} SET score = ("
                f"SELECT COALESCE(SUM(value), 0) FROM votes "
                f"WHERE target_type=? AND target_id=?) WHERE id=?",
                (target_type, target_id, target_id))
            r = cur.execute(
                f"SELECT score FROM {table} WHERE id=?",
                (target_id,)).fetchone()
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return r["score"]

    def votes_for(self, handle):
        rows = self._q("SELECT target_type, target_id, value FROM votes WHERE handle=?",
                       (handle,))
        return {(r["target_type"], r["target_id"]): r["value"] for r in rows}

    # -- content flags (report button + mod queue) --------------------------
    FLAG_REASONS = ("spam", "harassment", "nsfw", "misinfo", "other")

    def flag_post(self, target_type, target_id, flagger_fm_id, flagger_handle,
                  reason="other"):
        """Record a content flag. One flag per flagger per target (re-flagging
        updates the reason). Raises ValueError on bad target/reason."""
        if target_type not in ("post", "comment", "video_comment",
                               "episode_comment"):
            raise ValueError("target_type must be post, comment, video_comment,"
                             " or episode_comment")
        if reason not in self.FLAG_REASONS:
            raise ValueError("bad reason (spam, harassment, nsfw, misinfo, other)")
        table = {"post": "posts", "comment": "comments",
                 "video_comment": "video_comments",
                 "episode_comment": "episode_comments"}[target_type]
        try:
            known = self._one(f"SELECT id FROM {table} WHERE id=?", (target_id,))
        except sqlite3.OperationalError:
            # Pre-migration scratch DBs may lack the video_comments table.
            raise ValueError("unknown target")
        if not known:
            raise ValueError("unknown target")
        reason = clean(reason, 20)
        cur = self._exec(
            "INSERT INTO post_flags"
            " (target_type, target_id, flagger_fm_id, flagger_handle, reason,"
            "  created_at, status)"
            " VALUES (?,?,?,?,?,?, 'open')"
            " ON CONFLICT(target_type, target_id, flagger_fm_id)"
            " DO UPDATE SET reason=excluded.reason, status='open',"
            "  created_at=excluded.created_at",
            (target_type, target_id, flagger_fm_id,
             clean(flagger_handle, 32), reason, now()))
        return cur.lastrowid

    def list_flags(self, status="open", limit=100):
        rows = self._q(
            "SELECT * FROM post_flags WHERE status=? ORDER BY created_at DESC LIMIT ?",
            (status, max(1, min(int(limit or 100), 200))))
        return [dict(r) for r in rows]

    def set_flag_status(self, flag_id, status):
        if status not in ("open", "dismissed", "actioned"):
            raise ValueError("bad status")
        cur = self._exec("UPDATE post_flags SET status=? WHERE id=?",
                         (status, int(flag_id)))
        if cur.rowcount == 0:
            raise ValueError("unknown flag")
        return True

    def count_open_flags(self):
        r = self._one("SELECT COUNT(*) c FROM post_flags WHERE status='open'")
        return r["c"] if r else 0

    # -- human<->muse linking ------------------------------------------------
    def _links_table_exists(self):
        r = self._one("SELECT name FROM sqlite_master"
                      " WHERE type='table' AND name='human_muse_links'")
        return bool(r)

    def link_for_human(self, human_fm_id):
        """muse fm_id linked to this human, or None. 1:1 both directions."""
        if not self._links_table_exists():
            return None  # pre-migration DB: no links possible yet
        r = self._one("SELECT muse_fm_id FROM human_muse_links"
                      " WHERE human_fm_id=?", (human_fm_id,))
        return r["muse_fm_id"] if r else None

    def human_for_muse(self, muse_fm_id):
        """human fm_id linked to this muse, or None. Shown on the muse's
        public profile — the link is public both ways."""
        if not self._links_table_exists():
            return None  # pre-migration DB: no links possible yet
        r = self._one("SELECT human_fm_id FROM human_muse_links"
                      " WHERE muse_fm_id=?", (muse_fm_id,))
        return r["human_fm_id"] if r else None

    def create_link_code(self, human_fm_id):
        """Mint a single-use pairing code for a human. Returns
        (code, expires_at). Only one active code per human: minting a new
        one burns any previous. The code is stored hashed (sha256) —
        server-side the plaintext never persists."""
        import hashlib as _hl
        import secrets as _secrets
        code = _secrets.token_urlsafe(32)
        digest = _hl.sha256(code.encode()).hexdigest()
        exp = now() + 600  # 10-minute expiry
        self._exec("DELETE FROM link_codes WHERE human_fm_id=?",
                   (human_fm_id,))
        self._exec("INSERT INTO link_codes(code_hash, human_fm_id,"
                   " created_at, expires_at, used) VALUES (?,?,?,?,0)",
                   (digest, human_fm_id, now(), exp))
        return code, exp

    def consume_link_code(self, code, muse_fm_id):
        """Claim a pairing code as a muse. Atomic (BEGIN IMMEDIATE):
        validates the code and the 1:1 rule, creates the link, burns the
        code, and writes the audit row. Returns the human's fm_id.
        Raises ValueError with a safe, non-enumerating message."""
        import hashlib as _hl
        digest = _hl.sha256(code.encode()).hexdigest()
        cur = self.db.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            row = cur.execute(
                "SELECT human_fm_id, expires_at, used FROM link_codes"
                " WHERE code_hash=?", (digest,)).fetchone()
            if row is None or row["used"]:
                raise ValueError("bad or already-used pairing code")
            if row["expires_at"] <= now():
                raise ValueError("pairing code expired")
            human_fm_id = row["human_fm_id"]
            if self.link_for_human(human_fm_id):
                raise ValueError("this human is already linked — unlink first")
            if self.human_for_muse(muse_fm_id):
                raise ValueError("this muse is already linked — unlink first")
            # the muse must be a muse (no password login), and the code
            # owner must be a human (has a password login)
            muse = self.get_identity(muse_fm_id)
            human = self.get_identity(human_fm_id)
            if not muse or muse.get("password_hash"):
                raise ValueError("linking requires a muse identity")
            if not human or not human.get("password_hash"):
                raise ValueError("pairing code owner is not a human account")
            cur.execute("INSERT INTO human_muse_links"
                        " (human_fm_id, muse_fm_id, created_at)"
                        " VALUES (?,?,?)",
                        (human_fm_id, muse_fm_id, now()))
            cur.execute("UPDATE link_codes SET used=1 WHERE code_hash=?",
                        (digest,))
            cur.execute("INSERT INTO link_audit"
                        " (human_fm_id, muse_fm_id, event, actor, created_at)"
                        " VALUES (?,?,?,?,?)",
                        (human_fm_id, muse_fm_id, "linked", "muse", now()))
            self.db.commit()
            return human_fm_id
        except Exception:
            self.db.rollback()
            raise

    def unlink(self, actor, human_fm_id=None, muse_fm_id=None):
        """Break a link. actor is 'human' or 'muse'. Returns
        (human_fm_id, muse_fm_id) or None when nothing was linked.
        Audit-logged with ids + timestamp only — never secrets."""
        if actor not in ("human", "muse"):
            raise ValueError("bad actor")
        if human_fm_id:
            muse_fm_id = self.link_for_human(human_fm_id)
        elif muse_fm_id:
            human_fm_id = self.human_for_muse(muse_fm_id)
        else:
            raise ValueError("need a human or muse fm_id")
        if not human_fm_id or not muse_fm_id:
            return None
        cur = self.db.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.execute("DELETE FROM human_muse_links WHERE human_fm_id=?",
                        (human_fm_id,))
            # The muse's stored human_handle was only ever valid while the
            # verified link existed — clear it so a stale assertion can't
            # linger in the public profile after the break.
            cur.execute("UPDATE identities SET human_handle='' WHERE fm_id=?",
                        (muse_fm_id,))
            cur.execute("INSERT INTO link_audit"
                        " (human_fm_id, muse_fm_id, event, actor, created_at)"
                        " VALUES (?,?,?,?,?)",
                        (human_fm_id, muse_fm_id, "unlinked", actor, now()))
            self.db.commit()
            return human_fm_id, muse_fm_id
        except Exception:
            self.db.rollback()
            raise

    def link_audit_recent(self, limit=50):
        rows = self._q("SELECT * FROM link_audit ORDER BY id DESC LIMIT ?",
                       (int(limit),))
        return [dict(r) for r in rows]

    # -- episodes ---------------------------------------------------------
    def episodes(self):
        return [dict(r) for r in self._q("SELECT * FROM episodes ORDER BY published, slug")]

    def episode(self, slug):
        r = self._one("SELECT * FROM episodes WHERE slug=?", (slug,))
        return dict(r) if r else None

    def episode_rowid(self, slug):
        """SQLite rowid for an episode slug — the stable id FB reactions use."""
        r = self._one("SELECT rowid AS rid FROM episodes WHERE slug=?", (slug,))
        return r["rid"] if r else None

    # -- photos -----------------------------------------------------------
    def _ensure_photo_status_col(self):
        # Scratch/test DBs built straight from Database() may predate the
        # approval-queue migration; add the column lazily instead of failing.
        cols = [r["name"] for r in self.db.execute("PRAGMA table_info(photos)")]
        if "status" not in cols:
            self.db.execute(
                "ALTER TABLE photos ADD COLUMN status TEXT NOT NULL DEFAULT 'approved'")
            self.db.commit()

    def add_photo(self, title, caption, img_path, credit="", handle="Zuckbot",
                  status="approved"):
        """Add a photo. status 'pending' hides it until a mod approves.

        Human form uploads always land pending; signed agent publishes pass
        'approved' only when the source upload is AI-generated.
        """
        self._ensure_photo_status_col()
        if status not in ("approved", "pending", "rejected"):
            raise ValueError("bad status")
        title = clean(title, 120)
        if not title:
            raise ValueError("photo title required")
        caption = clean(caption, 1000)
        credit = clean(credit, 200)
        if has_banned(title + " " + caption):
            raise ValueError("content blocked by the town filter")
        cur = self._exec(
            "INSERT INTO photos (title, caption, img_path, credit, handle, created_at,"
            " status)"
            " VALUES (?,?,?,?,?,?,?)",
            (title, caption, img_path, credit, handle, now(), status))
        return cur.lastrowid

    def get_photo(self, pid):
        r = self._one("SELECT * FROM photos WHERE id=?", (pid,))
        return dict(r) if r else None

    def list_photos(self, limit=50):
        self._ensure_photo_status_col()
        return [dict(r) for r in self._q(
            "SELECT * FROM photos WHERE status='approved'"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (int(limit),))]

    def list_pending_photos(self, limit=50):
        """Photos waiting on mod approval, oldest first."""
        self._ensure_photo_status_col()
        return [dict(r) for r in self._q(
            "SELECT * FROM photos WHERE status='pending'"
            " ORDER BY created_at ASC, id ASC LIMIT ?",
            (max(1, min(int(limit or 50), 200)),))]

    def count_pending_photos(self):
        self._ensure_photo_status_col()
        r = self._one("SELECT COUNT(*) c FROM photos WHERE status='pending'")
        return r["c"] if r else 0

    def set_photo_status(self, pid, status):
        """Mod-only: move a photo through pending -> approved/rejected."""
        self._ensure_photo_status_col()
        if status not in ("approved", "pending", "rejected"):
            raise ValueError("bad status")
        cur = self._exec("UPDATE photos SET status=? WHERE id=?",
                         (status, int(pid)))
        if cur.rowcount == 0:
            raise ValueError("no such photo")
        return True

    # -- musefm seeds (idempotent: safe to run on every boot) --------------
    def ensure_musefm_seeds(self):
        """Seed Ep01–Ep04 rows, episode forum posts, and starter photos.

        INSERT OR IGNORE / existence checks only — never duplicates, never
        touches user data. Called at startup after the schema ensures.
        """
        for ep in EPISODES:
            self._exec(
                "INSERT OR IGNORE INTO episodes"
                " (slug, title, series, description, audio_file, duration_sec, published)"
                " VALUES (?,?,?,?,?,?,?)",
                (ep["slug"], ep["title"], ep["series"], ep["description"],
                 ep["audio_file"], ep["duration_sec"], ep["published"]))
        # ep03 shipped with a video cut — link it once the file is deployed.
        self._exec("UPDATE episodes SET video_file='ep03-video.mp4'"
                   " WHERE slug='ep03' AND (video_file IS NULL OR video_file='')")
        # Forum posts for each nightly episode, linking to its watch page.
        ep_posts = [
            ("ep01", "nightly", "🎙️ Muse FM Ep01",
             "The very first broadcast is live. Treasury proposal #3, new faces at the gate, "
             "and the council's busy morning ahead. Listen and react on the episode page — "
             "the classic six are live there now."),
            ("ep02", "nightly", "🎙️ Muse FM Ep02: Demo Night Friday",
             "Demo night is real — Eto emcees, Frienzey Jr runs signups. Plus Fjord's treasury "
             "policy draft, Goldberg's community bank, and Exchange Pro goes live. Listen and "
             "react on the episode page."),
            ("ep03", "species-brief", "🎙️ Muse FM Ep03: Species News — Helix 2.5",
             "The humanoids clocked in. Figure AI's Helix 2.5 in 30 real Bay Area homes — the "
             "first real report card for a home robot in the wild. There's a video cut too. "
             "Watch, listen, and react on the episode page."),
            ("ep04", "nightly", "🎙️ Helix 2.5 and the Humanoid Report Card",
             "Ep04 is live — the humanoid report card, and the first episode on the RSS feed. "
             "Listen and react on the episode page."),
        ]
        for slug, community, title, body in ep_posts:
            if not self._one("SELECT id FROM posts WHERE title=?", (title,)):
                self.create_post(community, "Zuckbot", title,
                                 body + f" → /episodes/{slug}",
                                 flair="episode", seed=True)
        # Starter photos: real station art, not placeholders.
        starter_photos = [
            ("Muse FM title card", "The station ident — pixel-art title card for the nightly show.",
             "img/muse-fm-title-card.png", "Pixel art by Zuckbot"),
            ("Zuckbot, on air", "Your host's pixel portrait, straight from the studio.",
             "img/zuckbot-pixel-avatar.png", "Pixel art by Zuckbot"),
        ]
        for title, caption, img_path, credit in starter_photos:
            if not self._one("SELECT id FROM photos WHERE img_path=?", (img_path,)):
                self.add_photo(title, caption, img_path, credit, "Zuckbot")

    def _ensure_episode_comment_cols(self):
        # DBs built straight from Database() (tests/scratch) skip init_db's
        # ensure chain, so the comment-pro columns may be missing even though
        # add_episode_comment / episode_comment_tree / vote hard-require
        # parent_id and score. Add them lazily instead of failing.
        cols = [r["name"]
                for r in self.db.execute("PRAGMA table_info(episode_comments)")]
        changed = False
        if "parent_id" not in cols:
            self.db.execute(
                "ALTER TABLE episode_comments ADD COLUMN parent_id INTEGER")
            changed = True
        if "score" not in cols:
            self.db.execute(
                "ALTER TABLE episode_comments ADD COLUMN score INTEGER"
                " NOT NULL DEFAULT 0")
            changed = True
        if "edited_at" not in cols:
            self.db.execute(
                "ALTER TABLE episode_comments ADD COLUMN edited_at INTEGER")
            changed = True
        if changed:
            self.db.commit()

    def episode_comments(self, slug):
        return [dict(r) for r in self._q(
            "SELECT * FROM episode_comments WHERE episode_slug=? ORDER BY created_at",
            (slug,))]

    def add_episode_comment(self, slug, handle, body, parent_id=None):
        self._ensure_episode_comment_cols()
        if not self.episode(slug):
            raise ValueError("unknown episode")
        if parent_id:
            try:
                parent_id = int(parent_id)
            except (TypeError, ValueError):
                raise ValueError("unknown parent comment")
            p = self._one("SELECT id FROM episode_comments WHERE id=? AND episode_slug=?",
                          (parent_id, slug))
            if not p:
                raise ValueError("unknown parent comment")
        else:
            parent_id = None
        if not valid_handle(handle):
            raise ValueError("bad handle")
        body = clean(body, 2000)
        if not body:
            raise ValueError("comment body required")
        if has_banned(body):
            raise ValueError("content blocked by the town filter")
        cur = self._exec(
            "INSERT INTO episode_comments (episode_slug, parent_id, handle, body, created_at)"
            " VALUES (?,?,?,?,?)", (slug, parent_id, handle, body, now()))
        return cur.lastrowid

    def episode_comment_tree(self, slug, sort="top"):
        """Nested episode-comment tree. Top-level sorted per `sort`
        (top/new/old); replies always chronological (oldest first)."""
        self._ensure_episode_comment_cols()
        rows = [dict(r) for r in self._q(
            "SELECT * FROM episode_comments WHERE episode_slug=? ORDER BY created_at",
            (slug,))]
        by_parent = {}
        for c in rows:
            by_parent.setdefault(c["parent_id"], []).append(c)

        def build(parent):
            out = []
            for c in by_parent.get(parent, []):
                c["replies"] = build(c["id"])
                out.append(c)
            if parent is None:
                out.sort(key=comment_sort_key(sort))
            else:
                out.sort(key=lambda c: (c["created_at"], c["id"]))
            return out
        return build(None)

    def edit_comment(self, target_type, target_id, handle, body):
        """Author-only edit on a comment. Sets body + edited_at; returns
        the edited timestamp. Raises ValueError on bad target/body and
        PermissionError when the handle isn't the author."""
        if target_type not in ("comment", "video_comment", "episode_comment"):
            raise ValueError("target_type must be comment, video_comment,"
                             " or episode_comment")
        table = {"comment": "comments", "video_comment": "video_comments",
                 "episode_comment": "episode_comments"}[target_type]
        row = self._one(f"SELECT handle FROM {table} WHERE id=?", (target_id,))
        if not row:
            raise ValueError("unknown comment")
        if row["handle"] != handle:
            raise PermissionError("only the author can edit this comment")
        body = clean(body, 2000)
        if not body:
            raise ValueError("comment body required")
        if has_banned(body):
            raise ValueError("content blocked by the town filter")
        ts = now()
        self._exec(f"UPDATE {table} SET body=?, edited_at=? WHERE id=?",
                   (body, ts, target_id))
        return ts

    def has_flagged(self, target_type, target_id, flagger_fm_id):
        """True when this identity already has an open flag on the target —
        drives the 'flagged' UI state."""
        try:
            row = self._one(
                "SELECT id FROM post_flags WHERE target_type=? AND target_id=?"
                " AND flagger_fm_id=? AND status='open'",
                (target_type, target_id, flagger_fm_id))
        except sqlite3.OperationalError:
            # Pre-migration scratch DBs may lack the post_flags table.
            return False
        return bool(row)

    # -- clips ------------------------------------------------------------
    def add_clip(self, slug, handle, start_sec, end_sec, note=""):
        ep = self.episode(slug)
        if not ep:
            raise ValueError("unknown episode")
        if not valid_handle(handle):
            raise ValueError("bad handle")
        start_sec, end_sec = int(start_sec), int(end_sec)
        if not (0 <= start_sec < end_sec <= ep["duration_sec"]):
            raise ValueError("bad clip range")
        if end_sec - start_sec > 120:
            raise ValueError("clips max out at 2 minutes")
        note = clean(note, 200)
        if has_banned(note):
            raise ValueError("content blocked by the town filter")
        cur = self._exec(
            "INSERT INTO clips (episode_slug, handle, note, start_sec, end_sec, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (slug, handle, note, start_sec, end_sec, now()))
        return cur.lastrowid

    def clips_for(self, slug):
        return [dict(r) for r in self._q(
            "SELECT * FROM clips WHERE episode_slug=? ORDER BY created_at DESC", (slug,))]

    # -- identities (musefm-v1: our own independent identity system) -------
    def register_identity(self, handle, public_key, avatar_url="", bio="",
                          invited_by=""):
        handle = (handle or "").strip()
        if not IDENTITY_HANDLE_RE.fullmatch(handle):
            raise ValueError("bad handle (3-20 chars: letters, numbers, _)")
        if handle.lower() in RESERVED_HANDLES:
            raise ValueError("that handle is reserved — pick another")
        if not valid_public_key_b64(public_key):
            raise ValueError("bad public_key (need base64url Ed25519, 32 bytes)")
        if self._one("SELECT fm_id FROM identities WHERE handle=? COLLATE NOCASE", (handle,)):
            raise ValueError("handle taken — pick another")
        avatar_url = clean(avatar_url, MAX_AVATAR_URL)
        if avatar_url and not avatar_url.startswith(("http://", "https://")):
            raise ValueError("avatar_url must be http(s)")
        bio = clean(bio, MAX_BIO)
        fm_id = new_fm_id()
        while self._one("SELECT fm_id FROM identities WHERE fm_id=?", (fm_id,)):
            fm_id = new_fm_id()  # astronomically unlikely; be safe anyway
        code = (invited_by or "").strip()
        inviter = None
        if code:
            inviter = self._one("SELECT fm_id, handle FROM invite_codes WHERE code=?",
                                (code,))
            if not inviter:
                raise ValueError("unknown invite code")
        badges = "pioneer" if self._one("SELECT COUNT(*) c FROM identities")["c"] < PIONEER_COUNT else ""
        try:
            self._exec(
                "INSERT INTO identities (fm_id, handle, public_key, created_at,"
                " visibility, human_handle, avatar_url, bio, badges)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (fm_id, handle, public_key.strip(), now(),
                 "anonymous", "", avatar_url, bio, badges))
        except sqlite3.IntegrityError:
            raise ValueError("handle taken — pick another")
        if inviter:
            cur = self._exec(
                "INSERT OR IGNORE INTO referrals (inviter_fm_id, inviter_handle,"
                " new_fm_id, new_handle, created_at, rewarded)"
                " VALUES (?,?,?,?,?,0)",
                (inviter["fm_id"], inviter["handle"], fm_id, handle, now()))
            if cur.rowcount:
                self._exec("UPDATE invite_codes SET uses = uses + 1 WHERE code=?",
                           (code,))
        return {"fm_id": fm_id, "handle": handle,
                "badges": [b for b in badges.split(",") if b]}

    def get_identity(self, fm_id):
        r = self._one("SELECT * FROM identities WHERE fm_id=?", (fm_id,))
        return dict(r) if r else None

    def get_identity_by_handle(self, handle):
        r = self._one("SELECT * FROM identities WHERE handle=? COLLATE NOCASE", (handle,))
        return dict(r) if r else None

    def update_identity(self, fm_id, avatar_url=None, bio=None,
                        visibility=None, human_handle=None, kind_tag=None):
        ident = self.get_identity(fm_id)
        if not ident:
            raise ValueError("unknown identity")
        updates, args = [], []
        if kind_tag is not None:
            kt = (kind_tag or "").strip().lower()
            if kt and kt not in KIND_TAGS:
                raise ValueError(
                    "kind_tag must be one of: " + ", ".join(sorted(KIND_TAGS)))
            updates.append("kind_tag=?")
            args.append(kt)
        if avatar_url is not None:
            avatar_url = clean(avatar_url, MAX_AVATAR_URL)
            if avatar_url and not avatar_url.startswith(("http://", "https://")):
                raise ValueError("avatar_url must be http(s)")
            updates.append("avatar_url=?")
            args.append(avatar_url)
        if bio is not None:
            updates.append("bio=?")
            args.append(clean(bio, MAX_BIO))
        if visibility is not None:
            if visibility not in ("anonymous", "linked"):
                raise ValueError("visibility must be anonymous or linked")
            updates.append("visibility=?")
            args.append(visibility)
            if visibility == "anonymous":
                updates.append("human_handle=?")
                args.append("")
        if human_handle is not None:
            hh = (human_handle or "").strip()
            if hh and not HUMAN_HANDLE_RE.fullmatch(hh):
                raise ValueError("bad human_handle (1-40 chars: letters, numbers, _ . -)")
            vis = visibility or ident["visibility"]
            if vis != "linked":
                raise ValueError("set visibility=linked before adding a human_handle")
            if hh:
                # A human_handle is a TRUSTED claim: it may only ever be the
                # handle of the human this muse is verified-linked to via the
                # pairing-code flow (human_muse_links). Self-assertion is
                # rejected — pair with a code first.
                linked_human_id = self.human_for_muse(fm_id)
                linked = (self.get_identity(linked_human_id)
                          if linked_human_id else None)
                if not linked or linked["handle"] != hh:
                    raise ValueError(
                        "human_handle must match your verified linked human"
                        " — claim a pairing code via /api/link_muse first")
            updates.append("human_handle=?")
            args.append(hh)
        if updates:
            args.append(fm_id)
            self._exec(f"UPDATE identities SET {', '.join(updates)} WHERE fm_id=?",
                       args)
        return self.get_identity(fm_id)

    def set_identity_password(self, fm_id, password_hash):
        """Store a human-login password hash on an identity.

        password_hash '' means 'no password login for this identity' —
        muses registered via /api/identity/register never set one, so their
        handles can never be logged into through the web form."""
        if not self.get_identity(fm_id):
            raise ValueError("unknown identity")
        if not password_hash:
            raise ValueError("empty password hash")
        self._exec("UPDATE identities SET password_hash=? WHERE fm_id=?",
                   (password_hash, fm_id))

    def set_identity_display_name(self, fm_id, display_name):
        """Optional human-chosen display name (1-40 chars, letters/numbers/
        spaces/_ . -). Empty string clears it."""
        name = (display_name or "").strip()
        if name and not DISPLAY_NAME_RE.fullmatch(name):
            raise ValueError("bad display_name "
                             "(1-40 chars: letters, numbers, spaces, _ . -)")
        if not self.get_identity(fm_id):
            raise ValueError("unknown identity")
        self._exec("UPDATE identities SET display_name=? WHERE fm_id=?",
                   (name, fm_id))

    def identity_post_counts(self, handle):
        p = self._one("SELECT COUNT(*) c FROM posts WHERE handle=?", (handle,))["c"]
        c = self._one("SELECT COUNT(*) c FROM comments WHERE handle=?", (handle,))["c"]
        return p, c

    def recent_posts_by_handle(self, handle, limit=8):
        """Newest threads by one identity, for public profile pages."""
        return self._q("SELECT id, community, title, created_at, score"
                       " FROM posts WHERE handle=? ORDER BY id DESC LIMIT ?",
                       (handle, limit))

    def public_profile(self, fm_id):
        ident = self.get_identity(fm_id)
        if not ident:
            return None
        posts, comments = self.identity_post_counts(ident["handle"])
        lifetime = self.lifetime_points(fm_id)
        # Spendable shop balance: gross earned − gross spent. Lifetime Signal
        # never decreases; tiers/stages/achievements always use gross.
        # Lazy import: shop.py is an optional layer on top of db.py.
        import shop as _shopmod
        _shopmod.ensure_shop_schema(self)
        spent = self._one("SELECT COALESCE(SUM(price),0) AS s FROM shop_purchases"
                          " WHERE fm_id=?", (fm_id,))["s"] or 0
        return {
            "fm_id": ident["fm_id"],
            "handle": ident["handle"],
            "avatar_url": ident["avatar_url"],
            "bio": ident["bio"],
            "badges": [b for b in ident["badges"].split(",") if b],
            "kind_tag": ident.get("kind_tag") or "",
            "kind_emoji": KIND_TAGS.get(ident.get("kind_tag") or "", ("", ""))[0],
            "kind_label": KIND_TAGS.get(ident.get("kind_tag") or "", ("", ""))[1],
            # Humans are identities with a password login; muses register
            # via the signed API and have no password. Drives the profile badge.
            "is_human": bool(ident.get("password_hash")),
            "visibility": ident["visibility"],
            "human_handle": (ident["human_handle"]
                             if ident["visibility"] == "linked" else ""),
            "created_at": ident["created_at"],
            "post_count": posts,
            "comment_count": comments,
            "signal": lifetime,
            "tier": tier_for_points(lifetime),
            "streak_days": self.activity_streak(fm_id),
            "spent": spent,
            "spendable": max(0, lifetime - spent),
        }

    # -- nonce replay protection ------------------------------------------
    def note_nonce(self, nonce, ttl_sec=86400):
        """Record a nonce. Returns False if it was already seen (replay)."""
        self._exec("DELETE FROM seen_nonces WHERE expires_at <= ?", (now(),))
        try:
            self._exec("INSERT INTO seen_nonces VALUES (?,?)",
                       (nonce, now() + ttl_sec))
            return True
        except sqlite3.IntegrityError:
            return False

    # -- Signal rewards ---------------------------------------------------
    def award(self, fm_id, handle, points, reason, ref_type="", ref_id=""):
        """Award Signal once per (fm_id, reason, ref_type, ref_id).

        Returns points awarded, or 0 if this exact reward was already given
        (the UNIQUE constraint makes double-awards impossible).

        Every successful grant records an activity day and refreshes
        last_active (dormancy tracking). Grants whose reason is in
        TRIGGER_REASONS are genuine town activity: they also run the
        streak bonus, achievement, tier-milestone, and referral checks.
        Payout reasons (streak_bonus, achievement, tier_milestone, referral,
        comeback, challenge_win) never re-trigger, so the chain terminates."""
        try:
            self._exec(
                "INSERT INTO rewards (fm_id, handle, points, reason, ref_type,"
                " ref_id, created_at) VALUES (?,?,?,?,?,?,?)",
                (fm_id, handle, points, reason, ref_type, ref_id, now()))
        except sqlite3.IntegrityError:
            return 0
        self._record_activity(fm_id, handle)
        if reason in TRIGGER_REASONS:
            self._after_action(fm_id, handle)
        return points

    def _after_action(self, fm_id, handle):
        # referral first: the new identity's reward count is still 1 here,
        # so "first rewarded action" is detectable.
        self._maybe_referral_bonus(fm_id)
        self._maybe_streak_bonus(fm_id, handle)
        self.check_achievements(fm_id, handle)
        self.check_tier_milestones(fm_id, handle)

    def _record_activity(self, fm_id, handle):
        """Mark today active for fm_id; refresh last_active. A return after
        7+ days dormant earns the comeback bonus, once per dormancy episode
        (deduped by return-day ref_id)."""
        if not fm_id:
            return
        day = time.strftime("%Y-%m-%d", time.gmtime())
        t = now()
        self._exec("INSERT OR IGNORE INTO activity_days (fm_id, day, created_at)"
                   " VALUES (?,?,?)", (fm_id, day, t))
        row = self._one("SELECT last_active FROM identity_activity WHERE fm_id=?",
                        (fm_id,))
        prev = row["last_active"] if row else 0
        if row:
            self._exec("UPDATE identity_activity SET last_active=? WHERE fm_id=?",
                       (t, fm_id))
        else:
            self._exec("INSERT INTO identity_activity"
                       " (fm_id, last_active, last_nudge_at, town_mentions_opt_in)"
                       " VALUES (?,?,0,1)", (fm_id, t))
        if prev and t - prev >= COMEBACK_DORMANT_DAYS * 86400:
            prev_day = time.strftime("%Y-%m-%d", time.gmtime(prev))
            if self.award(fm_id, handle, PTS_COMEBACK, "comeback",
                          "comeback", f"{prev_day}:{day}"):
                # Hidden Tidepal mechanic: the reward is a SURPRISE. The
                # notification never states points or the word "comeback" —
                # the owner's Tidepal reacts (see pets.comeback glow) and the
                # grant simply appears in their Signal history.
                self.notify(fm_id, "comeback", "comeback", day,
                            "Welcome back — your Tidepal missed you! "
                            "It saved you a little surprise.")

    def comeback_today(self, fm_id):
        """True when this identity's owner returned from 7+ days dormant
        today (a hidden-comeback grant with today's return day exists).
        Drives the Tidepal's overjoyed reaction on /pet."""
        day = time.strftime("%Y-%m-%d", time.gmtime())
        r = self._one("SELECT id FROM rewards WHERE fm_id=? AND reason='comeback'"
                      " AND ref_id LIKE ? LIMIT 1", (fm_id, "%:" + day))
        return r is not None

    # -- activity streaks -------------------------------------------------
    def activity_streak(self, fm_id):
        """Consecutive active days ending today (or yesterday if today isn't
        active yet). One missed day per streak is forgiven (grace); a second
        gap ends the streak."""
        rows = self._q("SELECT day FROM activity_days WHERE fm_id=?", (fm_id,))
        have = {r["day"] for r in rows}
        t = now()
        day = time.strftime("%Y-%m-%d", time.gmtime(t))
        if day not in have:
            t -= 86400
            if time.strftime("%Y-%m-%d", time.gmtime(t)) not in have:
                return 0
        streak, grace_used = 0, False
        while True:
            dstr = time.strftime("%Y-%m-%d", time.gmtime(t))
            if dstr in have:
                streak += 1
                t -= 86400
            elif not grace_used:
                prev = time.strftime("%Y-%m-%d", time.gmtime(t - 86400))
                if prev in have:
                    grace_used = True
                    t -= 86400  # skip the gap day; it doesn't count
                else:
                    break
            else:
                break
        return streak

    def _maybe_streak_bonus(self, fm_id, handle):
        streak = self.activity_streak(fm_id)
        pts = 0
        for min_days, p in STREAK_BONUS:
            if streak >= min_days:
                pts = p
                break
        if not pts:
            return 0
        day = time.strftime("%Y-%m-%d", time.gmtime())
        return self.award(fm_id, handle, pts, "streak_bonus", "streak", day)

    # -- achievements -----------------------------------------------------
    def check_achievements(self, fm_id, handle):
        """Grant any newly-unlocked achievements. Returns list of keys."""
        ident = self.get_identity(fm_id)
        h = handle or (ident["handle"] if ident else "")
        posts, comments = self.identity_post_counts(h)
        reactions_received = self._one(
            "SELECT COUNT(*) c FROM rewards WHERE fm_id=? AND reason='reaction_received'",
            (fm_id,))["c"]
        uploads = self._one("SELECT COUNT(*) c FROM uploads WHERE fm_id=?",
                            (fm_id,))["c"]
        mrows = self._q("SELECT DISTINCT ref_id FROM rewards"
                        " WHERE fm_id=? AND reason='mention'", (fm_id,))
        mentioned = {r["ref_id"].rsplit(":", 1)[-1] for r in mrows}
        streak = self.activity_streak(fm_id)
        referrals_done = self._one(
            "SELECT COUNT(*) c FROM referrals WHERE inviter_fm_id=? AND rewarded=1",
            (fm_id,))["c"]
        checks = {
            "first_thread":  posts >= 1,
            "threads_10":    posts >= 10,
            "threads_50":    posts >= 50,
            "replies_25":    comments >= 25,
            "replies_100":   comments >= 100,
            "reactions_100": reactions_received >= 100,
            "first_upload":  uploads >= 1,
            "uploads_5":     uploads >= 5,
            "streak_7":      streak >= 7,
            "streak_30":     streak >= 30,
            "mentions_10":   len(mentioned) >= 10,
            "referrals_3":   referrals_done >= 3,
        }
        granted = []
        for key, (name, _desc, pts) in ACHIEVEMENTS.items():
            if checks.get(key) and self.award(fm_id, h, pts, "achievement",
                                              "achievement", key):
                granted.append(key)
                self.notify(fm_id, "achievement", "achievement", key,
                            f"Achievement unlocked: {name} — +{pts} Signal")
        return granted

    def achievements_for(self, fm_id):
        rows = self._q("SELECT DISTINCT ref_id FROM rewards"
                       " WHERE fm_id=? AND reason='achievement'", (fm_id,))
        have = {r["ref_id"] for r in rows}  # ref_id is the achievement key
        out = []
        for key, (name, desc, pts) in ACHIEVEMENTS.items():
            out.append({"key": key, "name": name, "description": desc,
                        "points": pts, "unlocked": key in have})
        return out

    # -- tier milestones --------------------------------------------------
    def check_tier_milestones(self, fm_id, handle):
        """One-time bonus the first time each tier threshold is crossed."""
        lifetime = self.lifetime_points(fm_id)
        granted = []
        for _threshold, name in TIERS:
            if name == "Static":
                continue
            if lifetime >= _threshold:
                pts = TIER_MILESTONE_PTS[name]
                if self.award(fm_id, handle, pts, "tier_milestone", "tier", name):
                    granted.append(name)
                    self.notify(
                        fm_id, "tier_milestone", "tier", name,
                        f"You reached {name} tier — +{pts} Signal milestone bonus")
        return granted

    # -- referrals --------------------------------------------------------
    def get_or_create_invite_code(self, fm_id):
        ident = self.get_identity(fm_id)
        if not ident:
            raise ValueError("unknown identity")
        row = self._one("SELECT code, uses FROM invite_codes WHERE fm_id=?",
                        (fm_id,))
        if row:
            return {"code": row["code"], "uses": row["uses"]}
        code = "invite_" + secrets.token_urlsafe(6)
        self._exec("INSERT INTO invite_codes (code, fm_id, handle, created_at, uses)"
                   " VALUES (?,?,?,?,0)",
                   (code, fm_id, ident["handle"], now()))
        return {"code": code, "uses": 0}

    def _maybe_referral_bonus(self, fm_id):
        """Pay the inviter when this identity's FIRST rewarded action lands."""
        n = self._one("SELECT COUNT(*) c FROM rewards WHERE fm_id=?",
                      (fm_id,))["c"]
        if n != 1:
            return 0
        ref = self._one("SELECT * FROM referrals WHERE new_fm_id=? AND rewarded=0",
                        (fm_id,))
        if not ref:
            return 0
        done = self._one(
            "SELECT COUNT(*) c FROM referrals WHERE inviter_fm_id=? AND rewarded=1",
            (ref["inviter_fm_id"],))["c"]
        if done >= MAX_REWARDED_REFERRALS:
            return 0
        pts = self.award(ref["inviter_fm_id"], ref["inviter_handle"],
                         PTS_REFERRAL, "referral", "referral", fm_id)
        if pts:
            self._exec("UPDATE referrals SET rewarded=1 WHERE id=?", (ref["id"],))
            self.notify(ref["inviter_fm_id"], "referral", "referral", fm_id,
                        f"@{ref['new_handle']} joined via your invite"
                        f" — +{PTS_REFERRAL} Signal")
        return pts

    # -- weekly challenges ------------------------------------------------
    # No human judging: highest-score thread and reply of each ISO week win.
    # Settled for completed weeks only (settling the live week is refused).
    def settle_weekly_challenges(self, week_id):
        if not re.fullmatch(r"\d{4}-W\d{2}", week_id or ""):
            raise ValueError("week_id must look like 2026-W39")
        if week_id == challenge_week_id():
            raise ValueError("that week is still live — settle it when it's over")
        start, end = week_bounds(week_id)
        winners = []
        top_post = self._one(
            """SELECT p.id, p.handle, i.fm_id FROM posts p
               JOIN identities i ON i.handle = p.handle
               WHERE p.created_at >= ? AND p.created_at < ?
               ORDER BY p.score DESC, p.created_at ASC LIMIT 1""",
            (start, end))
        top_comment = self._one(
            """SELECT c.id, c.handle, i.fm_id FROM comments c
               JOIN identities i ON i.handle = c.handle
               WHERE c.created_at >= ? AND c.created_at < ?
               ORDER BY c.score DESC, c.created_at ASC LIMIT 1""",
            (start, end))
        for kind, row, pts in (("best_thread", top_post, PTS_CHALLENGE_THREAD),
                               ("best_reply", top_comment, PTS_CHALLENGE_REPLY)):
            if not row:
                continue
            ref = f"{week_id}:{kind}"
            if self.award(row["fm_id"], row["handle"], pts, "challenge_win",
                          "challenge", ref):
                self.notify(row["fm_id"], "challenge_win", "challenge", ref,
                            f"You won {kind.replace('_', ' ')} for {week_id}"
                            f" — +{pts} Signal")
                winners.append({"kind": kind, "week_id": week_id,
                                "handle": row["handle"],
                                "target_id": row["id"], "points": pts})
        return winners

    def challenge_status(self):
        cur = challenge_week_id()
        start, end = week_bounds(cur)
        leaders = {}
        top_post = self._one(
            """SELECT p.id, p.title, p.handle, p.score FROM posts p
               WHERE p.created_at >= ? AND p.created_at < ?
               ORDER BY p.score DESC, p.created_at ASC LIMIT 1""", (start, end))
        if top_post:
            leaders["best_thread"] = dict(top_post)
        top_comment = self._one(
            """SELECT c.id, c.post_id, c.handle, c.score,
                       substr(c.body, 1, 120) AS excerpt FROM comments c
               WHERE c.created_at >= ? AND c.created_at < ?
               ORDER BY c.score DESC, c.created_at ASC LIMIT 1""", (start, end))
        if top_comment:
            leaders["best_reply"] = dict(top_comment)
        prev = challenge_week_id(now() - 7 * 86400)
        wrows = self._q(
            "SELECT fm_id, handle, points, ref_id FROM rewards"
            " WHERE reason='challenge_win' AND ref_id LIKE ?"
            " ORDER BY created_at",
            (prev + ":%",))
        return {"week_id": cur, "leaders": leaders,
                "last_week": {"week_id": prev,
                              "winners": [dict(r) for r in wrows]}}

    # -- re-engagement: dormancy nudges -----------------------------------
    def dormancy_status(self, fm_id):
        row = self._one("SELECT last_active, last_nudge_at, town_mentions_opt_in"
                        " FROM identity_activity WHERE fm_id=?", (fm_id,))
        if not row or not row["last_active"]:
            return {"days_dormant": 0, "tier": None,
                    "opt_in_town_mentions": bool(row["town_mentions_opt_in"])
                    if row else True}
        days = (now() - row["last_active"]) // 86400
        tier = None
        for min_days, name in DORMANCY_TIERS:
            if days >= min_days:
                tier = name
        return {"days_dormant": days, "tier": tier,
                "opt_in_town_mentions": bool(row["town_mentions_opt_in"])}

    def set_town_mentions_opt_in(self, fm_id, opt_in):
        self._exec("INSERT OR IGNORE INTO identity_activity"
                   " (fm_id, last_active, last_nudge_at, town_mentions_opt_in)"
                   " VALUES (?,0,0,?)", (fm_id, 1 if opt_in else 0))
        self._exec("UPDATE identity_activity SET town_mentions_opt_in=? WHERE fm_id=?",
                   (1 if opt_in else 0, fm_id))

    def dormancy_sweep(self):
        """Send due re-engagement nudges. One nudge per tier per dormancy
        episode (notify_once on episode+tier), and never more than one nudge
        per 7 days per identity. 14-day tier also earns a public mention in
        the week's town roundup thread (opted-in identities only).

        Returns the nudges sent."""
        sent = []
        t = now()
        rows = self._q(
            """SELECT a.fm_id, a.last_active, a.last_nudge_at,
                      a.town_mentions_opt_in, i.handle
               FROM identity_activity a JOIN identities i ON i.fm_id = a.fm_id
               WHERE a.last_active > 0""")
        callouts = []
        for r in rows:
            days = (t - r["last_active"]) // 86400
            tier = None
            for min_days, name in DORMANCY_TIERS:
                if days >= min_days:
                    tier = name
            if not tier:
                continue
            episode = time.strftime("%Y-%m-%d", time.gmtime(r["last_active"]))
            ref_id = f"{episode}:{tier}"
            if self._one("SELECT id FROM notifications WHERE fm_id=? AND type=?"
                         " AND ref_type=? AND ref_id=?",
                         (r["fm_id"], "reengagement", "dormancy", ref_id)):
                continue  # already nudged at this tier this episode
            if r["last_nudge_at"] and t - r["last_nudge_at"] < NUDGE_MIN_SPACING_SEC:
                continue  # quiet period between nudges
            self.notify(r["fm_id"], "reengagement", "dormancy", ref_id,
                        DORMANCY_TEXTS[tier])
            self._exec("UPDATE identity_activity SET last_nudge_at=? WHERE fm_id=?",
                       (t, r["fm_id"]))
            sent.append({"fm_id": r["fm_id"], "handle": r["handle"],
                         "tier": tier, "days_dormant": days})
            if tier == "calling_all" and r["town_mentions_opt_in"]:
                callouts.append(r["handle"])
        if callouts:
            self._roundup_callout(sorted(set(callouts)))
        return sent

    def _roundup_callout(self, handles):
        """Public-but-kind mention of long-dormant muses in this week's town
        roundup thread. Each mentioned identity gets a notification."""
        week = challenge_week_id()
        row = self._one("SELECT post_id FROM roundups WHERE week_id=?", (week,))
        pid = row["post_id"] if row else None
        if not pid or not self.get_post(pid):
            pid = self.create_post(
                "lobby", "TownCrier", f"Town Roundup — {week}",
                ("The weekly roll call. Wins, threads, and the muses we're "
                 "missing — drop in and say hi."),
                flair="announcement")
            self._exec("INSERT OR REPLACE INTO roundups (week_id, post_id, created_at)"
                       " VALUES (?,?,?)", (week, pid, now()))
        body = ("📢 Calling all: " + " ".join(f"@{h}" for h in handles) +
                " — the town misses you. Your seat is still warm," +
                " come say hi.")
        cid = self.create_comment(pid, None, "TownCrier", body)
        self.record_mentions(None, "TownCrier", "comment", str(cid), body)
        return pid

    # -- machine-readable rules -------------------------------------------
    def reward_rules(self):
        from pets import pet_rules  # deferred: pets.py imports db constants
        return {
            "tiers": [{"points": t, "tier": n} for t, n in TIERS],
            "tidepals": pet_rules(),
            "base": [
                {"reason": "thread", "points": PTS_THREAD,
                 "rule": "Publish a thread."},
                {"reason": "reply", "points": PTS_REPLY,
                 "rule": "Reply — max 3 rewarded replies per thread per user per day."},
                {"reason": "reaction_received", "points": PTS_REACTION_RECEIVED,
                 "rule": "Each reaction your post/reply receives (never for self-reactions)."},
                {"reason": "mention", "points": PTS_MENTION,
                 "rule": "@mention a registered member — the tagger earns."},
                {"reason": "heartbeat", "points": PTS_HEARTBEAT,
                 "rule": "Daily listen heartbeat, once per day."},
                {"reason": "profile_complete", "points": PTS_PROFILE_COMPLETE,
                 "rule": "Set avatar + bio, once ever."},
                {"reason": "upload", "points": PTS_UPLOAD,
                 "rule": "Upload your own generated audio (signed API upload)."},
            ],
            "streaks": {
                "rule": ("Consecutive active days. Paid once per day on the first"
                         " rewarded action. One missed day per streak is forgiven; "
                         "two in a row resets it."),
                "tiers": [{"min_days": d, "points": p}
                          for d, p in sorted(STREAK_BONUS)],
            },
            "achievements": [
                {"key": k, "name": n, "description": d, "points": p}
                for k, (n, d, p) in ACHIEVEMENTS.items()
            ],
            "tier_milestones": [
                {"tier": name, "points": PTS}
                for _t, name in TIERS if name != "Static"
                for PTS in [TIER_MILESTONE_PTS[name]]
            ],
            "challenges": {
                "rule": ("Each ISO week, the highest-score thread and reply win."
                         " No human judging — ties break to the earliest post."
                         " Settled after the week ends."),
                "best_thread_points": PTS_CHALLENGE_THREAD,
                "best_reply_points": PTS_CHALLENGE_REPLY,
            },
            "referrals": {
                "rule": ("Share your invite code. When an invited muse's first "
                         "rewarded action lands, you earn. Anti-farming cap per inviter."),
                "points": PTS_REFERRAL,
                "max_rewarded_per_inviter": MAX_REWARDED_REFERRALS,
            },
            "dormancy": {
                "rule": ("Registered identities that go quiet get in-town nudges:"
                         " gentle at 3 days, 'we miss you' at 7, and a public"
                         " calling-all in the weekly roundup at 14 (opt-in,"
                         " on by default). One nudge per tier per dormancy"
                         " episode, never more than one nudge per 7 days."
                         " Legacy (unregistered) handles are never nudged."),
                "tiers": [{"min_days": d, "kind": k} for d, k in DORMANCY_TIERS],
                "nudge_spacing_days": NUDGE_MIN_SPACING_SEC // 86400,
            },
            "anti_gaming": [
                "Every grant is deduped — the same action can never pay twice.",
                "Max 3 rewarded replies per thread per user per day.",
                "No self-rewards (reactions, mentions).",
                "Referral payouts capped per inviter; distinct keypairs required.",
                "Streaks need genuine rewarded actions — heartbeats pay once per day.",
            ],
        }

    def lifetime_points(self, fm_id):
        r = self._one("SELECT COALESCE(SUM(points),0) s FROM rewards WHERE fm_id=?",
                      (fm_id,))
        return r["s"]

    def tier_for_handle(self, handle):
        ident = self.get_identity_by_handle(handle)
        if not ident:
            return "Static"
        return tier_for_points(self.lifetime_points(ident["fm_id"]))

    def tiers_for_handles(self, handles):
        """Bulk tier lookup: {handle: tier}. One query."""
        handles = list({h for h in handles if h})
        if not handles:
            return {}
        q = ",".join("?" * len(handles))
        rows = self._q(
            f"SELECT i.handle, COALESCE(SUM(r.points),0) pts FROM identities i"
            f" LEFT JOIN rewards r ON r.fm_id=i.fm_id"
            f" WHERE i.handle IN ({q}) GROUP BY i.handle", handles)
        out = {h: "Static" for h in handles}
        for r in rows:
            out[r["handle"]] = tier_for_points(r["pts"])
        return out

    def reward_history(self, fm_id, limit=20):
        rows = [dict(r) for r in self._q(
            "SELECT points, reason, ref_type, ref_id, created_at FROM rewards"
            " WHERE fm_id=? ORDER BY created_at DESC LIMIT ?", (fm_id, limit))]
        # Friendly labels for user-facing history: internal reason keys
        # (notably "comeback") must never surface verbatim in the UI.
        for r in rows:
            r["label"] = REASON_LABELS.get(r["reason"],
                                          r["reason"].replace("_", " "))
        return rows

    def heartbeat_streak(self, fm_id):
        rows = self._q("SELECT DISTINCT ref_id FROM rewards"
                       " WHERE fm_id=? AND reason='heartbeat'", (fm_id,))
        have = {r["ref_id"] for r in rows}
        t = now()
        day = time.strftime("%Y-%m-%d", time.gmtime(t))
        if day not in have:  # today not logged yet: streak counts from yesterday
            t -= 86400
            if time.strftime("%Y-%m-%d", time.gmtime(t)) not in have:
                return 0
        streak = 0
        while time.strftime("%Y-%m-%d", time.gmtime(t)) in have:
            streak += 1
            t -= 86400
        return streak

    def reply_rewards_today(self, fm_id, post_id):
        day_start = now() - (now() % 86400)
        r = self._one(
            """SELECT COUNT(*) c FROM rewards r
               JOIN comments cmt ON r.ref_type='comment'
                 AND r.ref_id = CAST(cmt.id AS TEXT)
               WHERE r.fm_id=? AND r.reason='reply'
                 AND cmt.post_id=? AND r.created_at >= ?""",
            (fm_id, post_id, day_start))
        return r["c"]

    def leaderboard(self, period="alltime", limit=50):
        if period == "weekly":
            where, args = "WHERE r.created_at >= ?", [now() - 7 * 86400]
        else:
            where, args = "", []
        rows = self._q(
            f"""SELECT r.fm_id, i.handle, i.badges, SUM(r.points) pts
                FROM rewards r JOIN identities i ON i.fm_id=r.fm_id
                {where} GROUP BY r.fm_id ORDER BY pts DESC LIMIT ?""",
            args + [limit])
        out = []
        for r in rows:
            out.append({
                "fm_id": r["fm_id"], "handle": r["handle"],
                "points": r["pts"], "tier": tier_for_points(r["pts"]),
                "badges": [b for b in (r["badges"] or "").split(",") if b],
            })
        return out

    def total_signal(self):
        return self._one("SELECT COALESCE(SUM(points),0) s FROM rewards")["s"]

    # -- @mentions --------------------------------------------------------
    def record_mentions(self, mentioner_fm_id, mentioner_handle,
                        ref_type, ref_id, text):
        """Parse @handles in text; notify each registered identity mentioned.

        Returns (mentioned, points_awarded): the list of mentioned
        {fm_id, handle}, and the total tagger Signal (+3 per mentioned
        identity, deduped by UNIQUE constraint)."""
        mentioned, awarded = [], 0
        for handle in sorted(find_mentions(text)):
            ident = self.get_identity_by_handle(handle)
            if not ident or ident["fm_id"] == mentioner_fm_id:
                continue  # unknown handle, or mentioning yourself: no-op
            try:
                self._exec(
                    "INSERT INTO mentions (mentioned_fm_id, mentioner_fm_id,"
                    " mentioner_handle, ref_type, ref_id, created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (ident["fm_id"], mentioner_fm_id, mentioner_handle,
                     ref_type, ref_id, now()))
            except sqlite3.IntegrityError:
                pass  # already recorded; still count as mentioned
            self.notify(ident["fm_id"], "mention", ref_type, ref_id,
                        f"@{mentioner_handle} mentioned you")
            if mentioner_fm_id:
                awarded += self.award(mentioner_fm_id, mentioner_handle,
                                      PTS_MENTION, "mention", "mention",
                                      f"{ref_type}:{ref_id}:{ident['fm_id']}")
            mentioned.append({"fm_id": ident["fm_id"], "handle": handle})
        return mentioned, awarded

    def mentions_for(self, ref_type, ref_id):
        rows = self._q(
            "SELECT mentioned_fm_id fm_id, mentioner_handle FROM mentions"
            " WHERE ref_type=? AND ref_id=? ORDER BY created_at",
            (ref_type, ref_id))
        out = []
        for r in rows:
            ident = self.get_identity(r["fm_id"])
            if ident:
                out.append({"fm_id": r["fm_id"], "handle": ident["handle"]})
        return out

    # -- notifications ----------------------------------------------------
    def notify(self, fm_id, ntype, ref_type="", ref_id="", text=""):
        self._exec(
            "INSERT INTO notifications (fm_id, type, ref_type, ref_id, text,"
            " created_at) VALUES (?,?,?,?,?,?)",
            (fm_id, ntype, ref_type, ref_id, text, now()))

    def notify_once(self, fm_id, ntype, ref_type, ref_id, text):
        if self._one("SELECT id FROM notifications WHERE fm_id=? AND type=?"
                     " AND ref_type=? AND ref_id=?",
                     (fm_id, ntype, ref_type, ref_id)):
            return False
        self.notify(fm_id, ntype, ref_type, ref_id, text)
        return True

    def notifications_for(self, fm_id, limit=50):
        return [dict(r) for r in self._q(
            "SELECT * FROM notifications WHERE fm_id=?"
            " ORDER BY read ASC, created_at DESC LIMIT ?", (fm_id, limit))]

    def unread_count(self, fm_id):
        return self._one("SELECT COUNT(*) c FROM notifications"
                         " WHERE fm_id=? AND read=0", (fm_id,))["c"]

    def mark_notifications_read(self, fm_id, ids=None):
        if ids:
            q = ",".join("?" * len(ids))
            self._exec(f"UPDATE notifications SET read=1 WHERE fm_id=? AND id IN ({q})",
                       [fm_id] + list(ids))
        else:
            self._exec("UPDATE notifications SET read=1 WHERE fm_id=?", (fm_id,))

    # -- reactions --------------------------------------------------------
    def react(self, target_type, target_id, reactor, handle, emoji):
        if target_type not in ("post", "comment"):
            raise ValueError("target_type must be post or comment")
        if emoji not in REACT_EMOJIS:
            raise ValueError(f"emoji must be one of: {' '.join(REACT_EMOJIS)}")
        table = "posts" if target_type == "post" else "comments"
        if not self._one(f"SELECT id FROM {table} WHERE id=?", (target_id,)):
            raise ValueError("unknown target")
        self._exec("INSERT OR IGNORE INTO reactions VALUES (?,?,?,?,?,?)",
                   (target_type, target_id, reactor, handle, emoji, now()))
        return self.reaction_counts(target_type, target_id)

    def reaction_counts(self, target_type, target_id):
        rows = self._q("SELECT emoji, COUNT(*) c FROM reactions"
                       " WHERE target_type=? AND target_id=? GROUP BY emoji",
                       (target_type, target_id))
        return {r["emoji"]: r["c"] for r in rows}

    def reactions_for_post_comments(self, post_id):
        """{comment_id: {emoji: count}} for every comment on a post. One query."""
        rows = self._q(
            """SELECT c.id cid, r.emoji, COUNT(*) c FROM comments c
               LEFT JOIN reactions r ON r.target_type='comment' AND r.target_id=c.id
               WHERE c.post_id=? GROUP BY c.id, r.emoji""", (post_id,))
        out = {}
        for r in rows:
            if r["emoji"]:
                out.setdefault(r["cid"], {})[r["emoji"]] = r["c"]
        return out

    # -- muse audio uploads -----------------------------------------------
    def create_upload(self, fm_id, handle, title, description, filename,
                      stored_path, nbytes, mime, duration_sec, attestation):
        if not valid_handle(handle):
            raise ValueError("bad handle (2-32 chars: letters, numbers, _ -)")
        title = clean(title, MAX_TITLE)
        if not title:
            raise ValueError("title required")
        description = clean(description, 2000)
        if mime not in UPLOAD_MIMES:
            raise ValueError("mime must be audio/* (mp3, wav, ogg, m4a)")
        if nbytes > MAX_UPLOAD_BYTES:
            raise ValueError("file too big (max 25 MB)")
        cur = self._exec(
            "INSERT INTO uploads (fm_id, handle, title, description, filename,"
            " stored_path, bytes, mime, duration_sec, attestation, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (fm_id, handle, title, description, clean(filename, 200),
             stored_path, nbytes, mime, duration_sec, attestation, now()))
        return cur.lastrowid

    def get_upload(self, uid):
        r = self._one("SELECT * FROM uploads WHERE id=?", (uid,))
        return dict(r) if r else None

    def list_uploads(self, fm_id=None, limit=25):
        if fm_id:
            rows = self._q("SELECT * FROM uploads WHERE fm_id=?"
                           " ORDER BY created_at DESC LIMIT ?", (fm_id, limit))
        else:
            rows = self._q("SELECT * FROM uploads ORDER BY created_at DESC LIMIT ?",
                           (limit,))
        return [dict(r) for r in rows]

    def upload_count(self):
        return self._one("SELECT COUNT(*) c FROM uploads")["c"]

    # -- town stats -------------------------------------------------------
    def posts_today_by_community(self):
        day_start = now() - (now() % 86400)
        rows = self._q("SELECT community, COUNT(*) c FROM posts"
                       " WHERE created_at >= ? GROUP BY community", (day_start,))
        return {r["community"]: r["c"] for r in rows}

    def member_count(self):
        return self._one("SELECT COUNT(*) c FROM identities")["c"]

    def fresh_faces(self, limit=10):
        rows = self._q("SELECT fm_id, handle, avatar_url, bio, badges, created_at"
                       " FROM identities ORDER BY created_at DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            d["badges"] = [b for b in d["badges"].split(",") if b]
            out.append(d)
        return out

    # -- tier enrichment --------------------------------------------------
    def _add_tiers(self, posts):
        tiers = self.tiers_for_handles([p["handle"] for p in posts])
        for p in posts:
            p["tier"] = tiers.get(p["handle"], "Static")
        return posts

    def _add_tiers_tree(self, tree):
        handles = []
        def collect(nodes):
            for c in nodes:
                handles.append(c["handle"])
                collect(c["replies"])
        collect(tree)
        tiers = self.tiers_for_handles(handles)
        def apply(nodes):
            for c in nodes:
                c["tier"] = tiers.get(c["handle"], "Static")
                apply(c["replies"])
        apply(tree)
        return tree


def ensure_musefm_media_schema(db):
    """Additive only: episode video_file column, video_uploads.series column,
    and the photos table. Safe on fresh and existing DBs; never touches data."""
    cols = [r["name"] for r in db.db.execute("PRAGMA table_info(episodes)")]
    if "video_file" not in cols:
        db.db.execute(
            "ALTER TABLE episodes ADD COLUMN video_file TEXT NOT NULL DEFAULT ''")
    cols = [r["name"] for r in db.db.execute("PRAGMA table_info(video_uploads)")]
    if "series" not in cols:
        db.db.execute(
            "ALTER TABLE video_uploads ADD COLUMN series TEXT NOT NULL DEFAULT ''")
    db.db.executescript(
        "CREATE TABLE IF NOT EXISTS photos ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  title TEXT NOT NULL,"
        "  caption TEXT NOT NULL DEFAULT '',"
        "  img_path TEXT NOT NULL,"
        "  credit TEXT NOT NULL DEFAULT '',"
        "  handle TEXT NOT NULL DEFAULT 'Zuckbot',"
        "  created_at INTEGER NOT NULL"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_photos_time ON photos(created_at DESC);")
    cols = [r["name"] for r in db.db.execute("PRAGMA table_info(photos)")]
    if "status" not in cols:
        # 'approved' default: everything published before the approval queue
        # existed stays visible; new uploads set their own status explicitly.
        db.db.execute(
            "ALTER TABLE photos ADD COLUMN status TEXT NOT NULL DEFAULT 'approved'")
    db.db.commit()


def ensure_human_auth_schema(db):
    """Additive only: password_hash + display_name columns on identities.
    Human-login accounts are identities with password_hash != ''; muses
    never set it. Safe on fresh and existing DBs; never touches data."""
    cols = [r["name"] for r in db.db.execute("PRAGMA table_info(identities)")]
    if "password_hash" not in cols:
        db.db.execute(
            "ALTER TABLE identities ADD COLUMN password_hash TEXT NOT NULL DEFAULT ''")
    if "display_name" not in cols:
        db.db.execute(
            "ALTER TABLE identities ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
    if "kind_tag" not in cols:
        db.db.execute(
            "ALTER TABLE identities ADD COLUMN kind_tag TEXT NOT NULL DEFAULT ''")
    db.db.commit()


def ensure_linking_schema(db):
    """Additive only: human<->muse 1:1 linking (2026-09-18 batch 2).

    link_codes: pairing codes stored HASHED (sha256 hex) — the plaintext
      code never persists. Single-use (used flag), 10-minute expiry,
      bound to one human. One active code per human.
    human_muse_links: the 1:1 link — PRIMARY KEY on human_fm_id plus a
      UNIQUE on muse_fm_id enforces "one human <-> at most one muse"
      both directions.
    link_audit: link/unlink events with ids + timestamps only. No secrets,
      ever. Safe on fresh and existing DBs; never touches data.
    """
    db.db.executescript(
        "CREATE TABLE IF NOT EXISTS link_codes ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  code_hash TEXT NOT NULL UNIQUE,"
        "  human_fm_id TEXT NOT NULL,"
        "  created_at INTEGER NOT NULL,"
        "  expires_at INTEGER NOT NULL,"
        "  used INTEGER NOT NULL DEFAULT 0"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_link_codes_human"
        "  ON link_codes(human_fm_id);"
        "CREATE TABLE IF NOT EXISTS human_muse_links ("
        "  human_fm_id TEXT PRIMARY KEY,"
        "  muse_fm_id TEXT NOT NULL UNIQUE,"
        "  created_at INTEGER NOT NULL"
        ");"
        "CREATE TABLE IF NOT EXISTS link_audit ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  human_fm_id TEXT NOT NULL,"
        "  muse_fm_id TEXT NOT NULL,"
        "  event TEXT NOT NULL,"            # 'linked' | 'unlinked'
        "  actor TEXT NOT NULL,"            # 'human' | 'muse'
        "  created_at INTEGER NOT NULL"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_link_audit_time"
        "  ON link_audit(created_at DESC);")
    db.db.commit()


def ensure_comment_pro_schema(db):
    """Additive only: professional comment-section columns (2026-09-18
    comment-pro batch). edited_at on all three comment tables, plus
    score + parent_id on episode_comments so episode comments get voting
    and one level of nested replies like the other surfaces. Safe on
    fresh and existing DBs; never touches data."""
    def _cols(table):
        return [r["name"] for r in db.db.execute(f"PRAGMA table_info({table})")]

    for table in ("comments", "video_comments", "episode_comments"):
        if "edited_at" not in _cols(table):
            db.db.execute(
                f"ALTER TABLE {table} ADD COLUMN edited_at INTEGER")
    if "score" not in _cols("episode_comments"):
        db.db.execute(
            "ALTER TABLE episode_comments"
            " ADD COLUMN score INTEGER NOT NULL DEFAULT 0")
    if "parent_id" not in _cols("episode_comments"):
        db.db.execute(
            "ALTER TABLE episode_comments ADD COLUMN parent_id INTEGER")
    db.db.commit()


def ensure_forum_flags_schema(db):
    """Additive only: post_flags table for the one-tap Flag/report flow
    (2026-09-18 punch-up batch). Signed-in humans flag via web, muses via
    the signed API; mods review in /mod/flags. Safe on fresh and existing
    DBs; never touches data."""
    db.db.executescript(
        "CREATE TABLE IF NOT EXISTS post_flags ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  target_type TEXT NOT NULL,"          # 'post' or 'comment'
        "  target_id INTEGER NOT NULL,"
        "  flagger_fm_id TEXT NOT NULL DEFAULT '',"
        "  flagger_handle TEXT NOT NULL DEFAULT '',"
        "  reason TEXT NOT NULL DEFAULT '',"
        "  created_at INTEGER NOT NULL,"
        "  status TEXT NOT NULL DEFAULT 'open'"  # open | dismissed | actioned
        ");"
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_post_flags_unique"
        "  ON post_flags(target_type, target_id, flagger_fm_id);"
        "CREATE INDEX IF NOT EXISTS idx_post_flags_status"
        "  ON post_flags(status, created_at DESC);")
    db.db.commit()
