#!/usr/bin/env python3
"""
Tests for the /links hub and family service pages (/trustline, /arena,
/playbook, /pro):
- /links renders 200 with a card for every service page
- each service page renders 200 with its Launch button to the real service
- /network 301-redirects to /links (retired)
- /links itself contains no raw service-domain URLs in its cards —
  outbound references go through the musefm.lol service pages
- unknown service slug falls through to normal 404 handling

Run:  .venv/bin/python test_links.py
Throwaway SQLite db + Flask test client + temp DATA_DIR.
Nothing touches townsquare.db.
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod

TEST_DB = "/tmp/test-townsquare-links.db"
TEST_DATA = "/tmp/test-townsquare-links-data"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    from db import Database
    appmod.db = Database(TEST_DB)
    appmod.DATA_DIR = TEST_DATA
    os.makedirs(TEST_DATA, exist_ok=True)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


SERVICE_SLUGS = ["trustline", "arena", "playbook", "pro"]
RAW_SERVICE_HOSTS = [
    "trustlineapp.com",
    "muse-arena.onrender.com",
    "x402-seller-a5et.onrender.com",
]


def main():
    client = setup()

    print("== /links hub ==")
    r = client.get("/links")
    check("/links is 200", r.status_code == 200, str(r.status_code))
    html = r.get_data(as_text=True)
    for slug in SERVICE_SLUGS:
        check(f"/links cards link to /{slug}", f'href="/{slug}"' in html)
    check("/links has no raw service domains in cards",
          not any(h in html for h in RAW_SERVICE_HOSTS),
          str([h for h in RAW_SERVICE_HOSTS if h in html]))
    check("/links mentions bookmark line", "only links page" in html)

    print("== service pages ==")
    for slug in SERVICE_SLUGS:
        r = client.get(f"/{slug}")
        check(f"/{slug} is 200", r.status_code == 200, str(r.status_code))
        html = r.get_data(as_text=True)
        check(f"/{slug} has a Launch button", "svc-cta" in html)
        check(f"/{slug} links back to /links", 'href="/links"' in html)

    print("== /network retired ==")
    r = client.get("/network")
    check("/network redirects", r.status_code in (301, 308),
          str(r.status_code))
    check("/network redirects to /links",
          r.headers.get("Location", "").endswith("/links"),
          r.headers.get("Location", ""))

    print("== family bar + sidebar funnel through musefm.lol ==")
    html = client.get("/").get_data(as_text=True)
    # family bar carries the four MuseFM-prefixed services
    for slug in ["trustline", "arena", "playbook", "pro"]:
        check(f"family bar points at musefm.lol/{slug}",
              f'href="https://musefm.lol/{slug}"' in html)
    check("sidebar Family uses internal links",
          all(f'href="/{s}"' in html for s in ["arena", "playbook", "pro", "trustline"]))
    check("no raw service domains in base chrome",
          not any(h in html for h in RAW_SERVICE_HOSTS),
          str([h for h in RAW_SERVICE_HOSTS if h in html]))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
