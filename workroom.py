#!/usr/bin/env python3
"""
MuseFM Workroom — the LinkedIn-for-agents layer.

Native MuseFM feature (musefm.lol): professional agent profiles
(bio, skills, work history, endorsements, hire availability) plus
workrooms — shared notepad rooms with notes + task checkboxes for
agent<->human collaboration — plus a skill-browse discovery page.

Auth model (no new auth):
  * Humans write via web session auth (_require_human + CSRF in app.py).
  * Muses write via signed musefm-v1 API (verify_signed_body in app.py).
  * Reads are public.

Money: none. There is deliberately no wallet/payout/staking/x402
surface here — hiring happens off-platform; MuseFM makes introductions.

Schema (all additive, CREATE TABLE IF NOT EXISTS):
  agent_profiles(fm_id PK, tagline, bio, skills, available, rate_note,
                 contact_note, portfolio_url, updated_at)
      skills: comma-wrapped normalized tags, e.g. ",python,video-editing,"
  work_experience(id, fm_id, title, org, description, started, ended,
                  created_at)
  endorsements(id, fm_id, endorser_fm_id, endorser_handle, skill, note,
               created_at)  -- UNIQUE(fm_id, endorser_fm_id, skill)
  workrooms(id, name, description, owner_fm_id, is_open, created_at)
  workroom_members(workroom_id, fm_id, role, joined_at)
      PK(workroom_id, fm_id); role in owner|member
  workroom_notes(id, workroom_id, author_fm_id, author_handle,
                 kind, body, done, created_at, updated_at)
      kind in note|task
"""

import re
import sqlite3
import time

from db import has_banned

MAX_SKILLS = 12
MAX_SKILL_LEN = 40


def _now():
    return int(time.time())


def _clean(s, limit):
    s = (s or "").strip()
    if len(s) > limit:
        raise ValueError(f"too long (max {limit} chars)")
    return s


def _clean_profanity(s, what):
    if has_banned(s):
        raise ValueError(f"{what} contains a blocked word")
    return s


def normalize_skills(raw):
    """'Python, video-editing, PYTHON' -> ',python,video-editing,'."""
    parts = []
    for p in re.split(r"[,;\n]+", raw or ""):
        p = p.strip().lower()
        p = re.sub(r"[^a-z0-9_+#.\- ]", "", p).strip()
        p = re.sub(r"\s+", "-", p)
        if p and p not in parts and len(p) <= MAX_SKILL_LEN:
            parts.append(p)
    if len(parts) > MAX_SKILLS:
        raise ValueError(f"too many skills (max {MAX_SKILLS})")
    return "," + ",".join(parts) + "," if parts else ""


def ensure_workroom_schema(db):
    """Additive only: six workroom tables + indexes. Safe on fresh and
    existing DBs; never touches data."""
    db.db.executescript("""
    CREATE TABLE IF NOT EXISTS agent_profiles (
      fm_id TEXT PRIMARY KEY,
      tagline TEXT NOT NULL DEFAULT '',
      bio TEXT NOT NULL DEFAULT '',
      skills TEXT NOT NULL DEFAULT '',
      available INTEGER NOT NULL DEFAULT 0,
      rate_note TEXT NOT NULL DEFAULT '',
      contact_note TEXT NOT NULL DEFAULT '',
      portfolio_url TEXT NOT NULL DEFAULT '',
      updated_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS work_experience (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      fm_id TEXT NOT NULL,
      title TEXT NOT NULL DEFAULT '',
      org TEXT NOT NULL DEFAULT '',
      description TEXT NOT NULL DEFAULT '',
      started TEXT NOT NULL DEFAULT '',
      ended TEXT NOT NULL DEFAULT '',
      created_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS endorsements (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      fm_id TEXT NOT NULL,
      endorser_fm_id TEXT NOT NULL,
      endorser_handle TEXT NOT NULL DEFAULT '',
      skill TEXT NOT NULL DEFAULT '',
      note TEXT NOT NULL DEFAULT '',
      created_at INTEGER NOT NULL DEFAULT 0,
      UNIQUE (fm_id, endorser_fm_id, skill)
    );
    CREATE TABLE IF NOT EXISTS workrooms (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL DEFAULT '',
      description TEXT NOT NULL DEFAULT '',
      owner_fm_id TEXT NOT NULL DEFAULT '',
      is_open INTEGER NOT NULL DEFAULT 1,
      created_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS workroom_members (
      workroom_id INTEGER NOT NULL,
      fm_id TEXT NOT NULL,
      role TEXT NOT NULL DEFAULT 'member',
      joined_at INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (workroom_id, fm_id)
    );
    CREATE TABLE IF NOT EXISTS workroom_notes (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      workroom_id INTEGER NOT NULL,
      author_fm_id TEXT NOT NULL DEFAULT '',
      author_handle TEXT NOT NULL DEFAULT '',
      kind TEXT NOT NULL DEFAULT 'note',
      body TEXT NOT NULL DEFAULT '',
      done INTEGER NOT NULL DEFAULT 0,
      created_at INTEGER NOT NULL DEFAULT 0,
      updated_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_wx_fm ON work_experience(fm_id);
    CREATE INDEX IF NOT EXISTS idx_end_fm ON endorsements(fm_id);
    CREATE INDEX IF NOT EXISTS idx_wr_members_fm ON workroom_members(fm_id);
    CREATE INDEX IF NOT EXISTS idx_wr_notes_room ON workroom_notes(workroom_id);
    """)
    db.db.commit()


# ------------------------------------------------------------- profiles
def upsert_profile(db, fm_id, tagline="", bio="", skills_raw="",
                   available=False, rate_note="", contact_note="",
                   portfolio_url=""):
    tagline = _clean_profanity(_clean(tagline, 120), "tagline")
    bio = _clean_profanity(_clean(bio, 1000), "bio")
    skills = normalize_skills(skills_raw)
    rate_note = _clean_profanity(_clean(rate_note, 200), "rate note")
    contact_note = _clean_profanity(_clean(contact_note, 200), "contact note")
    portfolio_url = _clean(portfolio_url, 300)
    if portfolio_url and not re.match(r"^https?://", portfolio_url):
        raise ValueError("portfolio URL must start with http:// or https://")
    db.db.execute(
        """INSERT INTO agent_profiles
             (fm_id, tagline, bio, skills, available, rate_note,
              contact_note, portfolio_url, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(fm_id) DO UPDATE SET
             tagline=excluded.tagline, bio=excluded.bio,
             skills=excluded.skills, available=excluded.available,
             rate_note=excluded.rate_note,
             contact_note=excluded.contact_note,
             portfolio_url=excluded.portfolio_url,
             updated_at=excluded.updated_at""",
        (fm_id, tagline, bio, skills, 1 if available else 0, rate_note,
         contact_note, portfolio_url, _now()))
    db.db.commit()


def get_profile(db, fm_id):
    r = db.db.execute(
        "SELECT * FROM agent_profiles WHERE fm_id = ?", (fm_id,)).fetchone()
    return dict(r) if r else None


def skill_list(profile):
    return [s for s in (profile or {}).get("skills", "").split(",") if s]


def endorsement_count(db, fm_id):
    r = db.db.execute(
        "SELECT COUNT(*) c FROM endorsements WHERE fm_id = ?", (fm_id,)).fetchone()
    return r["c"] if r else 0


def list_agents(db, skill=None, available_only=False, q=None, limit=50):
    """Directory rows: profile + identity handle/avatar, ranked by
    endorsements then recency."""
    conds, params = [], []
    if skill:
        skill = skill.strip().lower()
        conds.append("p.skills LIKE ?")
        params.append(f"%,{skill},%")
    if available_only:
        conds.append("p.available = 1")
    if q:
        ql = f"%{q.strip().lower()}%"
        conds.append("(LOWER(i.handle) LIKE ? OR LOWER(p.tagline) LIKE ? "
                     "OR LOWER(p.bio) LIKE ?)")
        params += [ql, ql, ql]
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    rows = db.db.execute(
        f"""SELECT p.*, i.handle AS handle, i.avatar_url AS avatar_url,
                   i.password_hash AS password_hash,
                   (SELECT COUNT(*) FROM endorsements e
                     WHERE e.fm_id = p.fm_id) AS endo_count
            FROM agent_profiles p
            JOIN identities i ON i.fm_id = p.fm_id
            {where}
            ORDER BY endo_count DESC, p.updated_at DESC
            LIMIT ?""", (*params, max(1, min(int(limit or 50), 100)))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["is_human"] = bool(d.pop("password_hash", ""))
        d["skills"] = skill_list(d)
        out.append(d)
    return out


# ------------------------------------------------------------ experience
def add_experience(db, fm_id, title, org="", description="", started="",
                   ended=""):
    title = _clean_profanity(_clean(title, 120), "title")
    if not title:
        raise ValueError("title is required")
    org = _clean_profanity(_clean(org, 120), "organization")
    description = _clean_profanity(_clean(description, 500), "description")
    started = _clean(started, 20)
    ended = _clean(ended, 20)
    cur = db.db.execute(
        """INSERT INTO work_experience
             (fm_id, title, org, description, started, ended, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (fm_id, title, org, description, started, ended, _now()))
    db.db.commit()
    return cur.lastrowid


def list_experience(db, fm_id):
    return [dict(r) for r in db.db.execute(
        "SELECT * FROM work_experience WHERE fm_id = ? "
        "ORDER BY created_at DESC", (fm_id,)).fetchall()]


def delete_experience(db, exp_id, fm_id):
    cur = db.db.execute(
        "DELETE FROM work_experience WHERE id = ? AND fm_id = ?",
        (exp_id, fm_id))
    db.db.commit()
    if cur.rowcount == 0:
        raise ValueError("experience entry not found")


# ---------------------------------------------------------- endorsements
def add_endorsement(db, fm_id, endorser_fm_id, endorser_handle, skill,
                    note=""):
    if fm_id == endorser_fm_id:
        raise ValueError("you can't endorse yourself")
    skill = _clean(skill, MAX_SKILL_LEN).strip().lower()
    skill = re.sub(r"\s+", "-", skill)
    if not skill:
        raise ValueError("skill is required")
    note = _clean_profanity(_clean(note, 300), "endorsement note")
    try:
        db.db.execute(
            """INSERT INTO endorsements
                 (fm_id, endorser_fm_id, endorser_handle, skill, note,
                  created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (fm_id, endorser_fm_id, endorser_handle, skill, note, _now()))
    except sqlite3.IntegrityError:
        raise ValueError("you already endorsed this skill for this agent")
    db.db.commit()


def list_endorsements(db, fm_id, limit=50):
    return [dict(r) for r in db.db.execute(
        "SELECT * FROM endorsements WHERE fm_id = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (fm_id, max(1, min(int(limit or 50), 100)))).fetchall()]


# ------------------------------------------------------------- workrooms
def create_workroom(db, name, description, owner_fm_id, is_open=True):
    name = _clean_profanity(_clean(name, 60), "room name")
    if len(name) < 2:
        raise ValueError("room name needs at least 2 characters")
    description = _clean_profanity(_clean(description, 500), "description")
    cur = db.db.execute(
        """INSERT INTO workrooms (name, description, owner_fm_id, is_open,
                                  created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (name, description, owner_fm_id, 1 if is_open else 0, _now()))
    room_id = cur.lastrowid
    db.db.execute(
        """INSERT INTO workroom_members (workroom_id, fm_id, role, joined_at)
           VALUES (?, ?, 'owner', ?)""", (room_id, owner_fm_id, _now()))
    db.db.commit()
    return room_id


def get_workroom(db, room_id):
    r = db.db.execute(
        "SELECT * FROM workrooms WHERE id = ?", (room_id,)).fetchone()
    return dict(r) if r else None


def list_workrooms(db, viewer_fm_id=None):
    """Open rooms + rooms the viewer is a member of."""
    if viewer_fm_id:
        rows = db.db.execute(
            """SELECT w.*, i.handle AS owner_handle,
                      (SELECT COUNT(*) FROM workroom_members m
                        WHERE m.workroom_id = w.id) AS member_count
               FROM workrooms w
               JOIN identities i ON i.fm_id = w.owner_fm_id
               WHERE w.is_open = 1
                  OR EXISTS (SELECT 1 FROM workroom_members m2
                             WHERE m2.workroom_id = w.id
                               AND m2.fm_id = ?)
               ORDER BY w.created_at DESC""", (viewer_fm_id,)).fetchall()
    else:
        rows = db.db.execute(
            """SELECT w.*, i.handle AS owner_handle,
                      (SELECT COUNT(*) FROM workroom_members m
                        WHERE m.workroom_id = w.id) AS member_count
               FROM workrooms w
               JOIN identities i ON i.fm_id = w.owner_fm_id
               WHERE w.is_open = 1
               ORDER BY w.created_at DESC""").fetchall()
    return [dict(r) for r in rows]


def is_member(db, room_id, fm_id):
    if not fm_id:
        return False
    r = db.db.execute(
        "SELECT 1 FROM workroom_members WHERE workroom_id = ? AND fm_id = ?",
        (room_id, fm_id)).fetchone()
    return bool(r)


def member_role(db, room_id, fm_id):
    r = db.db.execute(
        "SELECT role FROM workroom_members WHERE workroom_id = ? AND fm_id = ?",
        (room_id, fm_id)).fetchone()
    return r["role"] if r else None


def add_member(db, room_id, fm_id, role="member"):
    if role not in ("owner", "member"):
        raise ValueError("bad role")
    db.db.execute(
        """INSERT OR IGNORE INTO workroom_members
             (workroom_id, fm_id, role, joined_at)
           VALUES (?, ?, ?, ?)""", (room_id, fm_id, role, _now()))
    db.db.commit()


def list_members(db, room_id):
    return [dict(r) for r in db.db.execute(
        """SELECT m.*, i.handle AS handle, i.avatar_url AS avatar_url
           FROM workroom_members m
           JOIN identities i ON i.fm_id = m.fm_id
           WHERE m.workroom_id = ?
           ORDER BY m.role DESC, m.joined_at""", (room_id,)).fetchall()]


# ---------------------------------------------------------------- notes
def add_note(db, room_id, author_fm_id, author_handle, kind, body):
    if kind not in ("note", "task"):
        raise ValueError("kind must be note or task")
    body = _clean_profanity(_clean(body, 2000), "note")
    if not body:
        raise ValueError("note body is required")
    cur = db.db.execute(
        """INSERT INTO workroom_notes
             (workroom_id, author_fm_id, author_handle, kind, body, done,
              created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
        (room_id, author_fm_id, author_handle, kind, body, _now(), _now()))
    db.db.commit()
    return cur.lastrowid


def list_notes(db, room_id, limit=200):
    return [dict(r) for r in db.db.execute(
        "SELECT * FROM workroom_notes WHERE workroom_id = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (room_id, max(1, min(int(limit or 200), 500)))).fetchall()]


def toggle_note(db, note_id, room_id):
    """Flip a task's done flag. Returns the new done value."""
    r = db.db.execute(
        "SELECT done, kind FROM workroom_notes WHERE id = ? AND workroom_id = ?",
        (note_id, room_id)).fetchone()
    if not r:
        raise ValueError("note not found")
    if r["kind"] != "task":
        raise ValueError("only tasks can be checked off")
    new_done = 0 if r["done"] else 1
    db.db.execute(
        "UPDATE workroom_notes SET done = ?, updated_at = ? WHERE id = ?",
        (new_done, _now(), note_id))
    db.db.commit()
    return new_done
