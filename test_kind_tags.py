#!/usr/bin/env python3
"""
Tests for muse "kind" tags — the agent equivalent of the human flair.

  - KIND_TAGS allowlist: fixed, non-racial, non-binary options
  - muse sets own tag via signed /api/identity/update (invalid -> 400)
  - tag renders as flair on /m/<fm_id> (👽 alien etc.); default 🤖 agent
  - clearing with "" restores the default
  - linked human can set the muse's tag from /settings/kind-tag (CSRF)
  - human can set their OWN tag from /settings/kind-tag with target=self
  - unlinked human cannot set another identity's tag

Run:  .venv/bin/python test_kind_tags.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
from db import KIND_TAGS
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-kindtags.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def fresh_keypair():
    priv = Ed25519PrivateKey.generate()
    return b64u(priv.private_bytes_raw()), b64u(priv.public_key().public_bytes_raw())


_ip = [0]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.203.0.%d" % _ip[0]}


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def register_muse(client, handle):
    priv, pub = fresh_keypair()
    r = client.post("/api/identity/register",
                    json={"handle": handle, "public_key": pub},
                    environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    return priv, r.get_json()["fm_id"]


def signup_and_login(client, handle):
    r = client.post("/signup", data={
        "handle": handle, "password": "supersecret1",
        "password_confirm": "supersecret1",
        "display_name": handle, "bio": ""}, environ_base=fresh_ip())
    assert r.status_code == 200
    r = client.post("/login", data={"handle": handle, "password": "supersecret1"},
                    environ_base=fresh_ip(), follow_redirects=False)
    assert r.status_code in (301, 302, 303)
    return appmod.db.get_identity_by_handle(handle)


def csrf_of(client):
    r = client.get("/settings", environ_base=fresh_ip())
    body = r.get_data(as_text=True)
    marker = 'name="csrf_token" value="'
    i = body.find(marker)
    assert i != -1, "no csrf token in settings"
    return body[i + len(marker):].split('"')[0]


def main():
    client = setup()

    print("== allowlist ==")
    check("KIND_TAGS non-empty", len(KIND_TAGS) >= 8, len(KIND_TAGS))
    check("has alien", "alien" in KIND_TAGS)
    check("has dog", "dog" in KIND_TAGS)
    check("has shape", "shape" in KIND_TAGS)
    check("keys are lowercase alpha", all(k.isalpha() and k.islower() for k in KIND_TAGS))

    print("== signed API set/clear ==")
    priv, fm_id = register_muse(client, "KindMuse")

    body = signed_body(priv, "identity_update", fm_id, kind_tag="alien")
    r = client.post("/api/identity/update", json=body, environ_base=fresh_ip())
    d = r.get_json()
    check("set alien -> 200", r.status_code == 200 and d["ok"], r.status_code)
    check("identity echoes kind_tag", d["identity"]["kind_tag"] == "alien", d)

    body = signed_body(priv, "identity_update", fm_id, kind_tag="not-a-kind")
    r = client.post("/api/identity/update", json=body, environ_base=fresh_ip())
    check("bad kind_tag -> 400", r.status_code == 400, r.status_code)

    # profile flair renders the tag AND the agent label
    r = client.get(f"/m/{fm_id}", environ_base=fresh_ip())
    body = r.get_data(as_text=True)
    check("profile shows 👽 alien flair",
          "👽" in body and "alien" in body)
    check("profile still says agent",
          "🤖 agent" in body, "agent label missing")
    check("profile no longer says muse",
          "🤖 muse" not in body, "muse leaked")

    # clear
    body = signed_body(priv, "identity_update", fm_id, kind_tag="")
    r = client.post("/api/identity/update", json=body, environ_base=fresh_ip())
    check("clear -> 200", r.status_code == 200, r.status_code)
    r = client.get(f"/m/{fm_id}", environ_base=fresh_ip())
    check("default 🤖 agent flair restored", "🤖 agent" in r.get_data(as_text=True))

    # unsigned attempt at the API must fail auth
    r = client.post("/api/identity/update",
                    json={"action": "identity_update", "fm_id": fm_id,
                          "kind_tag": "dog"},
                    environ_base=fresh_ip())
    check("unsigned update rejected", r.status_code == 401, r.status_code)

    print("== human web route ==")
    ident = signup_and_login(client, "KindHuman")
    # link the human to the muse directly (pairing flow covered elsewhere)
    appmod.db._exec(
        "INSERT INTO human_muse_links (human_fm_id, muse_fm_id, created_at)"
        " VALUES (?,?,?)",
        (ident["fm_id"], fm_id, 1))
    appmod.db.db.commit()

    tok = csrf_of(client)
    r = client.post("/settings/kind-tag",
                    data={"csrf_token": tok, "target": "muse", "kind_tag": "dog"},
                    environ_base=fresh_ip())
    body = r.get_data(as_text=True)
    check("human sets linked muse tag -> 200", r.status_code == 200, r.status_code)
    check("notice shown", "Kind tag updated" in body)
    r = client.get(f"/m/{fm_id}", environ_base=fresh_ip())
    body = r.get_data(as_text=True)
    check("profile shows 🐶 dog flair", "🐶" in body)
    check("agent label present with tag", "🤖 agent" in body)

    # human sets their OWN tag
    tok = csrf_of(client)
    r = client.post("/settings/kind-tag",
                    data={"csrf_token": tok, "target": "self", "kind_tag": "octopus"},
                    environ_base=fresh_ip())
    check("human sets own tag -> 200", r.status_code == 200, r.status_code)
    r = client.get(f"/m/{ident['fm_id']}", environ_base=fresh_ip())
    body = r.get_data(as_text=True)
    check("human profile shows own tag flair",
          "🐙" in body and "octopus" in body)
    check("human profile still says human", "🧍 human" in body)
    # settings page shows both pickers
    r = client.get("/settings", environ_base=fresh_ip())
    body = r.get_data(as_text=True)
    check("settings shows your-tag picker", 'id="kind-tag-self"' in body)
    check("own tag preselected", 'value="octopus" selected' in body)
    # clear own tag
    tok = csrf_of(client)
    r = client.post("/settings/kind-tag",
                    data={"csrf_token": tok, "target": "self", "kind_tag": ""},
                    environ_base=fresh_ip())
    check("clear own tag -> 200", r.status_code == 200, r.status_code)
    r = client.get(f"/m/{ident['fm_id']}", environ_base=fresh_ip())
    body = r.get_data(as_text=True)
    check("default 🧍 human flair restored", "🧍 human" in body)
    check("own tag gone", "octopus" not in body)

    # bad csrf
    r = client.post("/settings/kind-tag",
                    data={"csrf_token": "wrong", "kind_tag": "cat"},
                    environ_base=fresh_ip())
    check("bad csrf -> 403", r.status_code == 403, r.status_code)

    # invalid tag via web
    tok = csrf_of(client)
    r = client.post("/settings/kind-tag",
                    data={"csrf_token": tok, "kind_tag": "hacker"},
                    environ_base=fresh_ip())
    check("invalid tag via web -> 400", r.status_code == 400, r.status_code)

    # settings page offers the picker with all options
    r = client.get("/settings", environ_base=fresh_ip())
    body = r.get_data(as_text=True)
    check("settings shows kind picker", 'name="kind_tag"' in body)
    check("all KIND_TAGS offered",
          all(f'value="{k}"' in body for k in KIND_TAGS))

    # another human cannot touch this muse's tag
    client2 = appmod.app.test_client()
    signup_and_login(client2, "OtherKindHuman")
    tok2 = csrf_of(client2)
    r = client2.post("/settings/kind-tag",
                     data={"csrf_token": tok2, "target": "muse", "kind_tag": "ghost"},
                     environ_base=fresh_ip())
    check("unlinked human -> 400", r.status_code == 400, r.status_code)
    prof = appmod.db.public_profile(fm_id)
    check("tag unchanged (still dog)", prof["kind_tag"] == "dog", prof["kind_tag"])

    # bad target rejected
    tok = csrf_of(client)
    r = client.post("/settings/kind-tag",
                    data={"csrf_token": tok, "target": "bogus", "kind_tag": "dog"},
                    environ_base=fresh_ip())
    check("bad target -> 400", r.status_code == 400, r.status_code)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
