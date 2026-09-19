#!/usr/bin/env python3
"""
Tests for the Tidepal world expansion (part A): wardrobe, deeper care,
6 new species, visual evolution.

Run:  .venv/bin/python test_tidepal_world.py
Throwaway SQLite db + Flask test client + Ed25519 signed requests.
Nothing touches townsquare.db.

NOTE: the new routes below are the app.py PATCH SPEC, mounted verbatim
onto the test app. The parent agent applies the same block to app.py.
"""
import base64
import os
import sys
import time
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import pets
import shop
from identity import IdentityError, signed_body, verify_signed_body

TEST_DB = "/tmp/test-townsquare-tidepal-world.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def fresh_keypair():
    priv = Ed25519PrivateKey.generate()
    return b64u(priv.private_bytes_raw()), b64u(priv.public_key().public_bytes_raw())


def _raises(fn):
    try:
        fn()
        return False
    except ValueError:
        return True


# ===========================================================================
# PATCH SPEC — this exact block goes into app.py (after api_pet_sweep,
# before the SIGNAL SHOP section). Imports to extend: the `from pets
# import (...)` line gains: buy_wardrobe_item, care_status, earn_item,
# equip_item, equipped_wardrobe, feed_pet, play_pet, release_pet, rest_pet,
# wardrobe_catalog.
# ===========================================================================
def mount_tidepal_world_routes():
    app = appmod.app
    db = appmod.db
    # These routes are now integrated into app.py itself (the harness's
    # copies below were the spec they were built from). Skip remounting so
    # the route-level tests below exercise the REAL routes instead of
    # colliding with them.
    existing = {r.rule for r in app.url_map.iter_rules()}
    if "/api/pet/wardrobe" in existing:
        return

    @app.route("/api/pet/wardrobe")
    def api_pet_wardrobe():
        """Signed. Your wardrobe catalog: every item, its unlock condition,
        what you own, and what's equipped."""
        ident, err = appmod.signed_query_identity("pet_wardrobe")
        if err:
            return err
        return appmod.jsonify({
            "ok": True,
            "catalog": pets.wardrobe_catalog(db, ident["fm_id"]),
            "equipped": pets.equipped_wardrobe(db, ident["fm_id"]),
        })

    @app.route("/api/pet/wardrobe/equip", methods=["POST"])
    def api_pet_wardrobe_equip():
        """Signed (action="pet_wardrobe"). Equip an owned wardrobe item —
        {"item_id": "party_hat"} — or unequip a slot: {"slot": "hat"}."""
        data = appmod.json_body()
        if not isinstance(data, dict):
            return data
        try:
            ident = verify_signed_body(data, db,
                                       expected_action="pet_wardrobe")
        except IdentityError as e:
            return appmod.api_error(f"musefm-v1 auth failed: {e}", 401)
        try:
            equipped = pets.equip_item(db, ident["fm_id"],
                                       appmod._fs(data, "item_id") or None,
                                       appmod._fs(data, "slot") or None)
        except ValueError as e:
            return appmod.api_error(str(e))
        return appmod.jsonify({"ok": True, "equipped": equipped,
                               "pet": pets.pet_status(db, ident["fm_id"])})

    @app.route("/api/pet/wardrobe/buy", methods=["POST"])
    def api_pet_wardrobe_buy():
        """Signed (action="pet_wardrobe_buy"). Buy a shop-unlock wardrobe
        item with spendable Signal — {"item_id": "cozy_beanie"}. Lifetime
        Signal never decreases; the charge lands in the shop_purchases
        ledger. No USD, no money, anywhere."""
        data = appmod.json_body()
        if not isinstance(data, dict):
            return data
        try:
            ident = verify_signed_body(data, db,
                                       expected_action="pet_wardrobe_buy")
        except IdentityError as e:
            return appmod.api_error(f"musefm-v1 auth failed: {e}", 401)
        try:
            res = pets.buy_wardrobe_item(db, ident["fm_id"],
                                         appmod._fs(data, "item_id").strip())
        except ValueError as e:
            return appmod.api_error(str(e))
        return appmod.jsonify({"ok": True, **res,
                               "pet": pets.pet_status(db, ident["fm_id"])})

    def _care_route(kind):
        data = appmod.json_body()
        if not isinstance(data, dict):
            return data
        try:
            ident = verify_signed_body(data, db,
                                       expected_action="pet_care")
        except IdentityError as e:
            return appmod.api_error(f"musefm-v1 auth failed: {e}", 401)
        try:
            res = {"feed": pets.feed_pet, "play": pets.play_pet,
                   "rest": pets.rest_pet}[kind](db, ident["fm_id"])
        except ValueError as e:
            return appmod.api_error(str(e))
        return appmod.jsonify({"ok": True, **res,
                               "pet": pets.pet_status(db, ident["fm_id"])})

    @app.route("/api/pet/feed", methods=["POST"])
    def api_pet_feed():
        """Signed (action="pet_care"). Feed your Tidepal: +25 hunger,
        +5 happiness, 4h cooldown. Consecutive-day streaks earn wardrobe."""
        return _care_route("feed")

    @app.route("/api/pet/play", methods=["POST"])
    def api_pet_play():
        """Signed (action="pet_care"). Play: +20 happiness, -5 hunger,
        2h cooldown."""
        return _care_route("play")

    @app.route("/api/pet/rest", methods=["POST"])
    def api_pet_rest():
        """Signed (action="pet_care"). Rest: +10 happiness, +5 hunger,
        8h cooldown."""
        return _care_route("rest")

    @app.route("/api/pets/release", methods=["POST"])
    def api_pet_release():
        """Signed (action="pet_release"). Release your Tidepal to the town
        pond. The feed streak survives — it's your record."""
        data = appmod.json_body()
        if not isinstance(data, dict):
            return data
        try:
            ident = verify_signed_body(data, db,
                                       expected_action="pet_release")
        except IdentityError as e:
            return appmod.api_error(f"musefm-v1 auth failed: {e}", 401)
        try:
            res = pets.release_pet(db, ident["fm_id"])
        except ValueError as e:
            return appmod.api_error(str(e))
        return appmod.jsonify({"ok": True, **res})

    # --- web care routes (logged-in humans; muses use the signed API) ---
    def _web_care(kind, label):
        ident = appmod.current_session_identity()
        if not ident:
            appmod.session["_pet_flash"] = ("Log in to care for your Tidepal.",
                                             True)
            return appmod.redirect("/pet")
        if not appmod._check_csrf():
            return "bad form token — reload and try again", 403
        try:
            {"feed": pets.feed_pet, "play": pets.play_pet,
             "rest": pets.rest_pet}[kind](db, ident["fm_id"])
        except ValueError as e:
            appmod.session["_pet_flash"] = (str(e), True)
            return appmod.redirect("/pet")
        appmod.session["_pet_flash"] = (f"💧 {label}", False)
        return appmod.redirect("/pet")

    @app.route("/pet/feed", methods=["POST"])
    def pet_web_feed():
        return _web_care("feed", "Yum! Your Tidepal is happily fed.")

    @app.route("/pet/play", methods=["POST"])
    def pet_web_play():
        return _web_care("play", "Wheee! Playtime is the best time.")

    @app.route("/pet/rest", methods=["POST"])
    def pet_web_rest():
        return _web_care("rest", "Shhh… your Tidepal is napping.")


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.AGENT_KEY = "test-agent-key"
    appmod.app.config["TESTING"] = True
    mount_tidepal_world_routes()
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


def main():
    c = setup()
    db = appmod.db

    print("== wardrobe catalog ==")
    cat = pets.wardrobe_catalog(db)
    check("17 wardrobe items", len(cat) == 17, len(cat))
    check("slots valid",
          all(e["slot"] in pets.WARDROBE_SLOTS for e in cat))
    check("every item has art + text",
          all(e["item_id"] in pets._WARDROBE_OVERLAY and e["unlock_text"]
              and e["description"] for e in cat))
    check("shop items priced, others not",
          all(("price" in e) == e["unlock"].startswith("shop:")
              for e in cat))
    bad = []
    for e in cat:
        try:
            ET.fromstring('<svg xmlns="http://www.w3.org/2000/svg">'
                          + pets._WARDROBE_OVERLAY[e["item_id"]]() + '</svg>')
        except Exception as ex:
            bad.append((e["item_id"], str(ex)))
    check("all 17 overlays XML-valid", not bad, bad[:2])
    check("unlock kinds known",
          all(e["unlock"].split(":")[0] in
              ("shop", "care_streak", "stage", "game", "seasonal", "event")
              for e in cat))

    print("== buy wardrobe with Signal ==")
    privA, fmA = reg(c, "WardrobeMuse")
    pets.adopt(db, fmA, "WardrobeMuse", "driplet", "Drippy")
    r = c.post("/api/pet/wardrobe/buy", json=signed_body(
        privA, "pet_wardrobe_buy", fmA, item_id="cozy_beanie"))
    check("buy with 0 Signal -> 400", r.status_code == 400,
          r.get_data(as_text=True)[:120])
    db.award(fmA, "WardrobeMuse", 100, "thread", "post", "w1")
    life_before = db.lifetime_points(fmA)
    spend_before = shop.spendable(db, fmA)
    r = c.post("/api/pet/wardrobe/buy", json=signed_body(
        privA, "pet_wardrobe_buy", fmA, item_id="nope_hat"))
    check("buy unknown item -> 400", r.status_code == 400)
    r = c.post("/api/pet/wardrobe/buy", json=signed_body(
        privA, "pet_wardrobe_buy", fmA, item_id="seaweed_crown"))
    check("buy non-shop item -> 400", r.status_code == 400,
          r.get_data(as_text=True)[:120])
    r = c.post("/api/pet/wardrobe/buy", json=signed_body(
        privA, "pet_wardrobe_buy", fmA, item_id="cozy_beanie"))
    d = r.get_json()
    check("buy cozy_beanie -> 200, charged 30",
          r.status_code == 200 and d["ok"] and d["charged"] == 30, d)
    check("spendable dropped by 30",
          d["spendable"] == spend_before - 30, d)
    check("lifetime Signal untouched by purchase (invariant)",
          db.lifetime_points(fmA) == life_before)
    check("bought item auto-equipped",
          d["equipped"].get("hat") == "cozy_beanie", d["equipped"])
    check("purchase ledgered",
          db._one("SELECT price FROM shop_purchases WHERE fm_id=? AND ref_id=?",
                  (fmA, "wardrobe:cozy_beanie"))["price"] == 30)
    r = c.post("/api/pet/wardrobe/buy", json=signed_body(
        privA, "pet_wardrobe_buy", fmA, item_id="cozy_beanie"))
    d = r.get_json()
    check("re-buy idempotent (no double charge)",
          r.status_code == 200 and d["already_owned"] and
          d["charged"] == 0 and db._one(
              "SELECT COUNT(*) n FROM shop_purchases WHERE fm_id=?",
              (fmA,))["n"] == 1, d)
    r = c.post("/api/pet/wardrobe/buy", json={"item_id": "kelp_scarf"})
    check("buy unsigned -> 401", r.status_code == 401)
    st = pets.pet_status(db, fmA)
    check("status svg layers the beanie",
          "cozy_beanie" in st["wardrobe"].values() and
          st["svg"].startswith("<svg"), st["wardrobe"])
    try:
        ET.fromstring(st["svg_large"])
        check("layered svg_large XML-valid", True)
    except Exception as e:
        check("layered svg_large XML-valid", False, str(e))

    print("== GET /api/pet/wardrobe ==")
    r = c.get("/api/pet/wardrobe",
              query_string=signed_body(privA, "pet_wardrobe", fmA))
    d = r.get_json()
    check("catalog 200 + 17 items",
          r.status_code == 200 and d["ok"] and len(d["catalog"]) == 17, d)
    beanie = [e for e in d["catalog"] if e["item_id"] == "cozy_beanie"][0]
    check("owned/equipped flags", beanie["owned"] and beanie["equipped"],
          beanie)
    crown = [e for e in d["catalog"] if e["item_id"] == "seaweed_crown"][0]
    check("unearned flagged", not crown["owned"] and not crown["equipped"]
          and "7 days" in crown["unlock_text"], crown["unlock_text"])
    r = c.get("/api/pet/wardrobe")
    check("wardrobe unsigned -> 401", r.status_code == 401)

    print("== equip: slot exclusivity + unlock gating ==")
    check("can't equip unearned",
          _raises(lambda: pets.equip_item(db, fmA, "party_hat")))
    r = c.post("/api/pet/wardrobe/equip", json=signed_body(
        privA, "pet_wardrobe", fmA, item_id="party_hat"))
    check("API equip unearned -> 400", r.status_code == 400)
    pets.earn_item(db, fmA, "party_hat", "event")
    eq = pets.equip_item(db, fmA, "party_hat")
    check("equip party_hat takes the hat slot",
          eq["hat"] == "party_hat", eq)
    check("cozy_beanie bumped (one per slot)",
          pets.equipped_wardrobe(db, fmA)["hat"] == "party_hat")
    eq = pets.equip_item(db, fmA, None, "hat")
    check("unequip slot", "hat" not in eq, eq)
    check("equip unknown item rejected",
          _raises(lambda: pets.equip_item(db, fmA, "nope")))
    check("unequip unknown slot rejected",
          _raises(lambda: pets.equip_item(db, fmA, None, "cape")))
    r = c.post("/api/pet/wardrobe/equip", json=signed_body(
        privA, "pet_wardrobe", fmA, item_id="party_hat"))
    d = r.get_json()
    check("API equip signed ok",
          r.status_code == 200 and d["equipped"]["hat"] == "party_hat", d)
    r = c.post("/api/pet/wardrobe/equip", json={"item_id": "party_hat"})
    check("equip unsigned -> 401", r.status_code == 401)

    print("== earn gating (reason must match unlock) ==")
    check("purchase reason on care item rejected",
          _raises(lambda: pets.earn_item(db, fmA, "seaweed_crown",
                                         "purchase")))
    check("care_streak reason without streak rejected",
          _raises(lambda: pets.earn_item(db, fmA, "seaweed_crown",
                                         "care_streak")))
    check("stage reason below stage rejected",
          _raises(lambda: pets.earn_item(db, fmA, "coral_cape", "stage")))
    check("seasonal reason off-season rejected",
          _raises(lambda: pets.earn_item(db, fmA, "moonlit_lagoon",
                                         "seasonal")))
    check("earn needs a pet",
          _raises(lambda: pets.earn_item(db, "fm_nobody", "kelp_scarf",
                                         "purchase")))
    r0 = pets.earn_item(db, fmA, "party_hat", "event")
    check("earn idempotent", r0["already_owned"] and not r0["earned"], r0)

    print("== deeper care: feed/play/rest ==")
    privB, fmB = reg(c, "CareMuse")
    check("care needs a pet",
          _raises(lambda: pets.feed_pet(db, fmB)))
    pets.adopt(db, fmB, "CareMuse", "bloop", "Bubbles")
    # Pin a neutral trait: decay math below assumes the base 12/day rate
    # (a random "calm" trait would soften it and break the numbers).
    db._exec("UPDATE tidepals SET trait='playful' WHERE fm_id=?", (fmB,))
    r = pets.feed_pet(db, fmB)
    check("feed: +25 hunger (cap), +5 happiness",
          r["hunger"] == 100 and r["happiness"] == 85 and
          r["feed_streak"] == 1, r)
    check("feed sets cooldown", r["feed_in"] > 0, r)
    check("feed twice in 4h refused",
          _raises(lambda: pets.feed_pet(db, fmB)))
    r = c.post("/api/pet/feed", json=signed_body(privB, "pet_care", fmB))
    check("API feed during cooldown -> 400", r.status_code == 400)
    r = pets.play_pet(db, fmB)
    check("play: +20 joy (cap), -5 hunger",
          r["happiness"] == 100 and r["hunger"] == 95, r)
    check("play cooldown enforced",
          _raises(lambda: pets.play_pet(db, fmB)))
    r = pets.rest_pet(db, fmB)
    check("rest: +10 joy (cap), +5 hunger (cap)",
          r["happiness"] == 100 and r["hunger"] == 100, r)
    check("rest cooldown enforced",
          _raises(lambda: pets.rest_pet(db, fmB)))
    r = c.post("/api/pet/play", json=signed_body(privB, "pet_care", fmB))
    check("API play during cooldown -> 400", r.status_code == 400)
    r = c.post("/api/pet/feed", json={"fm_id": fmB})
    check("API care unsigned -> 401", r.status_code == 401)
    cs = pets.care_status(db, fmB)
    check("care_status shape",
          cs["hunger"] == 100 and cs["happiness"] == 100 and
          cs["feed_streak"] == 1 and cs["feed_in"] > 0, cs)
    check("care_status None without pet",
          pets.care_status(db, "fm_nobody") is None)

    print("== neglect decay + mood shifts ==")
    # 6 days unfed: hunger 100 -> 100-72 = 28 < 30 -> peckish
    db._exec("UPDATE pet_care SET last_fed=?, last_played=?, last_rested=?"
             " WHERE fm_id=?",
             (int(time.time()) - 6 * 86400,) * 3 + (fmB,))
    st = pets.pet_status(db, fmB)
    check("neglect decays hunger", st["hunger"] == 28, st["hunger"])
    check("low hunger -> peckish mood", st["mood"] == "peckish", st["mood"])
    check("peckish face renders", "peckish" in st["svg"] or
          "#f59e0b" in st["svg"])
    # feed again (clear cooldown first): mood recovers
    db._exec("UPDATE pet_care SET last_fed=? WHERE fm_id=?",
             (int(time.time()) - 5 * 3600, fmB))
    pets.feed_pet(db, fmB)
    st = pets.pet_status(db, fmB)
    check("feeding cures peckish", st["mood"] != "peckish" and
          st["hunger"] == 100, (st["mood"], st["hunger"]))
    # low happiness, fine hunger -> grumpy
    db._exec("UPDATE pet_care SET happiness=50, last_fed=?, last_played=?"
             " WHERE fm_id=?",
             (int(time.time()), int(time.time()) - 6 * 86400, fmB))
    st = pets.pet_status(db, fmB)
    check("low happiness -> restless", st["mood"] == "restless",
          (st["mood"], st["hunger"], st["happiness"]))
    check("restless face renders", "restless" in st["svg"] or
          'Q' in st["svg"])
    # both low: hunger wins
    db._exec("UPDATE pet_care SET hunger=20, last_fed=? WHERE fm_id=?",
             (int(time.time()) - 6 * 86400, fmB))
    check("peckish outranks restless",
          pets.pet_status(db, fmB)["mood"] == "peckish")

    print("== feed streak auto-earns wardrobe ==")
    privC, fmC = reg(c, "StreakMuse")
    pets.adopt(db, fmC, "StreakMuse", "koi", "Streaky")
    db._exec("UPDATE pet_care SET last_fed=?, feed_streak=6 WHERE fm_id=?",
             (int(time.time()) - 86400 - 100, fmC))
    r = pets.feed_pet(db, fmC)
    check("7th consecutive day -> streak 7", r["feed_streak"] == 7, r)
    check("streak auto-earns seaweed_crown",
          r["earned"] == ["seaweed_crown"], r["earned"])
    check("streak notification stored",
          db._one("SELECT id FROM notifications WHERE fm_id=? AND type='pet'"
                  " AND ref_id='care:seaweed_crown'", (fmC,)) is not None)
    check("crown now equippable",
          pets.equip_item(db, fmC, "seaweed_crown")["hat"]
          == "seaweed_crown")
    # broken streak resets
    db._exec("UPDATE pet_care SET last_fed=?, feed_streak=4 WHERE fm_id=?",
             (int(time.time()) - 3 * 86400, fmC))
    r = pets.feed_pet(db, fmC)
    check("missed days reset streak", r["feed_streak"] == 1, r)

    print("== new species unlocks ==")
    privD, fmD = reg(c, "StageGate")
    check("crownjelly locked at 0 Signal",
          _raises(lambda: pets.adopt(db, fmD, "StageGate", "crownjelly",
                                     "Wob")))
    db.award(fmD, "StageGate", 200, "thread", "post", "sg1")
    pet = pets.adopt(db, fmD, "StageGate", "crownjelly", "Wob")
    check("crownjelly opens at Juvenile (200)",
          pet["species"] == "crownjelly", pet)
    privE, fmE = reg(c, "AbyssGate")
    db.award(fmE, "AbyssGate", 200, "thread", "post", "ag1")
    check("abyssal locked at Juvenile",
          _raises(lambda: pets.adopt(db, fmE, "AbyssGate", "abyssal",
                                     "Son")))
    db.award(fmE, "AbyssGate", 300, "thread", "post", "ag2")
    pet = pets.adopt(db, fmE, "AbyssGate", "abyssal", "Son")
    check("abyssal opens at Adult (500)", pet["species"] == "abyssal")
    privF, fmF = reg(c, "FrostGate")
    check("frostfin locked off-season (now %s)" % pets._current_season(),
          _raises(lambda: pets.adopt(db, fmF, "FrostGate", "frostfin",
                                     "Nip")))
    real_season = pets._current_season
    pets._current_season = lambda: "winter26"
    try:
        pet = pets.adopt(db, fmF, "FrostGate", "frostfin", "Nip")
        check("frostfin opens in winter26", pet["species"] == "frostfin")
    finally:
        pets._current_season = real_season
    # care-gated: streak 10, no pet yet (streak is the owner's record)
    privG, fmG = reg(c, "CareGate")
    check("kelpwarden locked without streak",
          _raises(lambda: pets.adopt(db, fmG, "CareGate", "kelpwarden",
                                     "Bri")))
    pets._care_row(db, fmG)
    db._exec("UPDATE pet_care SET feed_streak=10 WHERE fm_id=?", (fmG,))
    pet = pets.adopt(db, fmG, "CareGate", "kelpwarden", "Bri")
    check("kelpwarden opens at 10-day streak",
          pet["species"] == "kelpwarden", pet)
    # open species still open
    privH, fmH = reg(c, "OpenGate")
    pets.adopt(db, fmH, "OpenGate", "squiddy", "Inky")
    check("adopt squiddy", pets.get_pet(db, fmH)["species"] == "squiddy")

    print("== release -> Town Pond (no delete), slot frees, streak survives ==")
    privI, fmI = reg(c, "Releaser")
    pets.adopt(db, fmI, "Releaser", "driplet", "Drippy2")
    db._exec("UPDATE pet_care SET feed_streak=10 WHERE fm_id=?", (fmI,))
    res = pets.release_pet(db, fmI)
    check("release ok", res["released"] == "Drippy2" and res["pond"], res)
    check("pet row re-keyed to pond (not deleted)",
          pets.get_pet(db, fmI) is None
          and pets._pond_row_for_owner(db, fmI)["name"] == "Drippy2")
    check("pond lists the pet",
          any(p["name"] == "Drippy2" for p in pets.pond_list(db)))
    check("streak survives release (keeper's record)",
          pets.feed_streak_days(db, fmI) == 10)
    check("reclaim works",
          pets.reclaim_pet(db, fmI)["reclaimed"] == "Drippy2")
    check("reclaimed pet is home",
          pets.get_pet(db, fmI)["name"] == "Drippy2"
          and not pets.get_pet(db, fmI)["in_pond"])
    # Release again, then adopt fresh: slot is free, streak survives.
    pets.release_pet(db, fmI)
    pet = pets.adopt(db, fmI, "Releaser", "kelpwarden", "Bri2")
    check("adopt care-gated species after release",
          pet["species"] == "kelpwarden", pet)
    check("new pet gets fresh stats",
          pets.care_status(db, fmI)["hunger"] == 80)
    check("streak still the keeper's",
          pets.feed_streak_days(db, fmI) == 10)
    check("old pet still safe in pond",
          pets._pond_row_for_owner(db, fmI)["name"] == "Drippy2")
    check("release without pet rejected",
          _raises(lambda: pets.release_pet(db, "fm_nobody")))
    r = c.post("/api/pets/release", json=signed_body(
        privI, "pet_release", fmI))
    check("API release signed ok", r.status_code == 200 and
          r.get_json()["released"] == "Bri2", r.get_json())
    r = c.post("/api/pets/release", json=signed_body(
        privI, "pet_release", fmI))
    check("API release twice -> 400", r.status_code == 400)

    print("== visual evolution ==")
    privJ, fmJ = reg(c, "Evolver")
    pets.adopt(db, fmJ, "Evolver", "puffish", "Pip2")
    st = pets.pet_status(db, fmJ)
    check("no glow at adoption", st["stage_up_glow"] is False, st)
    # Hatch gate: award spendable, hatch, then the stage follows Signal.
    db._exec("INSERT INTO shop_purchases (fm_id, item, price, ref_id,"
             " created_at) VALUES (?,?,?,?,?)",
             (fmJ, "test_grant", -200, "tg_" + fmJ, int(time.time())))
    db._exec("UPDATE tidepals SET hatch_ready_at=0 WHERE fm_id=?", (fmJ,))  # test setup: egg ready now
    pets.hatch_pet(db, fmJ)
    db.award(fmJ, "Evolver", 60, "thread", "post", "ev1")
    st = pets.pet_status(db, fmJ)
    check("stage-up detected", st["stage"] == "Hatchling" and
          st["stage_idx"] == 1, st["stage"])
    check("celebration glow for 24h", st["stage_up_glow"] is True, st)
    check("gold aura in svg", "evo" in st["svg"], st["svg"][:200])
    check("evolution notification",
          db._one("SELECT id FROM notifications WHERE fm_id=? AND type='pet'"
                  " AND ref_id='stage:1'", (fmJ,)) is not None)
    try:
        ET.fromstring(st["svg_large"])
        check("celebration svg XML-valid", True)
    except Exception as e:
        check("celebration svg XML-valid", False, str(e))
    # 25h later: glow gone
    db._exec("UPDATE tidepals SET evolved_at=? WHERE fm_id=?",
             (int(time.time()) - 25 * 3600, fmJ))
    st = pets.pet_status(db, fmJ)
    check("glow expires after 24h", st["stage_up_glow"] is False, st)
    # legacy row (pre-wave-3): records silently, no false party
    privK, fmK = reg(c, "Legacy")
    db._exec("INSERT INTO tidepals (fm_id, species, name, adopted_at,"
             " evolved_at, evolved_stage) VALUES (?,?,?,?,?,?)",
             (fmK, "driplet", "Old", int(time.time()), 0, -1))
    db.award(fmK, "Legacy", 600, "thread", "post", "lg1")
    st = pets.pet_status(db, fmK)
    check("legacy row: no false celebration",
          st["stage_up_glow"] is False and st["stage_idx"] == 3,
          (st["stage_up_glow"], st["stage_idx"]))
    check("legacy row recorded silently",
          db._one("SELECT evolved_stage FROM tidepals WHERE fm_id=?",
                  (fmK,))["evolved_stage"] == 3)

    print("== pet_svg backward compatibility ==")
    import re as _re2
    norm = lambda s: _re2.sub(r'(id="|\(#|"#)[a-z]+[0-9]+', r'\1X', s)
    check("new kwargs default to old output",
          norm(pets.pet_svg("driplet", 2, "happy", 64)) ==
          norm(pets.pet_svg("driplet", 2, "happy", 64, (), (), False)))
    c1 = pets.pet_svg("bloop", 3, "content", 96, ["acc:sailor_hat"])
    check("shop accessories still layer",
          "translate(60 30)" in c1, c1[:200])
    check("unknown mood falls back to content",
          "content" in pets.pet_svg("koi", 1, "ecstatic", 64))
    check("overjoyed still glows",
          "aura" in pets.pet_svg("koi", 1, "overjoyed", 64))

    print("== rulebook ==")
    rules = pets.pet_rules()
    check("19 species in rulebook", len(rules["species"]) == 19)
    check("care + wardrobe sections",
          "care" in rules and "wardrobe" in rules)
    wtxt = str(rules["wardrobe"])
    check("no money anywhere in wardrobe rules",
          "$" not in wtxt and "never usd" in wtxt.lower())
    check("moods documented",
          "peckish" in rules["energy"]["moods"] and
          "restless" in rules["energy"]["moods"] and
          "grumpy" not in rules["energy"]["moods"])

    print("== web care routes (logged-in human) ==")
    r = c.post("/signup", data={"handle": "CareHuman",
                                "password": "s3cretpw!!",
                                "password_confirm": "s3cretpw!!"})
    assert r.status_code == 200, r.get_data(as_text=True)
    r = c.post("/login", data={"handle": "CareHuman",
                               "password": "s3cretpw!!"})
    assert r.status_code in (200, 302), r.get_data(as_text=True)
    r = c.post("/pet/adopt", data={"species": "driplet", "name": "Webby"},
               follow_redirects=True)
    assert r.status_code == 200 and "Webby" in r.get_data(as_text=True)
    import re as _re
    tok = _re.search(r'<meta name="csrf-token" content="([^"]+)">',
                     c.get("/").get_data(as_text=True)).group(1)
    r = c.post("/pet/feed", data={"csrf_token": tok}, follow_redirects=True)
    body = r.get_data(as_text=True)
    check("web feed works", "happily fed" in body or "Yum" in body,
          r.status_code)
    check("pet page shows hunger bar", "Hunger" in body, "Hunger" not in body)
    check("pet page shows happiness bar", "Happiness" in body)
    r = c.post("/pet/feed", data={"csrf_token": tok}, follow_redirects=True)
    check("web feed cooldown flashes", "full" in r.get_data(as_text=True))
    r = c.post("/pet/feed", data={"csrf_token": "bad"})
    check("web feed bad csrf -> 403", r.status_code == 403)

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
