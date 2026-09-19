#!/usr/bin/env python3
"""
Tests for Tidepal social layer (part B): showcase, visits/pats, co-raising,
mini-games, weekly rituals, pet XP.

Throwaway SQLite db + Flask test client (for identity registration) + temp
DATA_DIR. Nothing touches townsquare.db.

NOTE: the HTTP routes live in an app.py patch spec (see the parent task's
return item 3) and are NOT wired yet. These tests cover the pure functions
in tidepal_social.py / tidepal_games.py plus direct template rendering of
/tidepals and /pet/<handle>. Route-level tests should be added once the
patch lands (the handlers are thin wrappers over the functions below).

Run:  .venv/bin/python test_tidepal_social.py
"""
import base64
import datetime
import os
import shutil
import sys
import time
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import pets
import shop
import tidepal_social as tps
import tidepal_games as tpg

TEST_DB = "/tmp/test-townsquare-tidepals.db"
TEST_DATA = "/tmp/test-townsquare-tidepals-data"

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


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    # SHIM (test-only, no file edits): the sibling builder is mid-edit on
    # pets.py — adopt() references _care_row which isn't defined yet. Shim
    # it in-process until their care ledger lands.
    if not hasattr(pets, "_care_row"):
        pets._care_row = lambda db, fm_id: None
    from db import Database, ensure_human_auth_schema
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)  # mirrors app startup
    tps.ensure_tidepal_social_schema(appmod.db)
    tpg.ensure_game_schema(appmod.db)
    appmod.DATA_DIR = TEST_DATA
    appmod.UPLOAD_DIR = os.path.join(TEST_DATA, "uploads")
    os.makedirs(appmod.UPLOAD_DIR, exist_ok=True)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client(), appmod.db


def register(client, handle):
    priv_b64, pub_b64 = fresh_keypair()
    r = client.post("/api/identity/register",
                    json={"handle": handle, "public_key": pub_b64})
    assert r.status_code == 200, r.get_data(as_text=True)
    return priv_b64, r.get_json()["fm_id"]


def ct_ts(y, m, d, hh=12, mm=0):
    return int(datetime.datetime(y, m, d, hh, mm,
                                 tzinfo=ZoneInfo("America/Chicago")).timestamp())


def main():
    client, db = setup()

    print("== pats: cooldown + self-pat rejected ==")
    _, owner = register(client, "palowner")
    _, visitor = register(client, "palvisitor")
    _, loner = register(client, "palnoner")
    pets.adopt(db, owner, "palowner", "driplet", "Bubbles")
    pets.adopt(db, visitor, "palvisitor", "bloop", "Suds")
    # Hatch gate: test pets hatch so stage/mood/FF behavior matches adopted pets.
    for fm in (owner, visitor):
        db._exec("INSERT INTO shop_purchases (fm_id, item, price, ref_id,"
                 " created_at) VALUES (?,?,?,?,?)",
                 (fm, "test_grant", -200, "tg_" + fm, int(time.time())))
        db._exec("UPDATE tidepals SET hatch_ready_at=0 WHERE fm_id=?", (fm,))
        pets.hatch_pet(db, fm)
    check("test pets hatched",
          pets.pet_status(db, owner)["hatched"]
          and pets.pet_status(db, visitor)["hatched"])

    r = tps.pat(db, visitor, "palvisitor", owner)
    check("pat ok", r["patted"] == "Bubbles" and r["xp_added"] == 2, str(r))
    check("pat xp landed", tps.pet_xp_total(db, owner) == 2)
    check("pat happiness bumped",
          db._one("SELECT happiness FROM pet_care WHERE fm_id=?",
                  (owner,))["happiness"] == 90,  # 80 default + 10
          str(db._one("SELECT happiness FROM pet_care WHERE fm_id=?", (owner,))))
    check("pat count", tps.pat_count(db, owner) == 1)
    notifs = db.notifications_for(owner)
    check("pat notification", any("patted your Tidepal" in n["text"] for n in notifs),
          str([n["text"] for n in notifs]))
    try:
        tps.pat(db, visitor, "palvisitor", owner)
        check("pat cooldown", False, "second pat accepted!")
    except ValueError as e:
        check("pat cooldown", "24h" in str(e), str(e))
    try:
        tps.pat(db, owner, "palowner", owner)
        check("self-pat rejected", False, "self-pat accepted!")
    except ValueError as e:
        check("self-pat rejected", "own Tidepal" in str(e), str(e))
    try:
        tps.pat(db, visitor, "palvisitor", loner)
        check("pat unadopted rejected", False, "accepted!")
    except ValueError as e:
        check("pat unadopted rejected", "hasn't adopted" in str(e), str(e))

    print("== co-raise: invite/accept/decline + care gate ==")
    inv = tps.invite_coowner(db, owner, "palowner", "palvisitor")
    check("invite ok", inv["status"] == "invited" and inv["co_handle"] == "palvisitor")
    check("invite notification",
          any("co-raise" in n["text"] for n in db.notifications_for(visitor)))
    check("not caretaker before accept", not tps.is_caretaker(db, owner, visitor))
    try:
        tps.record_care(db, owner, visitor, "feed")
        check("non-coowner care rejected", False, "care accepted!")
    except ValueError as e:
        check("non-coowner care rejected", "co-owner" in str(e), str(e))
    try:
        tps.invite_coowner(db, owner, "palowner", "palvisitor")
        check("double invite rejected", False, "accepted!")
    except ValueError:
        check("double invite rejected", True)
    try:
        tps.invite_coowner(db, owner, "palowner", "nosuchhandle")
        check("invite unknown rejected", False, "accepted!")
    except ValueError:
        check("invite unknown rejected", True)
    try:
        tps.invite_coowner(db, owner, "palowner", "palowner")
        check("invite self rejected", False, "accepted!")
    except ValueError:
        check("invite self rejected", True)

    acc = tps.respond_coowner(db, owner, visitor, True)
    check("accept ok", acc["status"] == "accepted")
    check("caretaker after accept", tps.is_caretaker(db, owner, visitor))
    check("owner is caretaker", tps.is_caretaker(db, owner, owner))
    check("can_care alias", tps.can_care(db, owner, visitor))
    care = tps.record_care(db, owner, visitor, "feed")
    check("co-owner care works", care["recorded"] and care["action"] == "feed")
    check("care granted xp", tps.pet_xp_total(db, owner) == 3,  # 2 pat + 1 care
          str(tps.pet_xp_total(db, owner)))
    check("gallery sorts by care",
          tps.gallery(db)[0]["fm_id"] == owner,  # owner cared most recently
          str([g["fm_id"] for g in tps.gallery(db)]))
    caretakers = tps.caretakers(db, owner)
    check("caretakers listed",
          [c["role"] for c in caretakers] == ["owner", "co-owner"],
          str(caretakers))
    try:
        tps.respond_coowner(db, owner, visitor, True)
        check("re-accept rejected", False, "accepted!")
    except ValueError:
        check("re-accept rejected", True)

    # decline path on a second invite
    _, third = register(client, "palthird")
    tps.invite_coowner(db, owner, "palowner", "palthird")
    dec = tps.respond_coowner(db, owner, third, False)
    check("decline ok", dec["status"] == "declined")
    check("declined not caretaker", not tps.is_caretaker(db, owner, third))
    try:
        tps.respond_coowner(db, owner, loner, True)
        check("respond without invite rejected", False, "accepted!")
    except ValueError:
        check("respond without invite rejected", True)

    print("== tide toss: daily limit, server RNG ==")
    res = tpg.play_tide_toss(db, owner, 1)
    check("play returns shells", res["winning_shell"] in (0, 1, 2)
          and isinstance(res["won"], bool), str(res))
    check("play honest shape", res["pick"] == 1 and "day" in res)
    st = tpg.tide_toss_status(db, owner)
    check("status played_today", st["played_today"] is True)
    try:
        tpg.play_tide_toss(db, owner, 0)
        check("daily limit", False, "second play accepted!")
    except ValueError as e:
        check("daily limit", "per day" in str(e), str(e))
    try:
        tpg.play_tide_toss(db, visitor, 5)
        check("bad pick rejected", False, "accepted!")
    except ValueError:
        check("bad pick rejected", True)
    try:
        tpg.play_tide_toss(db, loner, 0)
        check("no-pet play rejected", False, "accepted!")
    except ValueError:
        check("no-pet play rejected", True)
    # server RNG is actually random over many draws (not client-pickable)
    wins = sum(tpg.secrets.randbelow(3) for _ in range(300))
    check("secrets rng sane", 150 < wins < 750, str(wins))

    print("== feed frenzy: window scoring + rate cap ==")
    c1 = tpg.feed_frenzy_click(db, visitor)
    check("first click", c1["clicks"] == 1 and c1["seconds_left"] > 0, str(c1))
    for _ in range(5):
        tpg.feed_frenzy_click(db, visitor)
    s = tpg.feed_frenzy_status(db, visitor)
    check("server counts clicks", s["active"] and s["clicks"] == 6, str(s))
    # rate cap: 12 marks stamped "now" -> next click must trip it
    db._exec("UPDATE feed_frenzy_sessions SET click_marks=? WHERE fm_id=?",
             (__import__("json").dumps([time.time()] * 12), visitor))
    try:
        tpg.feed_frenzy_click(db, visitor)
        check("rate cap", False, "flood accepted!")
    except ValueError as e:
        check("rate cap", "too fast" in str(e), str(e))
    # window expiry -> finalize pays tiers: 6 clicks -> +2 XP
    db._exec("UPDATE feed_frenzy_sessions SET started_at=?, click_marks='[]'"
             " WHERE fm_id=?", (int(time.time()) - 60, visitor))
    fin = tpg.feed_frenzy_status(db, visitor)
    check("expiry finalizes", fin["done"] and fin["clicks"] == 6, str(fin))
    check("tier xp (6 clicks -> 2)", fin.get("xp_added") == 2, str(fin))
    # idempotent finalize
    fin2 = tpg.finalize_feed_frenzy(db, visitor)
    check("finalize idempotent", fin2.get("already") is True, str(fin2))
    # bigger session -> 35 clicks -> +5 XP
    db._exec("INSERT OR REPLACE INTO feed_frenzy_sessions (fm_id, started_at,"
             " clicks, click_marks, finalized, score) VALUES (?,?,?,?,?,?)",
             (owner, int(time.time()) - 60, 35, "[]", 0, 0))
    fin3 = tpg.feed_frenzy_status(db, owner)
    check("tier xp (35 clicks -> 5)", fin3.get("xp_added") == 5, str(fin3))
    try:
        tpg.feed_frenzy_click(db, loner)
        check("no-pet frenzy rejected", False, "accepted!")
    except ValueError:
        check("no-pet frenzy rejected", True)

    print("== pet XP thresholds grant wardrobe ==")
    before = tps.pet_xp_total(db, visitor)
    xp = tps.award_pet_xp(db, visitor, 50 - before)
    check("threshold 50 grants sailor hat",
          any(g["item"] == "acc:sailor_hat" for g in xp["rewards"]), str(xp))
    check("owns granted item", shop.owns(db, visitor, "acc:sailor_hat"))
    check("auto-equipped (slot empty)",
          "acc:sailor_hat" in shop.equipped_accessories(db, visitor))
    check("entries include equipped pet",
          visitor in tps.fashion_friday_entries(db))
    # idempotent: crossing again grants nothing new
    xp2 = tps.award_pet_xp(db, visitor, 250)  # 50 -> 300: crosses 150 and 300
    check("threshold 150+300 grant",
          {g["item"] for g in xp2["rewards"]} == {"acc:star_shades", "acc:pearl_crown"},
          str(xp2))
    xp3 = tps.award_pet_xp(db, visitor, 10)
    check("no double grant", xp3["rewards"] == [], str(xp3))

    print("== fashion friday: vote-once + resolve ==")
    # entries: visitor has equipped items; owner has none
    check("owner not entered", owner not in tps.fashion_friday_entries(db))
    fri = ct_ts(2026, 9, 18, 12, 0)   # Friday noon CT
    sat = ct_ts(2026, 9, 19, 1, 0)    # Saturday 01:00 CT (after close)
    key, s_at, e_at, is_open = tps.fashion_friday_window(fri)
    check("window key", key == "ff-2026-09-18", key)
    check("window open friday", is_open is True)
    check("window closed saturday",
          tps.fashion_friday_window(sat)[3] is False)
    v = tps.vote_fashion_friday(db, owner, visitor, ts=fri)
    check("vote ok", v["pet_votes"] == 1, str(v))
    try:
        tps.vote_fashion_friday(db, owner, visitor, ts=fri)
        check("vote-once", False, "second vote accepted!")
    except ValueError as e:
        check("vote-once", "already voted" in str(e), str(e))
    try:
        tps.vote_fashion_friday(db, third, owner, ts=fri)
        check("vote non-entry rejected", False, "accepted!")
    except ValueError as e:
        check("vote non-entry rejected", "isn't entered" in str(e), str(e))
    try:
        tps.vote_fashion_friday(db, third, visitor, ts=sat)
        check("vote after close rejected", False, "accepted!")
    except ValueError as e:
        check("vote after close rejected", "closed" in str(e), str(e))
    res = tps.resolve_fashion_friday(db, ts=sat)
    check("resolve crowns winner", res["winner_fm_id"] == visitor
          and res["votes"] == 1, str(res))
    check("winner signal prize", res["signal_awarded"] == 25, str(res))
    check("winner got crown item",
          shop.owns(db, visitor, "acc:pearl_crown"))
    check("winner notified",
          any("won Fashion Friday" in n["text"]
              for n in db.notifications_for(visitor)))
    check("crowned listed", visitor in tps.crowned_fm_ids(db))
    check("past winners", any(w["winner_fm_id"] == visitor
                              for w in tps.past_winners(db)))
    res2 = tps.resolve_fashion_friday(db, ts=sat)
    check("resolve idempotent", res2.get("already") is True, str(res2))

    print("== templates render ==")
    with appmod.app.test_request_context("/tidepals"):
        st = pets.pet_status(db, owner)
        st["mood_emoji"] = {"happy": "😊"}.get(st["mood"], "💧")
        html = appmod.render_template(
            "tidepals.html",
            pets=[{**st, "handle": "palowner",
                   "svg": st["svg"], "crowned": False,
                   "pat_count": tps.pat_count(db, owner)}],
            ritual={"title": "👗 Fashion Friday", "is_open": True,
                    "status": "open", "ends_at_human": "Friday 23:59 CT"},
            entries=[], winners=[])
        check("/tidepals renders",
              "The Tidepool" in html and "Bubbles" in html and "<svg" in html,
              html[:200])
    with appmod.app.test_request_context("/pet/palowner"):
        st = pets.pet_status(db, owner)
        html = appmod.render_template(
            "pet_visit.html", pet=st, mood_emoji="😊",
            caretakers=tps.caretakers(db, owner),
            pat_count=tps.pat_count(db, owner),
            pet_xp=tps.pet_xp_total(db, owner))
        check("/pet/<handle> renders",
              "Bubbles" in html and "Caretakers" in html and "Pat Bubbles" in html,
              html[:200])

    print("== rulebooks ==")
    check("social_rules", tps.social_rules()["version"] == tps.SOCIAL_VERSION)
    check("game_rules", tpg.game_rules()["version"] == tpg.GAMES_VERSION)

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
