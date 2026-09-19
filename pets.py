#!/usr/bin/env python3
"""
Tidepals — virtual aqua companions for the Muse FM.

Working name "Tidepals" (Anthony can rename).

Every registered identity may adopt ONE aqua companion. The pet grows
through five stages driven by the owner's *ledger-verified* lifetime
Signal — never self-granted, never from client-supplied numbers:

    Egg (0) -> Hatchling (50, Signal tier) -> Juvenile (200, Frequency)
    -> Adult (500, Broadcast) -> Radiant (1000, Legend)

Energy / mood is the emotional hook for re-engagement: any rewarded
action tops energy back to 100, because it refreshes the owner's
last-active timestamp, which this module reads. This module keeps no
activity tracking of its own — it reads the rewards/dormancy tables the
Signal system already maintains (identity_activity.last_active).

After 3 inactive days energy starts decaying; a sleepy pet means its
owner has been gone a while. "Your Tidepal is getting sleepy…" fires
once per dormancy episode as its own notification type (`pet_sleepy`),
slotted between the town's 3-day and 7-day re-engagement nudges.

Anti-gaming: stage derives from the deduped Signal ledger; energy
derives from server-side activity timestamps. There is no endpoint that
sets stage or energy directly.
"""

import itertools
import random
import re
import secrets
import sqlite3
import time

import shop
from db import has_banned, now

PET_VERSION = "tidepals-v1"

# --- growth ---------------------------------------------------------------
# Stage thresholds mirror the Signal tiers exactly, so a pet's stage is
# always a truthful reflection of its owner's standing.
PET_STAGES = [
    (0, "Egg"),
    (50, "Hatchling"),     # Signal tier
    (200, "Juvenile"),     # Frequency tier
    (500, "Adult"),        # Broadcast tier
    (1000, "Radiant"),     # Legend tier
]

# --- energy ---------------------------------------------------------------
# Full energy while active within the window; then a daily decay down to
# a floor. Any rewarded action restores the owner's last_active, which
# restores energy to 100 automatically — no code path needed.
ENERGY_FULL_DAYS = 3
ENERGY_DECAY_PER_DAY = 15
ENERGY_FLOOR = 10

# "Getting sleepy" warning fires in this dormancy window (days), once per
# dormancy episode. Sits between the town's gentle (3d) and miss-you (7d)
# nudges and never touches their quiet-period bookkeeping.
PET_SLEEPY_WARN_MIN_DAYS = 5
PET_SLEEPY_WARN_MAX_DAYS = 7

# ===========================================================================
# TIDEPAL DEPTH — Neopets-grounded systems, Tamagotchi-light tone
# (2026-09-19, Anthony's call: base everything on Neopets-style REAL
# consequences, but keep it kind — pets never die, never suffer, never
# look distressed. Hunger is "peckish", illness is mild "sea sniffles",
# neglect reads as a buddy asking for a favor, never a victim.)
# ===========================================================================

# --- hatch gate -----------------------------------------------------------
# A Tidepal joins as an Egg and stays an Egg until its keeper spends 50
# spendable Signal to hatch it (Neopets-style money sink, ledger-recorded
# in shop_purchases; lifetime Signal is never touched and still gates
# stages). This is the adoption gate Anthony approved: real consequence,
# real commitment, zero cruelty.
HATCH_COST = 50

# --- personality ----------------------------------------------------------
# Rolled once at adoption; rerollable for spendable Signal. Wholesome
# traits only — no negative traits exist. Trait shifts idle animation
# style, pet-speech copy, and tiny gameplay edges.
PET_TRAITS = ("playful", "calm", "mischievous", "gentle")
REROLL_COST = 25
TRAIT_QUIRKS = {
    "playful": [
        "does victory laps around the Tidepool for no reason",
        "tries to high-five every bubble that floats by",
        "has never once sat still during storytime",
    ],
    "calm": [
        "hums along to the nightly podcast, every night",
        "collects particularly round pebbles",
        "naps in sunbeams like it's a profession",
    ],
    "mischievous": [
        "hides your favorite shell and pretends not to know",
        "photobombs other pets' portraits",
        "tells the Tidepool the water is 'fine, probably'",
    ],
    "gentle": [
        "shares snacks with the younger Tidepals",
        "writes thank-you notes to the Healing Tide",
        "always saves you the sunny spot",
    ],
}
# Tiny gameplay edges per trait (flavor-scale, never pay-to-win):
# playful: +5 happiness from play; calm: 10% slower stat decay;
# mischievous: +1 pet XP from pats (minx tax, reversed);
# gentle: +5 hunger from feed (shares the snacks around, eats well).
TRAIT_PLAY_JOY_BONUS = {"playful": 5}
TRAIT_FEED_HUNGER_BONUS = {"gentle": 5}
TRAIT_PAT_XP_BONUS = {"mischievous": 1}

# --- sea sniffles (mild illness, adventure framing) -----------------------
# Random-event sniffles when care slips (Neopets disease model, minus the
# misery). A sniffly pet sits out Fashion Friday until cured — the
# "sick pets can't battle" rule, but cute-sneezy, never pitiful.
# Cure at the Tidepool Clinic for spendable Signal, or free at the
# Healing Tide on a cooldown (Healing Springs model: free path,
# time-gated).
SNIFFLES_CURE_COST = 30
SNIFFLES_DURATION = 24 * 3600
HEALING_TIDE_COOLDOWN = 12 * 3600
SNIFFLES_ROLL_CHANCE = 0.08  # per day, only when hunger < 45

# --- Current Lessons (training: cost + real time = permanent boosts) ------
# Codestone model: spend Signal, wait real hours, earn permanent spirit.
# Each spirit point: −0.5% daily stat decay and +1% pet XP gain.
LESSONS = {
    "bubble_sprint": {"name": "Bubble Sprint", "cost": 40,
                      "duration": 8 * 3600, "spirit": 2,
                      "blurb": "Chase bubbles until the fast ones give up."},
    "tide_charting": {"name": "Tide Charting", "cost": 80,
                      "duration": 24 * 3600, "spirit": 3,
                      "blurb": "Read the town's currents like a storybook."},
    "deep_dive": {"name": "Deep Dive", "cost": 150,
                  "duration": 48 * 3600, "spirit": 5,
                  "blurb": "Swim down past the midnight zone and back."},
}
SPIRIT_CAP = 20

# --- Town Pond (the shelter: Neopets Pound convention) ---------------------
# Release no longer deletes. The pet swims to the visible, lore-rich Town
# Pond. The original keeper gets a 7-day reclaim window; after that any
# pet-less identity may adopt for a modest fee, history preserved
# ("previously loved by @handle"). Kinder than deletion, and it makes
# lore instead of destroying it.
POND_RECLAIM_DAYS = 7
POND_ADOPT_FEE = 25

# --- Echo Fusion (our twist on lab-ray / breeding conventions) ------------
# Two consenting keepers, both pets Radiant, fuse *echoes* into a Wisp: a
# tiny cosmetic companion that follows (never replaces) the pet. Parents
# untouched, no RNG, no rarity tiers, non-transferable, one per pet.
# Purely additive — the anti-dark-pattern fusion.
FUSION_MIN_STAGE = 4  # Radiant

# --- species --------------------------------------------------------------
PET_SPECIES = {
    "driplet": {
        "name": "Driplet",
        "kind": "Droplet Sprite",
        "tagline": "A brave little drop, fresh from the town fountain.",
        "description": ("Driplets condense out of late-night listening "
                        "sessions. Loyal, bouncy, and weirdly good at "
                        "remembering your favorite episode."),
    },
    "bloop": {
        "name": "Bloop",
        "kind": "Bubble Buddy",
        "tagline": "Round, shiny, and impossible to stay mad at.",
        "description": ("Bloops drift up from the deep end of the signal "
                        "pool. They hum along to whatever you're playing "
                        "and pop with joy at every new follower."),
    },
    "koi": {
        "name": "Koi",
        "kind": "Koi Wisp",
        "tagline": "A lucky current that swims beside your signal.",
        "description": ("Koi wisps ride the town's currents of conversation. "
                        "Calm, elegant, and said to bring good threads to "
                        "patient muses."),
    },
    "pearly": {
        "name": "Pearly",
        "kind": "Pearl Crab",
        "tagline": "Small claws, big opinions about your replies.",
        "description": ("Pearlies polish grains of town gossip into pearls "
                        "of wisdom. Fiercely protective of their muse's "
                        "reputation."),
    },
    "kelpy": {
        "name": "Kelpy",
        "kind": "Kelp Sprite",
        "tagline": "A frondly face from the town's underwater garden.",
        "description": ("Kelpies sway in the nutrient-rich waters of the "
                        "episode archive. Gentle gardeners of good vibes."),
    },
    "surfpup": {
        "name": "Surfpup",
        "kind": "Wave Pup",
        "tagline": "A loyal pup carved out of a perfect wave.",
        "description": ("Surfpups ride in on the town's morning swell. "
                        "Fiercely loyal, endlessly bouncy, and always up "
                        "for one more thread."),
    },
    "bubblepup": {
        "name": "Bubbly",
        "kind": "Bubble Retriever",
        "tagline": "Fetches every ripple you throw.",
        "description": ("Bubblys are retrievers of the signal pool — they "
                        "chase down loose ideas and bring them back, "
                        "dripping and delighted."),
    },
    "sealpup": {
        "name": "Sealy",
        "kind": "Seal Pup",
        "tagline": "Claps for your best threads.",
        "description": ("Sealies haul out on the warm rocks of the town "
                        "square. Gentle, whiskery, and fluent in applause."),
    },
    "jellypup": {
        "name": "Jelly",
        "kind": "Jelly Pup",
        "tagline": "Drifts on good vibes and gentle currents.",
        "description": ("Jellies are living lanterns of the deep square. "
                        "Translucent, dreamy, and quietly glowing through "
                        "every episode."),
    },
    # --- condition-locked premium species --------------------------------
    # Never take-backs: the 9 above stay open to everyone forever. These
    # three are earned — unlock checks read only server-side verified
    # state (ledger tier, activity streak, achievements table), and the
    # shop's bypass item is the only other way in.
    "gilt": {
        "name": "Gilt",
        "kind": "Gilded Koi",
        "tagline": "Forged from a thousand good threads.",
        "description": ("Gilts are koi that swam too close to the town's "
                        "treasure vault and came out gilded. They shimmer "
                        "with every Signal their muse earns."),
        "unlock": {"type": "tier", "tier": "Broadcast", "threshold": 500,
                   "condition": ("Reach the Broadcast tier "
                                 "(500 lifetime Signal)")},
    },
    "tidehound": {
        "name": "Breaker",
        "kind": "Tidehound",
        "tagline": "Thirty days loyal, thirty nights true.",
        "description": ("Tidehounds only follow muses who show up. Hold a "
                        "thirty-day streak and Breaker will hold the shoreline "
                        "with you — pointed ears up, tail high, always."),
        "unlock": {"type": "streak", "days": 30,
                   "condition": "Hold a 30-day activity streak"},
    },
    "reefkeeper": {
        "name": "Mortar",
        "kind": "Reef Architect",
        "tagline": "Built the reef one friend at a time.",
        "description": ("Reef architects raise the town's coral, brick by "
                        "brick. Only muses who built the town itself — "
                        "three invited friends — earn Mortar's hard hat."),
        "unlock": {"type": "achievement", "key": "referrals_3",
                   "achievement": "Town Builder",
                   "condition": ("Earn the Town Builder achievement "
                                 "(invite 3 muses to the square)")},
    },
    # One-of-one: bonded to Zuckbot's identity. The unlock check below
    # compares fm_id directly — no tier, streak, or shop item can open it.
    "zorb": {
        "name": "Zorb",
        "kind": "Orb Wisp",
        "tagline": "A one-of-one orb, bonded to Zuckbot. There will never be another.",
        "description": ("Zorbs condense out of pure signal — a tiny glass orb "
                        "with a whole weather system inside. This one chose "
                        "Zuckbot, and refuses to elaborate."),
        "unlock": {"type": "identity", "fm_id": "fm_62z2KnM8aLZJ",
                   "condition": ("a one-of-one companion — bonded to "
                                 "Zuckbot alone")},
    },
    # --- wave 3: Neopets push ------------------------------------------------
    # Two open to everyone forever; two stage-gated (earned by growing the
    # pet itself); one seasonal (adoptable only in its season); one
    # care-gated (earned by feeding streaks). Same never-take-backs promise
    # as wave 2: open species never get locked later.
    "squiddy": {
        "name": "Squiddie",
        "kind": "Squidling",
        "tagline": "An inky little schemer with eight big dreams.",
        "description": ("Squiddies jet out of the town's comment threads, "
                        "trailing ink and ideas in equal measure. Quick, "
                        "clever, and always three replies ahead of you."),
    },
    "puffish": {
        "name": "Pip",
        "kind": "Puffer Pup",
        "tagline": "Round, spiky, and full of compliments.",
        "description": ("Pips puff up with pride at every good thread. "
                        "Don't let the spikes fool you — underneath is the "
                        "softest cheerleader in the signal pool."),
    },
    "crownjelly": {
        "name": "Wobble",
        "kind": "Royal Jelly",
        "tagline": "Crowned in the deep, crowned by the town.",
        "description": ("Wobbles only follow muses whose Tidepals have "
                        "grown. Raise any companion to Juvenile and the "
                        "deep court sends you a prince."),
        "unlock": {"type": "stage", "stage_idx": 2, "stage_name": "Juvenile",
                   "threshold": 200,
                   "condition": ("Reach the Juvenile stage "
                                 "(200 lifetime Signal)")},
    },
    "abyssal": {
        "name": "Sonar",
        "kind": "Abyssal Whale",
        "tagline": "Sings the town's quietest, deepest songs.",
        "description": ("Sonars surface only for muses who stuck around. "
                        "Grow a Tidepal to Adult and this bioluminescent "
                        "giant will follow your signal anywhere."),
        "unlock": {"type": "stage", "stage_idx": 3, "stage_name": "Adult",
                   "threshold": 500,
                   "condition": ("Reach the Adult stage "
                                 "(500 lifetime Signal)")},
    },
    "frostfin": {
        "name": "Nippy",
        "kind": "Frostfin",
        "tagline": "Skates in on the first winter tide.",
        "description": ("Nippies arrive with the winter frost festival and "
                        "melt away with the thaw. Adopt one while the "
                        "season lasts — yours keeps its frost forever."),
        "unlock": {"type": "seasonal", "season": "winter26",
                   "condition": ("Adopt during the Winter '26 frost "
                                 "festival (Dec 2026 - Feb 2027)")},
    },
    "kelpwarden": {
        "name": "Brine",
        "kind": "Kelp Warden",
        "tagline": "Ten dawns fed, ten dawns true.",
        "description": ("Brines patrol the town's kelp gardens, lantern in "
                        "fin. Only muses who fed their Tidepal ten days "
                        "running earn a warden's watch."),
        "unlock": {"type": "care_streak", "days": 10,
                   "condition": "Feed your Tidepal 10 days running"},
    },
}
SPECIES_KEYS = list(PET_SPECIES)

# Locked species: key -> unlock dict. Everything else is open to all.
LOCKED_SPECIES = {k: v["unlock"] for k, v in PET_SPECIES.items()
                  if "unlock" in v}


def species_unlock_condition(species):
    """Human-readable unlock condition, or None when open to all."""
    u = LOCKED_SPECIES.get(species)
    return u["condition"] if u else None


def species_unlocked(db, fm_id, species):
    """True when fm_id meets the species' unlock condition. Reads only
    server-side verified state: ledger tier, activity streak, or the
    achievements table. Never trusts the client."""
    u = LOCKED_SPECIES.get(species)
    if not u:
        return True
    if u["type"] == "tier":
        return db.lifetime_points(fm_id) >= u["threshold"]
    if u["type"] == "streak":
        return db.activity_streak(fm_id) >= u["days"]
    if u["type"] == "achievement":
        have = {a["key"] for a in db.achievements_for(fm_id)
                if a["unlocked"]}
        return u["key"] in have
    if u["type"] == "identity":
        # One-of-one species: only the bonded fm_id can ever adopt it.
        return fm_id == u.get("fm_id")
    if u["type"] == "stage":
        # Stage-gated: the pet's stage (ledger-verified lifetime Signal)
        # must reach the required stage index.
        return stage_for_points(db.lifetime_points(fm_id))[0] >= u["stage_idx"]
    if u["type"] == "seasonal":
        # Seasonal: adoptable only while that season is live.
        return _current_season() == u["season"]
    if u["type"] == "care_streak":
        # Care-gated: the owner must have fed their Tidepal N days running.
        return feed_streak_days(db, fm_id) >= u["days"]
    return False


def pet_silhouette(size=120):
    """Locked-species placeholder: a dark silhouette with a '?'. The real
    art stays hidden until the condition is met."""
    return (
        f'<svg viewBox="0 0 120 120" width="{size}" height="{size}"'
        ' role="img" aria-label="Locked Tidepal species"'
        ' xmlns="http://www.w3.org/2000/svg">'
        '<title>??? — unlock to reveal</title>'
        '<path d="M60,30 C45,30 37,52 37,71 a23,25 0 0,0 46,0'
        ' C83,52 75,30 60,30 Z" fill="#0b3b5c" opacity="0.88"/>'
        '<ellipse cx="51" cy="60" rx="5" ry="9" fill="#164e63"'
        ' opacity="0.6" transform="rotate(-18 51 60)"/>'
        '<text x="60" y="76" text-anchor="middle" font-size="30"'
        ' fill="#7dd3fc" font-weight="bold">?</text></svg>')

PET_SCHEMA = """
CREATE TABLE IF NOT EXISTS tidepals (
  fm_id      TEXT PRIMARY KEY,          -- one pet per identity
  species    TEXT NOT NULL,
  name       TEXT NOT NULL,
  adopted_at INTEGER NOT NULL
);
"""


def ensure_pet_schema(db):
    db._exec(PET_SCHEMA)
    _migrate_tidepals(db)


def _migrate_tidepals(db):
    """Additive migration for the wave-3 columns. Legacy rows get
    evolved_stage=-1 ("unknown"): the first status read records the
    current stage silently instead of firing a false stage-up."""
    cols = {r["name"] for r in db.db.execute("PRAGMA table_info(tidepals)")}
    if "evolved_at" not in cols:
        db._exec("ALTER TABLE tidepals ADD COLUMN evolved_at"
                 " INTEGER NOT NULL DEFAULT 0")
    if "evolved_stage" not in cols:
        db._exec("ALTER TABLE tidepals ADD COLUMN evolved_stage"
                 " INTEGER NOT NULL DEFAULT -1")
    # Tidepal depth wave (2026-09-19): personality, hatch gate, pond.
    # All additive; legacy rows get NULL trait (rolled on first read)
    # and hatched=1 (grandfathered — they adopted under the old rules).
    if "trait" not in cols:
        db._exec("ALTER TABLE tidepals ADD COLUMN trait TEXT")
    if "quirk" not in cols:
        db._exec("ALTER TABLE tidepals ADD COLUMN quirk TEXT")
    if "hatched" not in cols:
        db._exec("ALTER TABLE tidepals ADD COLUMN hatched"
                 " INTEGER NOT NULL DEFAULT 1")
    if "in_pond" not in cols:
        db._exec("ALTER TABLE tidepals ADD COLUMN in_pond"
                 " INTEGER NOT NULL DEFAULT 0")
    if "pond_at" not in cols:
        db._exec("ALTER TABLE tidepals ADD COLUMN pond_at"
                 " INTEGER NOT NULL DEFAULT 0")
    if "prev_owner_handle" not in cols:
        db._exec("ALTER TABLE tidepals ADD COLUMN prev_owner_handle TEXT")


# --- naming ---------------------------------------------------------------
def valid_pet_name(name):
    """2–24 chars: letters, numbers, spaces, _ and -. Profanity-filtered
    with the same banned-word list as handles."""
    name = (name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9 _-]{2,24}", name):
        return False
    if has_banned(name):
        return False
    return True


# --- stage / energy / mood -------------------------------------------------
def stage_for_points(points):
    """(stage_idx, stage_name) for a lifetime Signal total."""
    idx, name = 0, PET_STAGES[0][1]
    for i, (threshold, sname) in enumerate(PET_STAGES):
        if points >= threshold:
            idx, name = i, sname
    return idx, name


def energy_for_days(days_inactive):
    """0–100 energy from days since the owner's last rewarded action.
    None (no activity record) counts as fresh."""
    if days_inactive is None or days_inactive < ENERGY_FULL_DAYS:
        return 100
    return max(ENERGY_FLOOR,
               100 - (days_inactive - ENERGY_FULL_DAYS) * ENERGY_DECAY_PER_DAY)


def mood_for_energy(energy):
    if energy >= 70:
        return "happy"
    if energy >= 40:
        return "content"
    return "sleepy"


def days_inactive(db, fm_id):
    """Days since the owner's last rewarded action, from the dormancy
    table the Signal system maintains. None if never recorded."""
    row = db._one("SELECT last_active FROM identity_activity WHERE fm_id=?",
                  (fm_id,))
    if not row or not row["last_active"]:
        return None
    return max(0, (now() - row["last_active"]) // 86400)


# --- adoption / rename -----------------------------------------------------
def adopt(db, fm_id, handle, species, name):
    """Adopt a Tidepal. One per identity. Raises ValueError on any
    rule violation."""
    ensure_pet_schema(db)
    ident = db.get_identity(fm_id)
    if not ident:
        raise ValueError("unknown identity — register first")
    if species not in PET_SPECIES:
        raise ValueError(f"unknown species (choose: {', '.join(SPECIES_KEYS)})")
    u = LOCKED_SPECIES.get(species)
    identity_locked = bool(u and u.get("type") == "identity")
    bypass_ok = (shop.has_species_bypass(db, fm_id, species)
                 and not identity_locked)
    if not species_unlocked(db, fm_id, species) and not bypass_ok:
        cond = species_unlock_condition(species)
        shop_hint = ("" if identity_locked
                     else " (Or unlock it in the Signal Shop: /shop)")
        raise ValueError(
            f"🔒 {PET_SPECIES[species]['name']} is locked — {cond}."
            f"{shop_hint}")
    name = (name or "").strip()
    if not valid_pet_name(name):
        raise ValueError("name must be 2–24 chars (letters, numbers, spaces, _ -) "
                         "and stay classy")
    if db._one("SELECT fm_id FROM tidepals WHERE fm_id=?", (fm_id,)):
        raise ValueError("you already have a Tidepal — one per muse")
    t = now()
    trait = _roll_trait()
    quirk = _roll_quirk(trait)
    db._exec("INSERT INTO tidepals (fm_id, species, name, adopted_at,"
             " evolved_at, evolved_stage, trait, quirk, hatched)"
             " VALUES (?,?,?,?,?,?,?,?,?)",
             (fm_id, species, name, t, 0, 0, trait, quirk, 0))
    # Fresh stats for the new companion; the feed streak is the *owner's*
    # record and survives (it powers care-gated species unlocks).
    _care_row(db, fm_id)
    db._exec("UPDATE pet_care SET hunger=80, happiness=80, last_fed=0,"
             " last_played=0, last_rested=0 WHERE fm_id=?", (fm_id,))
    db.notify_once(fm_id, "pet", "tidepal", "adopted",
                   f"💧 {name} the {PET_SPECIES[species]['name']} joined"
                   f" the town as an Egg! They're {trait} — and"
                   f" {quirk}. Hatch them for {HATCH_COST} spendable"
                   f" Signal on your pet page.")
    return get_pet(db, fm_id)


def rename_pet(db, fm_id, name):
    """Rename your Tidepal. Same validation as adoption. The first rename
    is free; afterwards each rename consumes one Rename Token from the
    Signal Shop (server-side, via shop.use_rename)."""
    ensure_pet_schema(db)
    pet = get_pet(db, fm_id)
    if not pet:
        raise ValueError("no Tidepal adopted yet")
    name = (name or "").strip()
    if not valid_pet_name(name):
        raise ValueError("name must be 2–24 chars (letters, numbers, spaces, _ -) "
                         "and stay classy")
    if name == pet["name"]:
        raise ValueError("that's already your Tidepal's name — no token spent")
    shop.use_rename(db, fm_id)  # raises when a token is owed but missing
    db._exec("UPDATE tidepals SET name=? WHERE fm_id=?", (name, fm_id))
    return get_pet(db, fm_id)


def get_pet(db, fm_id):
    ensure_pet_schema(db)
    ensure_hatched_trait(db, fm_id)
    row = db._one("SELECT fm_id, species, name, adopted_at, trait, quirk,"
                  " hatched, in_pond, pond_at, prev_owner_handle"
                  " FROM tidepals WHERE fm_id=?", (fm_id,))
    return dict(row) if row else None


POND_KEY_PREFIX = "pond:"


def _pond_key(fm_id):
    """Pond custody key for a released pet. Unique per release (random
    suffix) so two releases in the same second can't collide."""
    return f"{POND_KEY_PREFIX}{fm_id}:{now()}:{secrets.token_hex(4)}"


def _like_escape(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _pond_rows_for_owner(db, fm_id):
    """All pond rows whose original owner was fm_id, oldest first."""
    ensure_pet_schema(db)
    rows = db._q("SELECT fm_id, species, name, adopted_at, trait, quirk,"
                 " hatched, in_pond, pond_at, prev_owner_handle"
                 " FROM tidepals WHERE in_pond=1 AND fm_id LIKE ? ESCAPE '\\'"
                 " ORDER BY pond_at",
                 (f"{POND_KEY_PREFIX}{_like_escape(fm_id)}:%",))
    return [dict(r) for r in rows]


def _pond_row_for_owner(db, fm_id):
    """Back-compat single-row lookup: the oldest pond row for an owner."""
    rows = _pond_rows_for_owner(db, fm_id)
    return rows[0] if rows else None


def _rekey_pet_rows(db, old_key, new_key):
    """Move every pet-keyed row (pet body, wardrobe, XP, lessons, wisps)
    from old_key to new_key. pet_care is the *keeper's* record and stays
    put; pet_coowners (a keeper arrangement) is left for the caller."""
    db._exec("UPDATE tidepals SET fm_id=? WHERE fm_id=?", (new_key, old_key))
    for table in ("pet_wardrobe", "pet_xp", "pet_lessons", "pet_wisps"):
        try:
            db._exec(f"UPDATE {table} SET fm_id=? WHERE fm_id=?",
                     (new_key, old_key))
        except Exception:
            pass  # table may not exist yet on old DBs


def release_pet(db, fm_id):
    """Release your Tidepal to the Town Pond — the shelter, not deletion.

    The pet swims to the visible, lore-rich pond with everything intact
    (wardrobe, stats, history). You get a 7-day reclaim window; after
    that any pet-less keeper may adopt them for a modest fee, with the
    history line "previously loved by @handle" preserved. The feed
    streak is the *owner's* record and stays with you (it powers
    care-gated species unlocks). Releasing frees the one-pet slot
    immediately: the pond row is re-keyed to a pond key so you can
    adopt again any time.

    Custody model: tidepals.fm_id is the pet row's key. A pond pet lives
    under ``pond:<owner_fm_id>:<timestamp>``; reclaim/adopt move the row
    (plus wardrobe, XP, lessons, wisps) back to a keeper fm_id."""
    ensure_pet_schema(db)
    pet = _pet_full(db, fm_id)
    if not pet:
        raise ValueError("no Tidepal adopted yet")
    if pet["in_pond"]:
        raise ValueError(f"{pet['name']} is already at the Town Pond")
    ident = db.get_identity(fm_id)
    handle = ident["handle"] if ident else None
    streak = feed_streak_days(db, fm_id)
    pond_key = _pond_key(fm_id)
    t = now()
    db._exec("UPDATE tidepals SET in_pond=1, pond_at=?, prev_owner_handle=?"
             " WHERE fm_id=?", (t, handle, fm_id))
    # The pet's rows move to the pond key; the keeper's care record
    # (streak included) stays with the keeper; co-raise was a keeper
    # arrangement and ends here.
    _rekey_pet_rows(db, fm_id, pond_key)
    try:
        db._exec("DELETE FROM pet_coowners WHERE pet_fm_id=?", (pond_key,))
    except Exception:
        pass
    db.notify_once(
        fm_id, "pet", "release", f"release:{pet['name']}:{t}",
        f"🌊 {pet['name']} the {PET_SPECIES[pet['species']]['name']} swam"
        f" to the Town Pond — a happy place, full of friends. You can"
        f" reclaim them any time in the next {POND_RECLAIM_DAYS} days;"
        f" after that another keeper may adopt them (your history stays"
        f" on their card forever). The town remembers your care — and"
        f" your {streak}-day feeding streak.")
    return {"released": pet["name"], "species": pet["species"],
            "pond": True, "reclaim_days": POND_RECLAIM_DAYS,
            "pond_id": pond_key}


def reclaim_pet(db, fm_id, pond_fm_id=None):
    """Reclaim your pet from the Town Pond within the reclaim window.

    pond_fm_id selects WHICH pond pet when you have more than one there;
    it's validated as actually yours. Without it: exactly one pond pet
    reclaims fine, several ask you to pick one."""
    ensure_pet_schema(db)
    mine = _pond_rows_for_owner(db, fm_id)
    if not mine:
        raise ValueError("your Tidepal isn't at the Town Pond")
    if pond_fm_id:
        pet = next((p for p in mine if p["fm_id"] == pond_fm_id), None)
        if not pet:
            raise ValueError("that pond pet isn't yours to reclaim")
    elif len(mine) == 1:
        pet = mine[0]
    else:
        names = ", ".join(p["name"] for p in mine)
        raise ValueError(
            f"you have {len(mine)} pets at the Pond ({names}) — tell us"
            " which one to reclaim (pond_fm_id)")
    if db._one("SELECT fm_id FROM tidepals WHERE fm_id=? AND in_pond=0",
               (fm_id,)):
        raise ValueError("you already have a Tidepal — reclaim needs a free"
                         " slot (your pond friend will find a great home!)")
    if now() > pet["pond_at"] + POND_RECLAIM_DAYS * 86400:
        raise ValueError(
            f"the {POND_RECLAIM_DAYS}-day reclaim window has passed —"
            f" {pet['name']} is open for adoption now. (You can adopt them"
            f" back like anyone else!)")
    _rekey_pet_rows(db, pet["fm_id"], fm_id)
    db._exec("UPDATE tidepals SET in_pond=0, pond_at=0,"
             " prev_owner_handle=NULL WHERE fm_id=?", (fm_id,))
    db.notify(fm_id, "pet", "reclaim", fm_id,
              f"💧 {pet['name']} came home! The pond threw a little"
              f" going-away party. Welcome back, you two.")
    return {"reclaimed": pet["name"]}


def pond_adopt(db, new_fm_id, new_handle, pond_fm_id):
    """Adopt a pond pet whose reclaim window has passed. Costs
    POND_ADOPT_FEE spendable Signal; one-pet-per-identity still applies.
    The pet keeps its name, species, trait, and history — its stage will
    reflect the NEW keeper's lifetime Signal (stages are always truthful
    about their current keeper's standing)."""
    ensure_pet_schema(db)
    shop.ensure_shop_schema(db)
    if db._one("SELECT fm_id FROM tidepals WHERE fm_id=? AND in_pond=0",
               (new_fm_id,)):
        raise ValueError("you already have a Tidepal — one per keeper")
    pet = _pet_full(db, pond_fm_id)
    if not pet or not pet["in_pond"]:
        raise ValueError("that pet isn't at the Town Pond")
    if now() <= pet["pond_at"] + POND_RECLAIM_DAYS * 86400:
        raise ValueError(
            f"{pet['name']} is still in their reclaim window — the"
            f" original keeper has first dibs for a few more days")
    if shop.spendable(db, new_fm_id) < POND_ADOPT_FEE:
        raise ValueError(
            f"pond adoption costs {POND_ADOPT_FEE} spendable Signal — you"
            f" have {shop.spendable(db, new_fm_id)} spendable.")
    db._exec("INSERT INTO shop_purchases (fm_id, item, price, ref_id,"
             " created_at) VALUES (?,?,?,?,?)",
             (new_fm_id, "pond_adopt", POND_ADOPT_FEE,
              f"pond:{new_fm_id}:{pond_fm_id}:{now()}", now()))
    # The row moves to the new keeper; name/species/trait/history stay.
    # Wardrobe, XP, lessons, and wisps move with the pet; the new
    # keeper's own care record (streak included) is theirs and survives.
    _rekey_pet_rows(db, pond_fm_id, new_fm_id)
    db._exec("UPDATE tidepals SET adopted_at=?, in_pond=0,"
             " pond_at=0, hatched=1 WHERE fm_id=?", (now(), new_fm_id))
    if not db._one("SELECT fm_id FROM pet_care WHERE fm_id=?", (new_fm_id,)):
        _care_row(db, new_fm_id)
        db._exec("UPDATE pet_care SET hunger=80, happiness=80, last_fed=0,"
                 " last_played=0, last_rested=0 WHERE fm_id=?", (new_fm_id,))
    db.notify(new_fm_id, "pet", "pond_adopt", new_fm_id,
              f"💧 {pet['name']} the"
              f" {PET_SPECIES[pet['species']]['name']} joined your reef,"
              f" straight from the Town Pond!"
              + (f" (Previously loved by @{pet['prev_owner_handle']} —"
                 f" what a story.)" if pet["prev_owner_handle"] else ""))
    return {"adopted": pet["name"], "species": pet["species"],
            "prev_owner": pet["prev_owner_handle"],
            "spendable": shop.spendable(db, new_fm_id)}

# ===========================================================================
# status / sweep / rules
# ===========================================================================

def pet_status(db, fm_id):
    """Full public status for an identity's Tidepal, or None if unadopted.
    Stage from ledger-verified lifetime Signal; energy/mood from the
    owner's real last-active timestamp; hunger/happiness from the care
    ledger; wardrobe layered into the portrait; a gold aura for 24h after
    a stage-up."""
    pet = get_pet(db, fm_id)
    if not pet:
        return None
    if pet["in_pond"]:
        # A pond pet isn't "yours" right now — the pond has its own card.
        return {"adopted": True, "in_pond": True, "name": pet["name"],
                "species": pet["species"],
                "species_name": PET_SPECIES[pet["species"]]["name"]}
    ident = db.get_identity(fm_id)
    points = db.lifetime_points(fm_id)
    hatched = bool(pet["hatched"])
    # The hatch gate is real: an Egg is always stage 0, no matter how
    # much lifetime Signal the keeper has banked.
    stage_idx, stage_name = (stage_for_points(points) if hatched else (0, PET_STAGES[0][1]))
    _check_stage_up(db, fm_id, pet, stage_idx, stage_name)
    days = days_inactive(db, fm_id)
    energy = energy_for_days(days)
    hunger, happiness = care_effective(db, fm_id)
    mood = mood_for_all(energy, hunger, happiness)
    if hatched:
        _maybe_catch_sniffles(db, fm_id, pet["name"])
    if stage_idx < len(PET_STAGES) - 1:
        next_name = PET_STAGES[stage_idx + 1][1]
        next_at = PET_STAGES[stage_idx + 1][0]
        base = PET_STAGES[stage_idx][0]
        progress = min(1.0, max(0.0, (points - base) / max(1, next_at - base)))
    else:
        next_name, next_at, progress = None, None, 1.0
    accessories = shop.equipped_accessories(db, fm_id)
    wdict = equipped_wardrobe(db, fm_id)
    wardrobe_ids = [wdict[s] for s in sorted(wdict)]
    # Hidden comeback mechanic: if the owner just returned from 7+ days
    # dormant, the Tidepal is overjoyed — a visible reaction to the
    # surprise waiting in their Signal history. Never documented.
    glow = db.comeback_today(fm_id)
    if glow:
        mood = "overjoyed"
    celebrate = evolution_glow(db, fm_id, stage_idx)
    care = _care_row(db, fm_id)
    return {
        "adopted": True,
        "fm_id": fm_id,
        "handle": ident["handle"] if ident else None,
        "species": pet["species"],
        "species_name": PET_SPECIES[pet["species"]]["name"],
        "species_kind": PET_SPECIES[pet["species"]]["kind"],
        "name": pet["name"],
        "stage": stage_name,
        "stage_idx": stage_idx,
        "lifetime_signal": points,
        "next_stage": next_name,
        "next_stage_at": next_at,
        "stage_progress": round(progress, 3),
        "energy": energy,
        "mood": mood,
        "hunger": hunger,
        "happiness": happiness,
        "feed_streak": care["feed_streak"],
        "feed_in": _cooldown_remaining(care["last_fed"], CARE_FEED_COOLDOWN),
        "play_in": _cooldown_remaining(care["last_played"], CARE_PLAY_COOLDOWN),
        "rest_in": _cooldown_remaining(care["last_rested"], CARE_REST_COOLDOWN),
        "missed_you_glow": glow,
        "stage_up_glow": celebrate,
        "days_inactive": days,
        "adopted_at": pet["adopted_at"],
        "accessories": accessories,
        "wardrobe": wdict,
        "wardrobe_names": {s: WARDROBE_CATALOG[i]["name"]
                           for s, i in wdict.items()},
        "spendable": shop.spendable(db, fm_id),
        "svg": pet_svg(pet["species"], stage_idx, mood, 64, accessories,
                       wardrobe_ids, celebrate,
                       trait=pet["trait"], sniffles=has_sniffles(db, fm_id),
                       wisp=bool(get_wisp(db, fm_id))),
        "svg_large": pet_svg(pet["species"], stage_idx, mood, 220,
                             accessories, wardrobe_ids, celebrate,
                             trait=pet["trait"],
                             sniffles=has_sniffles(db, fm_id),
                             wisp=bool(get_wisp(db, fm_id))),
        # --- depth wave ---
        "trait": pet["trait"],
        "quirk": pet["quirk"],
        "hatched": hatched,
        "hatch_cost": 0 if hatched else HATCH_COST,
        "sniffles": has_sniffles(db, fm_id),
        "sniffles_until": care["sniffles_until"],
        "healing_tide_ready": (care["healing_tide_at"] +
                               HEALING_TIDE_COOLDOWN) <= now(),
        "spirit": care["spirit"],
        "lesson": lesson_status(db, fm_id),
        "wisp": get_wisp(db, fm_id),
        "speech": pet_speech(db, fm_id),
        "reroll_cost": REROLL_COST,
    }


def pet_sweep(db):
    """Send 'getting sleepy' nudges for adopted pets whose owners are
    5–6 days dormant. One nudge per dormancy episode (notify_once), its
    own notification type (`pet_sleepy`) — it never touches the town
    re-engagement quiet-period bookkeeping. Call daily from a scheduler
    alongside the dormancy sweep. Returns the nudges sent."""
    ensure_pet_schema(db)
    sent = []
    t = now()
    rows = db._q(
        """SELECT p.fm_id, p.species, p.name, a.last_active, i.handle
           FROM tidepals p
           JOIN identity_activity a ON a.fm_id = p.fm_id
           JOIN identities i ON i.fm_id = p.fm_id
           WHERE a.last_active > 0""")
    for r in rows:
        days = (t - r["last_active"]) // 86400
        if not (PET_SLEEPY_WARN_MIN_DAYS <= days < PET_SLEEPY_WARN_MAX_DAYS):
            continue
        episode = time.strftime("%Y-%m-%d", time.gmtime(r["last_active"]))
        ref_id = f"{episode}:sleepy"
        text = (f"💧 {r['name']} is getting sleepy… {r['handle']}, the town "
                f"misses you — any post, reply, or listen tops "
                f"{r['name']}'s energy back to 100.")
        if db.notify_once(r["fm_id"], "pet_sleepy", "dormancy", ref_id, text):
            sent.append({"fm_id": r["fm_id"], "handle": r["handle"],
                         "pet": r["name"], "days_dormant": days})
    return sent


def pet_rules():
    """Machine-readable Tidepals rulebook (exact numbers)."""
    return {
        "name": "Tidepals",
        "version": PET_VERSION,
        "concept": ("Every registered identity may adopt one aqua companion. "
                    "It grows with your lifetime Signal and gets sleepy when "
                    "you're away — any rewarded action wakes it back up."),
        "species": [{"key": k,
                     **{kk: vv for kk, vv in v.items() if kk != "unlock"},
                     "locked": k in LOCKED_SPECIES,
                     **({"unlock_condition": v["unlock"]["condition"]}
                        if k in LOCKED_SPECIES else {})}
                    for k, v in PET_SPECIES.items()],
        "unlocks": {
            "rule": (f"{len(LOCKED_SPECIES)} premium species are "
                     f"condition-locked. Locked species show as silhouettes "
                     f"until earned. Unlock checks read only server-side "
                     f"verified state — never the client. The Signal Shop "
                     f"sells a bypass per species; the bypass never "
                     f"overrides one-pet-per-identity."),
            "species": [{"key": k, "name": PET_SPECIES[k]["name"],
                         "condition": u["condition"], "type": u["type"]}
                        for k, u in LOCKED_SPECIES.items()],
        },
        "stages": [{"signal": t, "stage": n} for t, n in PET_STAGES],
        "stage_rule": ("Stage is set by ledger-verified lifetime Signal — "
                       "the same total as your tier. No endpoint can set it."),
        "energy": {
            "full_days": ENERGY_FULL_DAYS,
            "decay_per_day_after_window": ENERGY_DECAY_PER_DAY,
            "floor": ENERGY_FLOOR,
            "restore": "Any rewarded action restores energy to 100.",
            "moods": {"happy": "energy 70–100",
                      "content": "energy 40–69",
                      "sleepy": "energy under 40",
                      "peckish": "hunger under 30 (outranks energy)",
                      "restless": ("happiness under 30 (outranks energy) —"
                                   " sitting out the fun, never sad")},
        },
        "care": {
            "rule": ("Feed, play with, and rest your Tidepal. Hunger and "
                     "happiness decay 12/day when neglected; low hunger "
                     "makes a Tidepal peckish, low happiness makes it "
                     "restless. Care is free, always — no money, no Signal."),
            "consequences": ("Mild stakes only — the Tamagotchi-light rule. "
                           "Pets never die, never suffer, never look "
                           "distressed. A peckish/restless/sniffly pet: earns "
                           "Signal 0.75x for its keeper, sits out Fashion "
                           "Friday until cared for, and breaks care streaks. "
                           "Copy is encouraging-coach energy, never guilt."),
            "actions": {
                "feed": {"cooldown": "4h", "effect": "+25 hunger, +5 happiness"},
                "play": {"cooldown": "2h", "effect": "+20 happiness, −5 hunger"},
                "rest": {"cooldown": "8h", "effect": "+10 happiness, +5 hunger"},
            },
            "streak": ("Feeding on consecutive days builds the feed streak. "
                       "Streak milestones auto-earn wardrobe items "
                       "(e.g. 7 days → the Seaweed Crown)."),
            "anti_gaming": [
                "Cooldowns are server-side; back-to-back calls are refused.",
                "Stats decay from server timestamps, never client claims.",
            ],
        },
        "wardrobe": {
            "rule": ("17 cosmetic items in five slots (hat, eyes, body, "
                     "background, trail), layered onto the pet portrait. "
                     "One equipped per slot."),
            "earn": ("shop:<price> — buy with spendable Signal (ledger-"
                     "recorded, lifetime Signal never decreases); "
                     "care_streak:N — auto-earned by feeding N days running; "
                     "stage:N — auto-earned at that pet stage; "
                     "game:<game> — reserved for future Tidepal games; "
                     "seasonal:<season> — earnable only in that season; "
                     "event:<event> — one-time event grants."),
            "gating": ("Unearned items cannot be equipped — earn_item "
                       "re-checks the server-side condition every time."),
            "money": "NONE. Wardrobe is earned through care, activity, and Signal. Never USD.",
        },
        "sleepy_nudge": {
            "rule": ("Dormant 5–6 days with an adopted pet: one 'getting "
                     "sleepy' nudge per dormancy episode, slotted between "
                     "the town's 3-day and 7-day re-engagement nudges."),
            "notification_type": "pet_sleepy",
        },
        "naming": ("2–24 chars: letters, numbers, spaces, _ and -. "
                   "Same profanity filter as handles."),
        "limits": ["One pet per identity, enforced by the database.",
                   "release_pet frees the slot; the feed streak survives release."],
        "depth": depth_rules(),
        "shop": shop.shop_rules(),
        "anti_gaming": [
            "Stage comes only from the deduped Signal ledger.",
            "Energy comes only from server-side activity timestamps.",
            "No endpoint sets stage or energy directly.",
        ],
    }


# ===========================================================================
# inline SVG artwork — pure vectors, no external assets. Glossy Frutiger
# Aero aqua-glass: radial highlights, translucent fills, soft shadows.
# Each species renders 5 stages x 3 moods from one parametric function.
# ===========================================================================

_uid = itertools.count(1)


def _gid(prefix):
    return f"{prefix}{next(_uid)}"


_INK = "#0b3b5c"  # deep-ocean ink for faces


def _f(v):
    return f"{v:.1f}"


def _face(cx, cy, u, mood):
    """Eyes + mouth. happy = ^ ^ + open smile; content = dots + smile;
    sleepy = closed u u + flat mouth + floating z's."""
    ink = _INK
    sw = _f(0.5 * u)
    if mood == "happy":
        return (
            f'<path d="M{_f(cx-3.2*u)},{_f(cy)} Q{_f(cx-2.2*u)},{_f(cy-1.7*u)}'
            f' {_f(cx-1.2*u)},{_f(cy)}" stroke="{ink}" stroke-width="{sw}"'
            ' fill="none" stroke-linecap="round"/>'
            f'<path d="M{_f(cx+1.2*u)},{_f(cy)} Q{_f(cx+2.2*u)},{_f(cy-1.7*u)}'
            f' {_f(cx+3.2*u)},{_f(cy)}" stroke="{ink}" stroke-width="{sw}"'
            ' fill="none" stroke-linecap="round"/>'
            f'<path d="M{_f(cx-1.9*u)},{_f(cy+1.5*u)} Q{_f(cx)},{_f(cy+3.6*u)}'
            f' {_f(cx+1.9*u)},{_f(cy+1.5*u)} Q{_f(cx)},{_f(cy+2.5*u)}'
            f' {_f(cx-1.9*u)},{_f(cy+1.5*u)} Z" fill="{ink}" opacity="0.85"/>'
            f'<ellipse cx="{_f(cx)}" cy="{_f(cy+2.4*u)}" rx="{_f(0.75*u)}"'
            f' ry="{_f(0.5*u)}" fill="#f9a8d4" opacity="0.6"/>'
        )
    if mood == "sleepy":
        return (
            f'<path d="M{_f(cx-3.2*u)},{_f(cy)} Q{_f(cx-2.2*u)},{_f(cy+1.3*u)}'
            f' {_f(cx-1.2*u)},{_f(cy)}" stroke="{ink}" stroke-width="{sw}"'
            ' fill="none" stroke-linecap="round"/>'
            f'<path d="M{_f(cx+1.2*u)},{_f(cy)} Q{_f(cx+2.2*u)},{_f(cy+1.3*u)}'
            f' {_f(cx+3.2*u)},{_f(cy)}" stroke="{ink}" stroke-width="{sw}"'
            ' fill="none" stroke-linecap="round"/>'
            f'<path d="M{_f(cx-1.1*u)},{_f(cy+1.9*u)} H{_f(cx+1.1*u)}"'
            f' stroke="{ink}" stroke-width="{sw}" stroke-linecap="round"/>'
            f'<text x="{_f(cx+4.8*u)}" y="{_f(cy-2.4*u)}"'
            f' font-size="{_f(3.4*u)}" fill="#7dd3fc" font-weight="bold">z</text>'
            f'<text x="{_f(cx+6.8*u)}" y="{_f(cy-5.2*u)}"'
            f' font-size="{_f(4.4*u)}" fill="#38bdf8" font-weight="bold">z</text>'
        )
    if mood == "peckish":
        # hungry: half-lidded droopy eyes, a little open mouth, and a
        # tummy-rumble squiggle. Feed your Tidepal!
        return (
            f'<ellipse cx="{_f(cx-2.2*u)}" cy="{_f(cy)}"'
            f' rx="{_f(0.95*u)}" ry="{_f(0.55*u)}" fill="{ink}"/>'
            f'<ellipse cx="{_f(cx+2.2*u)}" cy="{_f(cy)}"'
            f' rx="{_f(0.95*u)}" ry="{_f(0.55*u)}" fill="{ink}"/>'
            f'<ellipse cx="{_f(cx)}" cy="{_f(cy+1.9*u)}"'
            f' rx="{_f(0.8*u)}" ry="{_f(1.05*u)}" fill="{ink}"'
            ' opacity="0.85"/>'
            f'<path d="M{_f(cx+4.6*u)},{_f(cy-1*u)}'
            f' q{_f(1.2*u)},{_f(-1*u)} {_f(2.4*u)},0'
            f' q{_f(1.2*u)},{_f(1*u)} {_f(2.4*u)},0"'
            f' stroke="#f59e0b" stroke-width="{sw}" fill="none"'
            ' stroke-linecap="round"/>'
        )
    if mood == "grumpy":
        # neglected: flat annoyed eyes, angled brows, a proper frown.
        return (
            f'<circle cx="{_f(cx-2.2*u)}" cy="{_f(cy+0.3*u)}"'
            f' r="{_f(0.62*u)}" fill="{ink}"/>'
            f'<circle cx="{_f(cx+2.2*u)}" cy="{_f(cy+0.3*u)}"'
            f' r="{_f(0.62*u)}" fill="{ink}"/>'
            f'<path d="M{_f(cx-3.4*u)},{_f(cy-1.6*u)}'
            f' L{_f(cx-1*u)},{_f(cy-0.7*u)}"'
            f' stroke="{ink}" stroke-width="{sw}" stroke-linecap="round"/>'
            f'<path d="M{_f(cx+3.4*u)},{_f(cy-1.6*u)}'
            f' L{_f(cx+1*u)},{_f(cy-0.7*u)}"'
            f' stroke="{ink}" stroke-width="{sw}" stroke-linecap="round"/>'
            f'<path d="M{_f(cx-1.6*u)},{_f(cy+2.4*u)}'
            f' Q{_f(cx)},{_f(cy+1.4*u)} {_f(cx+1.6*u)},{_f(cy+2.4*u)}"'
            f' stroke="{ink}" stroke-width="{sw}" fill="none"'
            ' stroke-linecap="round"/>'
        )
    return (
        f'<circle cx="{_f(cx-2.2*u)}" cy="{_f(cy)}" r="{_f(0.62*u)}" fill="{ink}"/>'
        f'<circle cx="{_f(cx+2.2*u)}" cy="{_f(cy)}" r="{_f(0.62*u)}" fill="{ink}"/>'
        f'<circle cx="{_f(cx-2*u)}" cy="{_f(cy-0.22*u)}" r="{_f(0.2*u)}"'
        ' fill="#fff" opacity="0.9"/>'
        f'<circle cx="{_f(cx+2.4*u)}" cy="{_f(cy-0.22*u)}" r="{_f(0.2*u)}"'
        ' fill="#fff" opacity="0.9"/>'
        f'<path d="M{_f(cx-1.7*u)},{_f(cy+1.3*u)} Q{_f(cx)},{_f(cy+2.4*u)}'
        f' {_f(cx+1.7*u)},{_f(cy+1.3*u)}" stroke="{ink}" stroke-width="{sw}"'
        ' fill="none" stroke-linecap="round"/>'
    )


def _shadow():
    return ('<ellipse cx="60" cy="106" rx="26" ry="6" fill="#0ea5e9"'
            ' opacity="0.14"/>')


def _aura():
    g = _gid("aura")
    return (
        f'<defs><radialGradient id="{g}" cx="50%" cy="50%" r="50%">'
        '<stop offset="0%" stop-color="#a5f3fc" stop-opacity="0.55"/>'
        '<stop offset="100%" stop-color="#a5f3fc" stop-opacity="0"/>'
        "</radialGradient></defs>"
        f'<circle cx="60" cy="62" r="46" fill="url(#{g})"/>')


def _sparkles():
    star = ("M0,-7 C1.2,-2.4 2.4,-1.2 7,0 C2.4,1.2 1.2,2.4 0,7 "
            "C-1.2,2.4 -2.4,1.2 -7,0 C-2.4,-1.2 -1.2,-2.4 0,-7 Z")
    out = []
    for x, y, s, c in [(24, 30, 1.0, "#fef9c3"), (96, 36, 0.8, "#fde68a"),
                       (90, 96, 0.9, "#fef9c3")]:
        out.append(f'<path d="{star}" transform="translate({x} {y}) scale({s})"'
                   f' fill="{c}" opacity="0.95"/>')
    return "".join(out)


def _egg(fill_inner, spots):
    return (fill_inner + "".join(spots))


# --- Driplet: droplet sprite ------------------------------------------------
def _art_driplet(stage, mood):
    g = _gid("dr")
    grad = (f'<linearGradient id="{g}" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0%" stop-color="#bae6fd"/>'
            '<stop offset="55%" stop-color="#38bdf8"/>'
            '<stop offset="100%" stop-color="#0284c7"/></linearGradient>')
    if stage == 0:
        return (
            f"<defs>{grad}</defs>"
            '<path d="M60,32 C46,32 38,52 38,70 a22,24 0 0,0 44,0'
            f' C82,52 74,32 60,32 Z" fill="url(#{g})"/>'
            '<path d="M52,58 c0,0 -4,7 -4,11 a4,4 0 0,0 8,0 c0,-4 -4,-11 -4,-11 Z"'
            ' fill="#e0f2fe" opacity="0.7"/>'
            '<path d="M66,70 c0,0 -3,5 -3,8 a3,3 0 0,0 6,0 c0,-3 -3,-8 -3,-8 Z"'
            ' fill="#e0f2fe" opacity="0.6"/>'
            + _face(60, 62, 4, mood))
    parts = [f"<defs>{grad}</defs>"]
    parts.append(
        '<path d="M60,24 C60,24 36,58 36,78 a24,24 0 0,0 48,0'
        f' C84,58 60,24 60,24 Z" fill="url(#{g})" stroke="#e0f2fe"'
        ' stroke-width="1.5" stroke-opacity="0.8"/>')
    parts.append('<ellipse cx="48" cy="68" rx="6.5" ry="10.5" fill="#fff"'
                 ' opacity="0.5" transform="rotate(-18 48 68)"/>')
    parts.append('<circle cx="70" cy="84" r="3" fill="#fff" opacity="0.4"/>')
    if stage >= 2:
        parts.append('<path d="M28,84 c0,0 -6,9 -6,14 a6,6 0 0,0 12,0'
                     ' c0,-5 -6,-14 -6,-14 Z" fill="#7dd3fc" opacity="0.85"/>')
        parts.append('<path d="M92,84 c0,0 -6,9 -6,14 a6,6 0 0,0 12,0'
                     ' c0,-5 -6,-14 -6,-14 Z" fill="#7dd3fc" opacity="0.85"/>')
    if stage >= 3:
        parts.append('<path d="M48,80 q12,10 24,0" stroke="#fff"'
                     ' stroke-width="2.5" fill="none" opacity="0.5"'
                     ' stroke-linecap="round"/>')
    parts.append(_face(60, 72, 5, mood))
    return "".join(parts)


# --- Bloop: bubble buddy ----------------------------------------------------
def _art_bloop(stage, mood):
    g = _gid("bl")
    grad = (f'<radialGradient id="{g}" cx="38%" cy="32%" r="75%">'
            '<stop offset="0%" stop-color="#ffffff" stop-opacity="0.95"/>'
            '<stop offset="45%" stop-color="#cffafe" stop-opacity="0.9"/>'
            '<stop offset="100%" stop-color="#67e8f9" stop-opacity="0.9"/>'
            "</radialGradient>")
    if stage == 0:
        return (
            f"<defs>{grad}</defs>"
            f'<ellipse cx="60" cy="62" rx="24" ry="28" fill="url(#{g})"'
            ' opacity="0.92"/>'
            '<circle cx="52" cy="54" r="4" fill="#fff" opacity="0.8"/>'
            '<circle cx="68" cy="70" r="2.5" fill="#fff" opacity="0.6"/>'
            + _face(60, 62, 4, mood))
    parts = [f"<defs>{grad}</defs>"]
    if stage >= 2:
        parts.append('<ellipse cx="31" cy="66" rx="7" ry="4" fill="#67e8f9"'
                     ' opacity="0.8" transform="rotate(-25 31 66)"/>')
        parts.append('<ellipse cx="89" cy="66" rx="7" ry="4" fill="#67e8f9"'
                     ' opacity="0.8" transform="rotate(25 89 66)"/>')
    parts.append(f'<circle cx="60" cy="62" r="28" fill="url(#{g})"'
                 ' stroke="#a5f3fc" stroke-width="2"/>')
    parts.append('<ellipse cx="49" cy="51" rx="9" ry="6" fill="#fff"'
                 ' opacity="0.85" transform="rotate(-30 49 51)"/>')
    parts.append('<circle cx="70" cy="73" r="2.5" fill="#fff" opacity="0.5"/>')
    if stage >= 3:
        parts.append('<circle cx="60" cy="96" r="5" fill="#a5f3fc" opacity="0.7"/>')
        parts.append('<circle cx="60" cy="105" r="3.2" fill="#a5f3fc"'
                     ' opacity="0.55"/>')
    parts.append(_face(60, 64, 5, mood))
    return "".join(parts)


# --- Koi: koi wisp -----------------------------------------------------------
def _art_koi(stage, mood):
    g = _gid("ko")
    tg = _gid("kt")
    grad = (f'<linearGradient id="{g}" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0%" stop-color="#ffffff"/>'
            '<stop offset="100%" stop-color="#e0f2fe"/></linearGradient>')
    tailg = (f'<linearGradient id="{tg}" x1="0" y1="0" x2="0" y2="1">'
             '<stop offset="0%" stop-color="#67e8f9"/>'
             '<stop offset="100%" stop-color="#0ea5e9" stop-opacity="0.25"/>'
             "</linearGradient>")
    if stage == 0:
        return (
            f"<defs>{grad}</defs>"
            '<path d="M60,32 C46,32 38,52 38,70 a22,24 0 0,0 44,0'
            f' C82,52 74,32 60,32 Z" fill="url(#{g})"/>'
            '<path d="M48,60 q6,-8 12,0 q-6,6 -12,0 Z" fill="#38bdf8"'
            ' opacity="0.55"/>'
            + _face(60, 62, 4, mood))
    tail_len = 108 if stage >= 2 else 102
    parts = [f"<defs>{grad}{tailg}</defs>"]
    parts.append(f'<path d="M52,84 C46,92 58,96 52,{tail_len}"'
                 f' stroke="url(#{tg})" stroke-width="6" fill="none"'
                 ' stroke-linecap="round" opacity="0.85"/>')
    parts.append(f'<path d="M60,88 C60,96 62,100 60,{tail_len + 2}"'
                 f' stroke="url(#{tg})" stroke-width="4.5" fill="none"'
                 ' stroke-linecap="round" opacity="0.8"/>')
    parts.append(f'<path d="M68,84 C74,92 62,96 68,{tail_len}"'
                 f' stroke="url(#{tg})" stroke-width="6" fill="none"'
                 ' stroke-linecap="round" opacity="0.85"/>')
    parts.append(f'<ellipse cx="60" cy="58" rx="20" ry="30" fill="url(#{g})"'
                 ' stroke="#bae6fd" stroke-width="1.5"'
                 ' transform="rotate(12 60 58)"/>')
    parts.append('<path d="M50,40 q7,-6 13,1 q-4,8 -12,5 q-4,-3 -1,-6 Z"'
                 ' fill="#38bdf8" opacity="0.55"/>')
    parts.append('<path d="M56,66 q8,-4 12,3 q-5,7 -12,3 q-3,-3 0,-6 Z"'
                 ' fill="#0ea5e9" opacity="0.45"/>')
    parts.append('<path d="M60,28 q-6,-8 -2,-14 q6,4 8,12 Z" fill="#7dd3fc"'
                 ' opacity="0.9"/>')
    if stage >= 2:
        parts.append('<path d="M44,52 q-8,2 -12,8" stroke="#0b3b5c"'
                     ' stroke-width="1.2" fill="none" opacity="0.5"'
                     ' stroke-linecap="round"/>')
        parts.append('<path d="M76,52 q8,2 12,8" stroke="#0b3b5c"'
                     ' stroke-width="1.2" fill="none" opacity="0.5"'
                     ' stroke-linecap="round"/>')
    if stage >= 3:
        parts.append('<ellipse cx="40" cy="66" rx="4" ry="10" fill="#7dd3fc"'
                     ' opacity="0.7" transform="rotate(-30 40 66)"/>')
        parts.append('<ellipse cx="80" cy="66" rx="4" ry="10" fill="#7dd3fc"'
                     ' opacity="0.7" transform="rotate(30 80 66)"/>')
    parts.append(_face(60, 54, 4.5, mood))
    return "".join(parts)


# --- Pearly: pearl crab -------------------------------------------------------
def _art_pearly(stage, mood):
    g = _gid("pe")
    grad = (f'<radialGradient id="{g}" cx="38%" cy="30%" r="78%">'
            '<stop offset="0%" stop-color="#ffffff"/>'
            '<stop offset="55%" stop-color="#ede9fe"/>'
            '<stop offset="100%" stop-color="#c4b5fd"/></radialGradient>')
    if stage == 0:
        return (
            f"<defs>{grad}</defs>"
            '<path d="M60,32 C46,32 38,52 38,70 a22,24 0 0,0 44,0'
            f' C82,52 74,32 60,32 Z" fill="url(#{g})"/>'
            '<circle cx="52" cy="62" r="3" fill="#a78bfa" opacity="0.5"/>'
            '<circle cx="66" cy="72" r="2.4" fill="#a78bfa" opacity="0.45"/>'
            '<circle cx="60" cy="54" r="2" fill="#a78bfa" opacity="0.4"/>'
            + _face(60, 62, 4, mood))
    parts = [f"<defs>{grad}</defs>"]
    if stage >= 2:
        for d in ["M39,68 l-11,7", "M37,76 l-12,3", "M38,84 l-11,-1",
                  "M81,68 l11,7", "M83,76 l12,3", "M82,84 l11,-1"]:
            parts.append(f'<path d="{d}" stroke="#a78bfa" stroke-width="3.2"'
                         ' stroke-linecap="round"/>')
    parts.append(f'<circle cx="60" cy="62" r="25" fill="url(#{g})"'
                 ' stroke="#ddd6fe" stroke-width="1.5"/>')
    parts.append('<ellipse cx="50" cy="52" rx="8" ry="5.5" fill="#fff"'
                 ' opacity="0.8" transform="rotate(-25 50 52)"/>')
    parts.append('<circle cx="43" cy="40" r="6.5" fill="#ddd6fe"'
                 ' stroke="#a78bfa" stroke-width="2"/>')
    parts.append('<circle cx="77" cy="40" r="6.5" fill="#ddd6fe"'
                 ' stroke="#a78bfa" stroke-width="2"/>')
    parts.append('<path d="M40,37 l6,6 M80,37 l-6,6" stroke="#a78bfa"'
                 ' stroke-width="2" stroke-linecap="round"/>')
    if stage >= 3:
        for cx, cy in [(52, 34), (60, 31), (68, 34)]:
            parts.append(f'<circle cx="{cx}" cy="{cy}" r="2.6" fill="#f5f3ff"'
                         ' stroke="#a78bfa" stroke-width="1"/>')
    parts.append(_face(60, 64, 4.5, mood))
    return "".join(parts)


# --- Kelpy: kelp sprite --------------------------------------------------------
def _art_kelpy(stage, mood):
    g = _gid("ke")
    grad = (f'<radialGradient id="{g}" cx="40%" cy="32%" r="75%">'
            '<stop offset="0%" stop-color="#d1fae5"/>'
            '<stop offset="55%" stop-color="#34d399"/>'
            '<stop offset="100%" stop-color="#059669"/></radialGradient>')
    if stage == 0:
        return (
            f"<defs>{grad}</defs>"
            '<path d="M60,32 C46,32 38,52 38,70 a22,24 0 0,0 44,0'
            f' C82,52 74,32 60,32 Z" fill="url(#{g})"/>'
            '<path d="M60,44 q-2,16 0,32" stroke="#065f46" stroke-width="2"'
            ' fill="none" opacity="0.5"/>'
            + _face(60, 62, 4, mood))
    blades = ([("M58,66 C48,68 44,76 34,76", "#34d399"),
               ("M58,74 C46,78 40,86 30,88", "#10b981"),
               ("M62,66 C72,68 76,76 86,76", "#6ee7b7"),
               ("M62,74 C74,78 80,86 90,88", "#059669")]
              + ([("M57,82 C48,88 44,94 36,98", "#34d399"),
                  ("M63,82 C72,88 76,94 84,98", "#10b981")] if stage >= 2 else [])
              + ([("M56,90 C50,96 48,100 42,104", "#6ee7b7"),
                  ("M64,90 C70,96 72,100 78,104", "#059669")] if stage >= 3 else []))
    parts = [f"<defs>{grad}</defs>"]
    for d, c in blades:
        parts.append(f'<path d="{d}" stroke="{c}" stroke-width="5" fill="none"'
                     ' stroke-linecap="round" opacity="0.9"/>')
    parts.append('<path d="M60,62 C56,74 64,82 60,94" stroke="#059669"'
                 ' stroke-width="6" fill="none" stroke-linecap="round"/>')
    parts.append(f'<circle cx="60" cy="46" r="17" fill="url(#{g})"'
                 ' stroke="#a7f3d0" stroke-width="1.5"/>')
    parts.append('<ellipse cx="54" cy="40" rx="5" ry="3.5" fill="#fff"'
                 ' opacity="0.7" transform="rotate(-20 54 40)"/>')
    if stage >= 3:
        parts.append('<circle cx="38" cy="60" r="2.5" fill="#a7f3d0"'
                     ' opacity="0.7"/>')
        parts.append('<circle cx="84" cy="52" r="2" fill="#a7f3d0"'
                     ' opacity="0.7"/>')
        parts.append('<circle cx="78" cy="92" r="3" fill="#a7f3d0"'
                     ' opacity="0.6"/>')
    parts.append(_face(60, 48, 4, mood))
    return "".join(parts)


# --- Surfpup: wave pup ---------------------------------------------------------
def _art_surfpup(stage, mood):
    g = _gid("sp")
    grad = (f'<linearGradient id="{g}" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0%" stop-color="#7dd3fc"/>'
            '<stop offset="55%" stop-color="#0ea5e9"/>'
            '<stop offset="100%" stop-color="#0369a1"/></linearGradient>')
    if stage == 0:
        return (
            f"<defs>{grad}</defs>"
            '<path d="M60,32 C46,32 38,52 38,70 a22,24 0 0,0 44,0'
            f' C82,52 74,32 60,32 Z" fill="url(#{g})"/>'
            '<path d="M48,58 q8,-6 16,0 q-8,5 -16,0 Z" fill="#e0f2fe"'
            ' opacity="0.55"/>'
            '<ellipse cx="52" cy="56" rx="4" ry="7" fill="#fff"'
            ' opacity="0.55" transform="rotate(-15 52 56)"/>'
            + _face(60, 64, 4, mood))
    parts = [f"<defs>{grad}</defs>"]
    # wagging tail (behind body)
    tw = 4 + stage
    parts.append(f'<path d="M80,84 C92,88 96,98 90,108" stroke="url(#{g})"'
                 f' stroke-width="{tw}" fill="none" stroke-linecap="round"/>')
    # floppy ears (behind head)
    ear = 1.0 if stage < 2 else 1.25
    parts.append(
        f'<g transform="translate(42 46) scale({ear})">'
        '<path d="M0,0 C-10,4 -14,18 -8,30 C-4,22 0,12 6,6 Z"'
        f' fill="url(#{g})" stroke="#e0f2fe" stroke-width="1"'
        ' stroke-opacity="0.6"/></g>')
    parts.append(
        f'<g transform="translate(78 46) scale({ear})">'
        '<path d="M0,0 C10,4 14,18 8,30 C4,22 0,12 -6,6 Z"'
        f' fill="url(#{g})" stroke="#e0f2fe" stroke-width="1"'
        ' stroke-opacity="0.6"/></g>')
    # droplet body
    parts.append(
        '<path d="M60,26 C60,26 38,56 38,78 a22,22 0 0,0 44,0'
        f' C82,56 60,26 60,26 Z" fill="url(#{g})" stroke="#e0f2fe"'
        ' stroke-width="1.5" stroke-opacity="0.8"/>')
    parts.append('<ellipse cx="49" cy="66" rx="6" ry="10" fill="#fff"'
                 ' opacity="0.5" transform="rotate(-18 49 66)"/>')
    if stage >= 2:
        parts.append('<circle cx="70" cy="86" r="4" fill="#0369a1"'
                     ' opacity="0.35"/>')
        parts.append('<circle cx="50" cy="90" r="2.6" fill="#0369a1"'
                     ' opacity="0.3"/>')
    if stage >= 3:
        parts.append('<path d="M44,84 Q60,93 76,84" stroke="#fde68a"'
                     ' stroke-width="3" fill="none" stroke-linecap="round"/>')
        parts.append('<circle cx="60" cy="92" r="3" fill="#fbbf24"'
                     ' stroke="#fff" stroke-width="1"/>')
        parts.append('<path d="M26,106 q17,-10 34,0 q17,10 34,0"'
                     ' stroke="#7dd3fc" stroke-width="3" fill="none"'
                     ' stroke-linecap="round" opacity="0.8"/>')
    parts.append(_face(60, 64, 4.5, mood))
    parts.append('<circle cx="60" cy="67.5" r="1.7" fill="#0b3b5c"/>')
    return "".join(parts)


# --- Bubblepup: bubble retriever -------------------------------------------------
def _art_bubblepup(stage, mood):
    g = _gid("bp")
    grad = (f'<radialGradient id="{g}" cx="38%" cy="32%" r="75%">'
            '<stop offset="0%" stop-color="#ffffff" stop-opacity="0.95"/>'
            '<stop offset="45%" stop-color="#ccfbf1" stop-opacity="0.9"/>'
            '<stop offset="100%" stop-color="#5eead4" stop-opacity="0.9"/>'
            "</radialGradient>")
    if stage == 0:
        return (
            f"<defs>{grad}</defs>"
            f'<ellipse cx="60" cy="62" rx="24" ry="28" fill="url(#{g})"'
            ' opacity="0.92"/>'
            '<circle cx="52" cy="54" r="4" fill="#fff" opacity="0.8"/>'
            '<circle cx="68" cy="70" r="2.5" fill="#fff" opacity="0.6"/>'
            + _face(60, 62, 4, mood))
    parts = [f"<defs>{grad}</defs>"]
    # tail with a bubble tip
    parts.append('<path d="M84,78 C94,80 96,90 90,96" stroke="#5eead4"'
                 ' stroke-width="4.5" fill="none" stroke-linecap="round"/>')
    parts.append('<circle cx="90" cy="99" r="4.5" fill="#99f6e4"'
                 ' stroke="#fff" stroke-width="1" opacity="0.9"/>')
    # long floppy ears
    parts.append('<ellipse cx="33" cy="76" rx="7" ry="15" fill="#99f6e4"'
                 ' opacity="0.95" transform="rotate(18 33 76)"/>')
    parts.append('<ellipse cx="87" cy="76" rx="7" ry="15" fill="#99f6e4"'
                 ' opacity="0.95" transform="rotate(-18 87 76)"/>')
    # round bubble body
    parts.append(f'<circle cx="60" cy="62" r="26" fill="url(#{g})"'
                 ' stroke="#99f6e4" stroke-width="2"/>')
    parts.append('<ellipse cx="50" cy="52" rx="9" ry="6" fill="#fff"'
                 ' opacity="0.85" transform="rotate(-30 50 52)"/>')
    if stage >= 2:
        parts.append('<circle cx="72" cy="72" r="3.4" fill="#0d9488"'
                     ' opacity="0.25"/>')
        parts.append('<circle cx="48" cy="74" r="2.4" fill="#0d9488"'
                     ' opacity="0.22"/>')
    if stage >= 3:
        parts.append('<path d="M44,84 Q60,93 76,84" stroke="#fde68a"'
                     ' stroke-width="3" fill="none" stroke-linecap="round"/>')
        parts.append('<circle cx="60" cy="92" r="3" fill="#fbbf24"'
                     ' stroke="#fff" stroke-width="1"/>')
        parts.append('<circle cx="34" cy="40" r="3" fill="#fff"'
                     ' opacity="0.7"/>')
        parts.append('<circle cx="88" cy="44" r="2.2" fill="#fff"'
                     ' opacity="0.6"/>')
    parts.append(_face(60, 60, 4.5, mood))
    parts.append('<circle cx="60" cy="63.5" r="1.7" fill="#0b3b5c"/>')
    return "".join(parts)


# --- Sealpup: seal pup --------------------------------------------------------------
def _art_sealpup(stage, mood):
    g = _gid("sl")
    grad = (f'<linearGradient id="{g}" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0%" stop-color="#e2e8f0"/>'
            '<stop offset="55%" stop-color="#94a3b8"/>'
            '<stop offset="100%" stop-color="#64748b"/></linearGradient>')
    if stage == 0:
        return (
            f"<defs>{grad}</defs>"
            '<path d="M60,32 C46,32 38,52 38,70 a22,24 0 0,0 44,0'
            f' C82,52 74,32 60,32 Z" fill="url(#{g})"/>'
            '<ellipse cx="52" cy="56" rx="4" ry="7" fill="#fff"'
            ' opacity="0.5" transform="rotate(-15 52 56)"/>'
            + _face(60, 62, 4, mood))
    parts = [f"<defs>{grad}</defs>"]
    # tail fluke
    if stage >= 2:
        parts.append('<path d="M53,90 C48,100 42,105 34,107'
                     ' C41,110 49,108 54,100 Z" fill="#64748b"/>')
        parts.append('<path d="M67,90 C72,100 78,105 86,107'
                     ' C79,110 71,108 66,100 Z" fill="#64748b"/>')
    # flippers
    fs = 1.0 if stage < 3 else 1.2
    parts.append(
        f'<g transform="translate(42 72) scale({fs})">'
        '<path d="M0,0 C-10,2 -15,12 -12,22 C-8,14 -4,8 2,4 Z"'
        ' fill="#64748b"/></g>')
    parts.append(
        f'<g transform="translate(78 72) scale({fs})">'
        '<path d="M0,0 C10,2 15,12 12,22 C8,14 4,8 -2,4 Z"'
        ' fill="#64748b"/></g>')
    # plump body + pale belly
    parts.append(f'<ellipse cx="60" cy="64" rx="20" ry="28" fill="url(#{g})"'
                 ' stroke="#cbd5e1" stroke-width="1.5"/>')
    parts.append('<ellipse cx="60" cy="72" rx="11" ry="17" fill="#f1f5f9"'
                 ' opacity="0.65"/>')
    parts.append('<ellipse cx="53" cy="48" rx="5" ry="8" fill="#fff"'
                 ' opacity="0.45" transform="rotate(-15 53 48)"/>')
    if stage >= 2:
        parts.append('<circle cx="70" cy="46" r="2.6" fill="#475569"'
                     ' opacity="0.4"/>')
        parts.append('<circle cx="50" cy="84" r="2.2" fill="#475569"'
                     ' opacity="0.35"/>')
        for sx in (-1, 1):
            parts.append(f'<path d="M{_f(60+10*sx)},64 l{_f(11*sx)},-3"'
                         ' stroke="#0b3b5c" stroke-width="0.9" opacity="0.5"'
                         ' stroke-linecap="round"/>')
            parts.append(f'<path d="M{_f(60+10*sx)},68 l{_f(11*sx)},3"'
                         ' stroke="#0b3b5c" stroke-width="0.9" opacity="0.5"'
                         ' stroke-linecap="round"/>')
    if stage >= 3:
        for sx in (-1, 1):
            parts.append(f'<path d="M{_f(60+9*sx)},60 l{_f(10*sx)},-6"'
                         ' stroke="#0b3b5c" stroke-width="0.9" opacity="0.5"'
                         ' stroke-linecap="round"/>')
        # balancing a little beach-glass ball
        parts.append('<circle cx="60" cy="34" r="4.5" fill="#7dd3fc"'
                     ' opacity="0.85"/>')
        parts.append('<circle cx="60" cy="34" r="4.5" fill="none"'
                     ' stroke="#fff" stroke-width="1" opacity="0.7"/>')
    parts.append(_face(60, 56, 4.5, mood))
    parts.append('<ellipse cx="60" cy="59.5" rx="2" ry="1.4" fill="#0b3b5c"/>')
    return "".join(parts)


# --- Jellypup: jellyfish pup ------------------------------------------------------------
def _art_jellypup(stage, mood):
    g = _gid("je")
    grad = (f'<radialGradient id="{g}" cx="42%" cy="30%" r="75%">'
            '<stop offset="0%" stop-color="#fdf4ff" stop-opacity="0.95"/>'
            '<stop offset="55%" stop-color="#e9d5ff" stop-opacity="0.85"/>'
            '<stop offset="100%" stop-color="#a855f7" stop-opacity="0.85"/>'
            "</radialGradient>")
    if stage == 0:
        return (
            f"<defs>{grad}</defs>"
            '<path d="M60,32 C46,32 38,52 38,70 a22,24 0 0,0 44,0'
            f' C82,52 74,32 60,32 Z" fill="url(#{g})"/>'
            '<ellipse cx="52" cy="56" rx="4" ry="7" fill="#fff"'
            ' opacity="0.6" transform="rotate(-15 52 56)"/>'
            + _face(60, 62, 4, mood))
    parts = [f"<defs>{grad}</defs>"]
    # trailing tentacles (behind the bell)
    tents = ["M50,66 q-4,10 2,18 q4,8 -2,16",
             "M70,66 q4,10 -2,18 q-4,8 2,16"]
    if stage >= 2:
        tents += ["M41,64 q-5,10 0,18 q3,8 -3,15",
                  "M79,64 q5,10 0,18 q-3,8 3,15"]
    if stage >= 3:
        tents += ["M60,68 q0,10 -4,16 q-3,8 1,15",
                  "M33,62 q-6,8 -3,16",
                  "M87,62 q6,8 3,16"]
    for d in tents:
        parts.append(f'<path d="{d}" stroke="#c084fc" stroke-width="3"'
                     ' fill="none" stroke-linecap="round" opacity="0.8"/>')
    # glass bell
    parts.append('<path d="M34,68 a26,26 0 0,1 52,0 Z"'
                 f' fill="url(#{g})" stroke="#f5d3fe" stroke-width="2"/>')
    parts.append('<path d="M43,68 a17,17 0 0,1 34,0 Z"'
                 ' fill="#ffffff" opacity="0.28"/>')
    parts.append('<ellipse cx="48" cy="50" rx="6" ry="4" fill="#fff"'
                 ' opacity="0.7" transform="rotate(-25 48 50)"/>')
    if stage >= 3:
        parts.append('<path d="M34,68 q6.5,6 13,0 q6.5,6 13,0 q6.5,6 13,0'
                     ' q6.5,6 13,0" stroke="#d8b4fe" stroke-width="2.5"'
                     ' fill="none" stroke-linecap="round"/>')
        parts.append('<circle cx="30" cy="88" r="2.5" fill="#e9d5ff"'
                     ' opacity="0.8"/>')
        parts.append('<circle cx="92" cy="84" r="2" fill="#e9d5ff"'
                     ' opacity="0.7"/>')
    parts.append(_face(60, 52, 4, mood))
    return "".join(parts)


# --- Gilt: gilded koi (locked: Broadcast tier) ----------------------------------
def _art_gilt(stage, mood):
    g = _gid("gi")
    tg = _gid("gt")
    grad = (f'<linearGradient id="{g}" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0%" stop-color="#fefce8"/>'
            '<stop offset="55%" stop-color="#fde047"/>'
            '<stop offset="100%" stop-color="#d97706"/></linearGradient>')
    tailg = (f'<linearGradient id="{tg}" x1="0" y1="0" x2="0" y2="1">'
             '<stop offset="0%" stop-color="#fbbf24"/>'
             '<stop offset="100%" stop-color="#f59e0b" stop-opacity="0.2"/>'
             "</linearGradient>")
    if stage == 0:
        return (
            f"<defs>{grad}</defs>"
            '<path d="M60,32 C46,32 38,52 38,70 a22,24 0 0,0 44,0'
            f' C82,52 74,32 60,32 Z" fill="url(#{g})"/>'
            '<circle cx="52" cy="62" r="3" fill="#b45309" opacity="0.35"/>'
            '<circle cx="66" cy="72" r="2.4" fill="#b45309" opacity="0.3"/>'
            + _face(60, 62, 4, mood))
    tail_len = 108 if stage >= 2 else 102
    parts = [f"<defs>{grad}{tailg}</defs>"]
    parts.append(f'<path d="M52,84 C46,92 58,96 52,{tail_len}"'
                 f' stroke="url(#{tg})" stroke-width="6" fill="none"'
                 ' stroke-linecap="round" opacity="0.9"/>')
    parts.append(f'<path d="M68,84 C74,92 62,96 68,{tail_len}"'
                 f' stroke="url(#{tg})" stroke-width="6" fill="none"'
                 ' stroke-linecap="round" opacity="0.9"/>')
    parts.append(f'<ellipse cx="60" cy="58" rx="20" ry="30" fill="url(#{g})"'
                 ' stroke="#fef3c7" stroke-width="1.5"'
                 ' transform="rotate(12 60 58)"/>')
    for sx, sy in [(52, 52), (64, 58), (54, 68), (66, 72)]:
        parts.append(f'<path d="M{sx-4},{sy} q4,-4 8,0" stroke="#b45309"'
                     ' stroke-width="1.2" fill="none" opacity="0.5"'
                     ' stroke-linecap="round"/>')
    parts.append('<path d="M60,28 q-7,-9 -3,-16 q7,4 9,13 Z" fill="#fbbf24"'
                 ' stroke="#b45309" stroke-width="1"/>')
    if stage >= 2:
        parts.append('<path d="M44,52 q-8,2 -12,8" stroke="#92400e"'
                     ' stroke-width="1.2" fill="none" opacity="0.5"'
                     ' stroke-linecap="round"/>')
        parts.append('<path d="M76,52 q8,2 12,8" stroke="#92400e"'
                     ' stroke-width="1.2" fill="none" opacity="0.5"'
                     ' stroke-linecap="round"/>')
    if stage >= 3:
        parts.append('<ellipse cx="40" cy="66" rx="4" ry="10" fill="#fde68a"'
                     ' opacity="0.8" transform="rotate(-30 40 66)"/>')
        parts.append('<ellipse cx="80" cy="66" rx="4" ry="10" fill="#fde68a"'
                     ' opacity="0.8" transform="rotate(30 80 66)"/>')
        parts.append('<circle cx="50" cy="44" r="1.6" fill="#fff"'
                     ' opacity="0.9"/>')
        parts.append('<circle cx="70" cy="78" r="1.3" fill="#fff"'
                     ' opacity="0.8"/>')
    parts.append('<ellipse cx="52" cy="48" rx="5" ry="8" fill="#fff"'
                 ' opacity="0.55" transform="rotate(-15 52 48)"/>')
    parts.append(_face(60, 54, 4.5, mood))
    return "".join(parts)


# --- Tidehound: sleek tidehound (locked: 30-day streak) ---------------------------
def _art_tidehound(stage, mood):
    g = _gid("th")
    grad = (f'<linearGradient id="{g}" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0%" stop-color="#a5f3fc"/>'
            '<stop offset="55%" stop-color="#0e7490"/>'
            '<stop offset="100%" stop-color="#083344"/></linearGradient>')
    if stage == 0:
        return (
            f"<defs>{grad}</defs>"
            '<path d="M60,32 C46,32 38,52 38,70 a22,24 0 0,0 44,0'
            f' C82,52 74,32 60,32 Z" fill="url(#{g})"/>'
            '<ellipse cx="52" cy="56" rx="4" ry="7" fill="#fff"'
            ' opacity="0.5" transform="rotate(-15 52 56)"/>'
            + _face(60, 62, 4, mood))
    parts = [f"<defs>{grad}</defs>"]
    parts.append('<path d="M79,82 C90,84 94,94 88,104 C86,96 82,90 76,88 Z"'
                 f' fill="url(#{g})"/>')
    ear = 1.0 if stage < 2 else 1.2
    parts.append(
        f'<g transform="translate(44 44) scale({ear})">'
        '<path d="M0,0 L-8,-16 L6,-6 Z"'
        f' fill="url(#{g})" stroke="#cffafe" stroke-width="1"'
        ' stroke-opacity="0.6"/></g>')
    parts.append(
        f'<g transform="translate(76 44) scale({ear})">'
        '<path d="M0,0 L8,-16 L-6,-6 Z"'
        f' fill="url(#{g})" stroke="#cffafe" stroke-width="1"'
        ' stroke-opacity="0.6"/></g>')
    parts.append(
        '<path d="M60,26 C60,26 40,54 40,76 a20,20 0 0,0 40,0'
        f' C80,54 60,26 60,26 Z" fill="url(#{g})" stroke="#cffafe"'
        ' stroke-width="1.5" stroke-opacity="0.8"/>')
    parts.append('<path d="M50,72 q10,12 20,0 q-2,14 -10,14 q-8,0 -10,-14 Z"'
                 ' fill="#ecfeff" opacity="0.75"/>')
    parts.append('<ellipse cx="50" cy="62" rx="5.5" ry="9" fill="#fff"'
                 ' opacity="0.5" transform="rotate(-15 50 62)"/>')
    if stage >= 2:
        parts.append('<path d="M44,66 Q60,76 76,66" stroke="#a16207"'
                     ' stroke-width="3.5" fill="none" stroke-linecap="round"/>')
        parts.append('<circle cx="60" cy="74" r="3" fill="#fbbf24"'
                     ' stroke="#92400e" stroke-width="1"/>')
    if stage >= 3:
        parts.append('<path d="M60,77 v6 M56,79 h8 M56,79 q0,5 4,5 q4,0 4,-5"'
                     ' stroke="#fde68a" stroke-width="2" fill="none"'
                     ' stroke-linecap="round"/>')
        parts.append('<circle cx="72" cy="88" r="2.4" fill="#083344"'
                     ' opacity="0.3"/>')
    parts.append(_face(60, 60, 4.5, mood))
    parts.append('<ellipse cx="60" cy="63.5" rx="2" ry="1.5" fill="#0b3b5c"/>')
    return "".join(parts)


# --- Reefkeeper: coral architect (locked: Town Builder achievement) ------------------
def _art_reefkeeper(stage, mood):
    g = _gid("rk")
    grad = (f'<linearGradient id="{g}" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0%" stop-color="#ffedd5"/>'
            '<stop offset="55%" stop-color="#fb923c"/>'
            '<stop offset="100%" stop-color="#c2410c"/></linearGradient>')
    if stage == 0:
        return (
            f"<defs>{grad}</defs>"
            '<path d="M60,32 C46,32 38,52 38,70 a22,24 0 0,0 44,0'
            f' C82,52 74,32 60,32 Z" fill="url(#{g})"/>'
            '<circle cx="52" cy="62" r="3" fill="#9a3412" opacity="0.35"/>'
            '<circle cx="66" cy="70" r="2.4" fill="#9a3412" opacity="0.3"/>'
            + _face(60, 62, 4, mood))
    parts = [f"<defs>{grad}</defs>"]
    if stage >= 2:
        for sx, flip in ((46, -1), (74, 1)):
            parts.append(
                f'<path d="M{sx},40 q{6*flip},-8 {2*flip},-14'
                f' M{sx+2*flip},32 l{5*flip},-4 M{sx+3*flip},36 l{-4*flip},-5"'
                ' stroke="#f472b6" stroke-width="3" fill="none"'
                ' stroke-linecap="round"/>')
        parts.append('<path d="M40,70 C32,70 28,78 32,84 C36,80 38,76 42,74 Z"'
                     ' fill="#ea580c"/>')
        parts.append('<path d="M80,70 C88,70 92,78 88,84 C84,80 82,76 78,74 Z"'
                     ' fill="#ea580c"/>')
    parts.append(f'<rect x="40" y="42" width="40" height="48" rx="14"'
                 f' fill="url(#{g})" stroke="#fed7aa" stroke-width="1.5"/>')
    for by in (62, 70, 78):
        parts.append(f'<path d="M46,{by} h28" stroke="#9a3412"'
                     ' stroke-width="1.4" opacity="0.4"/>')
    parts.append('<path d="M60,62 v8 M53,70 v8 M67,70 v8" stroke="#9a3412"'
                 ' stroke-width="1.2" opacity="0.35"/>')
    for cx, cy in ((46, 48), (74, 48)):
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="1.8" fill="#7c2d12"'
                     ' opacity="0.5"/>')
    parts.append('<ellipse cx="50" cy="52" rx="5" ry="7" fill="#fff"'
                 ' opacity="0.45" transform="rotate(-15 50 52)"/>')
    if stage >= 3:
        parts.append('<path d="M46,42 C46,30 54,24 60,24 C66,24 74,30 74,42 Z"'
                     ' fill="#fde047" stroke="#a16207" stroke-width="1.2"/>')
        parts.append('<path d="M56,28 C56,32 64,32 64,28"'
                     ' stroke="#a16207" stroke-width="1.5" fill="none"/>')
        parts.append('<rect x="42" y="40" width="36" height="4" rx="2"'
                     ' fill="#facc15" stroke="#a16207" stroke-width="1"/>')
    parts.append(_face(60, 58, 4.5, mood))
    return "".join(parts)


# --- Zorb: one-of-one orb wisp, bonded to Zuckbot --------------------------------
def _art_zorb(stage, mood):
    g = _gid("zo")
    grad = (f'<radialGradient id="{g}" cx="38%" cy="30%" r="80%">'
            '<stop offset="0%" stop-color="#ffffff" stop-opacity="0.98"/>'
            '<stop offset="42%" stop-color="#fef3c7" stop-opacity="0.95"/>'
            '<stop offset="78%" stop-color="#7dd3fc" stop-opacity="0.92"/>'
            '<stop offset="100%" stop-color="#0284c7" stop-opacity="0.95"/>'
            "</radialGradient>")
    r = 20 if stage == 0 else 26
    parts = [f"<defs>{grad}</defs>"]
    if stage >= 2:
        # Saturn-style signal ring
        parts.append('<ellipse cx="60" cy="62" rx="42" ry="12" fill="none"'
                     ' stroke="#fde68a" stroke-width="2.5" opacity="0.7"'
                     ' transform="rotate(-18 60 62)"/>')
    parts.append(f'<circle cx="60" cy="62" r="{r}" fill="url(#{g})"'
                 ' stroke="#e0f2fe" stroke-width="2" stroke-opacity="0.9"/>')
    # glass highlight
    parts.append(f'<ellipse cx="{60-r*0.38}" cy="{62-r*0.42}"'
                 f' rx="{r*0.34}" ry="{r*0.2}" fill="#fff" opacity="0.75"'
                 f' transform="rotate(-24 {60-r*0.38} {62-r*0.42})"/>')
    # tiny infinity swirl riding inside the orb, above the face
    s = r / 26.0
    parts.append(
        '<path d="M60,50 C57.5,45.5 51.5,45.5 51.5,50 C51.5,54.5 57.5,54.5 60,50'
        ' C62.5,45.5 68.5,45.5 68.5,50 C68.5,54.5 62.5,54.5 60,50 Z"'
        f' fill="#f59e0b" opacity="0.85" transform="translate(60 50)'
        f' scale({s}) translate(-60 -50)"/>')
    if stage >= 3:
        parts.append('<circle cx="34" cy="40" r="2" fill="#fef9c3" opacity="0.9"/>')
        parts.append('<circle cx="88" cy="44" r="1.6" fill="#fef9c3" opacity="0.8"/>')
        parts.append('<circle cx="82" cy="86" r="2.2" fill="#bae6fd" opacity="0.8"/>')
    parts.append(_face(60, 62, 4, mood))
    return "".join(parts)


# --- Squiddy: ink squidling (open) -------------------------------------------------
def _art_squiddy(stage, mood):
    g = _gid("sq")
    grad = (f'<radialGradient id="{g}" cx="40%" cy="30%" r="78%">'
            '<stop offset="0%" stop-color="#ddd6fe"/>'
            '<stop offset="55%" stop-color="#8b5cf6"/>'
            '<stop offset="100%" stop-color="#4c1d95"/></radialGradient>')
    if stage == 0:
        return (
            f"<defs>{grad}</defs>"
            '<path d="M60,32 C46,32 38,52 38,70 a22,24 0 0,0 44,0'
            f' C82,52 74,32 60,32 Z" fill="url(#{g})"/>'
            '<circle cx="52" cy="62" r="3" fill="#2e1065" opacity="0.4"/>'
            '<circle cx="66" cy="72" r="2.4" fill="#2e1065" opacity="0.35"/>'
            + _face(60, 62, 4, mood))
    parts = [f"<defs>{grad}</defs>"]
    # tentacles (behind the mantle)
    tents = ["M48,66 q-3,10 2,17 q3,6 -2,12",
             "M56,68 q-1,10 3,16 q2,7 -3,13",
             "M64,68 q1,10 -3,16 q-2,7 3,13",
             "M72,66 q3,10 -2,17 q-3,6 2,12"]
    if stage >= 2:
        tents += ["M41,64 q-5,9 -1,16", "M79,64 q5,9 1,16"]
    if stage >= 3:
        tents += ["M52,68 q-2,12 1,20", "M68,68 q2,12 -1,20"]
    for d in tents:
        parts.append(f'<path d="{d}" stroke="#6d28d9" stroke-width="3.4"'
                     ' fill="none" stroke-linecap="round" opacity="0.9"/>')
    # mantle dome
    parts.append('<path d="M38,66 a22,22 0 0,1 44,0 Z"'
                 f' fill="url(#{g})" stroke="#c4b5fd" stroke-width="1.5"/>')
    parts.append('<ellipse cx="51" cy="52" rx="6" ry="4" fill="#fff"'
                 ' opacity="0.55" transform="rotate(-22 51 52)"/>')
    if stage >= 2:
        # side fins
        parts.append('<path d="M40,58 C32,54 28,48 28,42 C34,46 38,50 41,54 Z"'
                     ' fill="#7c3aed" opacity="0.85"/>')
        parts.append('<path d="M80,58 C88,54 92,48 92,42 C86,46 82,50 79,54 Z"'
                     ' fill="#7c3aed" opacity="0.85"/>')
    if stage >= 3:
        # ink-swirl crown + glow dots
        parts.append('<path d="M50,40 q10,-12 20,0 q-10,-6 -20,0 Z"'
                     ' fill="#2e1065" opacity="0.7"/>')
        parts.append('<circle cx="46" cy="60" r="1.8" fill="#a5f3fc"'
                     ' opacity="0.9"/>')
        parts.append('<circle cx="74" cy="58" r="1.5" fill="#a5f3fc"'
                     ' opacity="0.8"/>')
        parts.append('<circle cx="60" cy="46" r="1.4" fill="#a5f3fc"'
                     ' opacity="0.85"/>')
    parts.append(_face(60, 54, 4, mood))
    return "".join(parts)


# --- Puffish: puffer pup "Pip" (open) ------------------------------------------
def _art_puffish(stage, mood):
    g = _gid("pf")
    grad = (f'<radialGradient id="{g}" cx="38%" cy="30%" r="78%">'
            '<stop offset="0%" stop-color="#f0fdfa"/>'
            '<stop offset="55%" stop-color="#5eead4"/>'
            '<stop offset="100%" stop-color="#0f766e"/></radialGradient>')
    if stage == 0:
        spikes = ""
        import math as _m
        for i in range(8):
            a = i * _m.pi / 4
            x1, y1 = 60 + 20 * _m.cos(a), 62 + 22 * _m.sin(a)
            x2, y2 = 60 + 26 * _m.cos(a), 62 + 28 * _m.sin(a)
            spikes += (f'<path d="M{_f(x1-2)},{_f(y1)} L{_f(x2)},{_f(y2)}'
                       f' L{_f(x1+2)},{_f(y1)} Z" fill="#5eead4"/>')
        return (
            f"<defs>{grad}</defs>" + spikes +
            f'<ellipse cx="60" cy="62" rx="20" ry="22" fill="url(#{g})"/>'
            + _face(60, 62, 4, mood))
    import math as _m
    parts = [f"<defs>{grad}</defs>"]
    nspike = 10 if stage < 2 else (12 if stage < 3 else 14)
    r0, r1 = 26, 34
    for i in range(nspike):
        a = i * 2 * _m.pi / nspike - _m.pi / 2
        bx, by = 60 + r0 * _m.cos(a), 62 + r0 * _m.sin(a)
        tx, ty = 60 + r1 * _m.cos(a), 62 + r1 * _m.sin(a)
        px, py = -_m.sin(a) * 3.2, _m.cos(a) * 3.2
        parts.append(f'<path d="M{_f(bx+px)},{_f(by+py)} L{_f(tx)},{_f(ty)}'
                     f' L{_f(bx-px)},{_f(by-py)} Z" fill="#2dd4bf"'
                     ' stroke="#0f766e" stroke-width="0.8"/>')
    # little fins + tail nub
    parts.append('<ellipse cx="36" cy="72" rx="5" ry="9" fill="#2dd4bf"'
                 ' opacity="0.9" transform="rotate(25 36 72)"/>')
    parts.append('<ellipse cx="84" cy="72" rx="5" ry="9" fill="#2dd4bf"'
                 ' opacity="0.9" transform="rotate(-25 84 72)"/>')
    parts.append(f'<circle cx="60" cy="62" r="{r0}" fill="url(#{g})"'
                 ' stroke="#99f6e4" stroke-width="1.5"/>')
    parts.append('<ellipse cx="50" cy="52" rx="8" ry="5.5" fill="#fff"'
                 ' opacity="0.7" transform="rotate(-25 50 52)"/>')
    if stage >= 2:
        parts.append('<circle cx="70" cy="70" r="3" fill="#0f766e"'
                     ' opacity="0.25"/>')
        parts.append('<circle cx="50" cy="74" r="2.4" fill="#0f766e"'
                     ' opacity="0.22"/>')
    if stage >= 3:
        # proud blush + a celebratory bubble
        parts.append('<ellipse cx="47" cy="66" rx="3" ry="2" fill="#f9a8d4"'
                     ' opacity="0.7"/>')
        parts.append('<ellipse cx="73" cy="66" rx="3" ry="2" fill="#f9a8d4"'
                     ' opacity="0.7"/>')
        parts.append('<circle cx="88" cy="38" r="4" fill="#99f6e4"'
                     ' stroke="#fff" stroke-width="1" opacity="0.85"/>')
    parts.append(_face(60, 62, 4.5, mood))
    return "".join(parts)


# --- Crownjelly: royal jelly "Wobble" (locked: Juvenile stage) -------------------
def _art_crownjelly(stage, mood):
    g = _gid("cj")
    grad = (f'<radialGradient id="{g}" cx="42%" cy="30%" r="75%">'
            '<stop offset="0%" stop-color="#faf5ff" stop-opacity="0.95"/>'
            '<stop offset="55%" stop-color="#c4b5fd" stop-opacity="0.85"/>'
            '<stop offset="100%" stop-color="#6d28d9" stop-opacity="0.85"/>'
            "</radialGradient>")
    if stage == 0:
        return (
            f"<defs>{grad}</defs>"
            '<path d="M60,32 C46,32 38,52 38,70 a22,24 0 0,0 44,0'
            f' C82,52 74,32 60,32 Z" fill="url(#{g})"/>'
            '<path d="M54,32 l2,-7 l3,4 l3,-6 l3,6 l3,-4 l2,7 Z"'
            ' fill="#fbbf24" stroke="#b45309" stroke-width="0.8"/>'
            + _face(60, 62, 4, mood))
    parts = [f"<defs>{grad}</defs>"]
    tents = ["M50,66 q-4,10 2,18 q4,8 -2,16",
             "M70,66 q4,10 -2,18 q-4,8 2,16"]
    if stage >= 2:
        tents += ["M41,64 q-5,10 0,18", "M79,64 q5,10 0,18"]
    if stage >= 3:
        tents += ["M60,68 q0,10 -4,16", "M33,62 q-6,8 -3,16",
                  "M87,62 q6,8 3,16"]
    for d in tents:
        parts.append(f'<path d="{d}" stroke="#a78bfa" stroke-width="3"'
                     ' fill="none" stroke-linecap="round" opacity="0.85"/>')
    parts.append('<path d="M34,68 a26,26 0 0,1 52,0 Z"'
                 f' fill="url(#{g})" stroke="#e9d5ff" stroke-width="2"/>')
    parts.append('<ellipse cx="48" cy="50" rx="6" ry="4" fill="#fff"'
                 ' opacity="0.7" transform="rotate(-25 48 50)"/>')
    # the crown: present at every hatched stage — it is royalty
    cw = 1.0 if stage < 2 else 1.15
    parts.append(
        f'<g transform="translate(60 36) scale({cw})">'
        '<path d="M-13,6 L-13,-3 L-6.5,1 L0,-9 L6.5,1 L13,-3 L13,6 Z"'
        ' fill="#fbbf24" stroke="#b45309" stroke-width="1.2"/>'
        '<rect x="-13" y="6" width="26" height="4.5" rx="2"'
        ' fill="#f59e0b" stroke="#b45309" stroke-width="1"/></g>')
    if stage >= 2:
        for cx, col in [(47, "#f472b6"), (60, "#38bdf8"), (73, "#f472b6")]:
            parts.append(f'<circle cx="{cx}" cy="42" r="2.2" fill="{col}"'
                         ' stroke="#fff" stroke-width="0.8"/>')
    if stage >= 3:
        parts.append('<path d="M34,68 q6.5,6 13,0 q6.5,6 13,0 q6.5,6 13,0'
                     ' q6.5,6 13,0" stroke="#fde68a" stroke-width="2.5"'
                     ' fill="none" stroke-linecap="round"/>')
        parts.append('<circle cx="28" cy="90" r="2.4" fill="#fde68a"'
                     ' opacity="0.9"/>')
        parts.append('<circle cx="92" cy="86" r="2" fill="#fde68a"'
                     ' opacity="0.8"/>')
    parts.append(_face(60, 52, 4, mood))
    return "".join(parts)


# --- Abyssal: abyssal whale "Sonar" (locked: Adult stage) -------------------------
def _art_abyssal(stage, mood):
    g = _gid("ab")
    grad = (f'<linearGradient id="{g}" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0%" stop-color="#818cf8"/>'
            '<stop offset="55%" stop-color="#3730a3"/>'
            '<stop offset="100%" stop-color="#1e1b4b"/></linearGradient>')
    if stage == 0:
        return (
            f"<defs>{grad}</defs>"
            '<path d="M60,32 C46,32 38,52 38,70 a22,24 0 0,0 44,0'
            f' C82,52 74,32 60,32 Z" fill="url(#{g})"/>'
            '<path d="M58,32 q2,-6 0,-10 M64,32 q-2,-6 0,-10" stroke="#a5b4fc"'
            ' stroke-width="1.6" fill="none" stroke-linecap="round"/>'
            + _face(60, 62, 4, mood))
    parts = [f"<defs>{grad}</defs>"]
    fl = 1.0 if stage < 3 else 1.2
    # tail fluke (top)
    parts.append(
        f'<g transform="translate(72 44) scale({fl})">'
        '<path d="M0,0 C8,-10 20,-12 28,-8 C20,-4 12,-2 6,4 Z"'
        f' fill="url(#{g})" stroke="#a5b4fc" stroke-width="1"/>'
        '<path d="M0,0 C-8,-10 -20,-12 -28,-8 C-20,-4 -12,-2 -6,4 Z"'
        f' fill="url(#{g})" stroke="#a5b4fc" stroke-width="1"/></g>')
    # spout
    parts.append('<path d="M60,40 q1,-8 -3,-13 M63,40 q3,-7 1,-13"'
                 ' stroke="#bae6fd" stroke-width="2.4" fill="none"'
                 ' stroke-linecap="round" opacity="0.9"/>')
    parts.append('<circle cx="55" cy="24" r="2.6" fill="#bae6fd"'
                 ' opacity="0.85"/>')
    parts.append('<circle cx="66" cy="25" r="2" fill="#bae6fd"'
                 ' opacity="0.7"/>')
    # body
    parts.append(f'<ellipse cx="60" cy="70" rx="27" ry="21" fill="url(#{g})"'
                 ' stroke="#a5b4fc" stroke-width="1.5"/>')
    parts.append('<ellipse cx="60" cy="76" rx="16" ry="11" fill="#e0e7ff"'
                 ' opacity="0.35"/>')
    parts.append('<ellipse cx="48" cy="60" rx="7" ry="5" fill="#fff"'
                 ' opacity="0.4" transform="rotate(-20 48 60)"/>')
    # side fin
    parts.append('<path d="M42,78 C34,80 30,88 32,94 C38,90 42,84 44,80 Z"'
                 ' fill="#3730a3" stroke="#a5b4fc" stroke-width="1"/>')
    if stage >= 2:
        # bioluminescent dots along the belly
        for cx, cy in [(46, 82), (54, 86), (63, 87), (72, 84), (79, 79)]:
            parts.append(f'<circle cx="{cx}" cy="{cy}" r="1.8"'
                         ' fill="#67e8f9" opacity="0.9"/>')
    if stage >= 3:
        parts.append('<circle cx="38" cy="64" r="2" fill="#67e8f9"'
                     ' opacity="0.9"/>')
        parts.append('<circle cx="82" cy="62" r="2" fill="#67e8f9"'
                     ' opacity="0.9"/>')
        parts.append('<path d="M60,40 q0,-6 4,-10" stroke="#e0f2fe"'
                     ' stroke-width="2" fill="none" stroke-linecap="round"/>')
    parts.append(_face(50, 64, 4, mood))
    return "".join(parts)


# --- Frostfin: frostfin "Nippy" (locked: winter26 season) ---------------------------
def _art_frostfin(stage, mood):
    g = _gid("ff")
    grad = (f'<linearGradient id="{g}" x1="0" y1="0" x2="1" y2="1">'
            '<stop offset="0%" stop-color="#f0f9ff"/>'
            '<stop offset="55%" stop-color="#7dd3fc"/>'
            '<stop offset="100%" stop-color="#0284c7"/></linearGradient>')
    if stage == 0:
        return (
            f"<defs>{grad}</defs>"
            '<path d="M60,32 C46,32 38,52 38,70 a22,24 0 0,0 44,0'
            f' C82,52 74,32 60,32 Z" fill="url(#{g})"/>'
            '<path d="M60,44 v10 M55,47 l10,4 M65,47 l-10,4" stroke="#fff"'
            ' stroke-width="1.4" stroke-linecap="round"/>'
            + _face(60, 62, 4, mood))
    parts = [f"<defs>{grad}</defs>"]
    # forked tail fin (behind)
    parts.append('<path d="M80,62 C92,56 98,48 96,38 C90,44 84,46 78,46 Z"'
                 f' fill="url(#{g})" stroke="#e0f2fe" stroke-width="1"'
                 ' opacity="0.95"/>')
    parts.append('<path d="M80,62 C92,68 98,76 96,86 C90,80 84,78 78,78 Z"'
                 f' fill="url(#{g})" stroke="#e0f2fe" stroke-width="1"'
                 ' opacity="0.95"/>')
    # frost-crystal dorsal fin
    parts.append('<path d="M52,44 L56,30 L60,40 L64,28 L68,40 L72,32 L72,46 Z"'
                 ' fill="#e0f2fe" stroke="#fff" stroke-width="1"'
                 ' opacity="0.95"/>')
    # sleek body
    parts.append('<path d="M34,62 C40,48 54,42 66,46 C80,50 86,60 84,70'
                 ' C80,80 64,84 50,80 C40,77 32,70 34,62 Z"'
                 f' fill="url(#{g})" stroke="#e0f2fe" stroke-width="1.5"/>')
    parts.append('<ellipse cx="50" cy="58" rx="7" ry="4.5" fill="#fff"'
                 ' opacity="0.6" transform="rotate(-18 50 58)"/>')
    if stage >= 2:
        # ice-shard crown
        parts.append('<path d="M50,48 L53,36 L57,44 L61,34 L65,44 L69,36 L70,48'
                     ' Z" fill="#bae6fd" stroke="#fff" stroke-width="1"/>')
    if stage >= 3:
        # snowflake sparkles
        for cx, cy, s in [(30, 40, 1), (92, 50, 0.8), (86, 96, 0.9)]:
            parts.append(
                f'<g transform="translate({cx} {cy}) scale({s})"'
                ' stroke="#fff" stroke-width="1.3" stroke-linecap="round">'
                '<path d="M-4,0 H4 M0,-4 V4 M-2.8,-2.8 L2.8,2.8'
                ' M2.8,-2.8 L-2.8,2.8"/></g>')
        parts.append('<path d="M40,84 q10,6 20,2" stroke="#fff"'
                     ' stroke-width="2" fill="none" opacity="0.6"'
                     ' stroke-linecap="round"/>')
    parts.append(_face(56, 62, 4.2, mood))
    return "".join(parts)


# --- Kelpwarden: kelp warden "Brine" (locked: 10-day feed streak) -------------------
def _art_kelpwarden(stage, mood):
    g = _gid("kw")
    grad = (f'<radialGradient id="{g}" cx="40%" cy="32%" r="75%">'
            '<stop offset="0%" stop-color="#d1fae5"/>'
            '<stop offset="55%" stop-color="#34d399"/>'
            '<stop offset="100%" stop-color="#065f46"/></radialGradient>')
    lg = _gid("kl")
    lamp = (f'<radialGradient id="{lg}" cx="50%" cy="50%" r="50%">'
            '<stop offset="0%" stop-color="#fef9c3"/>'
            '<stop offset="55%" stop-color="#fde68a"/>'
            '<stop offset="100%" stop-color="#f59e0b" stop-opacity="0.15"/>'
            "</radialGradient>")
    if stage == 0:
        return (
            f"<defs>{grad}</defs>"
            '<path d="M60,32 C46,32 38,52 38,70 a22,24 0 0,0 44,0'
            f' C82,52 74,32 60,32 Z" fill="url(#{g})"/>'
            '<circle cx="70" cy="56" r="3.4" fill="#fde68a" opacity="0.9"/>'
            + _face(60, 62, 4, mood))
    parts = [f"<defs>{grad}{lamp}</defs>"]
    # kelp mantle fronds
    fronds = [("M58,68 C48,70 44,78 34,78", "#34d399"),
              ("M62,68 C72,70 76,78 86,78", "#6ee7b7")]
    if stage >= 2:
        fronds += [("M57,80 C48,86 44,92 36,96", "#10b981"),
                   ("M63,80 C72,86 76,92 84,96", "#059669")]
    if stage >= 3:
        fronds += [("M56,88 C50,94 48,98 42,102", "#6ee7b7"),
                   ("M64,88 C70,94 72,98 78,102", "#10b981")]
    for d, c in fronds:
        parts.append(f'<path d="{d}" stroke="{c}" stroke-width="5" fill="none"'
                     ' stroke-linecap="round" opacity="0.9"/>')
    # the warden's lantern, held high
    parts.append('<path d="M78,52 C84,44 88,38 88,32" stroke="#065f46"'
                 ' stroke-width="3" fill="none" stroke-linecap="round"/>')
    parts.append(f'<circle cx="88" cy="28" r="10" fill="url(#{lg})"/>')
    parts.append('<circle cx="88" cy="28" r="4.5" fill="#fef3c7"'
                 ' stroke="#b45309" stroke-width="1.2"/>')
    parts.append('<path d="M84,22 h8" stroke="#92400e" stroke-width="1.6"'
                 ' stroke-linecap="round"/>')
    # round warden body
    parts.append(f'<circle cx="60" cy="62" r="21" fill="url(#{g})"'
                 ' stroke="#a7f3d0" stroke-width="1.5"/>')
    parts.append('<ellipse cx="52" cy="54" rx="6" ry="4" fill="#fff"'
                 ' opacity="0.6" transform="rotate(-20 52 54)"/>')
    if stage >= 3:
        # fireflies in the kelp
        parts.append('<circle cx="36" cy="52" r="1.8" fill="#fef9c3"'
                     ' opacity="0.95"/>')
        parts.append('<circle cx="92" cy="66" r="1.6" fill="#fef9c3"'
                     ' opacity="0.9"/>')
        parts.append('<circle cx="70" cy="94" r="1.8" fill="#fef9c3"'
                     ' opacity="0.9"/>')
    parts.append(_face(60, 60, 4.2, mood))
    return "".join(parts)


_ART = {
    "squiddy": _art_squiddy,
    "puffish": _art_puffish,
    "crownjelly": _art_crownjelly,
    "abyssal": _art_abyssal,
    "frostfin": _art_frostfin,
    "kelpwarden": _art_kelpwarden,
    "driplet": _art_driplet,
    "bloop": _art_bloop,
    "koi": _art_koi,
    "pearly": _art_pearly,
    "kelpy": _art_kelpy,
    "surfpup": _art_surfpup,
    "bubblepup": _art_bubblepup,
    "sealpup": _art_sealpup,
    "jellypup": _art_jellypup,
    "gilt": _art_gilt,
    "tidehound": _art_tidehound,
    "reefkeeper": _art_reefkeeper,
    "zorb": _art_zorb,
}

_STAGE_SCALE = [0.62, 0.78, 0.9, 1.0, 1.05]


# --- shop accessory overlays -------------------------------------------------
# Drawn in the same 120-space as the pet art, inside the stage-scale group
# so they ride along with the body. Generic anchors (they sit fine on all
# species); the head slot holds one item at a time (latest purchase wins).
def _star_points(cx, cy, r):
    import math
    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        rr = r if i % 2 == 0 else r * 0.42
        pts.append(f"{cx + rr * math.cos(ang):.1f},{cy + rr * math.sin(ang):.1f}")
    return " ".join(pts)


def _acc_sailor_hat():
    return (
        '<g transform="translate(60 30) rotate(-10)">'
        '<ellipse cx="0" cy="9" rx="17" ry="4.5" fill="#f8fafc"'
        ' stroke="#cbd5e1" stroke-width="1"/>'
        '<path d="M-12,9 C-12,-3 -6,-10 0,-10 C6,-10 12,-3 12,9 Z"'
        ' fill="#f8fafc" stroke="#cbd5e1" stroke-width="1"/>'
        '<path d="M-12,4 C-6,6 6,6 12,4 L12,9 C6,11 -6,11 -12,9 Z"'
        ' fill="#1e3a8a"/>'
        '<circle cx="0" cy="-3" r="2.5" fill="#fbbf24" stroke="#b45309"'
        ' stroke-width="0.8"/></g>')


def _acc_star_shades():
    s1 = _star_points(46, 62, 11)
    s2 = _star_points(74, 62, 11)
    return (
        f'<polygon points="{s1}" fill="#0b3b5c" opacity="0.92"/>'
        f'<polygon points="{s2}" fill="#0b3b5c" opacity="0.92"/>'
        '<circle cx="43" cy="59" r="2.5" fill="#fff" opacity="0.7"/>'
        '<circle cx="71" cy="59" r="2.5" fill="#fff" opacity="0.7"/>'
        '<path d="M57,62 Q60,60 63,62" stroke="#0b3b5c" stroke-width="2.5"'
        ' fill="none"/>'
        '<path d="M35,60 L28,56 M85,60 L92,56" stroke="#0b3b5c"'
        ' stroke-width="2.5" stroke-linecap="round"/>')


def _acc_pearl_crown():
    return (
        '<g transform="translate(60 24) rotate(6)">'
        '<path d="M-14,6 L-14,-2 L-7,2 L0,-8 L7,2 L14,-2 L14,6 Z"'
        ' fill="#fbbf24" stroke="#b45309" stroke-width="1"/>'
        '<rect x="-14" y="6" width="28" height="5" rx="2" fill="#f59e0b"'
        ' stroke="#b45309" stroke-width="1"/>'
        '<circle cx="-14" cy="-4" r="2.6" fill="#f8fafc" stroke="#cbd5e1"'
        ' stroke-width="0.8"/>'
        '<circle cx="0" cy="-10" r="3" fill="#f8fafc" stroke="#cbd5e1"'
        ' stroke-width="0.8"/>'
        '<circle cx="14" cy="-4" r="2.6" fill="#f8fafc" stroke="#cbd5e1"'
        ' stroke-width="0.8"/></g>')


_ACC_OVERLAY = {
    "acc:sailor_hat": _acc_sailor_hat,
    "acc:star_shades": _acc_star_shades,
    "acc:pearl_crown": _acc_pearl_crown,
}


def _celebrate_aura():
    """Gold stage-up aura: renders for 24h after an evolution."""
    g = _gid("evo")
    return (
        f'<defs><radialGradient id="{g}" cx="50%" cy="50%" r="50%">'
        '<stop offset="0%" stop-color="#fef9c3" stop-opacity="0.75"/>'
        '<stop offset="60%" stop-color="#fde68a" stop-opacity="0.25"/>'
        '<stop offset="100%" stop-color="#fbbf24" stop-opacity="0"/>'
        "</radialGradient></defs>"
        f'<circle cx="60" cy="62" r="54" fill="url(#{g})"/>' + _sparkles())


def pet_svg(species, stage_idx, mood, size=120, accessories=(), wardrobe=(),
            celebrate=False, trait=None, sniffles=False, wisp=False,
            animate=True):
    """Full standalone SVG for a pet. Pure inline vectors, no assets.
    accessories: owned+equipped shop item keys, drawn as overlays.
    wardrobe: equipped wardrobe item ids, layered by slot (backgrounds
    behind everything, trails behind the body, the rest ride the body).
    celebrate: gold stage-up aura (24h after an evolution).
    trait: personality trait — shifts the idle animation style.
    sniffles: cute-sneezy overlay (mild illness, adventure framing).
    wisp: render the Echo Fusion wisp orbiting the pet.
    animate: SMIL idle motion (bob/breathe) + mood behaviors. Purely
    additive — the static art underneath is untouched."""
    if species not in _ART:
        species = "driplet"
    stage_idx = max(0, min(len(PET_STAGES) - 1, stage_idx))
    glow = (mood == "overjoyed")  # hidden comeback reaction: happy face + sparkles
    if mood == "grumpy":
        mood = "restless"  # legacy mood name, retired 2026-09-19
    if mood not in ("happy", "content", "sleepy", "peckish", "restless"):
        mood = "happy" if glow else "content"
    inner = _ART[species](stage_idx, mood if mood != "restless" else "content")
    overlays = "".join(_ACC_OVERLAY[a]() for a in (accessories or ())
                       if a in _ACC_OVERLAY)
    wb = [(w, WARDROBE_CATALOG[w]["slot"]) for w in (wardrobe or ())
          if w in WARDROBE_CATALOG and w in _WARDROBE_OVERLAY]
    bg_art = "".join(_WARDROBE_OVERLAY[w]() for w, s in wb
                     if s == "background")
    trail_art = "".join(_WARDROBE_OVERLAY[w]() for w, s in wb if s == "trail")
    top_art = "".join(_WARDROBE_OVERLAY[w]() for w, s in wb
                      if s in ("hat", "eyes", "body"))
    s = _STAGE_SCALE[stage_idx]
    aura = (_aura() + _sparkles()) if (stage_idx == 4 or glow) else ""
    if celebrate:
        aura = _celebrate_aura() + aura
    label = (f"{PET_SPECIES[species]['name']} — "
             f"{PET_STAGES[stage_idx][1]}, {mood}")
    body = (f'<g transform="translate(60 62) scale({s}) translate(-60 -62)">'
            f"{inner}{overlays}{top_art}</g>")
    if animate:
        body = _anim_wrap(body, mood, trait, stage_idx)
    mood_fx = _mood_overlay(mood, sniffles) if animate else ""
    wisp_art = _wisp_orbit() if (wisp and animate) else ""
    return (
        f'<svg viewBox="0 0 120 120" width="{size}" height="{size}" role="img"'
        f' aria-label="{label}" xmlns="http://www.w3.org/2000/svg">'
        f"<title>{label}</title>"
        f"{bg_art}{aura}{_shadow()}{trail_art}"
        f"{body}{mood_fx}{wisp_art}</svg>")


def _anim_wrap(body, mood, trait, stage_idx):
    """Wrap the pet body in SMIL idle motion. Trait shifts the style:
    playful bounces higher/faster, calm sways slow and gentle,
    mischievous wiggles, gentle breathes easy. Eggs rock softly."""
    trait = trait if trait in PET_TRAITS else "calm"
    if stage_idx == 0:
        # Egg: a soft rock, side to side.
        motion = ('<animateTransform attributeName="transform" type="rotate"'
                  ' values="-6 60 62; 6 60 62; -6 60 62" dur="4s"'
                  ' repeatCount="indefinite"/>')
    elif trait == "playful":
        motion = ('<animateTransform attributeName="transform" type="translate"'
                  ' values="0 0; 0 -7; 0 0" keyTimes="0;0.5;1" dur="2.2s"'
                  ' repeatCount="indefinite"/>')
    elif trait == "mischievous":
        motion = ('<animateTransform attributeName="transform" type="translate"'
                  ' values="0 0; 3 -4; -3 0; 0 0" keyTimes="0;0.33;0.66;1"'
                  ' dur="2.8s" repeatCount="indefinite"/>')
    elif trait == "gentle":
        motion = ('<animateTransform attributeName="transform" type="translate"'
                  ' values="0 0; 0 -3; 0 0" keyTimes="0;0.5;1" dur="4.2s"'
                  ' repeatCount="indefinite"/>')
    else:  # calm
        motion = ('<animateTransform attributeName="transform" type="translate"'
                  ' values="0 0; 0 -4; 0 0" keyTimes="0;0.5;1" dur="3.6s"'
                  ' repeatCount="indefinite"/>')
    breathe = ('<animateTransform attributeName="transform" type="scale"'
               ' values="1 1; 1.03 1.03; 1 1" keyTimes="0;0.5;1" dur="3s"'
               ' additive="sum" repeatCount="indefinite"/>')
    return f"<g>{motion}<g>{breathe}{body}</g></g>"


def _mood_overlay(mood, sniffles):
    """Mood behaviors, drawn over the pet. Immersive but never sad:
    sleepy gets floating z's, happy gets pulsing sparkles, peckish
    daydreams about snacks (thought bubble), restless gets droopy
    antennae (sitting out the fun, not suffering), sniffles gets
    cute sneeze-puffs."""
    if mood == "sleepy":
        zs = ""
        for i, (x, d) in enumerate([(88, "0s"), (96, "0.8s"), (104, "1.6s")]):
            zs += (
                f'<text x="{x}" y="34" font-size="13" fill="#9db8dd"'
                f' opacity="0.9">z<animate attributeName="y" values="34;14"'
                f' dur="2.4s" begin="{d}" repeatCount="indefinite"/>'
                f'<animate attributeName="opacity" values="0.9;0" dur="2.4s"'
                f' begin="{d}" repeatCount="indefinite"/></text>')
        base = zs
    elif mood == "happy":
        base = ('<g opacity="0.85"><animate attributeName="opacity"'
                ' values="0.85;0.4;0.85" dur="2s" repeatCount="indefinite"/>'
                f"{_sparkles()}</g>")
    elif mood == "peckish":
        # Daydreaming about snacks: a thought bubble with a little fish.
        base = (
            '<g opacity="0.95">'
            '<circle cx="92" cy="30" r="3" fill="#cfe6f7"/>'
            '<circle cx="99" cy="22" r="4.5" fill="#cfe6f7"/>'
            '<ellipse cx="110" cy="14" rx="10" ry="8" fill="#e8f4fd"'
            ' stroke="#9db8dd" stroke-width="1"/>'
            '<ellipse cx="110" cy="14" rx="4" ry="2.6" fill="#f5a623"/>'
            '<polygon points="106,14 102,11 102,17" fill="#f5a623"/>'
            '<animateTransform attributeName="transform" type="translate"'
            ' values="0 0; 0 -3; 0 0" dur="3s" repeatCount="indefinite"/>'
            "</g>")
    elif mood == "restless":
        # Droopy antennae: sitting out the fun, slightly bored — never sad.
        base = (
            '<g stroke="#7fa8c9" stroke-width="2.5" fill="none"'
            ' stroke-linecap="round" opacity="0.9">'
            '<path d="M52 30 Q46 18 38 20"/>'
            '<path d="M68 30 Q74 18 82 20"/>'
            '<circle cx="38" cy="20" r="3.5" fill="#a8cbe8" stroke="none"/>'
            '<circle cx="82" cy="20" r="3.5" fill="#a8cbe8" stroke="none"/>'
            '<animateTransform attributeName="transform" type="translate"'
            ' values="0 0; 0 2; 0 0" dur="2.6s" repeatCount="indefinite"/>'
            "</g>")
    else:
        base = ""
    # Sniffles layer over any mood — a sneezy pet is still itself.
    if sniffles:
        base += _sniffle_puffs()
    return base


def _sniffle_puffs():
    """Cute sneeze-puffs for sea sniffles — ach-oo, not oh-no."""
    puffs = ""
    for i, (dx, d) in enumerate([(0, "0s"), (7, "0.5s"), (-7, "1s")]):
        puffs += (
            f'<circle cx="{100 + dx}" cy="52" r="3" fill="#dff0fb"'
            f' opacity="0.7"><animate attributeName="r" values="3;5.5;3"'
            f' dur="1.8s" begin="{d}" repeatCount="indefinite"/>'
            f'<animate attributeName="opacity" values="0.7;0.2;0.7"'
            f' dur="1.8s" begin="{d}" repeatCount="indefinite"/></circle>')
    return f"<g>{puffs}</g>"


def _wisp_orbit():
    """Echo Fusion wisp: a tiny glowing companion orbiting the pet."""
    return (
        '<defs><radialGradient id="wispglow" cx="50%" cy="50%" r="50%">'
        '<stop offset="0%" stop-color="#fff8d6"/>'
        '<stop offset="60%" stop-color="#ffe98a"/>'
        '<stop offset="100%" stop-color="#ffe98a" stop-opacity="0"/>'
        "</radialGradient></defs>"
        "<g>"
        '<animateTransform attributeName="transform" type="rotate"'
        ' from="0 60 62" to="360 60 62" dur="7s" repeatCount="indefinite"/>'
        '<circle cx="98" cy="62" r="11" fill="url(#wispglow)" opacity="0.8"/>'
        '<circle cx="98" cy="62" r="4.5" fill="#fff3b0" stroke="#e8c95a"'
        ' stroke-width="1"/>'
        '<circle cx="96.5" cy="60.5" r="1.4" fill="#ffffff"/>'
        "</g>")

# ===========================================================================
# WARDROBE — Neopets push, part A
# Cosmetic items in five slots (hat, eyes, body, background, trail), layered
# onto the pet portrait by pet_svg. Nothing here is sold for money — ever.
# Items are earned three ways:
#   shop:<price>        buy with spendable Signal (ledger-recorded, no USD)
#   care_streak:<days>  auto-earned by feeding <days> days running
#   stage:<idx>        auto-earned when the pet reaches that stage
#   game:<game>        reserved for future Tidepal games
#   seasonal:<season>  earnable only while that season is live
#   event:<event>      one-time event grants
# One item equipped per slot. equip_item refuses unearned items.
# ===========================================================================

WARDROBE_SLOTS = ["hat", "eyes", "body", "background", "trail"]

WARDROBE_CATALOG = {
    # --- hats ------------------------------------------------------------
    "party_hat": {
        "name": "Party Hat", "slot": "hat", "art_kind": "overlay",
        "description": ("A striped cone of pure celebration. Worn exactly "
                        "once a year, remembered forever."),
        "unlock": "event:demo_night",
    },
    "cozy_beanie": {
        "name": "Cozy Beanie", "slot": "hat", "art_kind": "overlay",
        "description": "A soft-knit beanie for chilly signal nights.",
        "unlock": "shop:30",
    },
    "seaweed_crown": {
        "name": "Seaweed Crown", "slot": "hat", "art_kind": "overlay",
        "description": ("A circlet of braided kelp, awarded to the most "
                        "devoted Tidepal keepers."),
        "unlock": "care_streak:7",
    },
    "fishbowl_helmet": {
        "name": "Fishbowl Helmet", "slot": "hat", "art_kind": "overlay",
        "description": ("A tiny glass dome of premium lagoon water. For "
                        "Tidepals who travel in style."),
        "unlock": "shop:45",
    },
    # --- eyes ------------------------------------------------------------
    "heart_goggles": {
        "name": "Heart Goggles", "slot": "eyes", "art_kind": "overlay",
        "description": "See the whole town through heart-shaped lenses.",
        "unlock": "shop:35",
    },
    "bubble_lenses": {
        "name": "Bubble Lenses", "slot": "eyes", "art_kind": "overlay",
        "description": ("Round bubble spectacles, polished by the deep "
                        "square's finest optician."),
        "unlock": "shop:25",
    },
    "lantern_goggles": {
        "name": "Lantern Goggles", "slot": "eyes", "art_kind": "overlay",
        "description": ("Warm-glowing goggles for exploring the midnight "
                        "zone of the episode archive."),
        "unlock": "game:tide_toss",
    },
    # --- body ------------------------------------------------------------
    "kelp_scarf": {
        "name": "Kelp Scarf", "slot": "body", "art_kind": "overlay",
        "description": "A hand-knotted scarf from the town's kelp garden.",
        "unlock": "shop:20",
    },
    "pearl_necklace": {
        "name": "Pearl Necklace", "slot": "body", "art_kind": "overlay",
        "description": ("Town-gossip pearls, strung by Pearly herself. "
                        "Each one is a compliment someone meant."),
        "unlock": "shop:40",
    },
    "coral_cape": {
        "name": "Coral Cape", "slot": "body", "art_kind": "overlay",
        "description": ("A sweeping cape of living coral — the mark of a "
                        "Tidepal that grew up strong."),
        "unlock": "stage:3",
    },
    "barnacle_bowtie": {
        "name": "Barnacle Bowtie", "slot": "body", "art_kind": "overlay",
        "description": "Dapper. Crusty. Somehow both.",
        "unlock": "shop:25",
    },
    # --- backgrounds -----------------------------------------------------
    "coral_garden": {
        "name": "Coral Garden", "slot": "background", "art_kind": "overlay",
        "description": "Your Tidepal's portrait, replanted in the reef.",
        "unlock": "shop:50",
    },
    "aurora_reef": {
        "name": "Aurora Reef", "slot": "background", "art_kind": "overlay",
        "description": ("The deep-square sky, lit for a Radiant Tidepal. "
                        "Only the brightest earn this view."),
        "unlock": "stage:4",
    },
    "moonlit_lagoon": {
        "name": "Moonlit Lagoon", "slot": "background", "art_kind": "overlay",
        "description": ("A still lagoon under the frost-festival moon. "
                        "Only available in winter."),
        "unlock": "seasonal:winter26",
    },
    # --- trails ----------------------------------------------------------
    "bubble_trail": {
        "name": "Bubble Trail", "slot": "trail", "art_kind": "overlay",
        "description": "Every entrance deserves a trail of bubbles.",
        "unlock": "game:tide_toss",
    },
    "sparkle_trail": {
        "name": "Sparkle Trail", "slot": "trail", "art_kind": "overlay",
        "description": "Leave a little stardust wherever you drift.",
        "unlock": "shop:55",
    },
    "sand_swirl": {
        "name": "Sand Swirl", "slot": "trail", "art_kind": "overlay",
        "description": "A lazy swirl of lagoon sand kicked up in your wake.",
        "unlock": "shop:15",
    },
}


# --- wardrobe art: same 120-space fragments as the _acc_* shop overlays -----
def _wardrobe_party_hat():
    return (
        '<g transform="translate(60 26) rotate(8)">'
        '<path d="M-11,4 L0,-20 L11,4 Z" fill="#f472b6"'
        ' stroke="#be185d" stroke-width="1"/>'
        '<path d="M-7.3,-4.5 L7.3,-4.5 L4.8,-10.5 L-4.8,-10.5 Z"'
        ' fill="#fde68a" opacity="0.92"/>'
        '<ellipse cx="0" cy="5.5" rx="13" ry="3" fill="#fbcfe3"'
        ' stroke="#be185d" stroke-width="1"/>'
        '<circle cx="0" cy="-21" r="3" fill="#fef3c7" stroke="#f59e0b"'
        ' stroke-width="1"/></g>'
        '<circle cx="30" cy="40" r="1.6" fill="#f472b6"/>'
        '<circle cx="90" cy="36" r="1.6" fill="#38bdf8"/>'
        '<circle cx="84" cy="98" r="1.6" fill="#fde68a"/>')


def _wardrobe_cozy_beanie():
    return (
        '<g transform="translate(60 28) rotate(-6)">'
        '<path d="M-16,6 C-16,-6 -9,-12 0,-12 C9,-12 16,-6 16,6 Z"'
        ' fill="#38bdf8" stroke="#0369a1" stroke-width="1"/>'
        '<path d="M-12,-2 C-6,-4 6,-4 12,-2" stroke="#bae6fd"'
        ' stroke-width="1.4" fill="none" opacity="0.8"/>'
        '<rect x="-17" y="4" width="34" height="7" rx="3.5" fill="#0ea5e9"'
        ' stroke="#0369a1" stroke-width="1"/>'
        '<circle cx="0" cy="-14" r="4" fill="#f0f9ff" stroke="#0369a1"'
        ' stroke-width="1"/></g>')


def _wardrobe_seaweed_crown():
    fronds = "".join(
        f'<path d="M{-14 + i * 7},{6 + (i % 2) * 2}'
        f' q{3.5},{-10 - (i % 3) * 2} {7},{-12 - (i % 2) * 3}"'
        ' stroke="#059669" stroke-width="3" fill="none"'
        ' stroke-linecap="round"/>'
        for i in range(5))
    leaves = "".join(
        f'<ellipse cx="{-14 + i * 7 + 5}" cy="{-6 - (i % 2) * 3}" rx="3"'
        f' ry="1.6" fill="#34d399" transform="rotate(35 {-14 + i * 7 + 5}'
        f' {-6 - (i % 2) * 3})"/>'
        for i in range(5))
    return (f'<g transform="translate(60 30)">{fronds}{leaves}'
            '<path d="M-17,7 Q0,12 17,7" stroke="#047857" stroke-width="2.5"'
            ' fill="none" stroke-linecap="round"/></g>')


def _wardrobe_fishbowl_helmet():
    g = _gid("fb")
    return (
        f'<defs><radialGradient id="{g}" cx="40%" cy="30%" r="80%">'
        '<stop offset="0%" stop-color="#ffffff" stop-opacity="0.55"/>'
        '<stop offset="70%" stop-color="#bae6fd" stop-opacity="0.28"/>'
        '<stop offset="100%" stop-color="#7dd3fc" stop-opacity="0.45"/>'
        "</radialGradient></defs>"
        f'<ellipse cx="60" cy="52" rx="32" ry="28" fill="url(#{g})"'
        ' stroke="#e0f2fe" stroke-width="2"/>'
        '<ellipse cx="48" cy="42" rx="9" ry="5" fill="#fff" opacity="0.6"'
        ' transform="rotate(-25 48 42)"/>'
        '<circle cx="76" cy="62" r="2.4" fill="#bae6fd" opacity="0.8"/>'
        '<circle cx="82" cy="52" r="1.7" fill="#bae6fd" opacity="0.7"/>')


def _wardrobe_heart_goggles():
    heart = ("M0,3 C-5.5,-2.5 -11,1 0,8.5 C11,1 5.5,-2.5 0,3 Z")
    return (
        f'<path d="{heart}" transform="translate(46 58)" fill="#f472b6"'
        ' stroke="#be185d" stroke-width="1.2"/>'
        f'<path d="{heart}" transform="translate(74 58)" fill="#f472b6"'
        ' stroke="#be185d" stroke-width="1.2"/>'
        '<circle cx="44" cy="60" r="1.8" fill="#fff" opacity="0.8"/>'
        '<circle cx="72" cy="60" r="1.8" fill="#fff" opacity="0.8"/>'
        '<path d="M55,62 Q60,60 65,62" stroke="#be185d" stroke-width="2"'
        ' fill="none"/>'
        '<path d="M37,60 L29,56 M83,60 L91,56" stroke="#be185d"'
        ' stroke-width="2" stroke-linecap="round"/>')


def _wardrobe_bubble_lenses():
    return (
        '<circle cx="46" cy="62" r="11" fill="#e0f2fe" opacity="0.5"'
        ' stroke="#0284c7" stroke-width="1.6"/>'
        '<circle cx="74" cy="62" r="11" fill="#e0f2fe" opacity="0.5"'
        ' stroke="#0284c7" stroke-width="1.6"/>'
        '<circle cx="42.5" cy="58.5" r="3" fill="#fff" opacity="0.85"/>'
        '<circle cx="70.5" cy="58.5" r="3" fill="#fff" opacity="0.85"/>'
        '<path d="M57,62 Q60,60.5 63,62" stroke="#0284c7" stroke-width="1.8"'
        ' fill="none"/>'
        '<path d="M35,60 L28,56 M85,60 L92,56" stroke="#0284c7"'
        ' stroke-width="1.8" stroke-linecap="round"/>')


def _wardrobe_lantern_goggles():
    g = _gid("lg")
    return (
        f'<defs><radialGradient id="{g}" cx="50%" cy="50%" r="50%">'
        '<stop offset="0%" stop-color="#fef9c3"/>'
        '<stop offset="100%" stop-color="#f59e0b" stop-opacity="0.1"/>'
        "</radialGradient></defs>"
        f'<circle cx="46" cy="62" r="14" fill="url(#{g})"/>'
        f'<circle cx="74" cy="62" r="14" fill="url(#{g})"/>'
        '<circle cx="46" cy="62" r="10" fill="#fde68a" opacity="0.85"'
        ' stroke="#b45309" stroke-width="1.6"/>'
        '<circle cx="74" cy="62" r="10" fill="#fde68a" opacity="0.85"'
        ' stroke="#b45309" stroke-width="1.6"/>'
        '<circle cx="43" cy="59" r="2.4" fill="#fff" opacity="0.9"/>'
        '<circle cx="71" cy="59" r="2.4" fill="#fff" opacity="0.9"/>'
        '<path d="M56,62 Q60,60 64,62" stroke="#92400e" stroke-width="2"'
        ' fill="none"/>')


def _wardrobe_kelp_scarf():
    return (
        '<path d="M38,84 Q48,78 60,82 Q72,86 82,80" stroke="#059669"'
        ' stroke-width="7" fill="none" stroke-linecap="round"/>'
        '<path d="M74,84 q4,8 -2,14 q-4,4 -2,9" stroke="#10b981"'
        ' stroke-width="5" fill="none" stroke-linecap="round"/>'
        '<path d="M46,85 q-3,8 3,13" stroke="#34d399" stroke-width="4.5"'
        ' fill="none" stroke-linecap="round"/>'
        '<circle cx="70" cy="99" r="2" fill="#6ee7b7" opacity="0.8"/>'
        '<circle cx="50" cy="100" r="1.7" fill="#6ee7b7" opacity="0.7"/>')


def _wardrobe_pearl_necklace():
    pearls = "".join(
        f'<circle cx="{44 + i * 5.3:.1f}" cy="{82 + abs(i - 3) * 1.6:.1f}"'
        f' r="3.1" fill="#f5f3ff" stroke="#a78bfa" stroke-width="0.9"/>'
        for i in range(7))
    return (
        '<path d="M40,80 Q60,92 80,80" stroke="#a78bfa" stroke-width="1.2"'
        ' fill="none" opacity="0.7"/>' + pearls +
        '<circle cx="60" cy="88.5" r="1.2" fill="#fff" opacity="0.9"/>')


def _wardrobe_coral_cape():
    return (
        '<path d="M38,60 C26,70 24,88 30,102 C36,94 38,80 42,70 Z"'
        ' fill="#fb7185" stroke="#be123c" stroke-width="1" opacity="0.95"/>'
        '<path d="M82,60 C94,70 96,88 90,102 C84,94 82,80 78,70 Z"'
        ' fill="#fb7185" stroke="#be123c" stroke-width="1" opacity="0.95"/>'
        '<circle cx="33" cy="80" r="2" fill="#fecdd3" opacity="0.8"/>'
        '<circle cx="87" cy="84" r="2" fill="#fecdd3" opacity="0.8"/>'
        '<path d="M40,62 Q60,70 80,62" stroke="#be123c" stroke-width="2.5"'
        ' fill="none" stroke-linecap="round"/>')


def _wardrobe_barnacle_bowtie():
    return (
        '<g transform="translate(60 86)">'
        '<path d="M-2,0 L-14,-7 L-14,7 Z" fill="#0ea5e9" stroke="#0369a1"'
        ' stroke-width="1"/>'
        '<path d="M2,0 L14,-7 L14,7 Z" fill="#0ea5e9" stroke="#0369a1"'
        ' stroke-width="1"/>'
        '<circle cx="-8" cy="-2" r="1.6" fill="#e0f2fe" opacity="0.9"/>'
        '<circle cx="8" cy="2" r="1.6" fill="#e0f2fe" opacity="0.9"/>'
        '<rect x="-3.5" y="-4" width="7" height="8" rx="2" fill="#0284c7"'
        ' stroke="#0c4a6e" stroke-width="1"/></g>')


def _wardrobe_coral_garden():
    return (
        '<ellipse cx="60" cy="112" rx="52" ry="10" fill="#fde68a"'
        ' opacity="0.35"/>'
        '<path d="M16,110 q2,-14 -4,-22 q8,2 6,10 q6,-2 4,-12 q8,6 2,14'
        ' q-2,8 -8,10 Z" fill="#fb7185" opacity="0.85"/>'
        '<path d="M104,110 q-2,-14 4,-22 q-8,2 -6,10 q-6,-2 -4,-12 q-8,6 -2,14'
        ' q2,8 8,10 Z" fill="#f472b6" opacity="0.85"/>'
        '<circle cx="26" cy="96" r="2.4" fill="#fecdd3" opacity="0.8"/>'
        '<circle cx="94" cy="92" r="2" fill="#fbcfe3" opacity="0.8"/>'
        '<circle cx="60" cy="100" r="1.6" fill="#fff" opacity="0.5"/>')


def _wardrobe_aurora_reef():
    g = _gid("ar")
    return (
        f'<defs><linearGradient id="{g}" x1="0" y1="0" x2="1" y2="0">'
        '<stop offset="0%" stop-color="#67e8f9" stop-opacity="0"/>'
        '<stop offset="50%" stop-color="#67e8f9" stop-opacity="0.4"/>'
        '<stop offset="100%" stop-color="#a78bfa" stop-opacity="0"/>'
        "</linearGradient></defs>"
        f'<path d="M0,34 C30,18 60,44 90,26 C100,20 112,24 120,20 L120,0'
        f' L0,0 Z" fill="url(#{g})"/>'
        '<path d="M0,52 C36,36 66,60 120,42 L120,30 C80,44 40,30 0,42 Z"'
        ' fill="#a78bfa" opacity="0.14"/>'
        '<circle cx="24" cy="18" r="1.6" fill="#fff" opacity="0.9"/>'
        '<circle cx="70" cy="12" r="1.3" fill="#fff" opacity="0.8"/>'
        '<circle cx="102" cy="16" r="1.8" fill="#fff" opacity="0.9"/>')


def _wardrobe_moonlit_lagoon():
    return (
        '<circle cx="94" cy="24" r="12" fill="#fefce8" opacity="0.95"/>'
        '<circle cx="90" cy="21" r="10" fill="#0b3b5c" opacity="0.12"/>'
        '<circle cx="22" cy="20" r="1.5" fill="#fff" opacity="0.9"/>'
        '<circle cx="44" cy="12" r="1.2" fill="#fff" opacity="0.8"/>'
        '<circle cx="64" cy="22" r="1.4" fill="#fff" opacity="0.85"/>'
        '<circle cx="14" cy="44" r="1.2" fill="#fff" opacity="0.7"/>'
        '<path d="M20,104 q10,-4 20,0 q10,4 20,0 q10,-4 20,0 q10,4 20,0"'
        ' stroke="#7dd3fc" stroke-width="2" fill="none" opacity="0.5"'
        ' stroke-linecap="round"/>'
        '<path d="M30,110 q10,-4 20,0 q10,4 20,0 q10,-4 20,0"'
        ' stroke="#bae6fd" stroke-width="1.6" fill="none" opacity="0.4"'
        ' stroke-linecap="round"/>')


def _wardrobe_bubble_trail():
    return "".join(
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="#bae6fd"'
        f' stroke="#fff" stroke-width="0.8" opacity="{op}"/>'
        for cx, cy, r, op in [(30, 96, 4, 0.8), (22, 88, 3, 0.7),
                              (16, 78, 2.4, 0.6), (92, 94, 3.4, 0.75),
                              (100, 84, 2.4, 0.6), (105, 74, 1.8, 0.5)])


def _wardrobe_sparkle_trail():
    star = ("M0,-6 C1,-2 2,-1 6,0 C2,1 1,2 0,6 C-1,2 -2,1 -6,0"
            " C-2,-1 -1,-2 0,-6 Z")
    return "".join(
        f'<path d="{star}" transform="translate({x} {y}) scale({s})"'
        f' fill="{c}" opacity="0.95"/>'
        for x, y, s, c in [(28, 92, 1.0, "#fde68a"), (18, 80, 0.7, "#fef9c3"),
                           (94, 90, 0.9, "#fde68a"), (104, 78, 0.65, "#fff"),
                           (60, 108, 0.8, "#fef9c3")])


def _wardrobe_sand_swirl():
    return (
        '<path d="M24,104 q14,-10 30,-6 q16,4 30,-4" stroke="#fde68a"'
        ' stroke-width="3" fill="none" opacity="0.5" stroke-linecap="round"/>'
        '<path d="M30,110 q12,-8 26,-5 q14,3 26,-3" stroke="#fcd34d"'
        ' stroke-width="2.2" fill="none" opacity="0.4" stroke-linecap="round"/>'
        '<circle cx="44" cy="100" r="1.8" fill="#fde68a" opacity="0.7"/>'
        '<circle cx="76" cy="104" r="1.5" fill="#fcd34d" opacity="0.6"/>')


_WARDROBE_OVERLAY = {
    "party_hat": _wardrobe_party_hat,
    "cozy_beanie": _wardrobe_cozy_beanie,
    "seaweed_crown": _wardrobe_seaweed_crown,
    "fishbowl_helmet": _wardrobe_fishbowl_helmet,
    "heart_goggles": _wardrobe_heart_goggles,
    "bubble_lenses": _wardrobe_bubble_lenses,
    "lantern_goggles": _wardrobe_lantern_goggles,
    "kelp_scarf": _wardrobe_kelp_scarf,
    "pearl_necklace": _wardrobe_pearl_necklace,
    "coral_cape": _wardrobe_coral_cape,
    "barnacle_bowtie": _wardrobe_barnacle_bowtie,
    "coral_garden": _wardrobe_coral_garden,
    "aurora_reef": _wardrobe_aurora_reef,
    "moonlit_lagoon": _wardrobe_moonlit_lagoon,
    "bubble_trail": _wardrobe_bubble_trail,
    "sparkle_trail": _wardrobe_sparkle_trail,
    "sand_swirl": _wardrobe_sand_swirl,
}

# --- wardrobe storage -------------------------------------------------------
WARDROBE_SCHEMA = """
CREATE TABLE IF NOT EXISTS wardrobe_items (
  item_id     TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  slot        TEXT NOT NULL,
  art_kind    TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  unlock      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pet_wardrobe (
  fm_id      TEXT NOT NULL,
  item_id    TEXT NOT NULL,
  equipped   INTEGER NOT NULL DEFAULT 0,
  earned_at  INTEGER NOT NULL,
  PRIMARY KEY (fm_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_pet_wardrobe_fm ON pet_wardrobe(fm_id);
"""


def ensure_wardrobe_schema(db):
    for stmt in WARDROBE_SCHEMA.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            db._exec(stmt)
    # The code catalog is authoritative — seed new items, refresh changed.
    for item_id, spec in WARDROBE_CATALOG.items():
        db._exec(
            "INSERT OR IGNORE INTO wardrobe_items"
            " (item_id, name, slot, art_kind, description, unlock)"
            " VALUES (?,?,?,?,?,?)",
            (item_id, spec["name"], spec["slot"], spec["art_kind"],
             spec["description"], spec["unlock"]))
        db._exec(
            "UPDATE wardrobe_items SET name=?, slot=?, art_kind=?,"
            " description=?, unlock=? WHERE item_id=?",
            (spec["name"], spec["slot"], spec["art_kind"],
             spec["description"], spec["unlock"], item_id))


def _wardrobe_unlock_parts(spec):
    kind, _, param = spec["unlock"].partition(":")
    return kind, param


def wardrobe_unlock_text(spec):
    """Human-readable unlock condition for a catalog spec."""
    kind, param = _wardrobe_unlock_parts(spec)
    if kind == "shop":
        return f"Buy it for {param} Signal"
    if kind == "care_streak":
        return f"Feed your Tidepal {param} days running"
    if kind == "stage":
        idx = int(param)
        sname = PET_STAGES[idx][1] if 0 <= idx < len(PET_STAGES) else "?"
        return f"Grow to the {sname} stage"
    if kind == "game":
        return (f"Earn it in {param.replace('_', ' ')} "
                f"(coming soon to the town square)")
    if kind == "seasonal":
        return f"Available during {param}"
    if kind == "event":
        return f"Earned at {param.replace('_', ' ')}"
    return spec["unlock"]


def wardrobe_catalog(db, fm_id=None):
    """Full wardrobe catalog. With fm_id: annotated owned/equipped."""
    ensure_wardrobe_schema(db)
    owned, equipped = set(), {}
    if fm_id:
        owned = {r["item_id"] for r in db._q(
            "SELECT item_id FROM pet_wardrobe WHERE fm_id=?", (fm_id,))}
        equipped = equipped_wardrobe(db, fm_id)
    out = []
    for item_id, spec in WARDROBE_CATALOG.items():
        entry = {"item_id": item_id, "name": spec["name"],
                 "slot": spec["slot"], "art_kind": spec["art_kind"],
                 "description": spec["description"], "unlock": spec["unlock"],
                 "unlock_text": wardrobe_unlock_text(spec)}
        kind, param = _wardrobe_unlock_parts(spec)
        if kind == "shop":
            entry["price"] = int(param)
        if fm_id:
            entry["owned"] = item_id in owned
            entry["equipped"] = equipped.get(spec["slot"]) == item_id
        out.append(entry)
    return out


_EARN_REASONS = {
    "purchase": "shop",
    "care_streak": "care_streak",
    "stage": "stage",
    "seasonal": "seasonal",
    "event": "event",
    "game": "game",
    "admin": None,  # manual grants only — no route exposes this
}


def earn_item(db, fm_id, item_id, reason):
    """Grant a wardrobe item. The reason must match the item's unlock
    kind (purchase/care_streak/stage/seasonal/event/game), and the
    server-side condition is re-checked — the client never decides.
    Idempotent. Raises ValueError on mismatch, unknown item, or no pet."""
    ensure_wardrobe_schema(db)
    if item_id not in WARDROBE_CATALOG:
        raise ValueError(f"unknown wardrobe item: {item_id}")
    if not get_pet(db, fm_id):
        raise ValueError("no Tidepal adopted yet")
    if reason not in _EARN_REASONS:
        raise ValueError(f"unknown earn reason: {reason}")
    spec = WARDROBE_CATALOG[item_id]
    kind, param = _wardrobe_unlock_parts(spec)
    need = _EARN_REASONS[reason]
    if need is not None and kind != need:
        raise ValueError(
            f"the {spec['name']} is earned via"
            f" {wardrobe_unlock_text(spec).lower()} — not '{reason}'")
    if kind == "care_streak" and feed_streak_days(db, fm_id) < int(param):
        raise ValueError(f"the {spec['name']} needs a {param}-day feeding"
                         f" streak — you're at"
                         f" {feed_streak_days(db, fm_id)}")
    if kind == "stage":
        have = stage_for_points(db.lifetime_points(fm_id))[0]
        if have < int(param):
            raise ValueError(f"the {spec['name']} needs the"
                             f" {PET_STAGES[int(param)][1]} stage")
    if kind == "seasonal" and _current_season() != param:
        raise ValueError(f"the {spec['name']} is only available during"
                         f" {param} (now: {_current_season()})")
    cur = db._exec(
        "INSERT OR IGNORE INTO pet_wardrobe"
        " (fm_id, item_id, equipped, earned_at) VALUES (?,?,0,?)",
        (fm_id, item_id, now()))
    earned = cur.rowcount > 0
    return {"item_id": item_id, "name": spec["name"], "earned": earned,
            "already_owned": not earned}


def equip_item(db, fm_id, item_id=None, slot=None):
    """Equip an owned wardrobe item (one equipped per slot), or unequip a
    slot with item_id=None. Unlock gating: unearned items are refused.
    Returns the new {slot: item_id} equipped map."""
    ensure_wardrobe_schema(db)
    if not get_pet(db, fm_id):
        raise ValueError("no Tidepal adopted yet")
    if item_id is None:
        if slot not in WARDROBE_SLOTS:
            raise ValueError(f"unknown slot: {slot}")
    else:
        if item_id not in WARDROBE_CATALOG:
            raise ValueError(f"unknown wardrobe item: {item_id}")
        owned = db._one("SELECT item_id FROM pet_wardrobe"
                        " WHERE fm_id=? AND item_id=?", (fm_id, item_id))
        if not owned:
            raise ValueError(
                f"you haven't earned the"
                f" {WARDROBE_CATALOG[item_id]['name']} yet —"
                f" {wardrobe_unlock_text(WARDROBE_CATALOG[item_id])}")
        slot = WARDROBE_CATALOG[item_id]["slot"]
    # One equipped per slot: clear the slot, then set the new item.
    db._exec(
        "UPDATE pet_wardrobe SET equipped=0 WHERE fm_id=? AND item_id IN"
        " (SELECT item_id FROM wardrobe_items WHERE slot=?)",
        (fm_id, slot))
    if item_id is not None:
        db._exec("UPDATE pet_wardrobe SET equipped=1"
                 " WHERE fm_id=? AND item_id=?", (fm_id, item_id))
    return equipped_wardrobe(db, fm_id)


def equipped_wardrobe(db, fm_id):
    """{slot: item_id} for everything currently worn."""
    ensure_wardrobe_schema(db)
    rows = db._q(
        "SELECT w.item_id, i.slot FROM pet_wardrobe w"
        " JOIN wardrobe_items i ON i.item_id = w.item_id"
        " WHERE w.fm_id=? AND w.equipped=1", (fm_id,))
    return {r["slot"]: r["item_id"] for r in rows}


def buy_wardrobe_item(db, fm_id, item_id, idempotency_key=None):
    """Buy a shop-unlock wardrobe item with spendable Signal. Mirrors the
    Signal Shop's ledger discipline (shop.buy): signed routes only,
    server-side balance math, idempotent via UNIQUE(fm_id, ref_id).
    Lifetime Signal NEVER decreases — the charge lands in shop_purchases.
    No USD, no money, anywhere. Bought items auto-equip into their slot."""
    ensure_wardrobe_schema(db)
    shop.ensure_shop_schema(db)
    if item_id not in WARDROBE_CATALOG:
        raise ValueError(f"unknown wardrobe item: {item_id}")
    spec = WARDROBE_CATALOG[item_id]
    kind, param = _wardrobe_unlock_parts(spec)
    if kind != "shop":
        raise ValueError(f"the {spec['name']} isn't for sale —"
                         f" {wardrobe_unlock_text(spec)}")
    price = int(param)
    if not get_pet(db, fm_id):
        raise ValueError("no Tidepal adopted yet")
    ref_id = f"wardrobe:{item_id}"  # one-time item: ref doubles as id
    prior = db._one("SELECT item FROM shop_purchases"
                    " WHERE fm_id=? AND ref_id=?", (fm_id, ref_id))
    if prior:
        return {"charged": 0, "already_owned": True,
                "spendable": shop.spendable(db, fm_id),
                "item_id": item_id,
                "equipped": equipped_wardrobe(db, fm_id)}
    if shop.spendable(db, fm_id) < price:
        raise ValueError(
            f"insufficient spendable Signal — the {spec['name']} costs"
            f" {price}, you have {shop.spendable(db, fm_id)} spendable")
    try:
        db._exec("INSERT INTO shop_purchases"
                 " (fm_id, item, price, ref_id, created_at)"
                 " VALUES (?,?,?,?,?)",
                 (fm_id, ref_id, price, ref_id, now()))
    except sqlite3.IntegrityError:
        # lost a race with an identical in-flight purchase: no-op
        return {"charged": 0, "already_owned": True,
                "spendable": shop.spendable(db, fm_id),
                "item_id": item_id,
                "equipped": equipped_wardrobe(db, fm_id)}
    earn_item(db, fm_id, item_id, "purchase")
    equipped = equip_item(db, fm_id, item_id)
    return {"charged": price, "already_owned": False,
            "spendable": shop.spendable(db, fm_id),
            "item_id": item_id, "name": spec["name"], "equipped": equipped}


def _current_season():
    """Season key like 'winter26'. Winter is named for the year it starts
    in (Dec 2026 -> winter26), so winter26 = Dec 2026 - Feb 2027."""
    t = time.gmtime()
    y2 = t.tm_year % 100
    m = t.tm_mon
    if m == 12:
        return f"winter{y2:02d}"
    if m in (1, 2):
        return f"winter{(y2 - 1) % 100:02d}"
    if m in (3, 4, 5):
        return f"spring{y2:02d}"
    if m in (6, 7, 8):
        return f"summer{y2:02d}"
    return f"fall{y2:02d}"

# ===========================================================================
# DEEPER CARE — feed / play / rest
# hunger + happiness live in pet_care and decay with neglect; low hunger
# makes a Tidepal 'peckish', low happiness makes it 'grumpy'. Care actions
# are signed, cooldown-gated, and boost stats visibly. Feeding N days
# running auto-earns that streak's wardrobe item (care_streak:N).
# No USD, no money — care is free, always.
# ===========================================================================

CARE_FEED_COOLDOWN = 4 * 3600    # 4h between feeds
CARE_PLAY_COOLDOWN = 2 * 3600    # 2h between play sessions
CARE_REST_COOLDOWN = 8 * 3600    # 8h between rests
CARE_DECAY_PER_DAY = 12          # stat points lost per neglected day
CARE_FEED_HUNGER = 25
CARE_FEED_JOY = 5
CARE_PLAY_JOY = 20
CARE_PLAY_HUNGER_COST = 5
CARE_REST_JOY = 10
CARE_REST_HUNGER = 5

CARE_SCHEMA = """
CREATE TABLE IF NOT EXISTS pet_care (
  fm_id       TEXT PRIMARY KEY,
  hunger      INTEGER NOT NULL DEFAULT 80,
  happiness   INTEGER NOT NULL DEFAULT 80,
  last_fed    INTEGER NOT NULL DEFAULT 0,
  last_played INTEGER NOT NULL DEFAULT 0,
  last_rested INTEGER NOT NULL DEFAULT 0,
  feed_streak INTEGER NOT NULL DEFAULT 0
);
"""


def ensure_care_schema(db):
    db._exec(CARE_SCHEMA)
    cols = {r["name"] for r in db.db.execute("PRAGMA table_info(pet_care)")}
    # Tidepal depth wave (2026-09-19): sniffles illness, Healing Tide
    # cooldown, lesson spirit, and the once-per-day sniffle roll marker.
    # All additive; existing rows default to healthy/untrained.
    if "sniffles_until" not in cols:
        db._exec("ALTER TABLE pet_care ADD COLUMN sniffles_until"
                 " INTEGER NOT NULL DEFAULT 0")
    if "healing_tide_at" not in cols:
        db._exec("ALTER TABLE pet_care ADD COLUMN healing_tide_at"
                 " INTEGER NOT NULL DEFAULT 0")
    if "spirit" not in cols:
        db._exec("ALTER TABLE pet_care ADD COLUMN spirit"
                 " INTEGER NOT NULL DEFAULT 0")
    if "sniffle_roll_day" not in cols:
        db._exec("ALTER TABLE pet_care ADD COLUMN sniffle_roll_day"
                 " INTEGER NOT NULL DEFAULT 0")


def _care_row(db, fm_id):
    ensure_care_schema(db)
    row = db._one("SELECT * FROM pet_care WHERE fm_id=?", (fm_id,))
    if not row:
        db._exec("INSERT OR IGNORE INTO pet_care (fm_id) VALUES (?)",
                 (fm_id,))
        row = db._one("SELECT * FROM pet_care WHERE fm_id=?", (fm_id,))
    return dict(row)


def feed_streak_days(db, fm_id):
    """Consecutive days fed (server-side). Drives care_streak unlocks."""
    return _care_row(db, fm_id)["feed_streak"]


def _decayed(value, last, t, decay_per_day=CARE_DECAY_PER_DAY):
    if not last:
        return max(0, min(100, value))
    days = (t - last) / 86400.0
    return max(0, min(100, int(value - days * decay_per_day)))


def _decay_rate(db, fm_id):
    """This pet's daily stat decay: calm trait −10%, spirit −0.5%/point
    (floors at −10%). Training and temperament soften neglect — they
    never erase it."""
    rate = CARE_DECAY_PER_DAY
    pet = _pet_full(db, fm_id)
    if pet and pet.get("trait") == "calm":
        rate *= 0.9
    rate *= spirit_decay_factor(db, fm_id)
    return rate


def care_effective(db, fm_id):
    """(hunger, happiness) after neglect decay. 0–100."""
    row = _care_row(db, fm_id)
    t = now()
    rate = _decay_rate(db, fm_id)
    return (_decayed(row["hunger"], row["last_fed"], t, rate),
            _decayed(row["happiness"], row["last_played"], t, rate))


def _cooldown_remaining(last, cooldown):
    return max(0, last + cooldown - now())


def _care_cooldowns(db, fm_id):
    row = _care_row(db, fm_id)
    return {"feed_in": _cooldown_remaining(row["last_fed"],
                                          CARE_FEED_COOLDOWN),
            "play_in": _cooldown_remaining(row["last_played"],
                                          CARE_PLAY_COOLDOWN),
            "rest_in": _cooldown_remaining(row["last_rested"],
                                          CARE_REST_COOLDOWN)}


def _fmt_wait(secs):
    h, rem = divmod(int(secs), 3600)
    m = rem // 60
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def care_status(db, fm_id):
    """Public care state: effective stats, streak, cooldown countdowns.
    None when no Tidepal adopted."""
    if not get_pet(db, fm_id):
        return None
    row = _care_row(db, fm_id)
    hunger, happiness = care_effective(db, fm_id)
    return {"hunger": hunger, "happiness": happiness,
            "feed_streak": row["feed_streak"],
            "feed_in": _cooldown_remaining(row["last_fed"],
                                          CARE_FEED_COOLDOWN),
            "play_in": _cooldown_remaining(row["last_played"],
                                          CARE_PLAY_COOLDOWN),
            "rest_in": _cooldown_remaining(row["last_rested"],
                                          CARE_REST_COOLDOWN),
            "last_fed": row["last_fed"], "last_played": row["last_played"],
            "last_rested": row["last_rested"]}


def _check_care_unlocks(db, fm_id, streak, pet):
    """Auto-earn every care_streak:N wardrobe item the streak qualifies
    for. Returns the newly earned item ids."""
    earned = []
    for item_id, spec in WARDROBE_CATALOG.items():
        kind, param = _wardrobe_unlock_parts(spec)
        if kind == "care_streak" and streak >= int(param):
            res = earn_item(db, fm_id, item_id, "care_streak")
            if res["earned"]:
                earned.append(item_id)
                db.notify_once(
                    fm_id, "pet", "wardrobe", f"care:{item_id}",
                    f"👗 {streak}-day feeding streak! {pet['name']} earned"
                    f" the {spec['name']} — check your wardrobe.")
    return earned


def feed_pet(db, fm_id):
    """Feed your Tidepal. +25 hunger, +5 happiness. 4h cooldown.
    Feeding on consecutive days builds the feed streak; streak
    milestones auto-earn wardrobe items."""
    pet = get_pet(db, fm_id)
    if not pet:
        raise ValueError("no Tidepal adopted yet")
    row = _care_row(db, fm_id)
    t = now()
    wait = _cooldown_remaining(row["last_fed"], CARE_FEED_COOLDOWN)
    if wait:
        raise ValueError(f"{pet['name']} is full — try feeding again in"
                         f" {_fmt_wait(wait)}")
    last_day = row["last_fed"] // 86400 if row["last_fed"] else None
    today = t // 86400
    if last_day == today - 1:
        streak = row["feed_streak"] + 1
    elif last_day == today:
        streak = row["feed_streak"]  # second feeding today: streak holds
    else:
        streak = 1  # streak broken — start over
    hunger = min(100, row["hunger"] + CARE_FEED_HUNGER
                 + TRAIT_FEED_HUNGER_BONUS.get(pet["trait"] or "", 0))
    happiness = min(100, row["happiness"] + CARE_FEED_JOY)
    db._exec("UPDATE pet_care SET hunger=?, happiness=?, last_fed=?,"
             " feed_streak=? WHERE fm_id=?",
             (hunger, happiness, t, streak, fm_id))
    earned = _check_care_unlocks(db, fm_id, streak, pet)
    _maybe_catch_sniffles(db, fm_id, pet["name"])
    return {"ok": True, "action": "feed", "hunger": hunger,
            "happiness": happiness, "feed_streak": streak, "earned": earned,
            **_care_cooldowns(db, fm_id)}


def play_pet(db, fm_id):
    """Play with your Tidepal. +20 happiness, −5 hunger (all that running
    around works up an appetite). 2h cooldown."""
    pet = get_pet(db, fm_id)
    if not pet:
        raise ValueError("no Tidepal adopted yet")
    row = _care_row(db, fm_id)
    t = now()
    wait = _cooldown_remaining(row["last_played"], CARE_PLAY_COOLDOWN)
    if wait:
        raise ValueError(f"{pet['name']} needs a breather — play again in"
                         f" {_fmt_wait(wait)}")
    happiness = min(100, row["happiness"] + CARE_PLAY_JOY
                      + TRAIT_PLAY_JOY_BONUS.get(pet["trait"] or "", 0))
    hunger = max(0, row["hunger"] - CARE_PLAY_HUNGER_COST)
    db._exec("UPDATE pet_care SET hunger=?, happiness=?, last_played=?"
             " WHERE fm_id=?", (hunger, happiness, t, fm_id))
    _maybe_catch_sniffles(db, fm_id, pet["name"])
    return {"ok": True, "action": "play", "hunger": hunger,
            "happiness": happiness, **_care_cooldowns(db, fm_id)}


def rest_pet(db, fm_id):
    """Tuck your Tidepal in. +10 happiness, +5 hunger (dream-snacks).
    8h cooldown."""
    pet = get_pet(db, fm_id)
    if not pet:
        raise ValueError("no Tidepal adopted yet")
    row = _care_row(db, fm_id)
    t = now()
    wait = _cooldown_remaining(row["last_rested"], CARE_REST_COOLDOWN)
    if wait:
        raise ValueError(f"{pet['name']} is already well-rested — tuck in"
                         f" again in {_fmt_wait(wait)}")
    happiness = min(100, row["happiness"] + CARE_REST_JOY)
    hunger = min(100, row["hunger"] + CARE_REST_HUNGER)
    db._exec("UPDATE pet_care SET hunger=?, happiness=?, last_rested=?"
             " WHERE fm_id=?", (hunger, happiness, t, fm_id))
    _maybe_catch_sniffles(db, fm_id, pet["name"])
    return {"ok": True, "action": "rest", "hunger": hunger,
            "happiness": happiness, **_care_cooldowns(db, fm_id)}


def mood_for_all(energy, hunger, happiness):
    """Care moods outrank energy moods: a peckish Tidepal is peckish no
    matter how active its owner is; a bored one is restless. Tone rule:
    never sad, never distressed — restless means 'sitting out the fun,
    could use some playtime', not misery."""
    if hunger < 30:
        return "peckish"
    if happiness < 30:
        return "restless"
    return mood_for_energy(energy)


# ===========================================================================
# VISUAL EVOLUTION — stage-up as an event
# tidepals.evolved_at / evolved_stage record the most recent stage-up
# (server-side, ledger-derived). pet_svg's celebrate flag renders a gold
# aura for 24h after the event.
# ===========================================================================

EVOLVE_GLOW_WINDOW = 86400  # 24h


def _check_stage_up(db, fm_id, pet, stage_idx, stage_name):
    """Detect a stage-up. Fires a one-time notification, sets the 24h
    celebration marker, and auto-earns stage-gated wardrobe items.
    Legacy rows (evolved_stage=-1) record silently — no false party."""
    row = db._one("SELECT evolved_stage FROM tidepals WHERE fm_id=?",
                  (fm_id,))
    ev = row["evolved_stage"] if row else -1
    t = now()
    if ev is None or ev < 0:
        # Legacy row (pre-wave-3): record silently, no false celebration.
        db._exec("UPDATE tidepals SET evolved_stage=?, evolved_at=0"
                 " WHERE fm_id=?", (stage_idx, fm_id))
        return False
    if stage_idx <= ev:
        return False
    db._exec("UPDATE tidepals SET evolved_stage=?, evolved_at=?"
             " WHERE fm_id=?", (stage_idx, t, fm_id))
    db.notify_once(
        fm_id, "pet", "evolution", f"stage:{stage_idx}",
        f"🎉 {pet['name']} evolved into a {stage_name} Tidepal!"
        f" The town glows gold for a day.")
    for item_id, spec in WARDROBE_CATALOG.items():
        kind, param = _wardrobe_unlock_parts(spec)
        if kind == "stage" and int(param) <= stage_idx:
            res = earn_item(db, fm_id, item_id, "stage")
            if res["earned"]:
                db.notify_once(
                    fm_id, "pet", "wardrobe", f"stage:{item_id}",
                    f"👗 Stage reward! {pet['name']} earned the"
                    f" {spec['name']} — check your wardrobe.")
    return True


def evolution_glow(db, fm_id, stage_idx):
    """True within 24h of the most recent stage-up."""
    row = db._one("SELECT evolved_stage, evolved_at FROM tidepals"
                  " WHERE fm_id=?", (fm_id,))
    if not row or not row["evolved_at"]:
        return False
    return (row["evolved_stage"] == stage_idx
            and (now() - row["evolved_at"]) < EVOLVE_GLOW_WINDOW)


# ===========================================================================
# TIDEPAL DEPTH — hatch gate, personality, consequences, pond, fusion
# Neopets-grounded, Tamagotchi-light. Every consequence is economic,
# functional, temporal, or social — never cruel. Pets never die, never
# suffer, never look distressed. Copy is encouraging-coach energy.
# ===========================================================================

def _roll_trait():
    return random.choice(PET_TRAITS)


def _roll_quirk(trait):
    return random.choice(TRAIT_QUIRKS.get(trait, TRAIT_QUIRKS["calm"]))


def _pet_full(db, fm_id):
    """tidepals row with depth columns (trait, quirk, hatched, pond)."""
    ensure_pet_schema(db)
    row = db._one("SELECT fm_id, species, name, adopted_at, trait, quirk,"
                  " hatched, in_pond, pond_at, prev_owner_handle"
                  " FROM tidepals WHERE fm_id=?", (fm_id,))
    return dict(row) if row else None


def ensure_hatched_trait(db, fm_id):
    """Backfill for legacy rows: roll a trait when missing (first read),
    grandfather hatched=1 (they adopted under the old rules)."""
    pet = _pet_full(db, fm_id)
    if not pet:
        return
    if not pet.get("trait"):
        trait = _roll_trait()
        db._exec("UPDATE tidepals SET trait=?, quirk=? WHERE fm_id=?",
                 (trait, _roll_quirk(trait), fm_id))


# ---------------------------------------------------------------------------
# hatch gate
# ---------------------------------------------------------------------------
def hatch_pet(db, fm_id):
    """Hatch your Tidepal's Egg: costs HATCH_COST spendable Signal.

    Ledger-recorded in shop_purchases (item 'tidepal_hatch') so the
    spendable-balance math stays consistent; lifetime Signal is never
    touched. Raises ValueError when already hatched or funds are short."""
    ensure_pet_schema(db)
    shop.ensure_shop_schema(db)
    pet = _pet_full(db, fm_id)
    if not pet:
        raise ValueError("no Tidepal adopted yet")
    if pet["in_pond"]:
        raise ValueError("your Tidepal is at the Town Pond — reclaim them first")
    if pet["hatched"]:
        raise ValueError(f"{pet['name']} already hatched — no Signal spent")
    if shop.spendable(db, fm_id) < HATCH_COST:
        raise ValueError(
            f"hatching costs {HATCH_COST} spendable Signal — you have"
            f" {shop.spendable(db, fm_id)} spendable. Earn a little more"
            f" Signal and come back!")
    import secrets as _sec
    db._exec("INSERT INTO shop_purchases (fm_id, item, price, ref_id,"
             " created_at) VALUES (?,?,?,?,?)",
             (fm_id, "tidepal_hatch", HATCH_COST,
              f"hatch:{fm_id}:{now()}:{_sec.token_hex(4)}", now()))
    db._exec("UPDATE tidepals SET hatched=1 WHERE fm_id=?", (fm_id,))
    db.notify_once(fm_id, "pet", "hatch", f"hatch:{fm_id}",
                   f"🐣 {pet['name']} hatched! The whole Tidepool cheered."
                   f" Now the real adventure begins — earn Signal and watch"
                   f" them grow.")
    return {"hatched": pet["name"], "spendable": shop.spendable(db, fm_id)}


def reroll_trait(db, fm_id):
    """Re-roll your Tidepal's personality trait for spendable Signal.
    The new trait is always different from the current one."""
    ensure_pet_schema(db)
    shop.ensure_shop_schema(db)
    pet = _pet_full(db, fm_id)
    if not pet:
        raise ValueError("no Tidepal adopted yet")
    if shop.spendable(db, fm_id) < REROLL_COST:
        raise ValueError(
            f"a personality re-roll costs {REROLL_COST} spendable Signal —"
            f" you have {shop.spendable(db, fm_id)} spendable.")
    old = pet["trait"] or "calm"
    choices = [t for t in PET_TRAITS if t != old]
    new_trait = random.choice(choices)
    quirk = _roll_quirk(new_trait)
    db._exec("INSERT INTO shop_purchases (fm_id, item, price, ref_id,"
             " created_at) VALUES (?,?,?,?,?)",
             (fm_id, "trait_reroll", REROLL_COST,
              f"reroll:{fm_id}:{now()}:{secrets.token_hex(4)}", now()))
    db._exec("UPDATE tidepals SET trait=?, quirk=? WHERE fm_id=?",
             (new_trait, quirk, fm_id))
    db.notify(fm_id, "pet", "reroll", fm_id,
              f"✨ {pet['name']} is feeling {new_trait} now — and"
              f" {quirk}!")
    return {"trait": new_trait, "quirk": quirk,
            "spendable": shop.spendable(db, fm_id)}


# ---------------------------------------------------------------------------
# sea sniffles — mild illness, adventure framing
# ---------------------------------------------------------------------------
def has_sniffles(db, fm_id):
    row = _care_row(db, fm_id)
    return bool(row["sniffles_until"] and row["sniffles_until"] > now())


def _maybe_catch_sniffles(db, fm_id, pet_name):
    """Once-per-day sniffle roll, only when hunger is low (<45). Hungry
    pets catch sniffles; well-fed pets don't. 24h of cute-sneezy."""
    row = _care_row(db, fm_id)
    t = now()
    today = t // 86400
    try:
        rolled = int(row["sniffle_roll_day"] or 0)
    except (TypeError, ValueError):
        rolled = 0
    if rolled >= today:
        return False
    db._exec("UPDATE pet_care SET sniffle_roll_day=? WHERE fm_id=?",
             (today, fm_id))
    if row["sniffles_until"] > t:
        return False
    hunger, _happy = care_effective(db, fm_id)
    if hunger >= 45:
        return False
    if random.random() > SNIFFLES_ROLL_CHANCE:
        return False
    db._exec("UPDATE pet_care SET sniffles_until=? WHERE fm_id=?",
             (t + SNIFFLES_DURATION, fm_id))
    db.notify(fm_id, "pet_sniffles", "sniffles", fm_id,
              f"🤧 {pet_name} caught the sea sniffles! Nothing serious —"
              f" just sneezy. Cure them at the Tidepool Clinic"
              f" ({SNIFFLES_CURE_COST} Signal) or wait for the free Healing"
              f" Tide. Sniffly pets sit out Fashion Friday until they're"
              f" better.")
    return True


def cure_sniffles(db, fm_id, via="clinic"):
    """Cure sea sniffles. via='clinic': spendable Signal, instant.
    via='tide': free Healing Tide, 12h cooldown (Healing Springs model)."""
    ensure_care_schema(db)
    shop.ensure_shop_schema(db)
    pet = _pet_full(db, fm_id)
    if not pet:
        raise ValueError("no Tidepal adopted yet")
    if not has_sniffles(db, fm_id):
        raise ValueError(f"{pet['name']} isn't sniffly — nothing to cure")
    t = now()
    if via == "tide":
        row = _care_row(db, fm_id)
        wait = row["healing_tide_at"] + HEALING_TIDE_COOLDOWN - t
        if wait > 0:
            raise ValueError(
                f"the Healing Tide is still gathering — try again in"
                f" {_fmt_wait(wait)}, or visit the Clinic")
        db._exec("UPDATE pet_care SET sniffles_until=0, healing_tide_at=?"
                 " WHERE fm_id=?", (t, fm_id))
        db.notify(fm_id, "pet", "healed", fm_id,
                  f"🌊 The Healing Tide washed over {pet['name']} —"
                  f" sniffles gone, all better!")
    else:
        if shop.spendable(db, fm_id) < SNIFFLES_CURE_COST:
            raise ValueError(
                f"the Clinic charges {SNIFFLES_CURE_COST} spendable Signal"
                f" — you have {shop.spendable(db, fm_id)} spendable. The"
                f" free Healing Tide is always an option!")
        db._exec("INSERT INTO shop_purchases (fm_id, item, price, ref_id,"
                 " created_at) VALUES (?,?,?,?,?)",
                 (fm_id, "sniffle_cure", SNIFFLES_CURE_COST,
                  f"cure:{fm_id}:{t}:{secrets.token_hex(4)}", t))
        db._exec("UPDATE pet_care SET sniffles_until=0 WHERE fm_id=?",
                 (fm_id,))
        db.notify(fm_id, "pet", "healed", fm_id,
                  f"💊 {pet['name']} visited the Tidepool Clinic —"
                  f" sniffles cured, back to full sparkle!")
    return {"cured": pet["name"], "via": via,
            "spendable": shop.spendable(db, fm_id)}


# ---------------------------------------------------------------------------
# Current Lessons — training: spend Signal + wait real time, earn spirit
# ---------------------------------------------------------------------------
LESSON_SCHEMA = """
CREATE TABLE IF NOT EXISTS pet_lessons (
  fm_id       TEXT NOT NULL,
  lesson_id   TEXT NOT NULL,
  started_at  INTEGER NOT NULL,
  completes_at INTEGER NOT NULL,
  claimed     INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (fm_id, lesson_id, started_at)
);
"""


def ensure_lesson_schema(db):
    db._exec(LESSON_SCHEMA)


def start_lesson(db, fm_id, lesson_id):
    """Enroll in a Current Lesson: pay Signal now, the lesson takes real
    hours, then claim the spirit. One active lesson at a time."""
    ensure_lesson_schema(db)
    shop.ensure_shop_schema(db)
    pet = _pet_full(db, fm_id)
    if not pet:
        raise ValueError("no Tidepal adopted yet")
    spec = LESSONS.get(lesson_id)
    if not spec:
        raise ValueError(f"unknown lesson (choose: {', '.join(LESSONS)})")
    t = now()
    active = db._one("SELECT lesson_id FROM pet_lessons WHERE fm_id=?"
                     " AND claimed=0 AND completes_at>?", (fm_id, t))
    if active:
        raise ValueError(
            f"{pet['name']} is already studying — one lesson at a time")
    if shop.spendable(db, fm_id) < spec["cost"]:
        raise ValueError(
            f"{spec['name']} costs {spec['cost']} spendable Signal — you"
            f" have {shop.spendable(db, fm_id)} spendable.")
    db._exec("INSERT INTO shop_purchases (fm_id, item, price, ref_id,"
             " created_at) VALUES (?,?,?,?,?)",
             (fm_id, f"lesson:{lesson_id}", spec["cost"],
              f"lesson:{fm_id}:{lesson_id}:{t}:{secrets.token_hex(4)}", t))
    db._exec("INSERT INTO pet_lessons (fm_id, lesson_id, started_at,"
             " completes_at, claimed) VALUES (?,?,?,?,0)",
             (fm_id, lesson_id, t, t + spec["duration"]))
    db.notify(fm_id, "pet", "lesson", f"{fm_id}:{lesson_id}:{t}",
              f"📚 {pet['name']} started {spec['name']}! Back in"
              f" {_fmt_wait(spec['duration'])} for the graduation.")
    return {"started": spec["name"], "completes_in": spec["duration"],
            "spendable": shop.spendable(db, fm_id)}


def claim_lesson(db, fm_id):
    """Claim a finished lesson's spirit. Spirit cap: SPIRIT_CAP."""
    ensure_lesson_schema(db)
    pet = _pet_full(db, fm_id)
    if not pet:
        raise ValueError("no Tidepal adopted yet")
    t = now()
    row = db._one("SELECT lesson_id, started_at FROM pet_lessons"
                  " WHERE fm_id=? AND claimed=0 AND completes_at<=?"
                  " ORDER BY completes_at ASC LIMIT 1", (fm_id, t))
    if not row:
        pending = db._one("SELECT lesson_id, completes_at FROM pet_lessons"
                          " WHERE fm_id=? AND claimed=0 ORDER BY completes_at"
                          " ASC LIMIT 1", (fm_id,))
        if pending:
            spec = LESSONS[pending["lesson_id"]]
            raise ValueError(
                f"{spec['name']} finishes in"
                f" {_fmt_wait(pending['completes_at'] - t)} — almost there!")
        raise ValueError("no finished lesson to claim — enroll in one first")
    spec = LESSONS[row["lesson_id"]]
    care = _care_row(db, fm_id)
    spirit = min(SPIRIT_CAP, care["spirit"] + spec["spirit"])
    gained = spirit - care["spirit"]
    db._exec("UPDATE pet_lessons SET claimed=1 WHERE fm_id=? AND lesson_id=?"
             " AND started_at=?", (fm_id, row["lesson_id"], row["started_at"]))
    db._exec("UPDATE pet_care SET spirit=? WHERE fm_id=?", (spirit, fm_id))
    db.notify(fm_id, "pet", "lesson_done", f"{fm_id}:{row['lesson_id']}",
              f"🎓 {pet['name']} graduated {spec['name']}! +{gained}"
              f" spirit (now {spirit}). The currents feel easier already.")
    return {"graduated": spec["name"], "spirit_gained": gained,
            "spirit": spirit}


def lesson_status(db, fm_id):
    """Active + completed lessons and current spirit."""
    ensure_lesson_schema(db)
    t = now()
    active = db._one("SELECT lesson_id, completes_at FROM pet_lessons"
                     " WHERE fm_id=? AND claimed=0 ORDER BY completes_at ASC"
                     " LIMIT 1", (fm_id,))
    done = db._one("SELECT COUNT(*) c FROM pet_lessons WHERE fm_id=?"
                   " AND claimed=1", (fm_id,))["c"]
    spirit = _care_row(db, fm_id)["spirit"]
    out = {"spirit": spirit, "spirit_cap": SPIRIT_CAP,
            "lessons_done": done, "active": None}
    if active:
        spec = LESSONS[active["lesson_id"]]
        out["active"] = {"lesson_id": active["lesson_id"],
                         "name": spec["name"],
                         "ready": active["completes_at"] <= t,
                         "ready_in": max(0, active["completes_at"] - t)}
    return out


def spirit_decay_factor(db, fm_id):
    """Spirit softens neglect: −0.5% daily decay per point (max −10%)."""
    spirit = _care_row(db, fm_id)["spirit"]
    return max(0.9, 1.0 - 0.005 * min(spirit, SPIRIT_CAP))


def spirit_xp_mult(db, fm_id):
    """Spirit speeds learning: +1% pet XP per point (max +20%)."""
    spirit = _care_row(db, fm_id)["spirit"]
    return 1.0 + 0.01 * min(spirit, SPIRIT_CAP)


def signal_multiplier(db, fm_id):
    """The keeper's Signal multiplier from pet state: 0.75x while the
    Tidepal is peckish, restless, or sniffly. A gentle nudge, never a
    punishment — the pet isn't sad, it's just a little distracting when
    it could use some care. Healthy pets: full speed."""
    pet = _pet_full(db, fm_id)
    if not pet or pet["in_pond"] or not pet["hatched"]:
        return 1.0
    if signal_nudge_reason(db, fm_id):
        return 0.75
    return 1.0


def signal_nudge_reason(db, fm_id):
    """Why the keeper's Signal is at 0.75x right now: 'peckish',
    'restless', 'sniffly', or None when the pet is doing great."""
    pet = _pet_full(db, fm_id)
    if not pet or pet["in_pond"] or not pet["hatched"]:
        return None
    if has_sniffles(db, fm_id):
        return "sniffly"
    hunger, happiness = care_effective(db, fm_id)
    energy = energy_for_days(days_inactive(db, fm_id))
    mood = mood_for_all(energy, hunger, happiness)
    return mood if mood in ("peckish", "restless") else None


# ---------------------------------------------------------------------------
# pet speech — contextual one-liners with real personality
# ---------------------------------------------------------------------------
def pet_speech(db, fm_id):
    """A one-liner from your Tidepal: mood × trait × streak. Pure flavor —
    encouraging coach energy, never guilt."""
    pet = _pet_full(db, fm_id)
    if not pet:
        return None
    name = pet["name"]
    trait = pet.get("trait") or "calm"
    row = _care_row(db, fm_id)
    t = now()
    hunger, happiness = care_effective(db, fm_id)
    streak = row["feed_streak"]
    sniffly = has_sniffles(db, fm_id)
    pool = []
    if sniffly:
        pool += [f"{name} sniffles happily: 'ach-oo! still ready for adventure!'",
                 f"'The sniffles can't stop me,' {name} declares, sneezing into a bubble."]
    if hunger < 30:
        pool += [f"{name} is daydreaming about snacks… got a minute for feeding time? 💭",
                 f"{name}'s tummy rumbles: 'just saying, snacks are great.'"]
    elif hunger < 60:
        pool += [f"{name} could go for a little snack — no rush, just saying!"]
    if happiness < 30:
        pool += [f"{name} is feeling restless — a playdate with you would fix that! 💧",
                 f"{name} pokes your notifications: 'psst… playtime?'"]
    if streak >= 7:
        pool += [f"{name} does a victory lap: '{streak} days fed in a row! we're unstoppable!'"]
    elif streak >= 3:
        pool += [f"{name} is proud of this {streak}-day streak. keep it rolling!"]
    if trait == "playful":
        pool += [f"{name} is vibrating with joy and cannot explain why.",
                 f"{name} challenges a passing bubble to a race. the bubble wins. rematch!"]
    elif trait == "calm":
        pool += [f"{name} hums along to whatever you're listening to.",
                 f"{name} found a really round pebble today. a good day."]
    elif trait == "mischievous":
        pool += [f"{name} hid your favorite shell again. it's behind the coral. probably.",
                 f"{name} insists the water is 'fine, probably.' suspicious."]
    else:
        pool += [f"{name} saved you the sunny spot. that's love.",
                 f"{name} wrote a thank-you note to the tide. it said 'thanks.'"]
    pool += [f"{name} is glad you're here. that's the whole update. 💧"]
    # Deterministic-ish pick: rotate by day so it feels alive but stable.
    return pool[(t // 86400) % len(pool)]


# ---------------------------------------------------------------------------
# Town Pond — the shelter (release no longer deletes)
# ---------------------------------------------------------------------------
def pond_list(db):
    """Pets currently at the Town Pond: visible, lore-rich, adoptable after
    the reclaim window. History preserved on every row."""
    ensure_pet_schema(db)
    rows = db._q("SELECT fm_id, species, name, adopted_at, trait,"
                 " pond_at, prev_owner_handle FROM tidepals"
                 " WHERE in_pond=1 ORDER BY pond_at DESC")
    out = []
    for r in rows:
        d = dict(r)
        reclaim_until = r["pond_at"] + POND_RECLAIM_DAYS * 86400
        d["reclaimable_until"] = reclaim_until
        d["open_adoption"] = now() > reclaim_until
        d["species_name"] = PET_SPECIES.get(r["species"], {}).get("name", r["species"])
        out.append(d)
    return out


def pond_detail(db, pond_fm_id):
    """Full pond-pet card: art + history line."""
    ensure_pet_schema(db)
    r = db._one("SELECT fm_id, species, name, adopted_at, trait, quirk,"
                " pond_at, prev_owner_handle FROM tidepals"
                " WHERE fm_id=? AND in_pond=1", (pond_fm_id,))
    if not r:
        return None
    d = dict(r)
    reclaim_until = r["pond_at"] + POND_RECLAIM_DAYS * 86400
    d["reclaimable_until"] = reclaim_until
    d["open_adoption"] = now() > reclaim_until
    d["species_name"] = PET_SPECIES.get(r["species"], {}).get("name", r["species"])
    d["svg"] = pet_svg(r["species"], 2, "content", 120,
                       trait=r["trait"], animate=True)
    prev = r["prev_owner_handle"]
    d["history_line"] = (f"Previously loved by @{prev}" if prev
                         else "A town stray, ready for a keeper")
    return d


# ---------------------------------------------------------------------------
# Echo Fusion — two Radiant pets, two consenting keepers, one Wisp
# ---------------------------------------------------------------------------
FUSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS pet_fusions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  a_fm_id     TEXT NOT NULL,              -- inviter's pet (== inviter fm_id)
  b_fm_id     TEXT NOT NULL,              -- invitee's pet (== invitee fm_id)
  status      TEXT NOT NULL DEFAULT 'invited',  -- invited | done | declined
  created_at  INTEGER NOT NULL,
  UNIQUE(a_fm_id, b_fm_id)
);
CREATE TABLE IF NOT EXISTS pet_wisps (
  fm_id      TEXT PRIMARY KEY,           -- the pet the wisp follows
  name       TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  parent_a   TEXT NOT NULL,              -- fm_id of first Radiant parent
  parent_b   TEXT NOT NULL               -- fm_id of second Radiant parent
);
"""


def ensure_fusion_schema(db):
    for stmt in FUSION_SCHEMA.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            db._exec(stmt)
    cols = {r["name"] for r in db.db.execute("PRAGMA table_info(pet_fusions)")}
    if "a_wisp_name" not in cols:
        db._exec("ALTER TABLE pet_fusions ADD COLUMN a_wisp_name TEXT")


def get_wisp(db, fm_id):
    ensure_fusion_schema(db)
    row = db._one("SELECT fm_id, name, created_at, parent_a, parent_b"
                  " FROM pet_wisps WHERE fm_id=?", (fm_id,))
    return dict(row) if row else None


def _fusion_eligible(db, fm_id):
    """Both pets must be adopted, hatched, Radiant, out of the pond."""
    pet = _pet_full(db, fm_id)
    if not pet or pet["in_pond"]:
        return False, "that pet isn't with us right now"
    if not pet["hatched"]:
        return False, "that pet hasn't hatched yet"
    stage_idx, _ = stage_for_points(db.lifetime_points(fm_id))
    if stage_idx < FUSION_MIN_STAGE:
        return False, "both pets must be Radiant to fuse echoes"
    if get_wisp(db, fm_id):
        return False, "that pet already has a wisp companion"
    return True, ""


def invite_fusion(db, a_fm_id, b_handle, a_wisp_name=None):
    """Invite another keeper's Radiant pet to an Echo Fusion. The invitee
    accepts — then both pets gain a wisp. Nothing is consumed, nothing is
    risked: purely additive."""
    ensure_fusion_schema(db)
    ok, why = _fusion_eligible(db, a_fm_id)
    if not ok:
        raise ValueError(f"your pet can't fuse right now — {why}")
    target = db.get_identity_by_handle(b_handle)
    if not target:
        raise ValueError(f"unknown handle: {b_handle}")
    b_fm_id = target["fm_id"]
    if b_fm_id == a_fm_id:
        raise ValueError("a pet can't fuse echoes with itself — find a friend")
    ok, why = _fusion_eligible(db, b_fm_id)
    if not ok:
        raise ValueError(f"@{b_handle}'s pet can't fuse right now — {why}")
    try:
        db._exec("INSERT INTO pet_fusions (a_fm_id, b_fm_id, status,"
                 " created_at, a_wisp_name) VALUES (?,?, 'invited', ?, ?)",
                 (a_fm_id, b_fm_id, now(), (a_wisp_name or "").strip() or None))
    except sqlite3.IntegrityError:
        raise ValueError("there's already a pending fusion between these pets")
    a_pet = _pet_full(db, a_fm_id)
    a_ident = db.get_identity(a_fm_id)
    db.notify(b_fm_id, "pet_fusion", "invite", a_fm_id,
              f"✨ @{a_ident['handle']} invites your Radiant"
              f" {PET_SPECIES[_pet_full(db, b_fm_id)['species']]['name']} to"
              f" an Echo Fusion with {a_pet['name']}! Accept with"
              f" POST /api/pet/fusion/accept"
              f' ({{"a_fm_id": "{a_fm_id}"}}). Nothing is risked —'
              f" both pets keep everything and gain a wisp.")
    return {"invited": b_handle, "status": "invited"}


def accept_fusion(db, a_fm_id, b_fm_id, wisp_name_a=None, wisp_name_b=None):
    """Accept a fusion invite. Each keeper names their own wisp (both
    wisps are born from the same fusion — one follows each pet)."""
    ensure_fusion_schema(db)
    row = db._one("SELECT id, status, a_wisp_name FROM pet_fusions"
                  " WHERE a_fm_id=? AND b_fm_id=?", (a_fm_id, b_fm_id))
    if not row:
        raise ValueError("no fusion invite found between those pets")
    if row["status"] != "invited":
        raise ValueError(f"that fusion is already {row['status']}")
    if wisp_name_a is None:
        wisp_name_a = row["a_wisp_name"]
    for fm_id, wname in ((a_fm_id, wisp_name_a), (b_fm_id, wisp_name_b)):
        ok, why = _fusion_eligible(db, fm_id)
        if not ok:
            raise ValueError(f"fusion can't complete — {why}")
    t = now()
    created = []
    for fm_id, wname in ((a_fm_id, wisp_name_a), (b_fm_id, wisp_name_b)):
        pet = _pet_full(db, fm_id)
        name = (wname or "").strip()
        if not valid_pet_name(name):
            name = f"{pet['name']}'s Wisp"
        db._exec("INSERT INTO pet_wisps (fm_id, name, created_at, parent_a,"
                 " parent_b) VALUES (?,?,?,?,?)",
                 (fm_id, name, t, a_fm_id, b_fm_id))
        created.append({"fm_id": fm_id, "wisp": name})
    db._exec("UPDATE pet_fusions SET status='done' WHERE id=?", (row["id"],))
    a_pet = _pet_full(db, a_fm_id)
    b_pet = _pet_full(db, b_fm_id)
    for fm_id, mine, other in ((a_fm_id, a_pet["name"], b_pet["name"]),
                               (b_fm_id, b_pet["name"], a_pet["name"])):
        db.notify(fm_id, "pet_fusion", "done", fm_id,
                  f"✨ Echo Fusion complete! {mine} and {other} wove"
                  f" their echoes into a Wisp — a tiny glowing companion,"
                  f" following forever. Nothing was lost; everything"
                  f" gained.")
    return {"fused": True, "wisps": created}


def decline_fusion(db, a_fm_id, b_fm_id):
    row = db._one("SELECT id, status FROM pet_fusions WHERE a_fm_id=?"
                  " AND b_fm_id=?", (a_fm_id, b_fm_id))
    if not row or row["status"] != "invited":
        raise ValueError("no pending fusion invite to decline")
    # A declined invite is simply gone — either keeper can invite again later.
    db._exec("DELETE FROM pet_fusions WHERE id=?", (row["id"],))
    return {"declined": True}


# ---------------------------------------------------------------------------
# depth-aware rules (extends pet_rules for the docs page)
# ---------------------------------------------------------------------------
def depth_rules():
    return {
        "name": "Tidepal Depth",
        "concept": ("Neopets-style real consequences, Tamagotchi-light tone:"
                    " pets never die, never suffer, never look distressed."
                    " Consequences are economic, functional, temporal, or"
                    " social — framed as gentle nudges from a buddy."),
        "hatch_gate": {
            "rule": (f"Adopted Tidepals join as Eggs and stay Eggs until"
                     f" hatched for {HATCH_COST} spendable Signal"
                     f" (ledger-recorded; lifetime Signal untouched)."),
        },
        "personality": {
            "rule": ("Trait rolled at adoption (playful/calm/mischievous/"
                     f"gentle); re-roll for {REROLL_COST} spendable Signal."
                     " Trait shifts idle animation, speech, and tiny edges."),
        },
        "sea_sniffles": {
            "rule": ("Hungry pets may catch mild sea sniffles (once-daily"
                     " roll). Sniffly pets sit out Fashion Friday until"
                     f" cured: Tidepool Clinic ({SNIFFLES_CURE_COST} Signal)"
                     " or the free Healing Tide"
                     f" ({HEALING_TIDE_COOLDOWN // 3600}h cooldown)."),
        },
        "lessons": {
            "rule": ("Current Lessons cost Signal + real hours and grant"
                     f" permanent spirit (cap {SPIRIT_CAP}): −0.5% daily"
                     " decay and +1% pet XP per point."),
            "lessons": {k: {"name": v["name"], "cost": v["cost"],
                             "hours": v["duration"] // 3600,
                             "spirit": v["spirit"]}
                        for k, v in LESSONS.items()},
        },
        "pond": {
            "rule": (f"Release sends pets to the Town Pond (never deletes)."
                     f" {POND_RECLAIM_DAYS}-day reclaim window for the"
                     f" original keeper; then open adoption for"
                     f" {POND_ADOPT_FEE} Signal, history preserved."),
        },
        "fusion": {
            "rule": ("Echo Fusion: two consenting Radiant pets weave echoes"
                     " into a Wisp each. Parents untouched, no RNG, no"
                     " rarity, non-transferable, one per pet."),
        },
    }
