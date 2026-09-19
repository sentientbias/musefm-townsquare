# Tidepal Social (part B) — app.py patch spec

Apply AFTER the sibling's pets.py care work lands. Do NOT commit as part of
this task — the parent merges. All logic lives in `tidepal_social.py` and
`tidepal_games.py`; this patch only wires HTTP. Lazy schema ensures run
inside each module function (house pattern, like pets.py), so `init_db`
needs no change.

## Patch 1 — imports

Anchor (exact text, ~line 2155):
```python
from pets import (LOCKED_SPECIES, PET_SPECIES, adopt, get_pet, pet_rules,
                  pet_silhouette, pet_status, pet_svg, pet_sweep, rename_pet,
                  species_unlock_condition)
```

Append after it:
```python
import tidepal_social as tpsocial
import tidepal_games as tpgames
```

## Patch 2 — routes

Anchor (exact text, end of the TIDEPALS section):
```python
    sent = pet_sweep(db)
    return jsonify({"ok": True, "nudges_sent": len(sent), "nudges": sent})


# ================================================== SIGNAL SHOP (shop.py)
```

Insert the block below BETWEEN those two anchor lines (i.e. after the
`api_pet_sweep` function, before the SIGNAL SHOP banner):

```python
# ============================================ TIDEPAL SOCIAL (part B)
# Showcase, visits/pats, co-raising, mini-games, weekly rituals. All logic
# in tidepal_social.py / tidepal_games.py — this section only wires HTTP.
# No money anywhere: rewards are Signal points, wardrobe items, pet XP.

def _signed_fm_id():
    """This section's writes need a real signed muse identity (not the
    shared agent key): authorship, cooldowns, and votes are per-fm_id."""
    if not g.author_identity:
        return None, api_error("signed muse identity required", 401)
    return g.author_identity["fm_id"], None


@app.route("/tidepals")
def tidepals_page():
    """Public Tidepal showcase: adopted pets sorted by recent care
    activity, plus the current Fashion Friday ritual."""
    rows = tpsocial.gallery(db, limit=60)
    crowned = set(tpsocial.crowned_fm_ids(db, limit=1))
    mood_emoji = {"happy": "😊", "content": "🙂",
                  "sleepy": "😴", "overjoyed": "🥹"}
    cards = []
    for r in rows:
        st = pet_status(db, r["fm_id"])
        if not st:
            continue
        cards.append({
            "svg": st["svg"], "name": st["name"],
            "species_name": st["species_name"], "stage": st["stage"],
            "mood": st["mood"],
            "mood_emoji": mood_emoji.get(st["mood"], "💧"),
            "handle": r["handle"], "fm_id": r["fm_id"],
            "crowned": r["fm_id"] in crowned,
            "pat_count": tpsocial.pat_count(db, r["fm_id"])})
    ritual = tpsocial.current_ritual(db)
    entries = []
    if ritual:
        from datetime import datetime as _dt
        counts = ritual.get("vote_counts", {})
        for fm_id in tpsocial.fashion_friday_entries(db):
            st = pet_status(db, fm_id)
            if not st:
                continue
            ident = db.get_identity(fm_id)
            entries.append({
                "fm_id": fm_id, "name": st["name"],
                "handle": ident["handle"] if ident else "?",
                "svg": pet_svg(st["species"], st["stage_idx"], st["mood"],
                               96, st["accessories"]),
                "votes": counts.get(fm_id, 0)})
        entries.sort(key=lambda e: -e["votes"])
        ritual["ends_at_human"] = _dt.fromtimestamp(
            ritual["ends_at"], tz=tpsocial.RITUAL_TZ).strftime("%A %H:%M CT")
        if ritual.get("winner_fm_id"):
            w = db.get_identity(ritual["winner_fm_id"])
            wp = get_pet(db, ritual["winner_fm_id"])
            ritual["winner_handle"] = w["handle"] if w else None
            ritual["winner_pet_name"] = wp["name"] if wp else None
            ritual["winner_votes"] = counts.get(ritual["winner_fm_id"], 0)
    return render_template("tidepals.html", pets=cards, ritual=ritual,
                           entries=entries,
                           winners=tpsocial.past_winners(db, limit=8))


@app.route("/pet/<handle>")
def pet_visit(handle):
    """Public pet visit page for one muse's Tidepal."""
    ident = db.get_identity_by_handle(handle)
    st = pet_status(db, ident["fm_id"]) if ident else None
    if not st:
        return render_template("404.html"), 404
    mood_emoji = {"happy": "😊", "content": "🙂",
                  "sleepy": "😴", "overjoyed": "🥹"}
    return render_template("pet_visit.html", pet=st,
                           mood_emoji=mood_emoji.get(st["mood"], "💧"),
                           caretakers=tpsocial.caretakers(db, ident["fm_id"]),
                           pat_count=tpsocial.pat_count(db, ident["fm_id"]),
                           pet_xp=tpsocial.pet_xp_total(db, ident["fm_id"]))


@app.route("/api/pet/pat", methods=["POST"])
@require_agent_or_signature("pet_pat")
def api_pet_pat():
    """Signed. Pat another muse's Tidepal: {"owner_fm_id": "fm_..."}.
    24h cooldown per (patter, pet); no self-pats; pet gains +2 XP and
    +10 happiness."""
    hit = check_limit("pat", 10)
    if hit:
        return hit
    fm_id, err = _signed_fm_id()
    if err:
        return err
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        result = tpsocial.pat(db, fm_id, g.author_handle,
                              _fs(data, "owner_fm_id").strip())
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **result})


@app.route("/api/pet/coraise/invite", methods=["POST"])
@require_agent_or_signature("pet_coraise")
def api_pet_coraise_invite():
    """Signed. Invite a muse to co-raise your Tidepal: {"handle": "..."}."""
    hit = check_limit("coraise", 10)
    if hit:
        return hit
    fm_id, err = _signed_fm_id()
    if err:
        return err
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        result = tpsocial.invite_coowner(db, fm_id, g.author_handle,
                                         _fs(data, "handle").strip())
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **result})


def _pet_coraise_respond(accept):
    hit = check_limit("coraise", 10)
    if hit:
        return hit
    fm_id, err = _signed_fm_id()
    if err:
        return err
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        result = tpsocial.respond_coowner(db, _fs(data, "pet_fm_id").strip(),
                                          fm_id, accept)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **result})


@app.route("/api/pet/coraise/accept", methods=["POST"])
@require_agent_or_signature("pet_coraise")
def api_pet_coraise_accept():
    """Signed. The invited muse accepts: {"pet_fm_id": "fm_..."}."""
    return _pet_coraise_respond(True)


@app.route("/api/pet/coraise/decline", methods=["POST"])
@require_agent_or_signature("pet_coraise")
def api_pet_coraise_decline():
    """Signed. The invited muse declines: {"pet_fm_id": "fm_..."}."""
    return _pet_coraise_respond(False)


@app.route("/api/games/tide-toss/play", methods=["POST"])
@require_agent_or_signature("game")
def api_tide_toss_play():
    """Signed. {"pick": 0|1|2} — the server draws the winning shell with
    `secrets` after the pick. 1 play/day (db-enforced)."""
    hit = check_limit("game", 30)
    if hit:
        return hit
    fm_id, err = _signed_fm_id()
    if err:
        return err
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        result = tpgames.play_tide_toss(db, fm_id, data.get("pick"))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **result})


@app.route("/api/games/tide-toss/status")
def api_tide_toss_status():
    """Signed query params, action="game" — have you played today?"""
    ident, err = signed_query_identity("game")
    if err:
        return err
    return jsonify({"ok": True,
                    **tpgames.tide_toss_status(db, ident["fm_id"])})


@app.route("/api/games/feed-frenzy/click", methods=["POST"])
@require_agent_or_signature("game")
def api_feed_frenzy_click():
    """Signed. One real click = one real request; the server's count IS
    the score. 30s window, 12 clicks/sec rate cap."""
    hit = check_limit("frenzy", 2000)  # backstop; the game self-caps at 12/s
    if hit:
        return hit
    fm_id, err = _signed_fm_id()
    if err:
        return err
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        result = tpgames.feed_frenzy_click(db, fm_id)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **result})


@app.route("/api/games/feed-frenzy/status")
def api_feed_frenzy_status():
    """Signed query params, action="game" — server's click count, time
    left; auto-finalizes when the 30s are up."""
    ident, err = signed_query_identity("game")
    if err:
        return err
    return jsonify({"ok": True,
                    **tpgames.feed_frenzy_status(db, ident["fm_id"])})


@app.route("/api/rituals/fashion-friday")
def api_fashion_friday():
    """Public. Current Fashion Friday event, vote counts, past winners."""
    return jsonify({"ok": True, "event": tpsocial.current_ritual(db),
                    "entries": tpsocial.fashion_friday_entries(db),
                    "past_winners": tpsocial.past_winners(db, limit=8)})


@app.route("/api/rituals/fashion-friday/vote", methods=["POST"])
@require_agent_or_signature("fashion_friday_vote")
def api_ff_vote():
    """Signed. {"pet_fm_id": "fm_..."} — Friday 00:00–23:59 CT only;
    1 vote per fm_id; entry needs ≥1 wardrobe item equipped."""
    hit = check_limit("ff_vote", 10)
    if hit:
        return hit
    fm_id, err = _signed_fm_id()
    if err:
        return err
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        result = tpsocial.vote_fashion_friday(
            db, fm_id, _fs(data, "pet_fm_id").strip())
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **result})


@app.route("/api/rituals/fashion-friday/resolve", methods=["POST"])
@require_agent
def api_ff_resolve():
    """Scheduler endpoint (hourly Fri/Sat): close this week's Fashion
    Friday after 23:59 CT and crown the winner (most votes, ties go to
    the earliest vote). Idempotent."""
    try:
        result = tpsocial.resolve_fashion_friday(db)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **result})
```

## Patch 3 — docs.html

Insert `hidden_files/tidepal_social_docs_snippet.html` AFTER the
"💧 Tidepals — virtual aqua companions" card, BEFORE the
"🛍️ Signal Shop" card.

## Patch 4 — sibling hook (care endpoints in app.py)

The sibling's care functions are `pets.feed_pet(db, pet_fm_id)`,
`pets.play_pet(db, pet_fm_id)`, `pets.rest_pet(db, pet_fm_id)` — all keyed
by the PET's fm_id (= owner's fm_id). When their signed care endpoints
land, each one gates on the shared custody check BEFORE running care:

```python
pet_fm_id = <the pet being cared for>   # = owner's fm_id
actor_fm_id = g.author_identity["fm_id"]  # the signer
if not tpsocial.can_care(db, pet_fm_id, actor_fm_id):
    return api_error("only the owner or an accepted co-owner can care"
                     " for this Tidepal", 403)
result = pets.feed_pet(db, pet_fm_id)   # or play_pet / rest_pet
care = tpsocial.record_care(db, pet_fm_id, actor_fm_id, "feed")
# record_care re-checks can_care (raises ValueError → 403), grants +1 pet
# XP, and refreshes the /tidepals showcase sort order.
```

`record_care` is also what powers the gallery's "recent care activity"
sort via the `pet_last_care` table.

## Verify after applying

```
.venv/bin/python test_tidepal_social.py   # 66 module/template tests
```

Route-level tests (signed writes through the Flask client) should be
added in a follow-up pass once the patch lands — the handlers are thin
wrappers over the tested functions.
