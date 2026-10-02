#!/usr/bin/env python3
"""
Proving test for P0 2026-10-01 (musefm-tester-loop 18:35 run):
GET /studio/<job_id>.mp4 500s with an unhandled sqlite3.OperationalError
("no such table: studio_jobs") when the studio schema was never created.

Root cause: studio.ensure_studio_schema exists (studio.py:65) but is wired
into NOTHING — app.init_db never calls it, so any fresh database
(including this loop's scratch test.db and evidently production)
has no studio_jobs table. studio.get_job (studio.py:182) then throws
inside the public route studio_artifact (app.py:12470) instead of
returning None for the route's 404 path.

Run: python3 test_testerloop_studio_missing_table_2026_10_01.py
Throwaway SQLite db. Nothing touches townsquare.db or production.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def main():
    import app as appmod
    tmpdir = tempfile.mkdtemp(prefix="studio-missing-table-")
    test_db = os.path.join(tmpdir, "test.db")
    data_dir = os.path.join(tmpdir, "data")
    os.makedirs(data_dir, exist_ok=True)

    # Fresh boot exactly like the loop does it (init_db runs the full
    # ensure sequence; the bug is that studio is not part of it).
    appmod.db = appmod.init_db(test_db)
    appmod.DATA_DIR = data_dir
    appmod.app.config["TESTING"] = True

    table = appmod.db._one(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='studio_jobs'")
    print("studio_jobs table present after init_db:", bool(table))

    client = appmod.app.test_client()
    for job_id in ("deadbeef", "00000000"):
        try:
            r = client.get(f"/studio/{job_id}.mp4")
            check(f"unknown studio job {job_id} -> 404, not 500",
                  r.status_code == 404, r.status_code)
        except Exception as e:
            # TESTING mode propagates the app's exception instead of
            # rendering the 500: same root cause, surfaced directly.
            check(f"unknown studio job {job_id} -> 404, not 500", False,
                  f"{type(e).__name__}: {e}")

    print("\n%d passed, %d failed: %s" % (len(PASS), len(FAIL), FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
