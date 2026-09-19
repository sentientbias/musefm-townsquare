#!/usr/bin/env python3
"""Tests for the Tidepals wow-factor expansion (2026-09-19):

A1  pet lifecycle events -> the webhook/event feed
A2  GET /api/pets/schema.json machine-readable schema
A3  GET /api/pets/mine custody digest
H1/H2/H4  template motion classes, mobile breakpoints, evolution ceremony
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("MUSEFM_TEST", "1")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import events
import pets
import tidepal_social as tps
from identity import b64u_encode, signed_body

TEST_DB = "/tmp/test_tidepal_wow.db"

passed = 0
failed = 0
failures = []


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name}")
    else:
        failed += 1
        failures.append(name)
        print(f"  FAIL {name} {extra}")


def fresh_keypair():
    priv = Ed25519PrivateKey.generate()
    return (b64u_encode(priv.private_bytes_raw()),
            b64u_encode(priv.public_key().public_bytes_raw()))


def reg(c, handle):
    priv, pub = fresh_keypair()
    r = c.post("/api/identity/register",
               json={"handle": handle, "public_key": pub})
    d = r.get_json()
    if r.status_code == 429:
        ident = appmod.db.register_identity(handle, pub)
        return priv, ident["fm_id"]
    assert r.status_code == 200 and d["ok"], d
    return priv, d["fm_id"]


def adopt(c, priv, fm_id, species="driplet", name="Pal"):
    r = c.post("/api/pets/adopt",
               json=signed_body(priv, "pet_adopt", fm_id,
                                species=species, name=name))
    assert r.status_code == 200, r.get_data(as_text=True)[:200]


def event_types_for(db, fm_id):
    evs = events.poll_events(db, fm_id, limit=200)
    return [e["type"] for e in evs]


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.AGENT_KEY = "test-agent-key"
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()
    db = appmod.db

    print("== A2 schema endpoint ==")
    r = c.get("/api/pets/schema.json")
    d = r.get_json()
    check("schema 200", r.status_code == 200 and d.get("ok"), r.status_code)
    schema = d["schema"]
    check("schema name", schema["name"] == "musefm-tidepals-v1", schema.get("name"))

    rules = {rule.rule for rule in appmod.app.url_map.iter_rules()}

    def path_of(entry):
        # "POST /api/pets/adopt" or "GET /api/events?since=<id>"
        p = entry.split(" ", 1)[1] if " " in entry else entry
        return p.split("?", 1)[0]

    listed = []
    for section in ("pages", "reads"):
        for entry in schema[section]:
            listed.append((section, entry, path_of(entry)))
    for entry in ("GET /api/events?since=<id>", "POST /api/webhooks"):
        listed.append(("webhooks", entry, path_of(entry)))
    for entry, spec in schema["writes"].items():
        listed.append(("writes", entry, path_of(entry)))
    missing = [f"{sec}:{e}" for sec, e, p in listed if p not in rules]
    check("every schema route exists in url_map", not missing, str(missing[:5]))

    pet_types = set(schema["webhooks"]["pet_event_types"])
    check("schema pet types registered",
          pet_types <= set(events.EVENT_TYPES), str(pet_types - set(events.EVENT_TYPES)))
    check("all 7 pet event types listed", len(pet_types) == 7, str(pet_types))

    print("== A3 custody digest ==")
    r = c.get("/api/pets/mine")
    check("mine unsigned -> 4xx", r.status_code in (400, 401, 403),
          r.status_code)
    privA, fmA = reg(c, "WowOwner")
    privB, fmB = reg(c, "WowCo")
    privC, fmC = reg(c, "WowStranger")
    adopt(c, privA, fmA, name="Aqua")
    adopt(c, privB, fmB, name="Bubbles")

    r = c.get("/api/pets/mine",
              query_string=dict(signed_body(privA, "pets_read", fmA)))
    d = r.get_json()
    check("mine 200", r.status_code == 200 and d.get("ok"), d)
    check("owner sees own pet",
          d["count"] == 1 and d["pets"][0]["role"] == "owner"
          and d["pets"][0]["name"] == "Aqua", str(d)[:200])
    check("digest carries care state",
          "hunger" in d["pets"][0] and "mood" in d["pets"][0],
          str(sorted(d["pets"][0].keys()))[:160])

    # co-raise: owner invites B, B accepts -> B's digest shows the pet
    r = c.post("/api/pet/coraise/invite",
               json=signed_body(privA, "pet_coraise", fmA, handle="WowCo"))
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    r = c.post("/api/pet/coraise/accept",
               json=signed_body(privB, "pet_coraise", fmB, pet_fm_id=fmA))
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    r = c.get("/api/pets/mine",
              query_string=dict(signed_body(privB, "pets_read", fmB)))
    d = r.get_json()
    roles = {(p["name"], p["role"]) for p in d["pets"]}
    check("co-owner digest: own + co-raised",
          d["count"] == 2 and ("Aqua", "co-owner") in roles
          and ("Bubbles", "owner") in roles, str(roles))
    r = c.get("/api/pets/mine",
              query_string=dict(signed_body(privC, "pets_read", fmC)))
    d = r.get_json()
    check("no pet -> empty digest", d["count"] == 0 and d["pets"] == [], str(d))

    print("== A1 pet events -> feed ==")
    pet = pets.get_pet(db, fmA)
    fired = pets._check_stage_up(db, fmA, pet, 1, "Hatchling")
    check("stage-up fires", fired is True)
    check("pet_stage_up in feed",
          "pet_stage_up" in event_types_for(db, fmA))

    tps.pat(db, fmC, "WowStranger", fmA)
    check("pet_patted in feed", "pet_patted" in event_types_for(db, fmA))

    # streak milestone: yesterday streak=2 -> feeding today hits 3
    day = 86400
    t_now = pets.now()
    db._exec("UPDATE pet_care SET last_fed=?, feed_streak=? WHERE fm_id=?",
             (t_now - day, 2, fmA))
    pets.feed_pet(db, fmA)
    check("pet_care_streak in feed",
          "pet_care_streak" in event_types_for(db, fmA))

    res = pets.earn_item(db, fmA, "party_hat", "event")
    if not res["earned"]:
        # already owned from an earlier run path — force a fresh item
        res = pets.earn_item(db, fmA, "sailor_scarf", "event")
    check("earn ok", res["earned"] or res["already_owned"], str(res))
    check("pet_wardrobe_earned in feed",
          "pet_wardrobe_earned" in event_types_for(db, fmA))

    check("pet_coraise_invite in feed",
          "pet_coraise_invite" in event_types_for(db, fmB))
    check("pet_coraise_accept in feed",
          "pet_coraise_accept" in event_types_for(db, fmA))

    # ritual win event: visitor equips via XP, one vote, resolve Saturday
    from zoneinfo import ZoneInfo
    privV, fmV = reg(c, "WowVisitor")
    adopt(c, privV, fmV, name="Coral")
    tps.award_pet_xp(db, fmV, 60)
    assert fmV in tps.fashion_friday_entries(db)
    fri = int(__import__("datetime").datetime(
        2026, 9, 18, 12, 0, tzinfo=ZoneInfo("America/Chicago")).timestamp())
    sat = int(__import__("datetime").datetime(
        2026, 9, 19, 1, 0, tzinfo=ZoneInfo("America/Chicago")).timestamp())
    tps.vote_fashion_friday(db, fmA, fmV, ts=fri)
    res = tps.resolve_fashion_friday(db, ts=sat)
    check("resolve crowns", res["winner_fm_id"] == fmV, str(res)[:160])
    check("pet_ritual_won in feed",
          "pet_ritual_won" in event_types_for(db, fmV))

    print("== H1/H2/H4 template markers ==")
    for tpl, need in [
        ("templates/tidepals.html",
         ["pet-motion", "mood-{{ p.mood }}", "@media (max-width:640px)",
          "prefers-reduced-motion", "@keyframes petBob"]),
        ("templates/pet_visit.html",
         ["pet-motion", "mood-{{ pet.mood }}", "@media (max-width:640px)",
          "burstHearts", "prefers-reduced-motion"]),
        ("templates/pet.html",
         ["pet-motion", "mood-{{ my_pet.mood }}", "@media (max-width:640px)",
          "evo-ceremony", "flashPop", "prefers-reduced-motion"]),
    ]:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               tpl)) as f:
            html = f.read()
        missing = [n for n in need if n not in html]
        check(f"{tpl} wow markers", not missing, str(missing))

    print(f"\n{passed} passed, {failed} failed")
    if failures:
        print("FAILURES:", failures)
        sys.exit(1)


if __name__ == "__main__":
    main()
