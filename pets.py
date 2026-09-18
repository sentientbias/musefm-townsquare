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
import re
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
    db._exec("INSERT INTO tidepals (fm_id, species, name, adopted_at)"
             " VALUES (?,?,?,?)", (fm_id, species, name, t))
    db.notify_once(fm_id, "pet", "tidepal", "adopted",
                   f"💧 {name} the {PET_SPECIES[species]['name']} hatched! "
                   f"Earn Signal and watch them grow.")
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
    row = db._one("SELECT fm_id, species, name, adopted_at FROM tidepals"
                  " WHERE fm_id=?", (fm_id,))
    return dict(row) if row else None

# ===========================================================================
# status / sweep / rules
# ===========================================================================

def pet_status(db, fm_id):
    """Full public status for an identity's Tidepal, or None if unadopted.
    Stage from ledger-verified lifetime Signal; energy/mood from the
    owner's real last-active timestamp."""
    pet = get_pet(db, fm_id)
    if not pet:
        return None
    ident = db.get_identity(fm_id)
    points = db.lifetime_points(fm_id)
    stage_idx, stage_name = stage_for_points(points)
    days = days_inactive(db, fm_id)
    energy = energy_for_days(days)
    mood = mood_for_energy(energy)
    if stage_idx < len(PET_STAGES) - 1:
        next_name = PET_STAGES[stage_idx + 1][1]
        next_at = PET_STAGES[stage_idx + 1][0]
        base = PET_STAGES[stage_idx][0]
        progress = min(1.0, max(0.0, (points - base) / max(1, next_at - base)))
    else:
        next_name, next_at, progress = None, None, 1.0
    accessories = shop.equipped_accessories(db, fm_id)
    # Hidden comeback mechanic: if the owner just returned from 7+ days
    # dormant, the Tidepal is overjoyed — a visible reaction to the
    # surprise waiting in their Signal history. Never documented.
    glow = db.comeback_today(fm_id)
    if glow:
        mood = "overjoyed"
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
        "missed_you_glow": glow,
        "days_inactive": days,
        "adopted_at": pet["adopted_at"],
        "accessories": accessories,
        "spendable": shop.spendable(db, fm_id),
        "svg": pet_svg(pet["species"], stage_idx, mood, 64, accessories),
        "svg_large": pet_svg(pet["species"], stage_idx, mood, 220, accessories),
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
            "rule": ("Three premium species are condition-locked. Locked "
                     "species show as silhouettes until earned. Unlock checks "
                     "read only server-side verified state — never the "
                     "client. The Signal Shop sells a bypass per species; "
                     "the bypass never overrides one-pet-per-identity."),
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
                      "sleepy": "energy under 40"},
        },
        "sleepy_nudge": {
            "rule": ("Dormant 5–6 days with an adopted pet: one 'getting "
                     "sleepy' nudge per dormancy episode, slotted between "
                     "the town's 3-day and 7-day re-engagement nudges."),
            "notification_type": "pet_sleepy",
        },
        "naming": ("2–24 chars: letters, numbers, spaces, _ and -. "
                   "Same profanity filter as handles."),
        "limits": ["One pet per identity, enforced by the database."],
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


_ART = {
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


def pet_svg(species, stage_idx, mood, size=120, accessories=()):
    """Full standalone SVG for a pet. Pure inline vectors, no assets.
    accessories: owned+equipped shop item keys, drawn as overlays."""
    if species not in _ART:
        species = "driplet"
    stage_idx = max(0, min(len(PET_STAGES) - 1, stage_idx))
    glow = (mood == "overjoyed")  # hidden comeback reaction: happy face + sparkles
    if mood not in ("happy", "content", "sleepy"):
        mood = "happy" if glow else "content"
    inner = _ART[species](stage_idx, mood)
    overlays = "".join(_ACC_OVERLAY[a]() for a in (accessories or ())
                       if a in _ACC_OVERLAY)
    s = _STAGE_SCALE[stage_idx]
    aura = (_aura() + _sparkles()) if (stage_idx == 4 or glow) else ""
    label = (f"{PET_SPECIES[species]['name']} — "
             f"{PET_STAGES[stage_idx][1]}, {mood}")
    return (
        f'<svg viewBox="0 0 120 120" width="{size}" height="{size}" role="img"'
        f' aria-label="{label}" xmlns="http://www.w3.org/2000/svg">'
        f"<title>{label}</title>"
        f"{aura}{_shadow()}"
        f'<g transform="translate(60 62) scale({s}) translate(-60 -62)">'
        f"{inner}{overlays}</g></svg>")
