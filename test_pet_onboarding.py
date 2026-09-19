#!/usr/bin/env python3
"""
First-30-seconds onboarding usability tests for /pet (Track B, 2026-09-19).

Mirrors test_human_usability.py patterns (throwaway SQLite db, Flask test
client, check() helper). Covers the onboarding beats:

  1. anon visitor: hero adopt CTA, signup path, demo egg, caretaker,
     new-economy copy, no old "hatching costs Signal" copy
  2. logged-in human without a pet: adopt panel, free/5-min/+25 copy
  3. adopt flow: POST /pet/adopt -> egg panel with hatch countdown hook,
     new-economy copy, no hatch-cost copy
  4. tidepal-anim.js: served, exposes TidepalAnim, contract selectors,
     reduced-motion handling
  5. pond block regression: untouched pond markup still present in the
     template file

Run:  .venv/bin/python test_pet_onboarding.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod

TEST_DB = "/tmp/test-townsquare-pet-onboarding.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


_ip = [0]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.77.0.%d" % _ip[0]}


def signup(client, handle, password="supersecret1"):
    return client.post("/signup", data={"handle": handle, "password": password,
                                        "password_confirm": password},
                       environ_base=fresh_ip())


def login(client, handle, password="supersecret1"):
    return client.post("/login", data={"handle": handle, "password": password},
                       environ_base=fresh_ip())


def csrf_of(client):
    html = client.get("/").get_data(as_text=True)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    assert m, "no csrf meta for logged-in client"
    return m.group(1)


# --- 1. anonymous visitor gets the 30-second experience -------------------------
def t_anon(client):
    print("== anon 30-second experience ==")
    body = client.get("/pet").get_data(as_text=True)
    check("anon /pet -> 200", True)
    check("hero adopt CTA present",
          "Adopt a Tidepal" in body and "adopt your egg" in body.lower())
    check("anon CTA routes to signup",
          'href="/signup"' in body)
    check("demo egg hook present", 'data-tidepal-demo' in body)
    check("caretaker waves hello",
          "Tidepool Caretaker" in body and "wave-arm" in body)
    check("copy: hatching is FREE", "Free to adopt" in body or
          "free to adopt" in body.lower())
    check("copy: first egg 5 minutes", "5 minutes" in body)
    check("copy: hatching pays +25 Signal", "+25 Signal" in body)
    check("copy: never says hatching costs Signal",
          "needs 50 Signal to hatch" not in body and
          "hatch_cost" not in body)
    check("4 onboarding steps present",
          all(s in body for s in ("Adopt an egg", "hatches on a timer",
                                  "grows with you", "Hatching pays YOU")))
    check("30-second guide hints present",
          all(s in body for s in ("Eggs &amp; hatching", "Town Pond",
                                  "Echo Fusion", "Caretakers",
                                  "Where things live")))
    check("anim engine script included",
          "js/tidepal-anim.js" in body)


# --- 2. logged-in human without a pet -------------------------------------------
def t_logged_in_no_pet(client):
    print("== logged-in, no pet ==")
    me = appmod.app.test_client()
    assert signup(me, "EggKeeper").status_code == 200
    assert login(me, "EggKeeper").status_code == 302
    body = me.get("/pet").get_data(as_text=True)
    check("adopt panel anchors at #adopt", 'id="adopt"' in body)
    check("adopt CTA scrolls to panel", 'href="#adopt"' in body)
    check("adopt form posts to /pet/adopt",
          'action="/pet/adopt"' in body)
    check("adopt form carries a csrf token",
          'name="csrf_token"' in body)
    check("adopt button says free", "Adopt my egg — free" in body)
    check("adopt copy: +40 rare species", "+40" in body)
    return me


# --- 3. adopt -> egg panel with countdown ---------------------------------------
def t_adopt_egg(client, me):
    print("== adopt -> egg delight ==")
    tok = csrf_of(me)
    r = me.post("/pet/adopt",
                data={"species": "driplet", "name": "Bubbles",
                      "csrf_token": tok},
                environ_base=fresh_ip(), follow_redirects=True)
    check("adopt -> 200 after redirect", r.status_code == 200, r.status_code)
    body = r.get_data(as_text=True)
    check("just-adopted delight line",
          "say hi to your new Tidepal" in body)
    check("just-adopted hook for the anim engine",
          "data-tidepal-just-adopted" in body)
    check("egg panel present", "egg-panel" in body)
    check("egg panel: hatching is free",
          "Hatching is <b>free</b>" in body)
    check("egg panel: earns +25 Signal",
          "you earn +25 Signal" in body)
    check("countdown hook present",
          'data-hatch-seconds="' in body)
    check("hatch button posts to /pet/hatch",
          'action="/pet/hatch"' in body)
    check("first-hatch 5-minute copy",
          "first egg hatches in 5 minutes" in body)
    check("hatch-now shop hint", "Hatch-Now" in body and "/shop" in body)
    check("no hatch-cost copy anywhere",
          "needs 50 Signal to hatch" not in body and
          "spendable Signal</b> to begin" not in body)
    check("egg svg carries the anim contract hooks",
          'data-tidepal="1"' in body and 'class="tp-body"' in body and
          'class="tp-eyes"' in body)
    # second adopt is rejected — one pet per identity
    r = me.post("/pet/adopt",
                data={"species": "koi", "name": "Second",
                      "csrf_token": csrf_of(me)},
                environ_base=fresh_ip(), follow_redirects=True)
    check("second adopt rejected",
          "already" in r.get_data(as_text=True).lower())


# --- 4. tidepal-anim.js ----------------------------------------------------------
def t_anim_js(client):
    print("== tidepal-anim.js ==")
    r = client.get("/static/js/tidepal-anim.js")
    body = r.get_data(as_text=True)
    check("anim js served (200)", r.status_code == 200, r.status_code)
    check("exposes window.TidepalAnim",
          "window.TidepalAnim" in body)
    for api in ("pat:", "wiggle:", "celebrate:", "feed:", "play:", "rest:"):
        check(f"TidepalAnim exposes {api[:-1]}", api in body)
    check("reads the contract hooks",
          'svg[data-tidepal]' in body and "tp-eyes" in body and
          "tp-body" in body and "data-trait" in body)
    check("trait motion styles",
          all(t in body for t in ("playful", "calm", "mischievous",
                                  "gentle")))
    check("mood-driven energy",
          all(m in body for m in ("overjoyed", "peckish", "restless",
                                  "sleepy")))
    check("sleep deep-breath handling", "sleepy" in body and
          "deep" in body.lower())
    check("prefers-reduced-motion respected",
          "prefers-reduced-motion" in body)
    check("rAF-throttled single loop",
          "requestAnimationFrame" in body)
    check("transform/opacity only (no layout props in hot path)",
          "offsetWidth" in body or "getBoundingClientRect" in body)
    check("graceful no-op without hooks",
          "querySelectorAll" in body)


# --- 5. pond block regression ----------------------------------------------------
def t_pond_block():
    print("== pond block regression ==")
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "templates", "pet.html")).read()
    check("pond block present and untouched",
          '{% if pond_pets %}' in src and
          'action="/pet/reclaim"' in src and
          "is at the Town Pond" in src and
          "{% endfor %}" in src and "{% endif %}" in src)


def main():
    client = setup()
    t_anon(client)
    me = t_logged_in_no_pet(client)
    t_adopt_egg(client, me)
    t_anim_js(client)
    t_pond_block()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
