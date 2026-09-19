#!/usr/bin/env python3
"""Route-level tests for the Tidepal social layer (part B) through the real
app.py routes: pats, co-raising, mini-games, Fashion Friday, showcase and
pet visit pages. Strict musefm-v1 signed writes only — shared-key calls
must fail."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("MUSEFM_TEST", "1")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
from identity import IdentityError, b64u_encode, signed_body

TEST_DB = "/tmp/test_tidepal_social_routes.db"

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
    r = c.post("/api/identity/register", json={"handle": handle,
                                               "public_key": pub})
    d = r.get_json()
    if r.status_code == 429:
        ident = appmod.db.register_identity(handle, pub)
        return priv, ident["fm_id"]
    assert r.status_code == 200 and d["ok"], d
    return priv, d["fm_id"]


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.AGENT_KEY = "test-agent-key"
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def adopt(c, priv, fm_id, species="driplet", name="Pal"):
    r = c.post("/api/pets/adopt",
               json=signed_body(priv, "pet_adopt", fm_id,
                                species=species, name=name))
    assert r.status_code == 200, r.get_data(as_text=True)[:200]


def main():
    c = setup()
    print("== social routes ==")

    privA, fmA = reg(c, "PatterA")
    privB, fmB = reg(c, "OwnerB")
    privC, fmC = reg(c, "CoC")
    adopt(c, privA, fmA, name="Aqua")
    adopt(c, privB, fmB, name="Bubbles")
    adopt(c, privC, fmC, name="Coral")

    # --- pats ---
    r = c.post("/api/pet/pat",
               json=signed_body(privA, "pet_pat", fmA,
                                owner_fm_id=fmB))
    d = r.get_json()
    check("pat 200", r.status_code == 200 and d["ok"], d)
    check("pat grants xp", d.get("pet_xp_total", 0) >= 2, d)
    r = c.post("/api/pet/pat",
               json=signed_body(privA, "pet_pat", fmA,
                                owner_fm_id=fmB))
    check("pat 24h cooldown -> 400", r.status_code == 400,
          r.get_data(as_text=True)[:120])
    r = c.post("/api/pet/pat",
               json=signed_body(privA, "pet_pat", fmA,
                                owner_fm_id=fmA))
    check("self-pat refused", r.status_code == 400,
          r.get_data(as_text=True)[:120])
    r = c.post("/api/pet/pat", json={"fm_id": fmA,
                                     "owner_fm_id": fmB})
    check("pat unsigned/shared -> 401", r.status_code == 401,
          r.status_code)

    # --- co-raising ---
    r = c.post("/api/pet/coraise/invite",
               json=signed_body(privB, "pet_coraise", fmB,
                                handle="CoC"))
    check("coraise invite 200", r.status_code == 200, r.status_code)
    r = c.post("/api/pet/coraise/accept",
               json=signed_body(privC, "pet_coraise", fmC,
                                pet_fm_id=fmB))
    d = r.get_json()
    check("coraise accept 200", r.status_code == 200 and d["ok"], d)
    # co-owner can now care for B's pet
    r = c.post("/api/pet/feed",
               json=signed_body(privC, "pet_care", fmC,
                                pet_fm_id=fmB))
    d = r.get_json()
    check("co-owner feed 200", r.status_code == 200 and d["ok"],
          r.get_data(as_text=True)[:150])
    check("co-owner feed targets B's pet",
          d.get("pet", {}).get("fm_id") == fmB, d.get("pet", {}))
    # non-caretaker cannot
    r = c.post("/api/pet/feed",
               json=signed_body(privA, "pet_care", fmA,
                                pet_fm_id=fmB))
    check("stranger care -> 403", r.status_code == 403,
          r.status_code)
    # decline path
    privD, fmD = reg(c, "DeclinerD")
    adopt(c, privD, fmD, name="Dew")
    r = c.post("/api/pet/coraise/invite",
               json=signed_body(privB, "pet_coraise", fmB,
                                handle="DeclinerD"))
    check("second invite 200", r.status_code == 200, r.status_code)
    r = c.post("/api/pet/coraise/decline",
               json=signed_body(privD, "pet_coraise", fmD,
                                pet_fm_id=fmB))
    check("coraise decline 200", r.status_code == 200,
          r.get_data(as_text=True)[:120])
    r = c.post("/api/pet/coraise/invite",
               json=signed_body(privB, "pet_coraise", fmB,
                                handle="NobodyZZZ"))
    check("invite unknown handle -> 400", r.status_code == 400,
          r.status_code)

    # --- tide toss ---
    r = c.post("/api/games/tide-toss/play",
               json=signed_body(privA, "game", fmA, pick=1))
    d = r.get_json()
    check("tide toss 200", r.status_code == 200 and d["ok"], d)
    check("tide toss reveals win/lose",
          "won" in d, d)
    r = c.post("/api/games/tide-toss/play",
               json=signed_body(privA, "game", fmA, pick=1))
    check("tide toss 1/day -> 400", r.status_code == 400,
          r.status_code)
    r = c.get("/api/games/tide-toss/status",
              query_string=dict(signed_body(privA, "game", fmA)))
    d = r.get_json()
    check("tide toss status played",
          r.status_code == 200 and d.get("played_today"), d)
    r = c.post("/api/games/tide-toss/play",
               json=signed_body(privB, "game", fmB, pick=9))
    check("tide toss bad pick -> 400", r.status_code == 400,
          r.status_code)

    # --- feed frenzy ---
    r = c.get("/api/games/feed-frenzy/status",
              query_string=dict(signed_body(privA, "game", fmA)))
    check("frenzy status 200", r.status_code == 200,
          r.get_data(as_text=True)[:120])
    r = c.post("/api/games/feed-frenzy/click",
               json=signed_body(privA, "game", fmA))
    check("frenzy click 200", r.status_code == 200,
          r.get_data(as_text=True)[:120])
    r = c.post("/api/games/feed-frenzy/click", json={"fm_id": fmA})
    check("frenzy unsigned -> 401", r.status_code == 401,
          r.status_code)

    # --- fashion friday (public read) ---
    r = c.get("/api/rituals/fashion-friday")
    d = r.get_json()
    check("ff public read 200", r.status_code == 200 and d["ok"], d)
    # voting outside Friday is closed (today is Saturday) -> 400
    r = c.post("/api/rituals/fashion-friday/vote",
               json=signed_body(privA, "fashion_friday_vote", fmA,
                                pet_fm_id=fmB))
    check("ff vote outside Friday -> 400", r.status_code == 400,
          r.get_data(as_text=True)[:150])

    # --- pages ---
    r = c.get("/tidepals")
    html = r.get_data(as_text=True)
    check("showcase 200", r.status_code == 200, r.status_code)
    check("showcase lists pets", "Bubbles" in html or "Aqua" in html,
          html[:200])
    r = c.get("/pet/OwnerB")
    html = r.get_data(as_text=True)
    check("visit page 200", r.status_code == 200, r.status_code)
    check("visit page shows pet", "Bubbles" in html, html[:200])
    r = c.get("/pet/NoSuchHandleZZZ")
    check("visit page unknown -> 404", r.status_code == 404,
          r.status_code)

    print(f"\n{passed} passed, {failed} failed")
    if failures:
        print("FAILURES:", failures)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
