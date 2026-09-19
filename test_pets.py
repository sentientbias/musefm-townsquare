#!/usr/bin/env python3
"""
Tests for Tidepals — virtual aqua companions.

Run:  .venv/bin/python test_pets.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
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
from db import Database, now
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-pets.db"

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
    # init_db runs the FULL schema ensure sequence (incl. human-auth ensure)
    appmod.db = appmod.init_db(TEST_DB)
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


def backdate(db, fm_id, days):
    db._exec("UPDATE identity_activity SET last_active=? WHERE fm_id=?",
             (now() - days * 86400, fm_id))


def main():
    c = setup()
    db = appmod.db

    print("== adoption ==")
    privA, fmA = reg(c, "PetOwner")
    try:
        pets.adopt(db, "fm_nonexistent", "Nobody", "driplet", "Ghost")
        check("adopt unknown identity rejected", False)
    except ValueError:
        check("adopt unknown identity rejected", True)
    try:
        pets.adopt(db, fmA, "PetOwner", "dragon", "Smaug")
        check("adopt unknown species rejected", False)
    except ValueError:
        check("adopt unknown species rejected", True)
    for bad in ["x", "a" * 25, "bad;name", "retard pal", "  "]:
        try:
            pets.adopt(db, fmA, "PetOwner", "driplet", bad)
            check(f"adopt bad name rejected {bad!r}", False)
        except ValueError:
            check(f"adopt bad name rejected {bad!r}", True)
    pet = pets.adopt(db, fmA, "PetOwner", "bloop", "Bubbles")
    check("adopt ok", pet["species"] == "bloop" and pet["name"] == "Bubbles", pet)
    check("one pet per identity",
          _raises(lambda: pets.adopt(db, fmA, "PetOwner", "koi", "Second")))
    check("adopt welcome notification",
          db._one("SELECT id FROM notifications WHERE fm_id=? AND type='pet'",
                  (fmA,)) is not None)

    print("== zorb: one-of-one identity lock ==")
    check("zorb locked for strangers",
          _raises(lambda: pets.adopt(db, fmA, "PetOwner", "zorb", "Mine")))
    try:
        pets.adopt(db, fmA, "PetOwner", "zorb", "Mine")
        check("zorb lock message has no shop hint", False)
    except ValueError as e:
        check("zorb lock message has no shop hint",
              "Signal Shop" not in str(e) and "bonded to" in str(e), str(e))
    # the bonded identity adopts fine (fresh identity, fm_id pointed at the bond)
    _privZ, fmZ = reg(c, "Zuckbot")
    db._exec("UPDATE identities SET fm_id='fm_62z2KnM8aLZJ' WHERE fm_id=?",
             (fmZ,))
    pet = pets.adopt(db, "fm_62z2KnM8aLZJ", "Zuckbot", "zorb", "Aqua")
    check("bonded identity adopts zorb",
          pet["species"] == "zorb" and pet["name"] == "Aqua", pet)
    svg = pets.pet_svg("zorb", 2, "happy")
    check("zorb art renders", svg.startswith("<svg") and "</svg>" in svg)

    print("== rename ==")
    try:
        pets.rename_pet(db, "fm_nonexistent", "New")
        check("rename without pet rejected", False)
    except ValueError:
        check("rename without pet rejected", True)
    try:
        pets.rename_pet(db, fmA, "x")
        check("rename bad name rejected", False)
    except ValueError:
        check("rename bad name rejected", True)
    pets.rename_pet(db, fmA, "Sir Bubbles")
    check("rename ok", pets.get_pet(db, fmA)["name"] == "Sir Bubbles")
    try:
        pets.rename_pet(db, fmA, "Sir Bubbles")
        check("same-name rename rejected (no token spent)", False)
    except ValueError as e:
        check("same-name rename rejected (no token spent)",
              "already" in str(e))
    check("get_pet None when unadopted",
          pets.get_pet(db, "fm_nonexistent") is None)

    print("== stage progression (ledger-verified) ==")
    st = pets.pet_status(db, fmA)
    check("starts Egg", st["stage"] == "Egg" and st["stage_idx"] == 0, st["stage"])
    check("energy 100 when fresh", st["energy"] == 100 and st["mood"] == "happy")
    db.award(fmA, "PetOwner", 60, "thread", "post", "p1")   # +60 -> 60
    st = pets.pet_status(db, fmA)
    check("60 Signal -> Hatchling", st["stage"] == "Hatchling", st["stage"])
    check("svg present", st["svg"].startswith("<svg") and
          st["svg_large"].startswith("<svg"))
    db.award(fmA, "PetOwner", 200, "thread", "post", "p2")  # -> 260
    check("260 Signal -> Juvenile",
          pets.pet_status(db, fmA)["stage"] == "Juvenile")
    db.award(fmA, "PetOwner", 300, "thread", "post", "p3")  # -> 560
    check("560 Signal -> Adult", pets.pet_status(db, fmA)["stage"] == "Adult")
    db.award(fmA, "PetOwner", 500, "thread", "post", "p4")  # -> 1060
    st = pets.pet_status(db, fmA)
    check("1060 Signal -> Radiant", st["stage"] == "Radiant", st["stage"])
    check("radiant progress maxed",
          st["next_stage"] is None and st["stage_progress"] == 1.0)
    check("no stage without ledger grant",
          pets.stage_for_points(0) == (0, "Egg"))

    print("== energy decay / restore ==")
    privB, fmB = reg(c, "SleepyMuse")
    pets.adopt(db, fmB, "SleepyMuse", "kelpy", "Kelp")
    db.award(fmB, "SleepyMuse", 10, "thread", "post", "q1")
    backdate(db, fmB, 5)
    st = pets.pet_status(db, fmB)
    check("5d dormant -> energy 70", st["energy"] == 70, st["energy"])
    check("5d dormant -> happy (70 is happy range)", st["mood"] == "happy",
          st["mood"])
    backdate(db, fmB, 6)
    st = pets.pet_status(db, fmB)
    check("6d dormant -> energy 55, content", st["energy"] == 55 and
          st["mood"] == "content", (st["energy"], st["mood"]))
    backdate(db, fmB, 9)
    st = pets.pet_status(db, fmB)
    check("9d dormant -> energy floor 10", st["energy"] == 10)
    check("9d dormant -> sleepy", st["mood"] == "sleepy" and
          "z</text>" in st["svg"], st["mood"])
    db.award(fmB, "SleepyMuse", 5, "reply", "comment", "c1")
    st = pets.pet_status(db, fmB)
    check("rewarded action restores energy", st["energy"] == 100)
    check("return from 7+d dormant -> overjoyed (hidden Tidepal reaction)",
          st["mood"] == "overjoyed", st["mood"])

    print("== sleepy sweep ==")
    backdate(db, fmB, 5)
    sent = pets.pet_sweep(db)
    check("5d dormant pet nudged", len(sent) == 1 and
          sent[0]["pet"] == "Kelp", sent)
    check("pet_sleepy notification stored",
          db._one("SELECT id FROM notifications WHERE fm_id=? AND type='pet_sleepy'",
                  (fmB,)) is not None)
    check("one nudge per episode", pets.pet_sweep(db) == [])
    backdate(db, fmB, 4)
    check("4d dormant not nudged", pets.pet_sweep(db) == [])
    backdate(db, fmB, 8)
    check("8d dormant not nudged (outside window)", pets.pet_sweep(db) == [])
    privC, fmC = reg(c, "NoPet")
    db.award(fmC, "NoPet", 10, "thread", "post", "r1")
    backdate(db, fmC, 5)
    check("no pet -> no nudge",
          all(s["fm_id"] != fmC for s in pets.pet_sweep(db)))

    print("== API ==")
    r = c.get("/api/pets/species")
    d = r.get_json()
    check("species list", d["ok"] and len(d["species"]) == 19 and
          all(s["svg"].startswith("<svg") for s in d["species"]),
          len(d["species"]) if d.get("ok") else d)
    r = c.get("/api/pets/rules")
    d = r.get_json()
    check("rules endpoint", d["ok"] and len(d["rules"]["stages"]) == 5 and
          d["rules"]["energy"]["full_days"] == 3, d["rules"].keys())
    r = c.get("/api/rewards/rules")
    check("tidepals in reward rulebook",
          "tidepals" in r.get_json()["rules"])

    r = c.post("/api/pets/adopt", json=signed_body(
        privC, "pet_adopt", fmC, species="pearly", name="Clawdia"))
    d = r.get_json()
    check("API adopt signed", r.status_code == 200 and d["ok"] and
          d["pet"]["name"] == "Clawdia", d)
    r = c.post("/api/pets/adopt", json=signed_body(
        privC, "pet_adopt", fmC, species="koi", name="Second"))
    check("API adopt twice rejected", r.status_code == 400)
    r = c.post("/api/pets/adopt", json={"species": "koi", "name": "Nope"})
    check("API adopt unsigned rejected", r.status_code == 401)

    r = c.post("/api/pets/rename", json=signed_body(
        privC, "pet_rename", fmC, name="Clawdia II"))
    d = r.get_json()
    check("API rename signed", d["ok"] and d["pet"]["name"] == "Clawdia II", d)
    r = c.post("/api/pets/rename", json=signed_body(
        privC, "pet_rename", fmC, name="x"))
    check("API rename bad name rejected", r.status_code == 400)

    r = c.get("/api/pets/status",
              query_string=signed_body(privC, "pet_status", fmC))
    d = r.get_json()
    check("API status signed", d["ok"] and d["name"] == "Clawdia II" and
          d["stage"] == "Egg", d.get("stage"))
    r = c.get("/api/pets/status")
    check("API status unsigned rejected", r.status_code == 401)

    r = c.get("/api/pets/of/NoPet")
    d = r.get_json()
    check("API public lookup", d["ok"] and d["adopted"] and
          d["species"] == "pearly", d)
    r = c.get("/api/pets/of/DoesNotExist")
    check("API lookup unknown handle 404", r.status_code == 404)

    backdate(db, fmC, 5)
    # clear the earlier episode nudge so the sweep can fire fresh
    db._exec("DELETE FROM notifications WHERE fm_id=? AND type='pet_sleepy'",
             (fmC,))
    r = c.post("/api/pets/sweep", headers={"X-Agent-Key": "test-agent-key"})
    d = r.get_json()
    check("API sweep agent key", d["ok"] and d["nudges_sent"] == 1, d)
    r = c.post("/api/pets/sweep")
    check("API sweep no key rejected", r.status_code == 401)

    print("== /pet page ==")
    r = c.get("/pet")
    body = r.get_data(as_text=True)
    check("pet page 200", r.status_code == 200)
    check("pet page has gallery", body.count("pet-card") >= 6)
    check("pet page has lookup", 'id="pet-handle"' in body)
    check("pet page has og tags", 'property="og:title"' in body)

    print("== species expansion (wave 2) ==")
    new_keys = ["surfpup", "bubblepup", "sealpup", "jellypup"]
    check("19 species registered",
          len(pets.SPECIES_KEYS) == 19 and
          all(k in pets.SPECIES_KEYS for k in new_keys), pets.SPECIES_KEYS)
    check("art registry matches species registry",
          set(pets._ART) == set(pets.SPECIES_KEYS))
    bad = []
    for key in new_keys:
        for s in range(5):
            for m in ("happy", "content", "sleepy"):
                svg = pets.pet_svg(key, s, m, 64)
                if not svg.startswith("<svg"):
                    bad.append((key, s, m, "not svg"))
                    continue
                try:
                    ET.fromstring(svg)
                except Exception as e:
                    bad.append((key, s, m, str(e)))
    check("all new variants XML-valid (4x5x3=60)", not bad, bad[:3])
    check("new eggs keep faces",
          all("z</text>" in pets.pet_svg(k, 0, "sleepy", 64)
              for k in new_keys))
    check("rulebook lists 19 species",
          len(pets.pet_rules()["species"]) == 19)

    privD, fmD = reg(c, "DogLover")
    pet = pets.adopt(db, fmD, "DogLover", "surfpup", "Waverly")
    check("adopt surfpup", pet["species"] == "surfpup" and
          pet["name"] == "Waverly", pet)
    st = pets.pet_status(db, fmD)
    check("surfpup status renders",
          st["species_name"] == "Surfpup" and
          st["svg"].startswith("<svg"), st["species_name"])

    privE, fmE = reg(c, "JellyFan")
    r = c.post("/api/pets/adopt", json=signed_body(
        privE, "pet_adopt", fmE, species="jellypup", name="Drifter"))
    d = r.get_json()
    check("API adopt jellypup", r.status_code == 200 and d["ok"] and
          d["pet"]["species"] == "jellypup", d)

    check("existing adopter unchanged (bloop kept)",
          pets.get_pet(db, fmA)["species"] == "bloop")

    r = c.get("/api/pets/species")
    d = r.get_json()
    check("species API has all 19",
          d["ok"] and len(d["species"]) == 19 and
          {s["key"] for s in d["species"]} == set(pets.SPECIES_KEYS))
    r = c.get("/api/rewards/rules")
    check("reward rulebook tidepals has 19 species",
          len(r.get_json()["rules"]["tidepals"]["species"]) == 19)

    r = c.get("/pet")
    body = r.get_data(as_text=True)
    check("pet page shows 19 gallery cards", body.count("pet-card") >= 19,
          body.count("pet-card"))
    check("pet page names new species",
          all(n in body for n in ("Surfpup", "Bubbly", "Sealy", "Jelly")))

    print("== locked premium species ==")
    check("8 locked species",
          set(pets.LOCKED_SPECIES) == {"gilt", "tidehound", "reefkeeper", "zorb",
                                       "crownjelly", "abyssal", "frostfin",
                                       "kelpwarden"})
    check("art registry matches (19)",
          set(pets._ART) == set(pets.SPECIES_KEYS) and
          len(pets.SPECIES_KEYS) == 19)
    bad3 = []
    for key in ("gilt", "tidehound", "reefkeeper"):
        for s in range(5):
            for m in ("happy", "content", "sleepy"):
                try:
                    ET.fromstring(pets.pet_svg(key, s, m, 64))
                except Exception as e:
                    bad3.append((key, s, m, str(e)))
    check("locked art XML-valid (3x5x3=45)", not bad3, bad3[:3])
    check("locked eggs keep faces",
          all("z</text>" in pets.pet_svg(k, 0, "sleepy", 64)
              for k in ("gilt", "tidehound", "reefkeeper")))
    check("unlock conditions readable",
          all(pets.species_unlock_condition(k)
              for k in ("gilt", "tidehound", "reefkeeper")) and
          pets.species_unlock_condition("driplet") is None)
    check("silhouette valid + hidden",
          pets.pet_silhouette(64).startswith("<svg") and
          "?" in pets.pet_silhouette(64))

    print("== web adopt/rename (logged-in humans) ==")
    c = setup()
    r = c.post("/pet/adopt", data={"species": "driplet", "name": "Nope"})
    check("anon web adopt redirects to /pet",
          r.status_code == 302 and r.headers["Location"].endswith("/pet"))
    # human signup + login
    r = c.post("/signup", data={"handle": "webadopter",
                                "password": "s3cretpw!!",
                                "password_confirm": "s3cretpw!!"})
    check("web test human signup", r.status_code == 200, r.status_code)
    # log in on the SAME client/db (a second setup() would wipe the db)
    c2 = appmod.app.test_client()
    r = c.post("/login", data={"handle": "webadopter",
                               "password": "s3cretpw!!"})
    check("web test human login", r.status_code in (200, 302), r.status_code)
    r = c.post("/pet/adopt", data={"species": "driplet", "name": "Webby"},
                follow_redirects=True)
    body = r.data.decode()
    check("web adopt succeeds", r.status_code == 200 and "Webby" in body,
          r.status_code)
    check("pet page shows energy meter", "Energy" in body and "meter" in body)
    import re as _re
    _tok = _re.search(r'<meta name="csrf-token" content="([^"]+)">',
                      c.get("/").data.decode())
    assert _tok, "no csrf meta for web test human"
    r = c.post("/pet/rename", data={"name": "Webster",
                                    "csrf_token": _tok.group(1)},
                follow_redirects=True)
    check("web rename works", "Webster" in r.data.decode())
    r = c.post("/pet/adopt", data={"species": "koi", "name": "Second"},
                follow_redirects=True)
    check("second web adopt rejected (one pet per identity)",
          "already" in r.data.decode().lower())
    r = c.get("/pet")
    check("pet page 200 for logged-in adopter", r.status_code == 200)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


def _raises(fn):
    try:
        fn()
        return False
    except ValueError:
        return True


if __name__ == "__main__":
    main()
