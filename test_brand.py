#!/usr/bin/env python3
"""
Tests for the graphic Muse FM wordmark brand (2026-09-18):
- the header brand, sidebar title, and footer render the graphic wordmark
  SVG (class="wordmark") instead of plain-text "Muse FM"
- the wordmark SVG has explicit width/height (no layout shift)
- a11y: the header link keeps aria-label "Muse FM home"; the standalone
  sidebar/footer marks expose role="img" aria-label="Muse FM"
- favicon: both the new mark SVG and the regenerated PNG serve (200)
- /static/img/muse-fm-mark.svg is valid SVG with the broadcast mark

Run:  python3 test_brand.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod

TEST_DB = "/tmp/test-townsquare-brand.db"
TEST_DATA = "/tmp/test-townsquare-brand-data"

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


def t_wordmark_chrome(client):
    print("== chrome wordmark ==")
    html = client.get("/").get_data(as_text=True)
    check("home 200", client.get("/").status_code == 200)

    marks = re.findall(r'<svg class="wordmark"[^>]*>', html)
    check("three wordmark SVGs on the page (topbar, sidebar, footer)",
          len(marks) == 3, f"found {len(marks)}")
    for i, m in enumerate(marks):
        check(f"wordmark {i} has explicit width+height",
              'width="' in m and 'height="' in m)

    # a11y: link labelled, decorative svg inside
    check('brand link keeps aria-label="Muse FM home"',
          'aria-label="Muse FM home"' in html)
    link_svg = re.search(r'aria-label="Muse FM home">\s*<svg class="wordmark"[^>]*>',
                         html)
    check("header wordmark is decorative (aria-hidden)",
          link_svg is not None and 'aria-hidden="true"' in link_svg.group(0))

    # standalone marks expose the name to screen readers
    check("standalone marks use role=img + aria-label",
          html.count('role="img" aria-label="Muse FM"') == 2)

    # old plain-text chrome is gone
    check("no old brand-word span", 'class="brand-word"' not in html)
    check("no old droplet brand-mark", 'class="brand-mark"' not in html)
    check("sidebar title has no plain-text Muse FM",
          '<span class="sidebar-title">Muse FM</span>' not in html)

    # lettering is a graphic: svg <text>, not HTML text
    check("wordmark uses svg text element", "<text " in html)

    # screen readers still hear the name somewhere
    check('"Muse FM" still hearable',
          'aria-label="Muse FM' in html)


def t_favicon(client):
    print("== favicon ==")
    r = client.get("/static/img/muse-fm-mark.svg")
    check("mark.svg 200", r.status_code == 200)
    check("mark.svg content type",
          "svg" in (r.content_type or ""), r.content_type)
    body = r.get_data(as_text=True)
    check("mark.svg is valid svg with broadcast arcs",
          "<svg" in body and "<linearGradient" in body and "M34.25" in body)
    r = client.get("/static/img/favicon.png")
    check("favicon.png 200", r.status_code == 200)
    check("favicon.png content type", "png" in (r.content_type or ""),
          r.content_type)
    check("favicon.png non-trivial size", len(r.get_data()) > 2000)
    html = client.get("/").get_data(as_text=True)
    check("svg favicon linked in head",
          'rel="icon" type="image/svg+xml"' in html and
          "img/muse-fm-mark.svg" in html)
    check("png favicon fallback still linked",
          'rel="icon" type="image/png"' in html)


def t_all_pages(client):
    print("== wordmark on other pages ==")
    for path in ("/shorts", "/musefm", "/episodes", "/signal", "/login",
                 "/settings"):
        r = client.get(path)
        body = r.get_data(as_text=True)
        check(f"{path} has graphic wordmark",
              r.status_code in (200, 302, 401) and
              (r.status_code != 200 or 'class="wordmark"' in body))


def main():
    client = setup()
    t_wordmark_chrome(client)
    t_favicon(client)
    t_all_pages(client)
    print(f"\nbrand: {len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
