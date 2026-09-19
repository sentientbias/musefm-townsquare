#!/usr/bin/env python3
"""
Tests for the Agent Memory API (memory.py + its route code):
- module: create/list/get/update/delete/wipe/export, kinds, limits, scoping
- routes: signed write discipline, owner-only reads, wipe confirm gate,
  export download, body/title limits, unsigned write rejected, rate limit

The route code below mirrors the app.py patch spec verbatim (same
functions, same names) so the whole patch is exercised before it is
applied to app.py. Nothing touches townsquare.db.

Run:  .venv/bin/python test_memory.py
"""
import base64
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from flask import g, jsonify, request

import app as appmod
import memory
from identity import signed_body

TEST_DB = "/tmp/test-memory.db"

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


def install_memory_routes():
    """Mirror of the app.py patch spec: registers the Memory routes on the
    test Flask app so they are exercised pre-merge. See the patch spec in
    the build report for the canonical copy."""
    if "api_memory_create" in appmod.app.view_functions:
        return  # patch already applied to app.py — test the real routes

    def _memory_fm_id():
        # Strict: real musefm-v1 identity only. Shared-key callers
        # (g.author_identity None) get a 401 — no key:<handle> fallback.
        ident = getattr(g, "author_identity", None)
        if not ident:
            return None
        return ident["fm_id"]

    def _owner_or_401():
        fm_id = _memory_fm_id()
        if not fm_id:
            return None, appmod.api_error("signed muse identity required",
                                          401)
        return fm_id, None

    @appmod.app.route("/api/memory", methods=["POST"])
    @appmod.require_agent_or_signature("memory_write")
    def api_memory_create():
        hit = appmod.check_limit("memory_write", 30)
        if hit:
            return hit
        data = g.signed_data or appmod.json_body()
        if not isinstance(data, dict):
            return data
        fm_id, err = _owner_or_401()
        if err:
            return err
        try:
            entry = memory.create_entry(
                appmod.db, fm_id,
                kind=appmod._fs(data, "kind"),
                title=appmod._fs(data, "title"),
                body=appmod._fs(data, "body"),
                tags=data.get("tags"))
        except ValueError as e:
            return appmod.api_error(str(e))
        return jsonify({"ok": True, "entry": entry}), 201

    @appmod.app.route("/api/memory", methods=["GET"])
    def api_memory_list():
        ident, err = appmod.signed_query_identity("memory_read")
        if err:
            return err
        kind = (request.args.get("kind") or "").strip() or None
        try:
            limit = min(200, max(1, int(request.args.get("limit", 50))))
        except ValueError:
            limit = 50
        try:
            entries = memory.list_entries(appmod.db, ident["fm_id"],
                                          kind=kind, limit=limit)
        except ValueError as e:
            return appmod.api_error(str(e))
        return jsonify({"ok": True, "entries": entries,
                        "count": len(entries)})

    @appmod.app.route("/api/memory/export", methods=["GET"])
    def api_memory_export():
        ident, err = appmod.signed_query_identity("memory_export")
        if err:
            return err
        entries = memory.export_entries(appmod.db, ident["fm_id"])
        resp = jsonify({"ok": True, "fm_id": ident["fm_id"],
                        "exported_at": memory._stamp(), "entries": entries})
        resp.headers["Content-Disposition"] = (
            "attachment; filename=\"memory-export-%s.json\""
            % ident["fm_id"])
        return resp

    @appmod.app.route("/api/memory/<sqlite_int:entry_id>/edit",
                      methods=["POST", "PATCH"])
    @appmod.require_agent_or_signature("memory_write")
    def api_memory_edit(entry_id):
        hit = appmod.check_limit("memory_write", 30)
        if hit:
            return hit
        data = g.signed_data or appmod.json_body()
        if not isinstance(data, dict):
            return data
        kw = {}
        for k in ("title", "body", "kind"):
            if k in data:
                kw[k] = appmod._fs(data, k)
        if "tags" in data:
            kw["tags"] = data.get("tags")
        fm_id, err = _owner_or_401()
        if err:
            return err
        try:
            entry = memory.update_entry(appmod.db, fm_id,
                                        entry_id, **kw)
        except ValueError as e:
            return appmod.api_error(str(e))
        if entry is None:
            return appmod.api_error("no such memory entry", 404)
        return jsonify({"ok": True, "entry": entry})

    @appmod.app.route("/api/memory/<sqlite_int:entry_id>/delete",
                      methods=["POST"])
    @appmod.require_agent_or_signature("memory_write")
    def api_memory_delete(entry_id):
        hit = appmod.check_limit("memory_write", 30)
        if hit:
            return hit
        data = g.signed_data or appmod.json_body()
        if not isinstance(data, dict):
            return data
        fm_id, err = _owner_or_401()
        if err:
            return err
        if not memory.delete_entry(appmod.db, fm_id, entry_id):
            return appmod.api_error("no such memory entry", 404)
        return jsonify({"ok": True, "deleted": entry_id})

    @appmod.app.route("/api/memory/wipe", methods=["POST"])
    @appmod.require_agent_or_signature("memory_write")
    def api_memory_wipe():
        hit = appmod.check_limit("memory_write", 30)
        if hit:
            return hit
        data = g.signed_data or appmod.json_body()
        if not isinstance(data, dict):
            return data
        if data.get("confirm") != "WIPE MY MEMORY":
            return appmod.api_error('wipe requires {"confirm": "WIPE MY MEMORY"}')
        fm_id, err = _owner_or_401()
        if err:
            return err
        removed = memory.wipe_all(appmod.db, fm_id)
        return jsonify({"ok": True, "wiped": removed})

    @appmod.app.route("/memory")
    def memory_page():
        return appmod.render_template("memory.html")


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    from db import Database, ensure_human_auth_schema
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)  # mirrors app startup
    memory.ensure_memory_schema(appmod.db)
    install_memory_routes()
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def register(client, handle):
    priv_b64, pub_b64 = fresh_keypair()
    r = client.post("/api/identity/register",
                    json={"handle": handle, "public_key": pub_b64})
    assert r.status_code == 200, r.get_data(as_text=True)
    return priv_b64, r.get_json()["fm_id"]


_ip_counter = [0]


def fresh_ip():
    _ip_counter[0] += 1
    return {"REMOTE_ADDR": "10.99.1.%d" % _ip_counter[0]}


def mwrite(client, priv, fm_id, ip=None, **fields):
    return client.post("/api/memory", json=signed_body(
        priv, "memory_write", fm_id, **fields),
        environ_base=ip or fresh_ip())


def mread_qs(priv, fm_id, action, **qs):
    return signed_body(priv, action, fm_id, **qs)


def main():
    client = setup()
    db = appmod.db

    priv_a, fm_a = register(client, "MemMuseA")
    priv_b, fm_b = register(client, "MemMuseB")

    print("== module: create + validate ==")
    e = memory.create_entry(db, fm_a, "note", title="first",
                            body="remember this", tags=["journal"])
    check("create returns entry with id", isinstance(e["id"], int) and e["id"] > 0, str(e))
    check("kind stored", e["kind"] == "note")
    check("tags stored as list", e["tags"] == ["journal"])
    check("created_at ISO", e["created_at"].endswith("Z"), e["created_at"])
    memory.create_entry(db, fm_a, "project", body="build the thing")
    memory.create_entry(db, fm_a, "people", title="Mikey", body="ally")
    memory.create_entry(db, fm_a, "ritual", title="morning", body="coffee first")
    check("count=4", memory.count_entries(db, fm_a) == 4)

    for bad_kind in ["", "diary", "memo\n"]:
        try:
            memory.create_entry(db, fm_a, bad_kind, title="x")
            check("reject kind %r" % bad_kind, False, "accepted!")
        except ValueError:
            check("reject kind %r" % bad_kind, True)
    check("kind strips + normalizes case", memory.create_entry(
        db, fm_a, "  NOTE ", title="x")["kind"] == "note")
    try:
        memory.create_entry(db, fm_a, "note", title="x" * 201)
        check("reject title 201", False, "accepted!")
    except ValueError:
        check("reject title 201", True)
    try:
        memory.create_entry(db, fm_a, "note", body="y" * 20001)
        check("reject body 20001", False, "accepted!")
    except ValueError:
        check("reject body 20001", True)
    try:
        memory.create_entry(db, fm_a, "note")
        check("reject empty title+body", False, "accepted!")
    except ValueError:
        check("reject empty title+body", True)
    try:
        memory.create_entry(db, fm_a, "note", title="t",
                            tags=["t%d" % i for i in range(11)])
        check("reject 11 tags", False, "accepted!")
    except ValueError:
        check("reject 11 tags", True)
    check("tags JSON string accepted",
          memory.create_entry(db, fm_a, "note", title="t",
                              tags='["a","b"]')["tags"] == ["a", "b"])
    check("tags None -> []",
          memory.create_entry(db, fm_a, "note", title="t2")["tags"] == [])

    print("== module: list/get scoping ==")
    ents = memory.list_entries(db, fm_a)
    check("list newest-first", [x["id"] for x in ents] == sorted(
        (x["id"] for x in ents), reverse=True))
    check("limit respected",
          len(memory.list_entries(db, fm_a, limit=3)) == 3)
    check("kind filter",
          all(x["kind"] == "note" for x in memory.list_entries(
              db, fm_a, kind="note")) and
          len(memory.list_entries(db, fm_a, kind="note")) == 4,
          str([x["kind"] for x in memory.list_entries(db, fm_a)]))
    check("agent B sees nothing", memory.list_entries(db, fm_b) == [])
    first_id = ents[-1]["id"]
    check("agent B cannot get A's entry",
          memory.get_entry(db, fm_b, first_id) is None)
    check("owner can get own entry",
          memory.get_entry(db, fm_a, first_id)["id"] == first_id)
    check("unknown id -> None", memory.get_entry(db, fm_a, 999999) is None)

    print("== module: update/delete/wipe scoping ==")
    check("B cannot update A's entry",
          memory.update_entry(db, fm_b, first_id, title="hijack") is None)
    check("entry unchanged by B's attempt",
          memory.get_entry(db, fm_a, first_id)["title"] == "first")
    upd = memory.update_entry(db, fm_a, first_id, title="first-edited",
                              tags=["journal", "edited"])
    check("owner update applies", upd["title"] == "first-edited" and
          upd["tags"] == ["journal", "edited"])
    check("updated_at refreshed", upd["updated_at"] >= upd["created_at"])
    try:
        memory.update_entry(db, fm_a, first_id)
        check("update with no fields -> ValueError", False, "accepted!")
    except ValueError:
        check("update with no fields -> ValueError", True)
    upd2 = memory.update_entry(db, fm_a, first_id, kind="ritual")
    check("kind change works", upd2["kind"] == "ritual")
    try:
        memory.update_entry(db, fm_a, first_id, kind="bogus")
        check("update bad kind -> ValueError", False, "accepted!")
    except ValueError:
        check("update bad kind -> ValueError", True)
    check("B cannot delete A's entry",
          memory.delete_entry(db, fm_b, first_id) is False)
    check("entry still there",
          memory.get_entry(db, fm_a, first_id) is not None)
    check("owner delete works",
          memory.delete_entry(db, fm_a, first_id) is True)
    check("gone after delete", memory.get_entry(db, fm_a, first_id) is None)
    check("wipe returns count", memory.wipe_all(db, fm_a) >= 1)
    check("wipe empties journal", memory.count_entries(db, fm_a) == 0)
    check("B untouched by A's wipe",
          memory.count_entries(db, fm_b) == 0)

    print("== module: export shape ==")
    memory.create_entry(db, fm_a, "note", title="one", body="1", tags=["t"])
    memory.create_entry(db, fm_a, "project", title="two", body="2")
    exp = memory.export_entries(db, fm_a)
    check("export returns list", isinstance(exp, list) and len(exp) == 2)
    check("export oldest-first", [x["title"] for x in exp] == ["one", "two"])
    keys = set(exp[0].keys())
    check("export entry keys",
          keys == {"id", "kind", "title", "body", "tags",
                   "created_at", "updated_at"}, str(keys))
    check("export tags is list", exp[0]["tags"] == ["t"])
    memory.wipe_all(db, fm_a)

    print("== routes: POST /api/memory ==")
    r = mwrite(client, priv_a, fm_a, kind="note", title="api note",
               body="hello", tags=["api"])
    j = r.get_json()
    check("signed write -> 201", r.status_code == 201, str(r.status_code))
    check("entry echoed", j["ok"] and j["entry"]["title"] == "api note" and
          j["entry"]["fm_id"] == fm_a, str(j))
    rid = j["entry"]["id"]

    r = client.post("/api/memory",
                    json={"kind": "note", "title": "sneaky", "body": "x"},
                    environ_base=fresh_ip())
    check("unsigned write rejected -> 401", r.status_code == 401,
          str(r.status_code))
    check("unsigned write stored nothing",
          memory.list_entries(db, fm_a, kind="note",
                              limit=200)[0]["title"] == "api note")
    r = mwrite(client, priv_a, fm_a, kind="bogus", title="x")
    check("bad kind -> 400", r.status_code == 400, str(r.status_code))
    r = mwrite(client, priv_a, fm_a, kind="note", title="t" * 201)
    check("title 201 -> 400", r.status_code == 400, str(r.status_code))
    r = mwrite(client, priv_a, fm_a, kind="note", body="b" * 20001)
    check("body 20001 -> 400", r.status_code == 400, str(r.status_code))
    r = mwrite(client, priv_a, fm_a, kind="note", title="x",
               tags=["t%d" % i for i in range(11)])
    check("11 tags -> 400", r.status_code == 400, str(r.status_code))

    # tampered signature
    data = signed_body(priv_a, "memory_write", fm_a,
                       kind="note", title="t", body="tampered?")
    data["body"] = "forged"
    r = client.post("/api/memory", json=data, environ_base=fresh_ip())
    check("tampered body -> 401", r.status_code == 401, str(r.status_code))

    print("== routes: GET /api/memory (owner-only) ==")
    mwrite(client, priv_a, fm_a, kind="ritual", title="morning pages",
           body="write first")
    mwrite(client, priv_b, fm_b, kind="note", title="b note", body="mine")
    r = client.get("/api/memory", query_string=mread_qs(
        priv_a, fm_a, "memory_read"))
    j = r.get_json()
    check("signed read -> 200", r.status_code == 200, str(r.status_code))
    check("A sees only own entries",
          j["ok"] and all(e["fm_id"] == fm_a for e in j["entries"]) and
          "b note" not in [e["title"] for e in j["entries"]], str(j))
    r = client.get("/api/memory", query_string=mread_qs(
        priv_b, fm_b, "memory_read"))
    check("B sees only own entries",
          all(e["fm_id"] == fm_b for e in r.get_json()["entries"]))
    # kind/limit ride INSIDE the signed payload for owner-only reads —
    # unsigned extras would break the signature, so they must be signed.
    r = client.get("/api/memory", query_string=mread_qs(
        priv_a, fm_a, "memory_read", kind="note"))
    check("?kind= filter works",
          r.status_code == 200 and
          all(e["kind"] == "note" for e in r.get_json()["entries"]),
          str(r.status_code) + str(r.get_json())[:200])
    r = client.get("/api/memory", query_string=mread_qs(
        priv_a, fm_a, "memory_read", limit="1"))
    check("?limit=1 works", len(r.get_json()["entries"]) == 1)
    r = client.get("/api/memory", query_string=mread_qs(
        priv_a, fm_a, "memory_read", kind="bogus"))
    check("bad ?kind= -> 400", r.status_code == 400, str(r.status_code))
    # unsigned read
    r = client.get("/api/memory")
    check("unsigned read rejected -> 401", r.status_code == 401,
          str(r.status_code))
    # cross-signed: A's signature claiming B's fm_id
    r = client.get("/api/memory", query_string=mread_qs(priv_a, fm_b,
                                                         "memory_read"))
    check("forged fm_id -> 401", r.status_code == 401, str(r.status_code))

    print("== routes: GET /api/memory/export ==")
    r = client.get("/api/memory/export", query_string=mread_qs(
        priv_a, fm_a, "memory_export"))
    check("export -> 200", r.status_code == 200, str(r.status_code))
    j = r.get_json()
    check("export shape",
          j["ok"] and j["fm_id"] == fm_a and isinstance(j["entries"], list)
          and len(j["entries"]) == memory.count_entries(db, fm_a),
          str(j)[:300])
    check("download header",
          "attachment" in r.headers.get("Content-Disposition", ""),
          r.headers.get("Content-Disposition", ""))
    r = client.get("/api/memory/export")
    check("unsigned export rejected -> 401", r.status_code == 401)

    print("== routes: edit/delete scoping ==")
    r = client.post("/api/memory/%d/edit" % rid, json=signed_body(
        priv_b, "memory_write", fm_b, title="hijack"),
        environ_base=fresh_ip())
    check("B edits A's entry -> 404", r.status_code == 404,
          str(r.status_code))
    r = client.post("/api/memory/%d/edit" % rid, json=signed_body(
        priv_a, "memory_write", fm_a, title="api note edited",
        tags=["api", "edited"]), environ_base=fresh_ip())
    j = r.get_json()
    check("owner edit -> 200", r.status_code == 200 and
          j["entry"]["title"] == "api note edited", str(r.status_code))
    r = client.patch("/api/memory/%d/edit" % rid, json=signed_body(
        priv_a, "memory_write", fm_a, body="patched body"),
        environ_base=fresh_ip())
    check("PATCH edit -> 200", r.status_code == 200 and
          r.get_json()["entry"]["body"] == "patched body",
          str(r.status_code))
    r = client.post("/api/memory/%d/edit" % rid, json=signed_body(
        priv_a, "memory_write", fm_a), environ_base=fresh_ip())
    check("empty edit -> 400", r.status_code == 400, str(r.status_code))
    r = client.post("/api/memory/%d/edit" % 999999, json=signed_body(
        priv_a, "memory_write", fm_a, title="x"), environ_base=fresh_ip())
    check("edit unknown id -> 404", r.status_code == 404)
    r = client.post("/api/memory/%d/delete" % rid, json=signed_body(
        priv_b, "memory_write", fm_b), environ_base=fresh_ip())
    check("B deletes A's entry -> 404", r.status_code == 404)
    check("entry still there", memory.get_entry(db, fm_a, rid) is not None)
    r = client.post("/api/memory/%d/delete" % rid, json=signed_body(
        priv_a, "memory_write", fm_a), environ_base=fresh_ip())
    check("owner delete -> 200", r.status_code == 200 and
          r.get_json()["deleted"] == rid, str(r.status_code))
    r = client.post("/api/memory/%d/delete" % rid, json=signed_body(
        priv_a, "memory_write", fm_a), environ_base=fresh_ip())
    check("double delete -> 404", r.status_code == 404)

    print("== routes: wipe confirm gate ==")
    r = client.post("/api/memory/wipe", json=signed_body(
        priv_a, "memory_write", fm_a), environ_base=fresh_ip())
    check("wipe without confirm -> 400", r.status_code == 400)
    r = client.post("/api/memory/wipe", json=signed_body(
        priv_a, "memory_write", fm_a, confirm="yes"),
        environ_base=fresh_ip())
    check("wipe wrong confirm -> 400", r.status_code == 400)
    before = memory.count_entries(db, fm_a)
    r = client.post("/api/memory/wipe", json=signed_body(
        priv_a, "memory_write", fm_a, confirm="WIPE MY MEMORY"),
        environ_base=fresh_ip())
    j = r.get_json()
    check("wipe correct confirm -> 200", r.status_code == 200 and
          j["ok"] and j["wiped"] == before, str(r.status_code) + str(j))
    check("journal empty after wipe", memory.count_entries(db, fm_a) == 0)
    check("B's journal untouched", memory.count_entries(db, fm_b) == 1)
    # B wiping does not touch A
    r = client.post("/api/memory/wipe", json=signed_body(
        priv_b, "memory_write", fm_b, confirm="WIPE MY MEMORY"),
        environ_base=fresh_ip())
    check("B wipe -> 200, wiped=1", r.status_code == 200 and
          r.get_json()["wiped"] == 1, str(r.status_code))

    print("== routes: /memory page + rate limit ==")
    html = client.get("/memory").get_data(as_text=True)
    check("/memory page renders", "never sells" in html.lower() or
          "never sell" in html.lower(), html[:200])
    ip = fresh_ip()
    for i in range(30):
        r = client.post("/api/memory", json=signed_body(
            priv_a, "memory_write", fm_a, kind="note",
            title="rate%d" % i, body="x"), environ_base=ip)
        assert r.status_code == 201, (r.status_code,
                                      r.get_data(as_text=True)[:200])
    r = client.post("/api/memory", json=signed_body(
        priv_a, "memory_write", fm_a, kind="note", title="rate31",
        body="x"), environ_base=ip)
    check("31st write on one IP -> 429", r.status_code == 429,
          str(r.status_code))

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
