#!/usr/bin/env python3
"""
Tests for the Reddit/Meta-style left sidebar + UI cleanup:
- sidebar renders on every base.html page with all section links
- sidebar comes AFTER main content in the DOM (content-first, fixed-position chrome)
- active states highlight the current section
- fullscreen shorts pages hide the chrome via CSS (body.shorts-mode)
- mobile drawer elements (hamburger, scrim, toggleSidebar) present
- Share-to-X wording says "Muse FM", never "Muse FM Town Square"
- no "brother" in product UI/copy; no "Town Square" branding anywhere

Run:  .venv/bin/python test_sidebar.py
Throwaway SQLite db + Flask test client + temp DATA_DIR.
Nothing touches townsquare.db.
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod

TEST_DB = "/tmp/test-townsquare-sidebar.db"
TEST_DATA = "/tmp/test-townsquare-sidebar-data"

PASS, FAIL = [], []
HERE = os.path.dirname(os.path.abspath(__file__))


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


SIDEBAR_LINKS = [
    "/", "/shorts", "/musefm", "/episodes", "/musefm/shorts",
    "/musefm/photos", "/submit", "/upload", "/pet", "/shop",
    "/signal", "/links", "/api/docs",
    "/arena", "/playbook", "/pro", "/trustline",
]


def main():
    client = setup()

    print("== sidebar renders everywhere ==")
    for path in ("/", "/musefm", "/episodes", "/shorts", "/musefm/shorts",
                 "/signal", "/links", "/api/docs", "/pet", "/shop"):
        html = client.get(path).get_data(as_text=True)
        check(f"sidebar on {path}", 'id="sidebar"' in html and 'class="sb-link' in html)
        missing = [h for h in SIDEBAR_LINKS if f'href="{h}"' not in html]
        check(f"all section links on {path}", not missing, str(missing))

    print("== content-first DOM order ==")
    html = client.get("/").get_data(as_text=True)
    wrap_i = html.find('<div class="wrap">')
    sb_i = html.find('id="sidebar"')
    check("sidebar after main content in DOM", 0 < wrap_i < sb_i)

    print("== active states ==")
    html = client.get("/").get_data(as_text=True)
    check("home highlights Forum",
          'class="sb-link active" href="/"' in html)
    html = client.get("/musefm").get_data(as_text=True)
    check("musefm hub highlights Muse FM",
          'class="sb-link active" href="/musefm"' in html)
    html = client.get("/episodes").get_data(as_text=True)
    check("episodes highlights Episodes",
          'class="sb-link active" href="/episodes"' in html)
    html = client.get("/musefm/shorts").get_data(as_text=True)
    check("fm shorts highlights FM Shorts",
          'class="sb-link active" href="/musefm/shorts"' in html)

    print("== fullscreen shorts chrome hidden via CSS ==")
    for path in ("/shorts", "/musefm/shorts"):
        html = client.get(path).get_data(as_text=True)
        check(f"{path} is shorts-mode", 'class="shorts-mode"' in html)

    print("== mobile drawer wiring ==")
    html = client.get("/").get_data(as_text=True)
    check("hamburger button", 'id="sb-toggle"' in html)
    check("scrim", 'id="sb-scrim"' in html)
    check("sidebar close button", 'class="sidebar-close"' in html)
    appjs = open(os.path.join(HERE, "static", "js", "app.js")).read()
    check("toggleSidebar defined", "function toggleSidebar(" in appjs)
    check("Escape closes drawer", 'key === \'Escape\'' in appjs or 'key === "Escape"' in appjs)

    print("== branding cleanup ==")
    check("share-to-X says Muse FM (not Muse FM Town Square)",
          "Muse FM Town Square" not in appjs)
    base = open(os.path.join(HERE, "templates", "base.html")).read()
    check("no 'Muse FM Town Square' in base.html", "Muse FM Town Square" not in base)
    check("no 'Town Square' in base.html", "Town Square" not in base)
    check("forum section labeled 'Forum'", "'⌂', 'Forum'" in base)
    # footer no longer duplicates the brand as the show-page link label
    check("footer show-page link labeled 'Show page'",
          ">Show page</a>" in base)
    for fname in os.listdir(os.path.join(HERE, "templates")):
        if not fname.endswith(".html"):
            continue
        t = open(os.path.join(HERE, "templates", fname)).read()
        check(f"no 'brother' in {fname}", "brother" not in t.lower())
    check("no 'brother' in app.js", "brother" not in appjs.lower())
    check("old top nav gone", 'class="nav"' not in base and "nav-links" not in base)

    print("== sidebar account block ==")
    from db import ensure_human_auth_schema, ensure_linking_schema
    ensure_human_auth_schema(appmod.db)
    ensure_linking_schema(appmod.db)
    me = appmod.app.test_client()
    r = me.post("/signup", data={"handle": "AcctBlockUser",
                                 "password": "supersecret1",
                                 "password_confirm": "supersecret1"})
    check("signup for account-block test", r.status_code == 200, r.status_code)
    r = me.post("/login", data={"handle": "AcctBlockUser",
                                "password": "supersecret1"})
    check("login for account-block test", r.status_code == 302, r.status_code)
    html = me.get("/").get_data(as_text=True)
    acct_i = html.find('class="sb-account"')
    top_i = html.find('class="sidebar-top"')
    group_i = html.find('class="sb-group"')
    check("account block renders when logged in", acct_i != -1)
    check("account block sits above the nav groups",
          -1 < top_i < acct_i < group_i)
    block = html[acct_i:acct_i + 900]
    check("account block shows @handle", "@AcctBlockUser" in block)
    check("account block links the profile", 'href="/m/fm_' in block)
    check("account block links Settings", 'href="/settings"' in block)
    check("topbar no longer carries the account chip",
          'class="auth-chip"' not in html)
    check("topbar settings gear removed", 'title="Settings"' not in html)
    settings_html = me.get("/settings").get_data(as_text=True)
    check("Settings link highlights on /settings",
          'class="sb-account-link active"' in settings_html)
    anon = appmod.app.test_client()
    anon_html = anon.get("/").get_data(as_text=True)
    check("no account block when logged out",
          'class="sb-account"' not in anon_html)
    check("logged-out topbar keeps Log in / Sign up",
          'href="/login"' in anon_html and 'href="/signup"' in anon_html)

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
