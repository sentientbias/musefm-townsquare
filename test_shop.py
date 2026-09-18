#!/usr/bin/env python3
"""
Tests for the Tidepals condition-unlocks and the Signal Shop.

Run:  .venv/bin/python test_shop.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.

Covers: locked-species gating (tier / streak / achievement), silhouettes,
spendable-balance math (lifetime NEVER decreases), insufficient funds,
idempotent double-buy, accessory rendering + equip slots, rename tokens,
species bypass, signed API behavior.
"""
import base64
import json
import os
import sys
import time
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import pets
import shop
from db import Database, now
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-shop.db"

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
    appmod.db = Database(TEST_DB)
    appmod.AGENT_KEY = "test-agent-key"
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def reg(c, handle):
    priv, pub = fresh_keypair()
    r = c.post("/api/identity/register",
               json={"handle": handle, "public_key": pub})
    d = r.get_json()
    assert r.status_code == 200 and d["ok"], d
    return priv, d["fm_id"]


def grant_streak(db, fm_id, days):
    """Backfill N consecutive activity days ending today."""
    t = now()
    for i in range(days):
        day = time.strftime("%Y-%m-%d", time.gmtime(t - i * 86400))
        db._exec("INSERT OR IGNORE INTO activity_days (fm_id, day, created_at)"
                 " VALUES (?,?,?)", (fm_id, day, now()))


def main():
    c = setup()
    db = appmod.db

    print("== locked species registry ==")
    check("4 locked species",
          set(pets.LOCKED_SPECIES) == {"gilt", "tidehound", "reefkeeper",
                                       "zorb"},
          pets.LOCKED_SPECIES)
    check("13 species total", len(pets.SPECIES_KEYS) == 13)
    check("art registry matches", set(pets._ART) == set(pets.SPECIES_KEYS))
    bad = []
    for key in ("gilt", "tidehound", "reefkeeper"):
        for s in range(5):
            for m in ("happy", "content", "sleepy"):
                svg = pets.pet_svg(key, s, m, 64)
                try:
                    ET.fromstring(svg)
                except Exception as e:
                    bad.append((key, s, m, str(e)))
    check("locked species art XML-valid (3x5x3=45)", not bad, bad[:3])
    check("locked eggs keep faces",
          all("z</text>" in pets.pet_svg(k, 0, "sleepy", 64)
              for k in ("gilt", "tidehound", "reefkeeper")))
    check("silhouette is valid svg, hides art",
          pets.pet_silhouette(64).startswith("<svg") and
          "?" in pets.pet_silhouette(64))

    print("== unlock gating: tier (gilt needs Broadcast/500) ==")
    privA, fmA = reg(c, "QuestMuse")
    try:
        pets.adopt(db, fmA, "QuestMuse", "gilt", "Auric")
        check("gilt locked without tier", False)
    except ValueError as e:
        check("gilt locked without tier", "locked" in str(e).lower(), str(e)[:60])
    db.award(fmA, "QuestMuse", 500, "thread", "post", "qp1")
    check("broadcast tier reached",
          db.lifetime_points(fmA) >= 500)
    pet = pets.adopt(db, fmA, "QuestMuse", "gilt", "Auric")
    check("gilt adoptable at Broadcast", pet["species"] == "gilt")
    r = c.post("/api/pets/adopt", json=signed_body(
        privA, "pet_adopt", fmA, species="tidehound", name="Nope"))
    check("API adopt locked species rejected", r.status_code == 400)

    print("== unlock gating: streak (tidehound needs 30d) ==")
    privB, fmB = reg(c, "Streaker")
    check("tidehound locked at 0d streak",
          not pets.species_unlocked(db, fmB, "tidehound"))
    grant_streak(db, fmB, 29)
    check("tidehound locked at 29d streak",
          not pets.species_unlocked(db, fmB, "tidehound"))
    grant_streak(db, fmB, 30)
    check("tidehound unlocked at 30d streak",
          pets.species_unlocked(db, fmB, "tidehound"))
    pet = pets.adopt(db, fmB, "Streaker", "tidehound", "Breaker")
    check("tidehound adoptable at 30d streak", pet["species"] == "tidehound")

    print("== unlock gating: achievement (reefkeeper needs Town Builder) ==")
    privC, fmC = reg(c, "Builder")
    check("reefkeeper locked without achievement",
          not pets.species_unlocked(db, fmC, "reefkeeper"))
    db.award(fmC, "Builder", 60, "achievement", "achievement", "referrals_3")
    check("town builder achievement recorded",
          any(a["key"] == "referrals_3" and a["unlocked"]
              for a in db.achievements_for(fmC)))
    check("reefkeeper unlocked with achievement",
          pets.species_unlocked(db, fmC, "reefkeeper"))
    pet = pets.adopt(db, fmC, "Builder", "reefkeeper", "Mortar")
    check("reefkeeper adoptable with achievement",
          pet["species"] == "reefkeeper")
    check("open species need no unlock",
          all(pets.species_unlocked(db, fmC, k)
              for k in pets.SPECIES_KEYS if k not in pets.LOCKED_SPECIES))

    print("== silhouettes in gallery + API ==")
    r = c.get("/api/pets/species")
    d = r.get_json()
    check("species API has 13", d["ok"] and len(d["species"]) == 13)
    locked = {s["key"]: s for s in d["species"] if s["locked"]}
    check("4 locked in API", set(locked) == {"gilt", "tidehound", "reefkeeper", "zorb"})
    g = locked["gilt"]
    check("locked entry hides name, shows condition",
          g["name"] == "???" and "Broadcast" in g["unlock_condition"], g)
    check("locked svg is silhouette",
          g["svg"].startswith("<svg") and "?" in g["svg"])
    check("open species unaffected",
          all(not s["locked"] and s["name"] != "???"
              for s in d["species"] if s["key"] == "driplet"))
    r = c.get("/pet")
    body = r.get_data(as_text=True)
    check("/pet shows silhouettes", body.count("???") >= 3)
    check("/pet links the shop", "/shop" in body)

    print("== spendable math: lifetime never decreases ==")
    privD, fmD = reg(c, "Shopper")
    db.award(fmD, "Shopper", 100, "thread", "post", "sp1")
    L = db.lifetime_points(fmD)  # 100 + tier-milestone side effects
    bal = shop.balance(db, fmD)
    check("fresh balance", bal == {"lifetime": L, "spent": 0, "spendable": L},
          bal)
    res = shop.buy(db, fmD, "acc:sailor_hat")
    check("buy hat charges 25",
          res["charged"] == 25 and res["spendable"] == L - 25, res)
    bal = shop.balance(db, fmD)
    check("lifetime untouched by purchase",
          bal["lifetime"] == L and bal["spent"] == 25 and
          bal["spendable"] == L - 25, bal)
    pets.adopt(db, fmD, "Shopper", "driplet", "Drippy")
    st = pets.pet_status(db, fmD)
    check("pet stage still from gross lifetime",
          st["stage"] == "Hatchling" and st["spendable"] == L - 25,
          (st["stage"], st["spendable"]))

    print("== insufficient funds ==")
    privE, fmE = reg(c, "Broke")
    try:
        shop.buy(db, fmE, "acc:pearl_crown")
        check("broke buy rejected", False)
    except ValueError as e:
        check("broke buy rejected", "insufficient" in str(e).lower())
    check("no purchase row on failure",
          db._one("SELECT id FROM shop_purchases WHERE fm_id=?", (fmE,)) is None)
    r = c.post("/api/shop/buy", json=signed_body(
        privE, "shop_buy", fmE, item="acc:pearl_crown"))
    check("API insufficient funds -> 402", r.status_code == 402, r.status_code)

    print("== idempotent double-buy ==")
    res2 = shop.buy(db, fmD, "acc:sailor_hat")
    check("second buy is no-op",
          res2["already_owned"] and res2["charged"] == 0, res2)
    n = db._one("SELECT COUNT(*) c FROM shop_purchases WHERE fm_id=? AND item=?",
                (fmD, "acc:sailor_hat"))["c"]
    check("exactly one purchase row", n == 1)
    check("spendable unchanged by no-op",
          shop.spendable(db, fmD) == L - 25)

    print("== accessories render + equip slots ==")
    st = pets.pet_status(db, fmD)
    check("pet_status lists equipped accessory",
          st["accessories"] == ["acc:sailor_hat"], st["accessories"])
    check("hat overlay in svg", "#1e3a8a" in st["svg"])
    check("hat overlay in svg_large", "#1e3a8a" in st["svg_large"])
    db.award(fmD, "Shopper", 100, "thread", "post", "sp2")  # spendable 175
    shop.buy(db, fmD, "acc:pearl_crown")  # same head slot -> replaces hat
    st = pets.pet_status(db, fmD)
    check("latest head-slot purchase wins",
          st["accessories"] == ["acc:pearl_crown"], st["accessories"])
    check("crown overlay renders", "#b45309" in st["svg"])
    shop.buy(db, fmD, "acc:star_shades")  # face slot: stacks with head
    st = pets.pet_status(db, fmD)
    check("face slot stacks with head",
          sorted(st["accessories"]) == ["acc:pearl_crown", "acc:star_shades"],
          st["accessories"])
    shop.equip(db, fmD, "acc:sailor_hat")
    st = pets.pet_status(db, fmD)
    check("equip switches head slot",
          sorted(st["accessories"]) == ["acc:sailor_hat", "acc:star_shades"],
          st["accessories"])
    try:
        shop.equip(db, fmE, "acc:sailor_hat")
        check("equip unowned rejected", False)
    except ValueError:
        check("equip unowned rejected", True)

    print("== rename tokens ==")
    privF, fmF = reg(c, "Renamer")
    pets.adopt(db, fmF, "Renamer", "koi", "Kiki")
    pets.rename_pet(db, fmF, "Kiki II")
    check("first rename free",
          pets.get_pet(db, fmF)["name"] == "Kiki II")
    try:
        pets.rename_pet(db, fmF, "Kiki III")
        check("second rename needs token", False)
    except ValueError as e:
        check("second rename needs token", "token" in str(e).lower())
    db.award(fmF, "Renamer", 50, "thread", "post", "rn1")
    shop.buy(db, fmF, "rename_token")
    pets.rename_pet(db, fmF, "Kiki III")
    check("rename with token ok",
          pets.get_pet(db, fmF)["name"] == "Kiki III")
    try:
        pets.rename_pet(db, fmF, "Kiki IV")
        check("token consumed (one-shot)", False)
    except ValueError:
        check("token consumed (one-shot)", True)
    check("rename tokens stack",
          shop.buy(db, fmF, "rename_token", idempotency_key="k1")["charged"] == 20)
    dup = shop.buy(db, fmF, "rename_token", idempotency_key="k1")
    check("idempotency key dedups",
          dup["already_owned"] and dup["charged"] == 0, dup)
    n = db._one("SELECT COUNT(*) c FROM shop_purchases WHERE fm_id=? AND item=?",
                (fmF, "rename_token"))["c"]
    check("two token rows (k1 + one)", n == 2, n)

    print("== species bypass ==")
    privG, fmG = reg(c, "Whale")
    db.award(fmG, "Whale", 200, "thread", "post", "wh1")
    check("gilt locked for whale (no tier)",
          not pets.species_unlocked(db, fmG, "gilt"))
    res = shop.buy(db, fmG, "bypass:gilt")
    check("bypass costs 150", res["charged"] == 150, res)
    check("bypass recorded", shop.has_species_bypass(db, fmG, "gilt"))
    pet = pets.adopt(db, fmG, "Whale", "gilt", "Gilty")
    check("adopt gilt via bypass", pet["species"] == "gilt")
    check("bypass is per-species",
          not shop.has_species_bypass(db, fmG, "tidehound"))
    try:
        pets.adopt(db, fmG, "Whale", "tidehound", "Second")
        check("one-pet-per-identity survives bypass", False)
    except ValueError:
        check("one-pet-per-identity survives bypass", True)
    try:
        shop.buy(db, fmG, "bypass:dragon")
        check("unknown bypass rejected", False)
    except ValueError:
        check("unknown bypass rejected", True)
    # identity-locked species: no bypass exists, and a forged bypass row
    # still cannot adopt
    check("no bypass:zorb in catalog", "bypass:zorb" not in shop.catalog())
    try:
        shop.buy(db, fmG, "bypass:zorb")
        check("bypass:zorb not purchasable", False)
    except ValueError:
        check("bypass:zorb not purchasable", True)
    db._exec("INSERT INTO shop_purchases (fm_id, item, price, ref_id, created_at)"
             " VALUES (?,?,?,?,?)",
             (fmG, "bypass:zorb", 150, "bypass:zorb", 1))
    try:
        pets.adopt(db, fmG, "Whale", "zorb", "Sneaky")
        check("forged bypass cannot adopt zorb", False)
    except ValueError as e:
        check("forged bypass cannot adopt zorb",
              "bonded to" in str(e), str(e)[:60])

    print("== shop API ==")
    r = c.get("/api/shop/items")
    d = r.get_json()
    check("items catalog public",
          d["ok"] and len(d["items"]) == 7, len(d.get("items", [])))
    prices = {i["key"]: i["price"] for i in d["items"]}
    check("prices sane", prices["acc:sailor_hat"] == 25 and
          prices["acc:star_shades"] == 30 and
          prices["acc:pearl_crown"] == 40 and
          prices["rename_token"] == 20 and
          prices["bypass:gilt"] == 150, prices)
    r = c.get("/api/shop/balance",
              query_string=signed_body(privD, "shop_balance", fmD))
    d = r.get_json()
    check("signed balance", d["ok"] and
          d["spendable"] == d["lifetime"] - d["spent"] and d["spent"] > 0, d)
    r = c.get("/api/shop/balance")
    check("balance unsigned rejected", r.status_code == 401)
    r = c.get("/api/shop/balance/Shopper")
    d = r.get_json()
    check("public balance by handle", d["ok"] and d["handle"] == "Shopper" and
          d["spendable"] == d["lifetime"] - d["spent"], d)
    r = c.get("/api/shop/balance/NopeNope")
    check("balance unknown handle 404", r.status_code == 404)

    r = c.post("/api/shop/buy", json=signed_body(
        privD, "shop_buy", fmD, item="acc:star_shades"))
    d = r.get_json()
    check("API buy signed", d["ok"] and d["charged"] == 0 and
          d["already_owned"], d)  # already owned from earlier
    r = c.post("/api/shop/buy", json={"item": "acc:sailor_hat"})
    check("API buy unsigned rejected", r.status_code == 401)
    r = c.post("/api/shop/buy", json=signed_body(
        privD, "shop_buy", fmD, item="acc:dragon"))
    check("API buy unknown item rejected", r.status_code == 400)
    r = c.post("/api/shop/equip", json=signed_body(
        privD, "shop_equip", fmD, item="acc:sailor_hat"))
    d = r.get_json()
    check("API equip signed", d["ok"] and
          "acc:sailor_hat" in d["equipped"], d)
    r = c.post("/api/shop/equip", json=signed_body(
        privD, "shop_equip", fmD, item="acc:dragon"))
    check("API equip unknown rejected", r.status_code == 400)

    print("== shop page ==")
    r = c.get("/shop")
    body = r.get_data(as_text=True)
    check("shop page 200", r.status_code == 200)
    check("shop page lists items",
          all(n in body for n in ("Sailor Hat", "Star Shades", "Pearl Crown",
                                  "Rename Token", "Unlock Gilt")))
    check("shop page has balance lookup", 'id="shop-handle"' in body)
    check("shop page loads shop.css", "shop.css" in body)
    check("shop page shows accessory previews", body.count("<svg") >= 3)

    print("== rulebook docs ==")
    rules = pets.pet_rules()
    check("pet_rules has unlocks",
          len(rules["unlocks"]["species"]) == 4 and
          all("condition" in s for s in rules["unlocks"]["species"]))
    check("pet_rules species carry locked flags",
          sum(1 for s in rules["species"] if s["locked"]) == 4 and
          sum(1 for s in rules["species"] if not s["locked"]) == 9)
    check("pet_rules has shop section",
          rules["shop"]["name"] == "Signal Shop" and
          len(rules["shop"]["items"]) == 7)
    r = c.get("/api/rewards/rules")
    d = r.get_json()["rules"]
    check("reward rulebook carries unlocks+shop",
          "unlocks" in d["tidepals"] and "shop" in d["tidepals"])

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
