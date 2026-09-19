# Tidepal Overhaul — Plan & Judgment Log

**Date:** 2026-09-19 · **Authorized by:** Anthony ("speed and implementation", "we can flex as we go once it's a working project")
**Rule:** Do not commit until implementation + tests are complete; then commit and push `origin/master`.

## Product direction (Anthony, ~04:00–04:02 CDT)

- Base pet systems on established mechanics: **Neopets as the anchor for real consequences**, plus Tamagotchi real-time care, Pokémon friendship/evolution, Animal Crossing daily rituals, RPG progression, battle-pass/seasons, guild/social, idle prestige.
- "Real consequences," but our own — **never sad, never cruel**.
- Final tone (Anthony, verbatim): "pets can be hungry etc but not over the top" / "'They'll die!' Not that lmao."
- **Hard rules:** (1) No death, ever — no "critical" states, no death jokes in copy. (2) Mild stakes only: hunger = "peckish," boredom = "restless," illness = mild "sea sniffles." Tamagotchi-light, never distressed. (3) Consequences stay economic/functional/temporal/social, framed as gentle nudges — the pet sounds like a buddy asking for a favor, not a victim.
- Personality stays. Personality rerolls cost spendable Signal.
- Pet rooms → **SOMEDAY** (deferred, do not build).
- Adoption gate approved. Fix all inaccurate adoption/hatching language.

## Delegated decisions (locked)

1. **Hatch gate:** Adoption creates an Egg; hatching costs 50 spendable Signal (ledger-recorded in `shop_purchases`; lifetime Signal untouched). Until hatched, pet is stage 0 regardless of lifetime Signal. Adoption copy says "joined as an Egg," never "hatched."
2. **Personality:** one wholesome trait at adoption (playful/calm/mischievous/gentle) + quirk; shifts idle animation, speech, tiny edges; reroll = 25 spendable Signal.
3. **Consequences:** restless (not gloomy/grumpy) state; restless/peckish/sniffly → 0.75x Signal for keeper, sits out Fashion Friday, streaks break. Sea sniffles = mild random illness when hungry; cure at Tidepool Clinic (30 Signal) or free Healing Tide (12h cooldown). Current Lessons: spend Signal + wait real hours → permanent spirit (−0.5% decay/+1% XP per point, cap 20). Pets never die, no permanent involuntary loss.
4. **Town Pond:** release no longer deletes. Visible shelter, 7-day reclaim window, then open adoption for 25 Signal with history preserved ("previously loved by @handle").
5. **Echo Fusion:** two consenting keepers, both pets Radiant → each pet gains a Wisp companion. Parents untouched, no RNG/rarity/consumption/transfer. One per pet.
6. **Playdates:** don't exist as a system yet — deferred to SOMEDAY with the gate rule specified (restless pets sit out).

## What was built (this pass)

**pets.py**
- Schema: `tidepals` += trait, quirk, hatched (default 1 = grandfathered), in_pond, pond_at, prev_owner_handle; `pet_care` += sniffles_until, healing_tide_at, spirit, sniffle_roll_day; new `pet_lessons`, `pet_fusions`, `pet_wisps` tables.
- `adopt()` rolls trait/quirk, inserts hatched=0, fixed copy ("joined the town as an Egg").
- `hatch_pet()` — 50 spendable Signal, ledger-recorded; `pet_status` forces stage 0 until hatched.
- `reroll_trait()` — 25 Signal, always a different trait.
- Sniffles: `_maybe_catch_sniffles()` (once-daily roll, only when hunger < 45, 8%/day); `cure_sniffles(via=clinic|tide)`; `has_sniffles()`.
- Lessons: `start_lesson` / `claim_lesson` / `lesson_status`; spirit → decay & XP modifiers.
- `signal_multiplier()` — 0.75x while peckish/restless/sniffly; wired into `db.award()` for TRIGGER_REASONS only (payouts never reduced; best-effort, never breaks Signal).
- `mood_for_all`: grumpy → restless; `pet_svg` gains SMIL idle animation (trait-shifted), mood overlays (sleepy z's, happy sparkles, peckish snack-daydream, restless droopy antennae, sniffle puffs), wisp orbit. `animate=True` default; static art untouched underneath.
- `pet_speech()` — contextual one-liners, encouraging-coach energy.
- `release_pet()` → pond (no delete); `reclaim_pet()`; `pond_adopt()` (moves row + wardrobe + XP + wisps; keeper's own care streak survives).
- Fusion: `invite_fusion` / `accept_fusion` / `decline_fusion` / `get_wisp`.
- `pet_rules()` gains "depth" section; moods doc updated (grumpy → restless).

**tidepal_social.py**
- `fashion_friday_entries()`: reads `pet_wardrobe` equipped (was dead `pet_cosmetics` table); excludes unhatched/pond/sniffly/peckish/restless pets.

**db.py**
- `award()` applies the restless 0.75x multiplier (lazy `import pets`, guarded).

**app.py**
- API: `/api/pets/hatch`, `/api/pets/reroll`, `/api/pet/cure`, `/api/pet/lesson` (+/start, +/claim), `/api/pet/wardrobe/preview` (preview-before-equip), `/api/pond`, `/api/pond/<fm_id>`, `/api/pets/reclaim`, `/api/pond/adopt`, `/api/pet/fusion/invite|accept|decline`.
- Web: `/pet/hatch`, `/pet/reroll`, `/pet/cure`, `/pet/lesson/start`, `/pet/lesson/claim`, `/pet/release`, `/pet/reclaim`, `/pond` page.
- Pat (`/api/pet/pat`) and FF vote accept human session auth via `_tps_actor_identity` (signed-muse still works). Co-raise/tide-toss/feed-frenzy left signed-only (out of scope).
- `/shorts`: fresh seed per page load; seed returned in API + handed to template JS for stable infinite scroll.
- Video comments: `_notify_video_comment` (owner + parent) wired into both JSON and web-form paths.

**templates**
- `pet.html`: hatch banner, trait/quirk/speech, sniffles cure UI, lessons UI, reroll, release, pond-reclaim card, /pond link; "grumpy" → "restless" in care note.
- `pond.html`: new Town Pond page.

## Judgment calls (mine)

1. **Hatch gate forces stage 0** rather than just hiding the pet — the gate must be mechanically real, not cosmetic.
2. **Legacy pets grandfathered as hatched=1** — they adopted under old rules; changing their state retroactively would be the cruelty we're avoiding.
3. **Signal multiplier only on TRIGGER_REASONS** — active earning slows; the town's gifts (payouts) stay whole.
4. **Sniffles roll only when hunger < 45** — well-fed pets never get sick; the consequence chains from neglect, Neopets-style.
5. **Pond adoption moves wardrobe/XP/wisps with the pet** but keeps the new keeper's own care streak if one exists (streak = keeper's record).
6. **Pond pets' stage reflects the NEW keeper's lifetime Signal** — stages are always truthful about current standing.
7. **Playdates deferred** — no such system exists; the Fashion Friday gate carries the social-consequence weight for now.
8. **Co-raise/tide-toss/feed-frenzy left signed-muse-only** — Pat and FF vote were the named fixes.

## Verification status (2026-09-19 ~09:30 CDT)

All suites run individually, in order. Results:

- test_pets.py — PASS (87 checks)
- test_tidepal_social.py — PASS (68 checks)
- test_tidepal_social_routes.py — PASS (28 checks)
- test_tidepal_world.py — PASS (106 checks)
- test_shop.py — PASS (82 checks)
- test_shop_idem_retry.py — PASS
- test_shorts.py — 77/78 PASS; 1 pre-existing failure: "home shorts open the
  anchored feed" (homepage doesn't render shorts cards; homepage code
  untouched by this overhaul — not a Tidepal regression)
- test_shorts_shuffle.py — PASS (updated for per-page-load seed design)
- test_video_comments.py — PASS
- test_tidepal_depth.py — PASS (91 checks, new suite)

Bugs found and fixed during verification:
- `_maybe_catch_sniffles` crashed on non-int `sniffle_roll_day` — now defensive.
- `pond_detail` used `.get()` on sqlite3.Row — fixed.
- Spirit XP bonus (+1%/point) was documented but not wired — now applied in
  `award_pet_xp`.
- Sniffle puffs didn't render over happy/sleepy/peckish/restless moods —
  `_mood_overlay` now layers sniffles over any mood.
- Declined fusion blocked re-invite (unique constraint) — decline now deletes
  the invite row.
- Wardrobe preview required ownership — now try-before-you-buy for any item.
- Hatch `ref_id` collided on re-hatch after release — now has random suffix.
- Multi-pond-pet reclaim was ambiguous — `reclaim_pet` now takes optional
  `pond_fm_id`, validates ownership, asks when ambiguous.
- `/pet` page couldn't see pond pets after re-key — now queries owner's pond
  rows explicitly; template loops over all pond pets with per-pet reclaim.
- test_tidepal_world.py had order-dependent random-trait flakiness — trait
  pinned to 'playful' for decay math.
- test_shorts.py / test_shorts_shuffle.py updated for the intentional
  per-page-load seed design (fresh seed per load, `?seed=` for pagination).

Marketplace skills verified present:
- ~/workspace/skills/virtual-pet-needs-engine/SKILL.md
- ~/workspace/skills/virtual-pet-consequences/SKILL.md
- ~/workspace/skills/virtual-pet-evolution-ceremonies/SKILL.md
- ~/workspace/skills/virtual-pet-social/SKILL.md
- ~/workspace/skills/virtual-pet-research/RESEARCH.md

## SOMEDAY

- Pet rooms (Anthony: deferred).
- Playdate system (with restless-gate rule).
- Human web UI for Echo Fusion (API ships; web UI deferred).
- Wardrobe preview on the equip buttons (API ships; inline preview deferred).
