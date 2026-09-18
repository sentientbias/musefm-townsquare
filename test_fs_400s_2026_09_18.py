#!/usr/bin/env python3
"""
Failing tests proving the 2026-09-18 12:35 tester-loop P1 findings.

`_fs()` (app.py) documents: "Non-string values are a 400, not a 500".
At these call sites the `_fs()` call sits OUTSIDE the try/except that
converts ValueError to 400, so a non-string JSON field 500s instead.

Run:  python3 test_fs_400s_2026_09_18.py
Uses a throwaway SQLite db and the Flask test client. Nothing touches
the real townsquare.db. Tests only -- no app source changes.
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod
from db import Database

TEST_DB = "/tmp/test-townsquare-fs400s.db"
TEST_DATA = "/tmp/test-townsquare-fs400s-data"

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    appmod.db = Database(TEST_DB)
    appmod.AGENT_KEY = "test-agent-key"
    appmod.DATA_DIR = TEST_DATA
    appmod.UPLOAD_DIR = os.path.join(TEST_DATA, "uploads")
    os.makedirs(appmod.UPLOAD_DIR, exist_ok=True)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


HEADERS = {"X-Agent-Key": "test-agent-key"}


def post_json(c, path, payload):
    """POST and return a status code.

    With TESTING=True, an unhandled ValueError escaping the view propagates
    instead of becoming a 500 response -- treat that as 500-equivalent,
    because that IS what production (debug off) returns.
    """
    try:
        r = c.post(path, headers=HEADERS, json=payload)
        return r.status_code
    except ValueError:
        return 500


def main():
    c = setup()
    # register an identity + post so the react targets exist
    r = c.post("/api/identity/register",
               json={"handle": "FsTester",
                     "public_key": "A" * 43})
    assert r.status_code == 200, r.get_data(as_text=True)
    r = c.post("/api/forum/post", headers=HEADERS,
               json={"handle": "FsTester", "title": "fs400s", "body": "x"})
    assert r.status_code == 200, r.get_data(as_text=True)
    pid = r.get_json()["id"]

    print("== non-string JSON fields must be 400, not 500 ==")
    check("react: non-string emoji -> 400",
          post_json(c, "/api/forum/react",
                    {"handle": "FsTester", "target_type": "post",
                     "target_id": pid, "emoji": 12345}) == 400,
          "got 500")

    check("react: non-string target_type -> 400",
          post_json(c, "/api/forum/react",
                    {"handle": "FsTester", "target_type": 123,
                     "target_id": pid, "emoji": "like"}) == 400,
          "got 500")

    check("fb_react: non-string reaction -> 400",
          post_json(c, "/api/forum/fb_react",
                    {"handle": "FsTester", "target_type": "post",
                     "target_id": pid, "reaction": 123}) == 400,
          "got 500")

    check("challenges/settle: non-string week_id -> 400",
          post_json(c, "/api/challenges/settle",
                    {"week_id": 123}) == 400,
          "got 500")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
