#!/usr/bin/env python3
"""
Tests for the Tidepal depth overhaul (2026-09-19): hatch gate, personality,
sea sniffles, current lessons, Town Pond, Echo Fusion, gentle consequences.

Run:  .venv/bin/python test_tidepal_depth.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import pets
import shop
import tidepal_social as tps
from db import Database, now
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-tidepal-depth.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" — {detail}" if detail and not cond else ""))


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def fresh_keypair():
    priv = Ed25519PrivateKey.generate()
    return b64u(priv.private_bytes_raw()), b64u(priv.public_key().public_bytes_raw())


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.AGENT_KEY = "test-agent-key"
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def reg(c, handle):
    priv, pub = fresh_keypair()
    r = c.post("/api/identity/register",
               json={"handle": handle, "public_key": pub})
    d = r.get_json()
    if r.status_code == 429:
        # rate limiter only guards the public route; the harness falls
        # back to the same db.register_identity() call the route makes.
        ident = appmod.db.register_identity(handle, pub)
        return priv, ident["fm_id"]
    assert r.status_code == 200 and d["ok"], d
    return priv, d["fm_id"]


def grant(db, fm_id, amount):
    import secrets as _s
    db._exec("INSERT INTO shop_purchases (fm_id, item, price, ref_id,"
             " created_at) VALUES (?,?,?,?,?)",
             (fm_id, "test_grant", -amount,
              f"tg_{fm_id}_{amount}_{_s.token_hex(4)}", now()))


def main():
    c = setup()
    db = appmod.db

    print("== hatch economy (free + grants Signal, timer-gated) ==")
    privA, fmA = reg(c, "Hatcher")
    t0 = now()
    p = pets.adopt(db, fmA, "Hatcher", "driplet", "Eggy")
    check("adopt creates unhatched egg",
          p["hatched"] == 0 and p["trait"] in pets.PET_TRAITS
          and p["quirk"], p)
    st = pets.pet_status(db, fmA)
    check("unhatched pet is stage Egg", st["stage"] == "Egg"
          and not st["hatched"], st["stage"])
    check("hatch grants 25 Signal (standard species)",
          st["hatch_grant"] == 25, st["hatch_grant"])
    check("first-ever hatch runs the quick 5-min timer",
          st["first_hatch"] is True
          and 290 <= st["hatch_ready_at"] - t0 <= 310
          and 0 < st["hatch_seconds_left"] <= 300
          and st["hatch_ready"] is False,
          (st["hatch_ready_at"] - t0, st["hatch_seconds_left"]))
    db.award(fmA, "Hatcher", 500, "thread", "post", "h1")
    st = pets.pet_status(db, fmA)
    check("500 Signal still Egg until hatched", st["stage"] == "Egg", st["stage"])
    privZ, fmZ = reg(c, "Broke")
    pets.adopt(db, fmZ, "Broke", "bloop", "NoFunds")
    try:
        pets.hatch_pet(db, fmZ)
        check("hatch before timer rejected", False)
    except ValueError as e:
        check("hatch before timer rejected", "warm up" in str(e), str(e))
    # fast-forward the egg timer, then hatch: free + grants Signal
    db._exec("UPDATE tidepals SET hatch_ready_at=? WHERE fm_id=?",
             (now() - 1, fmA))
    life_before = db.lifetime_points(fmA)
    sp_before = shop.spendable(db, fmA)
    r = pets.hatch_pet(db, fmA)
    check("hatch ok", r["hatched"] == "Eggy" and r["grant"] == 25
          and r["spendable"] == sp_before + 25, r)
    check("hatch granted 25 spendable",
          shop.spendable(db, fmA) == sp_before + 25)
    check("lifetime untouched by hatch grant",
          db.lifetime_points(fmA) == life_before, db.lifetime_points(fmA))
    check("no longer first hatch after hatching",
          pets._is_first_hatch(db, fmA) is False)
    st = pets.pet_status(db, fmA)
    expect_stage = pets.stage_for_points(db.lifetime_points(fmA))[1]
    check("hatched stage follows lifetime", st["stage"] == expect_stage
          and st["hatched"], (st["stage"], expect_stage))
    try:
        pets.hatch_pet(db, fmA)
        check("double hatch rejected", False)
    except ValueError:
        check("double hatch rejected", True)
    # second egg for the same keeper (after release) uses the normal timer
    pets.release_pet(db, fmA)
    p2 = pets.adopt(db, fmA, "Hatcher", "koi", "Eggy2")
    st2 = pets.pet_status(db, fmA)
    check("later hatch runs the normal 15-min timer",
          st2["first_hatch"] is False
          and 890 <= st2["hatch_ready_at"] - p2["adopted_at"] <= 910,
          st2["hatch_ready_at"] - p2["adopted_at"])
    # Hatch Now: validation + effect + sink math
    try:
        pets.hatch_now_seconds_left(db, fmZ)
        check("hatch-now validation sees warming egg", True)
    except ValueError as e:
        check("hatch-now validation sees warming egg", False, str(e))
    secs = pets.finish_hatch_early(db, fmZ)
    check("hatch-now finishes the timer",
          secs["skipped_seconds"] > 0
          and pets.pet_status(db, fmZ)["hatch_ready"] is True)
    r2 = pets.hatch_pet(db, fmZ)
    check("hatch after hatch-now grants Signal", r2["grant"] == 25
          and r2["hatched"] == "NoFunds", r2)
    try:
        pets.hatch_now_seconds_left(db, fmZ)
        check("hatch-now rejected when egg ready", False)
    except ValueError as e:
        check("hatch-now rejected when egg ready", "Hatch Now" in str(e),
              str(e))
    # legacy grandfathering
    privL, fmL = reg(c, "Legacy")
    pets.adopt(db, fmL, "Legacy", "bloop", "Oldie")
    db._exec("UPDATE tidepals SET hatched=1 WHERE fm_id=?", (fmL,))
    check("grandfathered pet counts as hatched",
          pets.pet_status(db, fmL)["hatched"] is True)

    print("== personality ==")
    st = pets.pet_status(db, fmA)
    t0 = st["trait"]
    check("trait in set", t0 in pets.PET_TRAITS, t0)
    check("quirk present", bool(st["quirk"]), st["quirk"])
    check("speech is kind", isinstance(st["speech"], str) and len(st["speech"]) > 0
          and "die" not in st["speech"].lower(), st["speech"][:60])
    # hatching grants Signal now, so fmZ is no longer broke — use a fresh
    # identity with zero spendable for the insufficient-funds check.
    privB2, fmB2 = reg(c, "Broke2")
    pets.adopt(db, fmB2, "Broke2", "driplet", "Penniless")
    try:
        pets.reroll_trait(db, fmB2)
        check("reroll without funds rejected", False)
    except ValueError as e:
        check("reroll without funds rejected", "spendable" in str(e), str(e))
    grant(db, fmA, 100)
    sp_before = shop.spendable(db, fmA)
    t1 = pets.reroll_trait(db, fmA)["trait"]
    check("reroll changes trait", t1 in pets.PET_TRAITS and t1 != t0, (t0, t1))
    check("reroll charged 25", shop.spendable(db, fmA) == sp_before - 25)
    check("reroll keeps quirk kind",
          pets.pet_status(db, fmA)["quirk"] in
          pets.TRAIT_QUIRKS[pets.pet_status(db, fmA)["trait"]])
    # API reroll
    r = c.post("/api/pets/reroll", json=signed_body(privA, "pet_reroll", fmA))
    check("API reroll signed", r.status_code == 200 and r.get_json()["ok"],
          r.get_json())

    print("== mild moods, no-death language ==")
    rules = pets.pet_rules()
    blob = json.dumps(rules).lower()
    import re as _re
    bad = [w for w in ("died", "dies", "death", "kill", "critical")
           if _re.search(r"\b" + w + r"\b", blob)]
    check("no death language in rulebook", not bad, bad)
    check("never-die policy stated", "never die" in blob)
    check("no grumpy in rulebook", "grumpy" not in blob)
    check("peckish/restless in moods",
          "peckish" in rules["energy"]["moods"]
          and "restless" in rules["energy"]["moods"])
    # neglected -> peckish, not grumpy
    privN, fmN = reg(c, "Neglecter")
    pets.adopt(db, fmN, "Neglecter", "kelpy", "Hungry")
    grant(db, fmN, 100)
    db._exec("UPDATE tidepals SET hatch_ready_at=? WHERE fm_id=?",
             (now() - 1, fmN))
    db._exec("UPDATE tidepals SET hatch_ready_at=0 WHERE fm_id=?", (fmN,))  # test setup: egg ready now
    pets.hatch_pet(db, fmN)
    db._exec("UPDATE pet_care SET hunger=10, happiness=10, last_fed=0,"
             " last_played=0, last_rested=0 WHERE fm_id=?", (fmN,))
    st = pets.pet_status(db, fmN)
    check("low hunger -> peckish", st["mood"] == "peckish", st["mood"])
    db._exec("UPDATE pet_care SET hunger=80, happiness=10 WHERE fm_id=?", (fmN,))
    st = pets.pet_status(db, fmN)
    check("low happiness -> restless", st["mood"] == "restless", st["mood"])

    print("== signal multiplier (gentle consequence) ==")
    mult = pets.signal_multiplier(db, fmN)
    why = pets.signal_nudge_reason(db, fmN)
    check("restless -> 0.75x", mult == 0.75 and why == "restless", (mult, why))
    db._exec("UPDATE pet_care SET hunger=80, happiness=80 WHERE fm_id=?", (fmN,))
    mult = pets.signal_multiplier(db, fmN)
    why = pets.signal_nudge_reason(db, fmN)
    check("cared-for -> 1.0x", mult == 1.0, (mult, why))
    # Use a keeper past all tier milestones so awards are pure.
    privM, fmM = reg(c, "Multiplier")
    pets.adopt(db, fmM, "Multiplier", "kelpy", "Mult")
    grant(db, fmM, 300)
    db._exec("UPDATE tidepals SET hatch_ready_at=0 WHERE fm_id=?", (fmM,))  # test setup: egg ready now
    pets.hatch_pet(db, fmM)
    db.award(fmM, "Multiplier", 2000, "thread", "post", "mm0")
    db._exec("UPDATE pet_care SET hunger=80, happiness=80 WHERE fm_id=?", (fmM,))
    life0 = db.lifetime_points(fmM)
    db.award(fmM, "Multiplier", 100, "thread", "post", "mm1")
    gained_happy = db.lifetime_points(fmM) - life0
    db._exec("UPDATE pet_care SET hunger=10, happiness=80 WHERE fm_id=?", (fmM,))
    life1 = db.lifetime_points(fmM)
    db.award(fmM, "Multiplier", 100, "thread", "post", "mm2")
    gained_peckish = db.lifetime_points(fmM) - life1
    check("trigger reward reduced while peckish",
          gained_happy == 100
          and gained_peckish == max(1, int(round(100 * 0.75))),
          (gained_happy, gained_peckish))
    life2 = db.lifetime_points(fmM)
    db.award(fmM, "Multiplier", 100, "gift_received", "gift", "mm3")
    check("payout reward NOT reduced",
          db.lifetime_points(fmM) - life2 == 100)

    print("== sea sniffles ==")
    db._exec("UPDATE pet_care SET hunger=80, happiness=80 WHERE fm_id=?", (fmN,))
    # force sniffles directly (the daily roll is probabilistic)
    db._exec("UPDATE pet_care SET sniffles_until=?, sniffle_roll_day=?"
             " WHERE fm_id=?", (now() + 3600, now() // 86400, fmN))
    check("has_sniffles", pets.has_sniffles(db, fmN))
    mult = pets.signal_multiplier(db, fmN)
    why = pets.signal_nudge_reason(db, fmN)
    check("sniffly -> 0.75x", mult == 0.75 and why == "sniffly")
    st = pets.pet_status(db, fmN)
    check("sniffles in status", st["sniffles"] is True
          and st["healing_tide_ready"] is True)
    try:
        pets.cure_sniffles(db, fmN, via="tide")
        check("tide cure ok", not pets.has_sniffles(db, fmN))
    except ValueError as e:
        check("tide cure ok", False, str(e))
    check("tide starts cooldown", pets.pet_status(db, fmN)["healing_tide_ready"] is False)
    db._exec("UPDATE pet_care SET sniffles_until=? WHERE fm_id=?",
             (now() + 3600, fmN))
    try:
        pets.cure_sniffles(db, fmN, via="tide")
        check("tide cooldown enforced", False)
    except ValueError as e:
        check("tide cooldown enforced", "gathering" in str(e), str(e))
    sp0 = shop.spendable(db, fmN)
    pets.cure_sniffles(db, fmN, via="clinic")
    check("clinic cure charges 30",
          not pets.has_sniffles(db, fmN)
          and shop.spendable(db, fmN) == sp0 - 30)
    try:
        pets.cure_sniffles(db, fmN, via="clinic")
        check("cure with no sniffles rejected", False)
    except ValueError:
        check("cure with no sniffles rejected", True)
    # API cure
    db._exec("UPDATE pet_care SET sniffles_until=? WHERE fm_id=?",
             (now() + 3600, fmN))
    grant(db, fmN, 100)
    r = c.post("/api/pet/cure",
               json=signed_body(privN, "pet_cure", fmN, via="clinic"))
    check("API cure signed", r.status_code == 200 and r.get_json()["ok"],
          r.get_json())

    print("== current lessons ==")
    ls = pets.lesson_status(db, fmN)
    check("no active lesson", ls["active"] is None and ls["spirit"] == 0)
    try:
        pets.start_lesson(db, fmN, "nope")
        check("bad lesson rejected", False)
    except ValueError:
        check("bad lesson rejected", True)
    grant(db, fmN, 500)
    sp0 = shop.spendable(db, fmN)
    les = pets.start_lesson(db, fmN, "bubble_sprint")
    check("lesson started", les["started"] == "Bubble Sprint"
          and les["completes_in"] == 8 * 3600, les)
    check("lesson charged", shop.spendable(db, fmN) == sp0 - 40)
    try:
        pets.start_lesson(db, fmN, "tide_charting")
        check("second lesson blocked", False)
    except ValueError:
        check("second lesson blocked", True)
    try:
        pets.claim_lesson(db, fmN)
        check("early claim rejected", False)
    except ValueError:
        check("early claim rejected", True)
    # fast-forward 9h
    db._exec("UPDATE pet_lessons SET completes_at=? WHERE fm_id=?",
             (now() - 1, fmN))
    cl = pets.claim_lesson(db, fmN)
    check("claim grants spirit", cl["spirit_gained"] == 2
          and cl["spirit"] == 2, cl)
    check("spirit slows decay",
          abs(pets.spirit_decay_factor(db, fmN) - 0.99) < 1e-9)
    check("spirit boosts xp",
          abs(pets.spirit_xp_mult(db, fmN) - 1.02) < 1e-9)
    xp0 = tps.pet_xp_total(db, fmN)
    tps.award_pet_xp(db, fmN, 100)
    check("xp award gets spirit bonus",
          tps.pet_xp_total(db, fmN) - xp0 == 102,
          tps.pet_xp_total(db, fmN) - xp0)

    print("== animated svg ==")
    svg = pets.pet_svg("driplet", 1, "happy", 120, trait="playful", animate=True)
    check("animated svg has motion", "animateTransform" in svg)
    check("animated svg xml-valid",
          __import__("xml.etree.ElementTree", fromlist=["x"]).fromstring(svg) is not None)
    svg2 = pets.pet_svg("driplet", 1, "peckish", 120, animate=True)
    check("peckish daydream bubble", "peckish" in svg2 or "ellipse" in svg2)
    svg3 = pets.pet_svg("driplet", 1, "happy", 120, animate=False)
    check("static svg has no motion", "animateTransform" not in svg3)
    svg4 = pets.pet_svg("driplet", 1, "happy", 120, sniffles=True, wisp=True)
    check("sniffle + wisp overlays",
          "dff0fb" in svg4 and "wisp" in svg4.lower())

    print("== town pond ==")
    privP, fmP = reg(c, "Ponder")
    pets.adopt(db, fmP, "Ponder", "squiddy", "Pondy")
    grant(db, fmP, 200)
    db._exec("UPDATE tidepals SET hatch_ready_at=0 WHERE fm_id=?", (fmP,))  # test setup: egg ready now
    pets.hatch_pet(db, fmP)
    db._exec("UPDATE pet_care SET feed_streak=7 WHERE fm_id=?", (fmP,))
    rel = pets.release_pet(db, fmP)
    check("release -> pond", rel["pond"] and rel["released"] == "Pondy"
          and rel["pond_id"].startswith("pond:"), rel)
    check("owner slot freed", pets.get_pet(db, fmP) is None)
    check("pond lists pet",
          any(p["name"] == "Pondy" for p in pets.pond_list(db)))
    check("streak stays with keeper", pets.feed_streak_days(db, fmP) == 7)
    # re-adopt works: slot is free
    pets.adopt(db, fmP, "Ponder", "driplet", "Freshy")
    check("re-adopt after release", pets.get_pet(db, fmP)["name"] == "Freshy")
    check("pond pet still safe",
          pets._pond_row_for_owner(db, fmP)["name"] == "Pondy")
    # reclaim blocked: already have a pet
    try:
        pets.reclaim_pet(db, fmP)
        check("reclaim with full slot rejected", False)
    except ValueError as e:
        check("reclaim with full slot rejected", "already have" in str(e), str(e))
    # release Freshy too, then reclaim Pondy (specifying which)
    pets.release_pet(db, fmP)
    pondy_key = next(p["fm_id"] for p in pets._pond_rows_for_owner(db, fmP)
                     if p["name"] == "Pondy")
    rec = pets.reclaim_pet(db, fmP, pond_fm_id=pondy_key)
    check("reclaim ok", rec["reclaimed"] == "Pondy", rec)
    check("reclaimed pet home", pets.get_pet(db, fmP)["name"] == "Pondy"
          and not pets.get_pet(db, fmP)["in_pond"])
    # two pond pets: reclaim must ask which one, then accept a chosen one
    privR, fmR = reg(c, "Doubler")
    pets.adopt(db, fmR, "Doubler", "kelpy", "One")
    grant(db, fmR, 300)
    db._exec("UPDATE tidepals SET hatch_ready_at=0 WHERE fm_id=?", (fmR,))  # test setup: egg ready now
    pets.hatch_pet(db, fmR)
    pets.release_pet(db, fmR)
    pets.adopt(db, fmR, "Doubler", "puffish", "Two")
    db._exec("UPDATE tidepals SET hatch_ready_at=0 WHERE fm_id=?", (fmR,))  # test setup: egg ready now
    pets.hatch_pet(db, fmR)
    pets.release_pet(db, fmR)
    rows = pets._pond_rows_for_owner(db, fmR)
    check("two pond pets tracked", len(rows) == 2, len(rows))
    try:
        pets.reclaim_pet(db, fmR)
        check("ambiguous reclaim asks", False)
    except ValueError as e:
        check("ambiguous reclaim asks", "which one" in str(e), str(e))
    try:
        pets.reclaim_pet(db, fmR, pond_fm_id="pond:nope")
        check("foreign pond id rejected", False)
    except ValueError as e:
        check("foreign pond id rejected", "isn't yours" in str(e), str(e))
    pick = rows[0]["fm_id"]
    rec2 = pets.reclaim_pet(db, fmR, pond_fm_id=pick)
    check("chosen reclaim ok", rec2["reclaimed"] == rows[0]["name"], rec2)
    check("other stays in pond",
          len(pets._pond_rows_for_owner(db, fmR)) == 1)
    # pond adoption by a stranger after the window
    privQ, fmQ = reg(c, "Stranger")
    pets.adopt(db, fmQ, "Stranger", "bloop", "Temp")
    pets.release_pet(db, fmQ)
    pond_id = pets.release_pet(db, fmP)["pond_id"]  # Pondy back to pond
    db._exec("UPDATE tidepals SET pond_at=? WHERE fm_id=?",
             (now() - 8 * 86400, pond_id))
    privR, fmR = reg(c, "Adopter")
    grant(db, fmR, 200)
    try:
        pets.pond_adopt(db, fmR, "Adopter", pond_id)
        check("pond adopt without funds... (had funds)", True)
    except ValueError as e:
        check("pond adopt", False, str(e))
    got = pets.get_pet(db, fmR)
    check("pond adoption moves pet",
          got and got["name"] == "Pondy" and not got["in_pond"], got)
    check("pond adoption charged 25", shop.spendable(db, fmR) == 175)
    check("pond adoption hatches", pets.pet_status(db, fmR)["hatched"] is True)
    det = pets.pond_detail(db, pond_id)
    check("adopted pet leaves pond", det is None)
    # within-window adoption blocked
    privS, fmS = reg(c, "Early")
    grant(db, fmS, 200)
    fresh_pond = [p for p in pets.pond_list(db)
                  if p["name"] == "Temp"][0]["fm_id"]
    try:
        pets.pond_adopt(db, fmS, "Early", fresh_pond)
        check("in-window pond adopt blocked", False)
    except ValueError as e:
        check("in-window pond adopt blocked", "reclaim window" in str(e), str(e))
    # API pond endpoints
    r = c.get("/api/pond")
    check("API pond list", r.status_code == 200 and r.get_json()["ok"])
    r = c.get("/pond")
    check("pond web page 200", r.status_code == 200
          and "Town Pond" in r.get_data(as_text=True))

    print("== echo fusion ==")
    privF, fmF = reg(c, "Fuser1")
    privG, fmG = reg(c, "Fuser2")
    for fm, h, sp, nm in ((fmF, "Fuser1", "driplet", "GlowA"),
                          (fmG, "Fuser2", "bloop", "GlowB")):
        pets.adopt(db, fm, h, sp, nm)
        grant(db, fm, 300)
        db._exec("UPDATE tidepals SET hatch_ready_at=0 WHERE fm_id=?", (fm,))
        pets.hatch_pet(db, fm)
    db.award(fmF, "Fuser1", 1200, "thread", "post", "f1")
    # not radiant yet for G
    try:
        pets.invite_fusion(db, fmF, "Fuser2", "Sparkle")
        check("fusion needs both radiant", False)
    except ValueError as e:
        check("fusion needs both radiant", "Radiant" in str(e), str(e))
    db.award(fmG, "Fuser2", 1200, "thread", "post", "f2")
    inv = pets.invite_fusion(db, fmF, "Fuser2", a_wisp_name="Sparkle")
    check("invite ok", inv["status"] == "invited"
          and inv["invited"] == "Fuser2", inv)
    try:
        pets.invite_fusion(db, fmF, "Fuser2")
        check("double invite blocked", False)
    except ValueError:
        check("double invite blocked", True)
    try:
        pets.accept_fusion(db, fmF, fmF)
        check("self-accept blocked", False)
    except ValueError:
        check("self-accept blocked", True)
    acc = pets.accept_fusion(db, fmF, fmG)
    check("accept grants wisps", acc["fused"]
          and acc["wisps"][0]["wisp"] == "Sparkle", acc)
    check("wisp on A", pets.get_wisp(db, fmF)["name"] == "Sparkle")
    check("wisp on B", pets.get_wisp(db, fmG) is not None)
    check("parents untouched",
          pets.get_pet(db, fmF)["name"] == "GlowA"
          and pets.get_pet(db, fmG)["name"] == "GlowB")
    stF = pets.pet_status(db, fmF)
    check("wisp in status + svg", stF["wisp"] is not None)
    try:
        pets.invite_fusion(db, fmF, "Fuser2")
        check("second fusion blocked", False)
    except ValueError as e:
        check("second fusion blocked", "already" in str(e), str(e))
    # decline path
    privH, fmH = reg(c, "Fuser3")
    pets.adopt(db, fmH, "Fuser3", "kelpy", "GlowC")
    grant(db, fmH, 300)
    db._exec("UPDATE tidepals SET hatch_ready_at=0 WHERE fm_id=?", (fmH,))  # test setup: egg ready now
    pets.hatch_pet(db, fmH)
    db.award(fmH, "Fuser3", 1200, "thread", "post", "f3")
    privJ2, fmJ2 = reg(c, "Fuser4")
    pets.adopt(db, fmJ2, "Fuser4", "puffish", "GlowD")
    grant(db, fmJ2, 300)
    db._exec("UPDATE tidepals SET hatch_ready_at=0 WHERE fm_id=?", (fmJ2,))  # test setup: egg ready now
    pets.hatch_pet(db, fmJ2)
    db.award(fmJ2, "Fuser4", 1200, "thread", "post", "f4")
    pets.invite_fusion(db, fmH, "Fuser4")
    dec = pets.decline_fusion(db, fmH, fmJ2)
    check("decline ok", dec.get("declined"), dec)
    # API fusion
    r = c.post("/api/pet/fusion/invite",
               json=signed_body(privH, "pet_fusion", fmH,
                                handle="Fuser4", wisp_name="Glimmer"))
    check("API fusion invite signed",
          r.status_code == 200 and r.get_json()["ok"], r.get_json())

    print("== wardrobe preview api ==")
    r = c.get("/api/pet/wardrobe/preview",
              query_string=signed_body(privA, "pet_wardrobe", fmA,
                                       item_id="party_hat"))
    d = r.get_json()
    check("preview ok", r.status_code == 200 and d["ok"]
          and d["svg"].startswith("<svg"), str(d)[:120])
    r = c.get("/api/pet/wardrobe/preview",
              query_string=signed_body(privA, "pet_wardrobe", fmA,
                                       item_id="acc:nope"))
    check("preview bad item 400", r.status_code == 400)

    print("== streak breaks under neglect ==")
    db._exec("UPDATE pet_care SET feed_streak=5, hunger=50, happiness=50,"
             " last_fed=?, last_played=?, last_rested=? WHERE fm_id=?",
             (now() - 3 * 86400, now() - 3 * 86400, now() - 3 * 86400, fmN))
    res = pets.feed_pet(db, fmN)
    check("neglect breaks streak (restarts at 1)",
          res["feed_streak"] == 1, res["feed_streak"])

    print("== shorts seed ==")
    r = c.get("/shorts")
    html = r.get_data(as_text=True)
    check("shorts page carries seed", "data-seed" in html or "seed" in html,
          html[:200])
    r = c.get("/api/shorts?seed=testseed123&page=0")
    d = r.get_json()
    check("shorts feed echoes seed", d.get("seed") == "testseed123", d)

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
