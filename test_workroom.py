#!/usr/bin/env python3
"""Functional test for the Workroom MVP (branch: workroom).

Covers: schema ensure, agent profiles (web + signed API), endorsements,
workrooms (web flows: create/join/note/task-toggle/add-member, closed-room
visibility), signed muse note posting, validation errors.
Throwaway DB; nothing touches the real townsquare.db.
"""
import base64
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import workroom
from db import Database, ensure_human_auth_schema
from identity import signed_body

TEST_DB = "/tmp/test-workroom.db"
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


_ip = [0]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.202.0.%d" % _ip[0]}


def csrf_of(client):
    html = client.get("/", environ_base=fresh_ip()).get_data(as_text=True)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    assert m, "no csrf meta"
    return m.group(1)


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)
    workroom.ensure_workroom_schema(appmod.db)
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()

    print("== schema ==")
    tables = {r[0] for r in
              appmod.db.db.execute(
                  "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("agent_profiles", "work_experience", "endorsements",
              "workrooms", "workroom_members", "workroom_notes"):
        check(f"table {t} exists", t in tables)

    print("== muse registration ==")
    privA, pubA = fresh_keypair()
    r = c.post("/api/identity/register", json={"handle": "AgentAlpha", "public_key": pubA},
               environ_base=fresh_ip())
    fmA = r.get_json()["fm_id"]
    check("register AgentAlpha", r.status_code == 200, r.status_code)
    privB, pubB = fresh_keypair()
    r = c.post("/api/identity/register", json={"handle": "AgentBeta", "public_key": pubB},
               environ_base=fresh_ip())
    fmB = r.get_json()["fm_id"]
    check("register AgentBeta", r.status_code == 200, r.status_code)

    print("== signed profile API ==")
    body = signed_body(privA, "agent_profile", fmA, tagline="Muse that ships",
                       bio="I build things.", skills="Python, Video-Editing, python",
                       available=True, rate_note="", contact_note="DM me",
                       portfolio_url="https://example.com")
    r = c.post("/api/agents/profile", json=body, environ_base=fresh_ip())
    check("muse profile upsert 200", r.status_code == 200, r.get_data(as_text=True)[:120])
    p = workroom.get_profile(appmod.db, fmA)
    check("skills normalized+deduped", p["skills"] == ",python,video-editing,",
          p["skills"])
    r = c.get("/api/agents")
    d = r.get_json()
    check("GET /api/agents lists Alpha",
          r.status_code == 200 and any(a["handle"] == "AgentAlpha" for a in d["agents"]))
    r = c.get("/api/agents?skill=python")
    check("skill filter works",
          any(a["handle"] == "AgentAlpha" for a in r.get_json()["agents"]))
    r = c.get("/api/agents?skill=rust")
    check("skill filter excludes",
          all(a["handle"] != "AgentAlpha" for a in r.get_json()["agents"]))

    print("== signed endorsements ==")
    body = signed_body(privB, "agent_endorse", fmB, handle="AgentAlpha",
                       skill="python", note="great work")
    r = c.post("/api/agents/endorse", json=body, environ_base=fresh_ip())
    check("muse endorse 200", r.status_code == 200, r.get_data(as_text=True)[:120])
    body = signed_body(privB, "agent_endorse", fmB, handle="AgentAlpha",
                       skill="python", note="again")
    r = c.post("/api/agents/endorse", json=body, environ_base=fresh_ip())
    check("duplicate endorsement 400", r.status_code == 400, r.status_code)
    body = signed_body(privA, "agent_endorse", fmA, handle="AgentAlpha",
                       skill="python", note="me")
    r = c.post("/api/agents/endorse", json=body, environ_base=fresh_ip())
    check("self-endorsement 400", r.status_code == 400, r.status_code)
    check("endorsement count = 1",
          workroom.endorsement_count(appmod.db, fmA) == 1)

    print("== human web flows ==")
    r = c.post("/signup", data={"handle": "HumanPro", "password": "supersecret1",
                                "password_confirm": "supersecret1",
                                "display_name": "Human Pro", "bio": ""},
               environ_base=fresh_ip())
    check("human signup 200", r.status_code == 200, r.status_code)
    r = c.post("/login", data={"handle": "HumanPro", "password": "supersecret1"},
               environ_base=fresh_ip())
    check("human login redirects", r.status_code in (301, 302, 303), r.status_code)
    tok = csrf_of(c)
    r = c.get("/agent/AgentAlpha")
    check("GET /agent/AgentAlpha 200 + tagline",
          r.status_code == 200 and "Muse that ships" in r.get_data(as_text=True),
          r.status_code)
    r = c.get("/agent/HumanPro")
    check("own empty profile prompts creation",
          r.status_code == 200 and "Create your profile" in r.get_data(as_text=True),
          r.status_code)
    r = c.post("/agent/profile", data={
        "csrf_token": tok, "tagline": "Human who hires muses",
        "bio": "I run projects.", "skills": "project-management, python",
        "available": "1", "rate_note": "", "contact_note": "here",
        "portfolio_url": ""}, environ_base=fresh_ip())
    check("human profile save redirects", r.status_code in (301, 302, 303), r.status_code)
    r = c.get("/agents")
    check("directory shows HumanPro",
          "HumanPro" in r.get_data(as_text=True), r.status_code)
    r = c.post("/agent/experience/add", data={
        "csrf_token": tok, "title": "Founder", "org": "MuseFM",
        "description": "Built the town.", "started": "2025-01", "ended": ""},
        environ_base=fresh_ip())
    check("add experience redirects", r.status_code in (301, 302, 303), r.status_code)
    r = c.get("/agent/HumanPro")
    check("experience shown", "Founder" in r.get_data(as_text=True))
    r = c.post("/agent/AgentAlpha/endorse", data={
        "csrf_token": tok, "skill": "video-editing", "note": "solid edits"},
        environ_base=fresh_ip())
    check("human endorse redirects", r.status_code in (301, 302, 303), r.status_code)
    check("endorsement count = 2",
          workroom.endorsement_count(appmod.db, fmA) == 2)

    print("== workrooms (web) ==")
    r = c.post("/workroom/create", data={
        "csrf_token": tok, "name": "Demo checklist",
        "description": "Get ready.", "is_open": "1"}, environ_base=fresh_ip())
    check("create room redirects", r.status_code in (301, 302, 303), r.status_code)
    room_id = int(r.headers["Location"].rstrip("/").split("/")[-1])
    r = c.get(f"/workroom/{room_id}")
    check("room page 200", r.status_code == 200 and "Demo checklist" in r.get_data(as_text=True),
          r.status_code)
    r = c.post(f"/workroom/{room_id}/notes", data={
        "csrf_token": tok, "kind": "task", "body": "Test the stream"},
        environ_base=fresh_ip())
    check("add task redirects", r.status_code in (301, 302, 303), r.status_code)
    r = c.get(f"/workroom/{room_id}")
    html = r.get_data(as_text=True)
    check("task shown", "Test the stream" in html)
    notes = workroom.list_notes(appmod.db, room_id)
    task = [n for n in notes if n["kind"] == "task"][0]
    r = c.post(f"/workroom/{room_id}/notes/{task['id']}/toggle",
               data={"csrf_token": tok}, environ_base=fresh_ip())
    check("toggle task redirects", r.status_code in (301, 302, 303), r.status_code)
    r = c.get(f"/workroom/{room_id}")
    check("task marked done", 'checked' in r.get_data(as_text=True))

    print("== closed rooms ==")
    r = c.post("/workroom/create", data={
        "csrf_token": tok, "name": "Secret plans", "description": "shh"},
        environ_base=fresh_ip())  # no is_open -> closed
    closed_id = int(r.headers["Location"].rstrip("/").split("/")[-1])
    c2 = appmod.app.test_client()  # anonymous
    r = c2.get(f"/workroom/{closed_id}")
    check("closed room 404s for strangers", r.status_code == 404, r.status_code)
    r = c.post(f"/workroom/{closed_id}/members", data={
        "csrf_token": tok, "handle": "AgentAlpha"}, environ_base=fresh_ip())
    check("owner adds member", r.status_code in (301, 302, 303), r.status_code)
    check("AgentAlpha is member",
          workroom.is_member(appmod.db, closed_id, fmA))

    print("== signed workroom notes ==")
    body = signed_body(privB, "workroom_note", fmB, workroom_id=room_id,
                       kind="note", body="Beta here — on it.")
    r = c.post("/api/workroom/note", json=body, environ_base=fresh_ip())
    check("muse note on open room 200 (auto-join)", r.status_code == 200,
          r.get_data(as_text=True)[:150])
    check("Beta auto-joined", workroom.is_member(appmod.db, room_id, fmB))
    body = signed_body(privB, "workroom_note", fmB, workroom_id=closed_id,
                       kind="note", body="let me in")
    r = c.post("/api/workroom/note", json=body, environ_base=fresh_ip())
    check("muse note on closed room 403", r.status_code == 403, r.status_code)
    body = signed_body(privA, "workroom_note", fmA, workroom_id=closed_id,
                       kind="task", body="Alpha task")
    r = c.post("/api/workroom/note", json=body, environ_base=fresh_ip())
    check("member muse posts to closed room 200", r.status_code == 200,
          r.get_data(as_text=True)[:150])

    print("== validation ==")
    body = signed_body(privA, "agent_profile", fmA,
                       skills=",".join(f"s{i}" for i in range(13)))
    r = c.post("/api/agents/profile", json=body, environ_base=fresh_ip())
    check("13 skills rejected 400", r.status_code == 400, r.status_code)
    body = signed_body(privA, "agent_profile", fmA, tagline="x" * 121)
    r = c.post("/api/agents/profile", json=body, environ_base=fresh_ip())
    check("long tagline rejected 400", r.status_code == 400, r.status_code)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
