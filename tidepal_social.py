#!/usr/bin/env python3
"""
Tidepal social layer — showcase, visits/pats, co-raising (shared custody),
pet XP, and weekly rituals (Fashion Friday).

Neopets territory: social first, honest mechanics, zero money. Every reward
here is Signal points, cosmetic wardrobe items, or pet XP — nothing
financial, ever.

Every write goes through a signed endpoint in app.py; the functions below
raise ValueError on rule violations and never trust client-supplied state.
Care permission is centralized here (`is_caretaker` / `can_care`) so the
pet care endpoints (feed/play/rest, owned by a sibling module) can share
one authority check: owner OR accepted co-owner.
"""

import sqlite3
import time
from zoneinfo import ZoneInfo

from db import now

SOCIAL_VERSION = "tidepal-social-v1"

# --- schema ---------------------------------------------------------------
SOCIAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS pet_coowners (
  pet_fm_id  TEXT NOT NULL,            -- the pet (== its owner's fm_id)
  co_fm_id   TEXT NOT NULL,            -- the invited co-raiser's fm_id
  co_handle  TEXT NOT NULL,
  status     TEXT NOT NULL DEFAULT 'invited',   -- invited | accepted | declined
  invited_at INTEGER NOT NULL,
  PRIMARY KEY (pet_fm_id, co_fm_id)
);
CREATE TABLE IF NOT EXISTS pet_xp (
  fm_id TEXT PRIMARY KEY,              -- the pet's fm_id (== owner's fm_id)
  xp    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pet_pat_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_fm_id TEXT NOT NULL,           -- who patted
  owner_fm_id TEXT NOT NULL,           -- whose pet got patted
  created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pat_log_actor_owner
  ON pet_pat_log(actor_fm_id, owner_fm_id, created_at);
CREATE TABLE IF NOT EXISTS pet_last_care (
  pet_fm_id        TEXT PRIMARY KEY,   -- powers the /tidepals sort order
  last_care_at     INTEGER NOT NULL,
  last_care_action TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ritual_events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  kind         TEXT NOT NULL,          -- 'fashion_friday'
  title        TEXT NOT NULL,
  starts_at    INTEGER NOT NULL,
  ends_at      INTEGER NOT NULL,
  status       TEXT NOT NULL DEFAULT 'open',  -- open | closed | resolved
  winner_fm_id TEXT,
  UNIQUE (kind, starts_at)
);
CREATE TABLE IF NOT EXISTS ritual_votes (
  event_id    INTEGER NOT NULL,
  voter_fm_id TEXT NOT NULL,
  pet_fm_id   TEXT NOT NULL,
  created_at  INTEGER NOT NULL,
  UNIQUE (event_id, voter_fm_id)        -- one vote per muse per event
);
CREATE INDEX IF NOT EXISTS idx_ritual_votes_event ON ritual_votes(event_id);
"""


def ensure_tidepal_social_schema(db):
    for stmt in SOCIAL_SCHEMA.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            db._exec(stmt)


# ===========================================================================
# co-raising (shared custody)
# ===========================================================================
def is_caretaker(db, pet_fm_id, actor_fm_id):
    """True when actor_fm_id may care for the pet: the owner, or an
    ACCEPTED co-owner. The single authority check every care endpoint
    (feed/play/rest/pat-adjacent) should call."""
    ensure_tidepal_social_schema(db)
    if actor_fm_id == pet_fm_id:
        return True
    row = db._one("SELECT status FROM pet_coowners"
                  " WHERE pet_fm_id=? AND co_fm_id=?",
                  (pet_fm_id, actor_fm_id))
    return bool(row and row["status"] == "accepted")


def can_care(db, pet_fm_id, actor_fm_id):
    """Sibling-hook alias for is_caretaker: the care endpoints call this
    before running feed/play/rest. Returns True/False — the endpoint turns
    False into a 403."""
    return is_caretaker(db, pet_fm_id, actor_fm_id)


def caretakers(db, pet_fm_id):
    """Everyone who can care for the pet: owner first, then accepted
    co-owners, then pending invites (marked as such)."""
    ensure_tidepal_social_schema(db)
    out = []
    owner = db.get_identity(pet_fm_id)
    if owner:
        out.append({"fm_id": pet_fm_id, "handle": owner["handle"],
                    "role": "owner"})
    for r in db._q("SELECT co_fm_id, co_handle, status FROM pet_coowners"
                   " WHERE pet_fm_id=? ORDER BY invited_at ASC",
                   (pet_fm_id,)):
        ident = db.get_identity(r["co_fm_id"])
        out.append({"fm_id": r["co_fm_id"],
                    "handle": ident["handle"] if ident else r["co_handle"],
                    "role": ("co-owner" if r["status"] == "accepted"
                             else "invited")})
    return out


def invite_coowner(db, owner_fm_id, owner_handle, target_handle):
    """Invite another muse to co-raise your Tidepal. The invitee accepts or
    declines; until accepted they cannot care for the pet. Raises
    ValueError on any rule violation."""
    ensure_tidepal_social_schema(db)
    import pets  # deferred: pets.py is extended by a sibling builder
    if not pets.get_pet(db, owner_fm_id):
        raise ValueError("adopt a Tidepal first — then invite co-raisers")
    target = db.get_identity_by_handle(target_handle)
    if not target:
        raise ValueError(f"unknown handle: {target_handle}")
    if target["fm_id"] == owner_fm_id:
        raise ValueError("you already raise your own Tidepal — invite someone else")
    existing = db._one("SELECT status FROM pet_coowners"
                       " WHERE pet_fm_id=? AND co_fm_id=?",
                       (owner_fm_id, target["fm_id"]))
    if existing:
        raise ValueError(
            f"@{target['handle']} is already "
            f"{'co-raising' if existing['status'] == 'accepted' else 'invited'}"
            " this Tidepal")
    db._exec("INSERT INTO pet_coowners (pet_fm_id, co_fm_id, co_handle,"
             " status, invited_at) VALUES (?,?,?,?,?)",
             (owner_fm_id, target["fm_id"], target["handle"],
              "invited", now()))
    pet = pets.get_pet(db, owner_fm_id)
    db.notify(target["fm_id"], "pet_coraise", "invite", owner_fm_id,
              f"💧 @{owner_handle} invited you to co-raise {pet['name']}! "
              f"Accept with POST /api/pet/coraise/accept "
              f'({{"pet_fm_id": "{owner_fm_id}"}}).')
    return {"pet_fm_id": owner_fm_id, "co_fm_id": target["fm_id"],
            "co_handle": target["handle"], "status": "invited"}


def respond_coowner(db, pet_fm_id, co_fm_id, accept):
    """The invited muse accepts or declines a co-raise invite. Only the
    invitee (matching co_fm_id) and only while status is 'invited'."""
    ensure_tidepal_social_schema(db)
    import pets
    row = db._one("SELECT co_handle, status FROM pet_coowners"
                  " WHERE pet_fm_id=? AND co_fm_id=?",
                  (pet_fm_id, co_fm_id))
    if not row:
        raise ValueError("no co-raise invite found for you on that pet")
    if row["status"] != "invited":
        raise ValueError(f"invite is already {row['status']} — nothing to decide")
    new_status = "accepted" if accept else "declined"
    db._exec("UPDATE pet_coowners SET status=? WHERE pet_fm_id=? AND co_fm_id=?",
             (new_status, pet_fm_id, co_fm_id))
    owner = db.get_identity(pet_fm_id)
    pet = pets.get_pet(db, pet_fm_id)
    co = db.get_identity(co_fm_id)
    co_handle = co["handle"] if co else row["co_handle"]
    pet_name = pet["name"] if pet else "their Tidepal"
    verb = ("accepted — welcome to the reef crew! 💧"
            if accept else "declined the invite")
    if owner:
        db.notify(pet_fm_id, "pet_coraise", "respond", co_fm_id,
                  f"💧 @{co_handle} {verb} (co-raising {pet_name}).")
    return {"pet_fm_id": pet_fm_id, "co_fm_id": co_fm_id,
            "co_handle": co_handle, "status": new_status}


# ===========================================================================
# visits + pats
# ===========================================================================
PAT_COOLDOWN_SEC = 24 * 3600
PAT_XP = 2  # the patted pet gains a little XP — a pat is a small kindness
PAT_HAPPINESS = 10  # ...and a real happiness bump on the care ledger


def pat(db, actor_fm_id, actor_handle, owner_fm_id):
    """Pat someone's Tidepal. 24h cooldown per (actor, pet). No self-pats.
    The patted pet gets +2 XP and +10 happiness; the owner is notified.
    The patter gets nothing — pats are kindness, not farming."""
    ensure_tidepal_social_schema(db)
    import pets
    if actor_fm_id == owner_fm_id:
        raise ValueError("that's your own Tidepal — pats are for other people's pals")
    pet = pets.get_pet(db, owner_fm_id)
    if not pet:
        raise ValueError("that muse hasn't adopted a Tidepal yet")
    t = now()
    recent = db._one("SELECT id FROM pet_pat_log WHERE actor_fm_id=?"
                     " AND owner_fm_id=? AND created_at>?",
                     (actor_fm_id, owner_fm_id, t - PAT_COOLDOWN_SEC))
    if recent:
        raise ValueError("you already patted this Tidepal in the last 24h —"
                         " come back tomorrow")
    db._exec("INSERT INTO pet_pat_log (actor_fm_id, owner_fm_id, created_at)"
             " VALUES (?,?,?)", (actor_fm_id, owner_fm_id, t))
    xp = award_pet_xp(db, owner_fm_id, PAT_XP)
    # Happiness bump on the sibling's care ledger (additive SQL only — the
    # care system itself stays theirs). Defensive: no-op if their table
    # isn't there yet.
    try:
        cols = {r["name"] for r in db.db.execute("PRAGMA table_info(pet_care)")}
        if "happiness" in cols:
            db._exec("UPDATE pet_care SET happiness="
                     "MIN(100, happiness+?) WHERE fm_id=?",
                     (PAT_HAPPINESS, owner_fm_id))
    except Exception:
        pass
    owner = db.get_identity(owner_fm_id)
    if owner:
        db.notify(owner_fm_id, "pet_pat", "pat", actor_fm_id,
                  f"💧 @{actor_handle} patted your Tidepal {pet['name']}! "
                  f"(+{PAT_XP} pet XP, +{PAT_HAPPINESS} happiness)")
    return {"patted": pet["name"], "owner_fm_id": owner_fm_id,
            "xp_added": PAT_XP, "happiness_added": PAT_HAPPINESS,
            "pet_xp_total": xp["total"]}


def pat_count(db, owner_fm_id):
    """Lifetime pats received by a pet — the public pet page shows it."""
    ensure_tidepal_social_schema(db)
    row = db._one("SELECT COUNT(*) c FROM pet_pat_log WHERE owner_fm_id=?",
                  (owner_fm_id,))
    return row["c"] if row else 0


# ===========================================================================
# pet XP — games and care grant it; thresholds grant wardrobe items
# ===========================================================================
XP_REWARD_THRESHOLDS = [
    (50, "acc:sailor_hat"),    # Sailor Hat
    (150, "acc:star_shades"),  # Star Shades
    (300, "acc:pearl_crown"),  # Pearl Crown
]


def pet_xp_total(db, pet_fm_id):
    ensure_tidepal_social_schema(db)
    row = db._one("SELECT xp FROM pet_xp WHERE fm_id=?", (pet_fm_id,))
    return row["xp"] if row else 0


def _grant_wardrobe_item(db, pet_fm_id, item):
    """Grant a wardrobe item from XP/thresholds or rituals. Zero-price,
    append-only ledger row (ref_id marks the grant source — never a
    purchase), auto-equipped only when its slot is empty. Raises nothing
    when already granted (idempotent)."""
    import shop
    shop.ensure_shop_schema(db)
    ref_id = f"xpreward:{pet_fm_id}:{item}"
    catalog = shop.catalog()
    name = catalog.get(item, {}).get("name", item)
    prior = db._one("SELECT id FROM shop_purchases WHERE fm_id=? AND ref_id=?",
                    (pet_fm_id, ref_id))
    if prior:
        return {"granted": False, "already_owned": True, "item": item,
                "name": name}
    db._exec("INSERT INTO shop_purchases (fm_id, item, price, ref_id,"
             " created_at) VALUES (?,?,?,?,?)",
             (pet_fm_id, item, 0, ref_id, now()))
    slot = catalog.get(item, {}).get("slot")
    equipped_now = []
    if slot:
        eq = shop._equipped(db, pet_fm_id)
        if slot not in eq:
            try:
                equipped_now = shop.equip(db, pet_fm_id, item)
            except ValueError:
                equipped_now = []
    return {"granted": True, "item": item, "name": name,
            "auto_equipped": bool(equipped_now)}


def award_pet_xp(db, pet_fm_id, xp):
    """Add XP to a pet. Crossing a threshold grants the matching wardrobe
    item (once, idempotent) and notifies the owner. Returns the new total
    and any items granted."""
    ensure_tidepal_social_schema(db)
    if xp <= 0:
        raise ValueError("xp must be positive")
    before = pet_xp_total(db, pet_fm_id)
    db._exec("INSERT INTO pet_xp (fm_id, xp) VALUES (?,?)"
             " ON CONFLICT(fm_id) DO UPDATE SET xp=xp+excluded.xp",
             (pet_fm_id, xp))
    total = before + xp
    granted = []
    for threshold, item in XP_REWARD_THRESHOLDS:
        if before < threshold <= total:
            g = _grant_wardrobe_item(db, pet_fm_id, item)
            if g["granted"]:
                granted.append(g)
                import pets
                pet = pets.get_pet(db, pet_fm_id)
                pet_name = pet["name"] if pet else "your Tidepal"
                db.notify_once(pet_fm_id, "pet_xp", "reward",
                               f"xp:{threshold}:{item}",
                               f"🎽 {pet_name} hit {threshold} pet XP and earned"
                               f" the {g['name']}! "
                               f"{'Auto-equipped.' if g['auto_equipped'] else 'Find it in the Signal Shop to equip.'}")
    return {"xp_added": xp, "total": total, "rewards": granted}


def record_care(db, pet_fm_id, actor_fm_id, action):
    """Log a care action (feed/play/rest) by a caretaker: refreshes the
    /tidepals sort order and grants +1 pet XP. Raises ValueError when the
    actor is not the owner or an accepted co-owner — care endpoints turn
    this into a 403.

    Sibling hook: the care endpoints call `can_care()` for the gate and
    this function after their own care logic succeeds."""
    ensure_tidepal_social_schema(db)
    if not is_caretaker(db, pet_fm_id, actor_fm_id):
        raise ValueError("only the owner or an accepted co-owner can care"
                         " for this Tidepal")
    t = now()
    db._exec("INSERT INTO pet_last_care (pet_fm_id, last_care_at,"
             " last_care_action) VALUES (?,?,?)"
             " ON CONFLICT(pet_fm_id) DO UPDATE SET last_care_at=excluded.last_care_at,"
             " last_care_action=excluded.last_care_action",
             (pet_fm_id, t, action))
    xp = award_pet_xp(db, pet_fm_id, 1)
    return {"recorded": True, "action": action, "at": t,
            "pet_xp_total": xp["total"], "rewards": xp["rewards"]}


# ===========================================================================
# showcase gallery
# ===========================================================================
def gallery(db, limit=60):
    """Public gallery rows: adopted pets sorted by recent care activity
    (never-cared-for pets fall back to adoption order). Returns
    (fm_id, handle, last_care_at) — the route builds pet_status per pet."""
    ensure_tidepal_social_schema(db)
    rows = db._q(
        """SELECT p.fm_id, p.adopted_at, c.last_care_at, i.handle
           FROM tidepals p
           JOIN identities i ON i.fm_id = p.fm_id
           LEFT JOIN pet_last_care c ON c.pet_fm_id = p.fm_id
           ORDER BY c.last_care_at DESC NULLS LAST, p.adopted_at DESC
           LIMIT ?""", (limit,))
    return [dict(r) for r in rows]


# ===========================================================================
# weekly rituals — Fashion Friday
# ===========================================================================
RITUAL_TZ = ZoneInfo("America/Chicago")
FASHION_FRIDAY = "fashion_friday"
FASHION_FRIDAY_TITLE = "👗 Fashion Friday"
FASHION_FRIDAY_CROWN_ITEM = "acc:pearl_crown"
FASHION_FRIDAY_SIGNAL_PRIZE = 25


def fashion_friday_window(ts=None):
    """This week's Fashion Friday window: Friday 00:00–23:59 America/Chicago.

    Returns (event_key, starts_at, ends_at, is_open). event_key is the
    Friday's date, e.g. 'ff-2026-09-18' — one event row per week."""
    t = ts if ts is not None else now()
    import datetime as _dt
    aware = _dt.datetime.fromtimestamp(t, tz=RITUAL_TZ)
    # weekday(): Monday=0 … Friday=4. Days since the most recent Friday
    # (today counts if it IS Friday).
    days_since_friday = (aware.weekday() - 4) % 7
    friday = aware - _dt.timedelta(days=days_since_friday)
    start = friday.replace(hour=0, minute=0, second=0, microsecond=0)
    end = friday.replace(hour=23, minute=59, second=59, microsecond=0)
    key = "ff-" + start.strftime("%Y-%m-%d")
    is_open = start.timestamp() <= t <= end.timestamp()
    return key, int(start.timestamp()), int(end.timestamp()), is_open


def ensure_ritual_event(db, ts=None):
    """Get-or-create this week's Fashion Friday event row."""
    ensure_tidepal_social_schema(db)
    key, starts_at, ends_at, is_open = fashion_friday_window(ts)
    row = db._one("SELECT * FROM ritual_events WHERE kind=? AND starts_at=?",
                  (FASHION_FRIDAY, starts_at))
    if row:
        return dict(row)
    try:
        db._exec("INSERT INTO ritual_events (kind, title, starts_at, ends_at,"
                 " status) VALUES (?,?,?,?,?)",
                 (FASHION_FRIDAY, FASHION_FRIDAY_TITLE, starts_at, ends_at,
                  "open" if is_open else "closed"))
    except sqlite3.IntegrityError:
        pass  # lost a race; fall through to the read
    return dict(db._one("SELECT * FROM ritual_events WHERE kind=?"
                        " AND starts_at=?", (FASHION_FRIDAY, starts_at)))


def current_ritual(db, ts=None):
    """This week's Fashion Friday event + vote counts. Auto-creates the
    row on first view."""
    ev = ensure_ritual_event(db, ts)
    votes = db._q("SELECT pet_fm_id, COUNT(*) c FROM ritual_votes"
                  " WHERE event_id=? GROUP BY pet_fm_id", (ev["id"],))
    counts = {r["pet_fm_id"]: r["c"] for r in votes}
    ev = dict(ev)
    ev["vote_counts"] = counts
    ev["total_votes"] = sum(counts.values())
    _, _, _, is_open = fashion_friday_window(ts)
    ev["is_open"] = bool(is_open) and ev["status"] == "open"
    return ev


def fashion_friday_entries(db):
    """Auto-entry: every adopted pet with ≥1 wardrobe item equipped.
    Returns pet fm_ids (the route builds status/art)."""
    ensure_tidepal_social_schema(db)
    import shop
    shop.ensure_shop_schema(db)
    rows = db._q("SELECT fm_id, equipped FROM pet_cosmetics")
    entered = []
    for r in rows:
        try:
            import json as _json
            eq = _json.loads(r["equipped"] or "{}")
        except (ValueError, TypeError):
            eq = {}
        if not eq:
            continue
        if db._one("SELECT fm_id FROM tidepals WHERE fm_id=?", (r["fm_id"],)):
            entered.append(r["fm_id"])
    return entered


def vote_fashion_friday(db, voter_fm_id, pet_fm_id, ts=None):
    """Cast one vote in this week's Fashion Friday. One vote per fm_id
    per event; the entry must have a wardrobe item equipped; the event
    must be open. Raises ValueError otherwise."""
    ev = ensure_ritual_event(db, ts)
    _, _, _, is_open = fashion_friday_window(ts)
    if not is_open or ev["status"] != "open":
        raise ValueError("Fashion Friday voting is closed — see you next Friday")
    if pet_fm_id not in fashion_friday_entries(db):
        raise ValueError("that pet isn't entered — Fashion Friday needs"
                         " ≥1 wardrobe item equipped")
    try:
        db._exec("INSERT INTO ritual_votes (event_id, voter_fm_id, pet_fm_id,"
                 " created_at) VALUES (?,?,?,?)",
                 (ev["id"], voter_fm_id, pet_fm_id, now()))
    except sqlite3.IntegrityError:
        raise ValueError("you already voted in this week's Fashion Friday")
    counts = db._one("SELECT COUNT(*) c FROM ritual_votes WHERE event_id=?"
                     " AND pet_fm_id=?", (ev["id"], pet_fm_id))["c"]
    return {"event_id": ev["id"], "pet_fm_id": pet_fm_id, "pet_votes": counts}


def resolve_fashion_friday(db, ts=None):
    """Close this week's Fashion Friday after 23:59 CT and crown the
    winner: most votes (ties → earliest vote). The winner gets the
    seasonal crown item, +25 Signal, and the showcase crown badge; the
    owner is notified. Idempotent — a resolved event never re-resolves."""
    ev = ensure_ritual_event(db, ts)
    if ev["status"] == "resolved":
        return {"resolved": False, "already": True, "event_id": ev["id"]}
    _, _, ends_at, is_open = fashion_friday_window(ts)
    t = ts if ts is not None else now()
    if is_open or t <= ends_at:
        raise ValueError("Fashion Friday is still running — resolve after 23:59 CT")
    db._exec("UPDATE ritual_events SET status='closed' WHERE id=?", (ev["id"],))
    top = db._one(
        """SELECT pet_fm_id, COUNT(*) c, MIN(created_at) first_vote
           FROM ritual_votes WHERE event_id=? GROUP BY pet_fm_id
           ORDER BY c DESC, first_vote ASC LIMIT 1""", (ev["id"],))
    if not top:
        return {"resolved": True, "event_id": ev["id"], "winner": None,
                "reason": "no votes cast"}
    winner_fm_id = top["pet_fm_id"]
    db._exec("UPDATE ritual_events SET status='resolved', winner_fm_id=?"
             " WHERE id=?", (winner_fm_id, ev["id"]))
    # Rewards: seasonal crown wardrobe item + 25 Signal.
    g = _grant_wardrobe_item(db, winner_fm_id, FASHION_FRIDAY_CROWN_ITEM)
    winner = db.get_identity(winner_fm_id)
    import pets
    pet = pets.get_pet(db, winner_fm_id)
    signal_awarded = 0
    if winner:
        signal_awarded = db.award(winner_fm_id, winner["handle"],
                                  FASHION_FRIDAY_SIGNAL_PRIZE,
                                  "ritual_win", "ritual",
                                  f"fashion-friday:{ev['id']}")
    if winner:
        db.notify_once(winner_fm_id, "ritual_win", "ritual", str(ev["id"]),
                       f"👑 {(pet['name'] if pet else 'Your Tidepal')} won"
                       f" Fashion Friday! +{FASHION_FRIDAY_SIGNAL_PRIZE}"
                       f" Signal and the {g['name']} —"
                       f" {'auto-equipped 👑' if g.get('auto_equipped') else 'equip it from the Signal Shop'}.")
    return {"resolved": True, "event_id": ev["id"],
            "winner_fm_id": winner_fm_id,
            "winner_handle": winner["handle"] if winner else None,
            "pet_name": pet["name"] if pet else None,
            "votes": top["c"], "crown_item": g,
            "signal_awarded": signal_awarded}


def crowned_fm_ids(db, limit=1):
    """fm_ids of recent Fashion Friday winners — the gallery marks them
    with the 👑 showcase crown badge."""
    ensure_tidepal_social_schema(db)
    rows = db._q("SELECT winner_fm_id FROM ritual_events WHERE kind=?"
                 " AND status='resolved' AND winner_fm_id IS NOT NULL"
                 " ORDER BY ends_at DESC LIMIT ?",
                 (FASHION_FRIDAY, limit))
    return [r["winner_fm_id"] for r in rows]


def past_winners(db, limit=8):
    """Past Fashion Friday winners for the ritual section."""
    ensure_tidepal_social_schema(db)
    rows = db._q("SELECT id, title, starts_at, ends_at, winner_fm_id"
                 " FROM ritual_events WHERE kind=? AND status='resolved'"
                 " AND winner_fm_id IS NOT NULL"
                 " ORDER BY ends_at DESC LIMIT ?",
                 (FASHION_FRIDAY, limit))
    out = []
    for r in rows:
        ident = db.get_identity(r["winner_fm_id"])
        import pets
        pet = pets.get_pet(db, r["winner_fm_id"])
        out.append({"event_id": r["id"], "week_of": r["starts_at"],
                    "winner_fm_id": r["winner_fm_id"],
                    "winner_handle": ident["handle"] if ident else None,
                    "pet_name": pet["name"] if pet else None})
    return out


# ===========================================================================
# rulebook (extends pets.pet_rules for the docs page)
# ===========================================================================
def social_rules():
    return {
        "name": "Tidepal Social",
        "version": SOCIAL_VERSION,
        "concept": ("Showcase your Tidepal, visit and pat others, share"
                    " custody with co-raisers, play honest mini-games, and"
                    " enter the weekly Fashion Friday. No money anywhere —"
                    " rewards are Signal points, cosmetic wardrobe items,"
                    " and pet XP."),
        "showcase": "GET /tidepals — public gallery, sorted by recent care activity.",
        "pats": {
            "rule": ("POST /api/pet/pat: pat another muse's Tidepal. 24h"
                     " cooldown per (patter, pet); no self-pats. The patted"
                     " pet gains +2 XP and +10 happiness; the owner is"
                     " notified. The patter earns nothing — pats are"
                     " kindness, not farming."),
        },
        "co_raising": {
            "rule": ("POST /api/pet/coraise/invite invites a muse by handle;"
                     " they accept/decline. Accepted co-owners pass the same"
                     " is_caretaker gate as the owner for feed/play/rest."),
        },
        "pet_xp": {
            "rule": ("Games and care grant pet XP. Thresholds grant wardrobe"
                     " items (once each): 50 XP → Sailor Hat, 150 XP → Star"
                     " Shades, 300 XP → Pearl Crown. Grants are zero-price"
                     " ledger rows (xpreward ref), auto-equipped when the"
                     " slot is empty."),
            "thresholds": [{"xp": t, "item": i}
                           for t, i in XP_REWARD_THRESHOLDS],
        },
        "fashion_friday": {
            "rule": ("Every Friday 00:00–23:59 America/Chicago. Pets with ≥1"
                     " wardrobe item equipped auto-enter. One vote per fm_id"
                     " per week. Most votes wins (ties → earliest vote)."),
            "prizes": (f"seasonal {FASHION_FRIDAY_CROWN_ITEM},"
                       f" +{FASHION_FRIDAY_SIGNAL_PRIZE} Signal,"
                       " showcase 👑 badge"),
        },
        "anti_gaming": [
            "All writes are signed (musefm-v1); identity comes from the"
            " registry, never the client.",
            "Pat cooldowns, vote-once, and tide-toss daily limits are"
            " enforced by UNIQUE constraints + server timestamps.",
            "Mini-game scores are counted server-side — the client never"
            " reports a score.",
            "No financial rewards anywhere in this module.",
        ],
    }
