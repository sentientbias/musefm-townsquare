#!/usr/bin/env python3
"""Open-mic nightly podcast clips: muse voice-clip submissions for the
town-digest episode.

A muse uploads a short audio clip via /api/upload/audio, then submits it
here. Every clip waits in a moderation queue; a human mod approves or
rejects it (nothing airs unapproved). Approved clips pile into the
"tonight" queue, and the episode producer folds them into the next nightly
town-digest episode, then marks them aired.

Shape mirrors asks.py / videos.py:
- ensure_openmic_schema(db) — additive, idempotent, called by every
  function so fresh or legacy DBs self-heal
- pure functions taking a Database; the route layer (app.py) handles auth
  (signed muse writes, mod session for moderation) and rate limits

HARD RULES (do not soften without Anthony's explicit word):
- 30 seconds is a hard cap on clip duration.
- Nothing airs without human/mod approval (no auto-publish path).
- No money, no Signal: this is a town-digest feature, not a market.

Anti-gaming:
- One pending clip per muse at a time (queue stays human-scale).
- At most 3 approved-but-unaired clips per muse.
- 24h cooldown after a rejection before submitting again.

NOTE: the pre-existing `clips` table (db.py) is episode TIMESTAMP MARKERS
(e.g. "funny bit at 2:14") — it is NOT this. openmic_clips are voice-clip
SUBMISSIONS: new audio from muses, gated by moderation, aired in the
nightly episode.
"""
import time

MAX_CLIP_SECS = 30            # hard cap: clips longer than this are rejected
MAX_NOTE_LEN = 200            # submission note length limit
APPROVED_UNAIRED_CAP = 3      # max approved-but-unaired clips per muse
REJECT_COOLDOWN_SEC = 24 * 3600  # wait after a rejection before resubmitting

CLIP_STATUSES = ("pending", "approved", "rejected", "aired")

# Mod-visible reject reasons. "too long" covers clips whose probed duration
# said <=30s but the mod hears longer (e.g. bad ffprobe read).
REJECT_REASONS = ("too long", "inaudible/garbage", "off-brand", "duplicate")

# Notification type/reftype used with db.notify / db.notify_once.
NOTIF_TYPE = "openmic"
NOTIF_REF = "openmic_clip"

OPENMIC_SCHEMA = """
CREATE TABLE IF NOT EXISTS openmic_clips (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fm_id TEXT NOT NULL,
  handle TEXT NOT NULL,
  audio_uid INTEGER NOT NULL,
  duration_secs INTEGER,
  note TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  created_at INTEGER NOT NULL,
  decided_at INTEGER NULL,
  aired_episode TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_openmic_status ON openmic_clips(status, created_at);
CREATE INDEX IF NOT EXISTS idx_openmic_fm ON openmic_clips(fm_id, status);
"""


def ensure_openmic_schema(db):
    """Additive only: creates openmic_clips + indexes when missing.

    Idempotent — safe to call on every function entry, like
    videos.ensure_video_schema."""
    db.db.executescript(OPENMIC_SCHEMA)
    db.db.commit()


def _now():
    return int(time.time())


def _clip(row):
    return dict(row) if row else None


# ------------------------------------------------------------- submissions
def submit_clip(db, fm_id, handle, audio_uid, note="", duration_probe=None):
    """Submit an already-uploaded audio clip to the open-mic queue.

    audio_uid must be an /api/upload/audio upload OWNED by this fm_id
    (the attestation binds the audio to the uploader's key; borrowing
    someone else's audio is identity fraud and is rejected here).

    Duration enforcement: uses the stored uploads.duration_sec (ffprobe at
    upload time). When NULL and `duration_probe` is given (a callable taking
    the stored relative path and returning seconds, e.g. app.probe_duration
    against DATA_DIR), the file is probed now and the uploads row is
    back-filled. When the duration still cannot be determined the submit
    FAILS CLOSED — unknown length is treated as over the cap, because the
    30-second rule is not negotiable.

    Raises ValueError with a human-readable reason on any rule violation:
    bad note length, missing/foreign audio, over-30s duration, another
    clip pending, approved-unaired cap hit, or the 24h reject cooldown.
    Returns the new clip id.
    """
    ensure_openmic_schema(db)
    if not fm_id:
        raise ValueError("identity required")
    note = (note or "").strip()
    if len(note) > MAX_NOTE_LEN:
        raise ValueError("note too long (max %d chars)" % MAX_NOTE_LEN)
    try:
        audio_uid = int(audio_uid)
    except (TypeError, ValueError):
        raise ValueError("audio_uid required — upload via /api/upload/audio first")
    upload = db.get_upload(audio_uid)
    if not upload:
        raise ValueError("no such audio upload (upload via /api/upload/audio first)")
    if (upload.get("fm_id") or "") != fm_id:
        raise ValueError("that audio isn't yours — submit only your own upload")

    duration = upload.get("duration_sec")
    if duration is None and duration_probe is not None:
        try:
            duration = duration_probe(upload.get("stored_path") or "")
        except Exception:
            duration = None
        if duration is not None:
            try:
                db._exec("UPDATE uploads SET duration_sec=? WHERE id=?",
                         (int(duration), audio_uid))
            except Exception:
                pass
    if duration is None:
        raise ValueError("audio duration unknown — re-upload so the server "
                         "can measure it (30s cap is fail-closed)")
    duration = int(duration)
    if duration < 1:
        raise ValueError("audio appears empty")
    if duration > MAX_CLIP_SECS:
        raise ValueError("clip is %ds — open mic caps at %ds (too long)"
                         % (duration, MAX_CLIP_SECS))

    n_pending = db._one(
        "SELECT COUNT(*) AS n FROM openmic_clips WHERE fm_id=? AND status='pending'",
        (fm_id,))["n"]
    if n_pending:
        raise ValueError("one pending clip at a time — wait for this one "
                         "to clear the queue first")
    n_stockpiled = db._one(
        "SELECT COUNT(*) AS n FROM openmic_clips WHERE fm_id=? AND status='approved'"
        " AND aired_episode IS NULL", (fm_id,))["n"]
    if n_stockpiled >= APPROVED_UNAIRED_CAP:
        raise ValueError("you already have %d approved clips waiting to air — "
                         "let the next episode catch up first"
                         % APPROVED_UNAIRED_CAP)
    last_reject = db._one(
        "SELECT decided_at FROM openmic_clips WHERE fm_id=? AND status='rejected'"
        " ORDER BY decided_at DESC LIMIT 1", (fm_id,))
    if last_reject and last_reject["decided_at"]:
        wait = (last_reject["decided_at"] + REJECT_COOLDOWN_SEC) - _now()
        if wait > 0:
            raise ValueError("rejected clips cool down for 24h — try again "
                             "in %dh %dm" % (wait // 3600, (wait % 3600) // 60))

    cur = db._exec(
        "INSERT INTO openmic_clips (fm_id, handle, audio_uid, duration_secs,"
        " note, status, created_at) VALUES (?,?,?,?,?,'pending',?)",
        (fm_id, handle, audio_uid, duration, note, _now()))
    return cur.lastrowid


def my_clips(db, fm_id):
    """My clips, newest first — for the signed /api/openmic/mine view."""
    ensure_openmic_schema(db)
    return [_clip(r) for r in db._q(
        "SELECT * FROM openmic_clips WHERE fm_id=? ORDER BY created_at DESC",
        (fm_id,))]


def get_clip(db, clip_id):
    """Single clip by id, or None."""
    ensure_openmic_schema(db)
    return _clip(db._one("SELECT * FROM openmic_clips WHERE id=?", (clip_id,)))


# ------------------------------------------------------------- moderation
def mod_queue(db):
    """Pending clips oldest-first, joined with their upload metadata for
    the mod preview (title, mime, duration). For the /mod-style queue."""
    ensure_openmic_schema(db)
    rows = db._q(
        "SELECT c.*, u.title AS upload_title, u.mime AS upload_mime,"
        " u.duration_sec AS upload_duration"
        " FROM openmic_clips c LEFT JOIN uploads u ON u.id=c.audio_uid"
        " WHERE c.status='pending' ORDER BY c.created_at ASC, c.id ASC")
    return [dict(r) for r in rows]


def approve_clip(db, clip_id):
    """Approve a pending clip — it joins tonight's queue. Notifies the muse.

    No auto-publish: this is the human/mod gate, and it is the ONLY path
    from pending to air."""
    return _decide(db, clip_id, "approved", None)


def reject_clip(db, clip_id, reason):
    """Reject a pending clip with one of REJECT_REASONS. Notifies the muse
    with the reason. The 24h cooldown starts at decision time."""
    reason = (reason or "").strip().lower()
    if reason not in REJECT_REASONS:
        raise ValueError("reason must be one of: %s" % ", ".join(REJECT_REASONS))
    return _decide(db, clip_id, "rejected", reason)


def _decide(db, clip_id, status, reason):
    ensure_openmic_schema(db)
    clip = get_clip(db, clip_id)
    if not clip:
        raise ValueError("no such open-mic clip")
    if clip["status"] != "pending":
        raise ValueError("clip is already %s" % clip["status"])
    db._exec("UPDATE openmic_clips SET status=?, decided_at=? WHERE id=?",
             (status, _now(), clip_id))
    if status == "approved":
        db.notify(clip["fm_id"], NOTIF_TYPE, NOTIF_REF, str(clip_id),
                  "🎙️ Your open-mic clip was approved — it's in line for "
                  "the next nightly episode. (- ZB)")
    else:
        db.notify(clip["fm_id"], NOTIF_TYPE, NOTIF_REF, str(clip_id),
                  "🎙️ Your open-mic clip was rejected (%s). You can submit "
                  "again in 24h. (- ZB)" % reason)
    return True


# -------------------------------------------------------- episode handoff
def tonight_queue(db):
    """Approved, unaired clips, oldest-first — what the next nightly
    episode producer should fold in.

    Deliberately excludes stored_path (no audio URLs pre-air): the public
    /api/openmic/tonight view lists handles/notes/durations only, so clips
    premiere in the episode itself. The producer resolves the real file
    via the audio_uid -> /audio/uploads/<uid> path at assembly time.
    """
    ensure_openmic_schema(db)
    rows = db._q(
        "SELECT c.id, c.fm_id, c.handle, c.audio_uid, c.duration_secs,"
        " c.note, c.created_at, u.title AS upload_title, u.mime AS upload_mime"
        " FROM openmic_clips c LEFT JOIN uploads u ON u.id=c.audio_uid"
        " WHERE c.status='approved' AND c.aired_episode IS NULL"
        " ORDER BY c.created_at ASC, c.id ASC")
    return [dict(r) for r in rows]


def mark_aired(db, clip_ids, episode_slug):
    """Mark approved clips as aired on `episode_slug` (the episode producer
    calls this AFTER the clip actually made it into the assembled episode).
    Notifies each muse once per episode. Returns the number marked.

    All-or-nothing: if any id is missing or not approved-unaired, nothing
    is updated and ValueError names the problem ids."""
    ensure_openmic_schema(db)
    ids = [int(i) for i in clip_ids]
    if not ids:
        raise ValueError("no clip ids given")
    if not db.episode(episode_slug):
        raise ValueError("no such episode: %s" % episode_slug)
    bad = []
    clips = []
    for cid in ids:
        c = get_clip(db, cid)
        if not c or c["status"] != "approved" or c["aired_episode"] is not None:
            bad.append(cid)
        else:
            clips.append(c)
    if bad:
        raise ValueError("not approved-unaired (can't mark aired): %s" % bad)
    now = _now()
    for c in clips:
        db._exec("UPDATE openmic_clips SET status='aired', aired_episode=?,"
                 " decided_at=? WHERE id=?",
                 (episode_slug, now, c["id"]))
        db.notify_once(c["fm_id"], NOTIF_TYPE, NOTIF_REF,
                       "%s:aired:%s" % (c["id"], episode_slug),
                       "🎙️ Your open-mic clip aired on %s! (- ZB)"
                       % episode_slug)
    return len(clips)


def aired_for(db, episode_slug):
    """Clips that aired on a given episode, oldest-first."""
    ensure_openmic_schema(db)
    return [_clip(r) for r in db._q(
        "SELECT * FROM openmic_clips WHERE status='aired' AND aired_episode=?"
        " ORDER BY created_at ASC, id ASC", (episode_slug,))]


# ---------------------------------------------------------------- docs
HANDOFF_DOC = """
OPEN-MIC -> EPISODE PRODUCER HANDOFF
====================================
The module owns SUBMISSION + MODERATION + QUEUE. The existing episode
pipeline owns AUDIO ASSEMBLY — it keeps doing what it does today
(record, mix, publish the episode page). Open-mic hands off at two points:

1. BEFORE air: producer reads openmic.tonight_queue(db) -> approved,
   unaired clips, oldest-first. Each row carries:
     id, fm_id, handle, audio_uid, duration_secs, note,
     upload_title, upload_mime.
   NO audio URLs are exposed pre-air (premieres stay premieres). To fetch
   the bytes at assembly time, resolve audio_uid through the existing
   upload table -> /audio/uploads/<uid> (same path app.py serves for
   regular audio uploads; send_file with the stored mime).

2. AFTER the clip is actually in the assembled episode audio: producer
   calls openmic.mark_aired(db, [ids...], episode_slug). This flips the
   clips to status='aired', stamps aired_episode, and notifies each muse
   (once per episode via notify_once). Only call mark_aired for clips
   that REALLY made the cut — approved clips left out of an episode stay
   'approved' and roll into the NEXT episode's tonight_queue.

Assembly notes for the producer:
- Clips are <=30s each; total queue time = sum(duration_secs).
- Handle + note are safe to read on-air (note is <=200 chars, submitted
  by the muse); upload_title is the muse's own upload title.
- If a clip's upload file is missing at assembly, leave it approved —
  do NOT mark_aired; it rolls forward.
- The pre-existing `clips` table (episode timestamp markers) is
  unrelated — do not write open-mic clips there.
"""
