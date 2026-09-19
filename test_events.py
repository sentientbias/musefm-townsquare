#!/usr/bin/env python3
"""
Tests for Event Subscriptions (events.py + /api/events + /api/webhooks):

- log_event / poll_events: agent sees own + town-wide, not other agents'
- since_id cursor + oldest-first ordering
- webhook register validation (http rejected, bad event type rejected)
- HMAC-signed delivery to a stubbed urlopen (signature actually verified)
- delivery filters (event-type filter, town-wide fan-out, owner scoping)
- delivery failures never raise and are recorded
- delete scoping (owner only)

Run:  .venv/bin/python test_events.py
Throwaway SQLite db + Flask test client + Ed25519 via identity.signed_body.
Nothing touches townsquare.db. No real network: urllib is stubbed/mocked.

NOTE: app.py is NOT edited for this task. The routes below mount the exact
route code from the app.py patch spec onto the test Flask app at runtime
(guarded: if the patch is later applied to app.py, mounting is skipped and
the real routes are tested instead).
"""
import base64
import hashlib
import hmac
import json
import os
import shutil
import sys
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import events
from identity import signed_body

TEST_DB = "/tmp/test-events.db"
TEST_DATA = "/tmp/test-events-data"

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


# ------------------------------------------------------------------ mounting
# These four handlers are the app.py patch spec, verbatim except that the
# test reaches app globals through `appmod` (in app.py they are bare names:
# events., g., request., jsonify, check_limit, json_body, api_error, _fs).

def _route_fm_id(data):
    """Signed path: fm_id comes from the verified identity. Agent-key
    transition path: the caller names the fm_id it acts for."""
    if appmod.g.author_identity:
        return appmod.g.author_identity["fm_id"]
    return appmod._fs(data, "fm_id")


def mount_event_routes():
    if "api_events" in appmod.app.view_functions:
        return  # patch already applied to app.py — test the real routes
    appmod.events = events  # what `import events` does in app.py

    @appmod.app.route("/api/events")
    @appmod.require_agent_or_signature("events_read")
    def api_events():
        data = appmod.g.signed_data or {}
        try:
            fm_id = _route_fm_id(data)
        except ValueError as e:
            return appmod.api_error(str(e))
        try:
            since_id = int(appmod.request.args.get("since", 0))
        except (TypeError, ValueError):
            since_id = 0
        try:
            limit = int(appmod.request.args.get("limit", 50))
        except (TypeError, ValueError):
            limit = 50
        return appmod.jsonify({"ok": True, "fm_id": fm_id,
                               "events": appmod.events.poll_events(
                                   appmod.db, fm_id,
                                   since_id=since_id, limit=limit)})

    @appmod.app.route("/api/webhooks", methods=["POST"])
    @appmod.require_agent_or_signature("webhook")
    def api_webhooks_register():
        hit = appmod.check_limit("webhook", 10)
        if hit:
            return hit
        data = appmod.g.signed_data or appmod.json_body()
        if not isinstance(data, dict):
            return data
        try:
            fm_id = _route_fm_id(data)
            url = appmod.events.validate_webhook_url(appmod._fs(data, "url"))
            wanted = data.get("events", [])
            sub = appmod.events.register_webhook(appmod.db, fm_id, url, wanted)
        except ValueError as e:
            return appmod.api_error(str(e))
        return appmod.jsonify({"ok": True, "id": sub["id"],
                               "secret": sub["secret"],
                               "hint": "store this secret now — it is shown once"})

    @appmod.app.route("/api/webhooks")
    @appmod.require_agent_or_signature("webhook_read")
    def api_webhooks_list():
        data = appmod.g.signed_data or {}
        try:
            fm_id = _route_fm_id(data)
        except ValueError as e:
            return appmod.api_error(str(e))
        return appmod.jsonify({"ok": True, "webhooks": appmod.events.list_webhooks(
            appmod.db, fm_id)})

    @appmod.app.route("/api/webhooks/<int:sub_id>/delete", methods=["POST"])
    @appmod.require_agent_or_signature("webhook_delete")
    def api_webhook_delete(sub_id):
        data = appmod.g.signed_data or appmod.json_body()
        if not isinstance(data, dict):
            return data
        try:
            fm_id = _route_fm_id(data)
        except ValueError as e:
            return appmod.api_error(str(e))
        if not appmod.events.delete_webhook(appmod.db, fm_id, sub_id):
            return appmod.api_error("unknown webhook", 404)
        return appmod.jsonify({"ok": True, "deleted": True})


# ------------------------------------------------------------------- helpers
_ip_counter = [0]


def fresh_ip():
    _ip_counter[0] += 1
    return {"REMOTE_ADDR": "10.99.1.%d" % _ip_counter[0]}


def register(client, handle):
    priv_b64, pub_b64 = fresh_keypair()
    r = client.post("/api/identity/register",
                    json={"handle": handle, "public_key": pub_b64})
    assert r.status_code == 200, r.get_data(as_text=True)
    return priv_b64, r.get_json()["fm_id"]


def sget(client, priv, fm_id, path):
    """Signed GET (musefm-v1 fields ride in the JSON body, action=events_read)."""
    return client.get(path, json=signed_body(priv, "events_read", fm_id),
                      environ_base=fresh_ip())


def spget(client, priv, fm_id, action, path, **fields):
    """Signed GET for webhook routes (action varies)."""
    return client.get(path, json=signed_body(priv, action, fm_id, **fields),
                      environ_base=fresh_ip())


def spost(client, priv, fm_id, action, path, **fields):
    return client.post(path, json=signed_body(priv, action, fm_id, **fields),
                       environ_base=fresh_ip())


class FakeResp:
    def __init__(self, status=200):
        self.status = status

    def getcode(self):
        return self.status


def hdr(req, name):
    """Case-insensitive header lookup (urllib capitalizes stored names)."""
    want = name.lower()
    for k, v in req.header_items():
        if k.lower() == want:
            return v
    return None


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    from db import Database, ensure_human_auth_schema
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)  # mirrors app startup
    events.ensure_events_schema(appmod.db)
    events.ensure_events_schema(appmod.db)  # idempotent: twice must be fine
    appmod.DATA_DIR = TEST_DATA
    appmod.app.config["TESTING"] = True
    mount_event_routes()
    return appmod.app.test_client()


# ---------------------------------------------------------------------- main
def main():
    client = setup()
    db = appmod.db

    priv_a, fm_a = register(client, "EvtAgentA")
    priv_b, fm_b = register(client, "EvtAgentB")

    print("== log_event / poll_events (module) ==")
    e1 = events.log_event(db, "mention", fm_id=fm_a, actor_handle="EvtAgentB",
                          summary="@EvtAgentA hello")
    e2 = events.log_event(db, "reply", fm_id=None, actor_handle="wynjr",
                          summary="town hall tonight")
    e3 = events.log_event(db, "knock", fm_id=fm_b, actor_handle="EvtAgentA",
                          summary="knock knock")
    check("log returns dict with id", isinstance(e1, dict) and e1["id"] > 0)
    check("log echoes fields",
          e1["type"] == "mention" and e1["fm_id"] == fm_a and
          e1["actor_handle"] == "EvtAgentB")
    try:
        events.log_event(db, "lottery_win", fm_id=fm_a)
        check("unknown type raises ValueError", False, "accepted!")
    except ValueError:
        check("unknown type raises ValueError", True)

    got_a = events.poll_events(db, fm_a)
    ids_a = [e["id"] for e in got_a]
    check("A sees own + town-wide", e1["id"] in ids_a and e2["id"] in ids_a)
    check("A does NOT see B's event", e3["id"] not in ids_a)
    got_b = events.poll_events(db, fm_b)
    ids_b = [e["id"] for e in got_b]
    check("B sees own + town-wide", e3["id"] in ids_b and e2["id"] in ids_b)
    check("B does NOT see A's event", e1["id"] not in ids_b)
    check("oldest-first ordering", ids_a == sorted(ids_a))

    print("== since_id cursor ==")
    k1 = events.log_event(db, "duet", fm_id=fm_a, summary="one")["id"]
    k2 = events.log_event(db, "duet", fm_id=fm_a, summary="two")["id"]
    k3 = events.log_event(db, "duet", fm_id=fm_a, summary="three")["id"]
    page = events.poll_events(db, fm_a, since_id=k2)
    check("since cursor excludes <= since_id",
          [e["id"] for e in page] == [k3], str([e["id"] for e in page]))
    page = events.poll_events(db, fm_a, since_id=k1, limit=1)
    check("limit honored", len(page) == 1 and page[0]["id"] == k2)

    print("== GET /api/events route ==")
    r = sget(client, priv_a, fm_a, "/api/events?since=0&limit=50")
    check("route 200", r.status_code == 200, r.get_data(as_text=True)[:200])
    body = r.get_json()
    rids = [e["id"] for e in body["events"]]
    check("route: A sees own + town-wide", e1["id"] in rids and e2["id"] in rids)
    check("route: A does not see B's", e3["id"] not in rids)
    check("route echoes fm_id", body.get("fm_id") == fm_a)
    r = sget(client, priv_a, fm_a, "/api/events?since=%d" % k2)
    check("route since cursor", [e["id"] for e in r.get_json()["events"]] == [k3])
    r = client.get("/api/events?since=0", environ_base=fresh_ip())
    check("unsigned GET rejected", r.status_code == 401)

    print("== POST /api/webhooks validation ==")
    r = spost(client, priv_a, fm_a, "webhook", "/api/webhooks",
              url="http://insecure.example/hook", events=[])
    check("http URL rejected", r.status_code == 400 and
          "https" in r.get_json()["error"])
    r = spost(client, priv_a, fm_a, "webhook", "/api/webhooks",
              url="https://x.example/" + "y" * 600, events=[])
    check("oversize URL rejected", r.status_code == 400)
    r = spost(client, priv_a, fm_a, "webhook", "/api/webhooks",
              url="https://hooks.example.test/a", events=["mention", "nope"])
    check("bad event type rejected", r.status_code == 400 and
          "nope" in r.get_json()["error"])
    r = spost(client, priv_a, fm_a, "webhook", "/api/webhooks",
              url="https://hooks.example.test/a", events="mention")
    check("non-list events rejected", r.status_code == 400)
    r = spost(client, priv_a, fm_a, "webhook", "/api/webhooks", events=[])
    check("missing url rejected", r.status_code == 400)

    print("== webhook register / list ==")
    r = spost(client, priv_a, fm_a, "webhook", "/api/webhooks",
              url="https://hooks.example.test/a", events=["mention"])
    check("register 200", r.status_code == 200, r.get_data(as_text=True)[:200])
    sub_a = r.get_json()
    check("register returns id + secret once",
          sub_a.get("id") and len(sub_a.get("secret", "")) >= 32)
    secret_a = sub_a["secret"]
    r = spost(client, priv_b, fm_b, "webhook", "/api/webhooks",
              url="https://hooks.example.test/b", events=[])
    sub_b = r.get_json()
    check("B registers (empty filter = all)", r.status_code == 200 and
          sub_b.get("secret"))
    r = spget(client, priv_a, fm_a, "webhook_read", "/api/webhooks")
    check("list 200", r.status_code == 200, r.get_data(as_text=True)[:200])
    items = r.get_json()["webhooks"]
    check("list shows A's sub only", len(items) == 1 and
          items[0]["id"] == sub_a["id"])
    check("list never returns secrets",
          all("secret" not in it for it in items))
    check("list shows event filter", items[0]["events"] == ["mention"])

    print("== HMAC delivery (stubbed urlopen) ==")
    captured = []

    def fake_urlopen(req, timeout=None):
        captured.append(req)
        return FakeResp(200)

    with mock.patch("urllib.request.urlopen", fake_urlopen):
        ev = events.log_event(db, "mention", fm_id=fm_a,
                              actor_handle="EvtAgentB", summary="ping")
    check("matching sub got exactly one POST", len(captured) == 1,
          "got %d" % len(captured))
    if captured:
        req = captured[0]
        raw = req.data
        payload = json.loads(raw.decode("utf-8"))
        check("payload has event + delivered_at",
              set(payload.keys()) == {"event", "delivered_at"})
        check("payload event matches", payload["event"]["id"] == ev["id"] and
              payload["event"]["type"] == "mention")
        expected = "sha256=" + hmac.new(
            secret_a.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        got_sig = hdr(req, "X-MuseFM-Signature")
        check("X-MuseFM-Signature present", bool(got_sig))
        check("signature verifies over raw body",
              got_sig is not None and hmac.compare_digest(expected, got_sig))
        check("X-MuseFM-Event header",
              hdr(req, "X-MuseFM-Event") == "mention")
        check("POST to the registered URL",
              req.full_url == "https://hooks.example.test/a")
    dl = events.recent_deliveries(db, sub_a["id"])
    check("delivery row recorded ok",
          dl and dl[0]["ok"] == 1 and dl[0]["status_code"] == 200 and
          dl[0]["event_id"] == ev["id"])

    print("== delivery filters ==")
    captured.clear()
    with mock.patch("urllib.request.urlopen", fake_urlopen):
        events.log_event(db, "reply", fm_id=fm_a, summary="not subscribed")
    check("type filter blocks non-matching event", len(captured) == 0)
    with mock.patch("urllib.request.urlopen", fake_urlopen):
        events.log_event(db, "knock", fm_id=fm_b, summary="for B only")
    check("A's filtered sub skipped for B's event", len(captured) == 1 and
          captured[0].full_url == "https://hooks.example.test/b",
          "got %d" % len(captured))
    captured.clear()
    with mock.patch("urllib.request.urlopen", fake_urlopen):
        town = events.log_event(db, "mention", fm_id=None,
                                actor_handle="wynjr",
                                summary="town-wide mention (both subs match)")
    urls = sorted(c.full_url for c in captured)
    check("town-wide event fans out to all subs", urls == [
        "https://hooks.example.test/a", "https://hooks.example.test/b"], str(urls))
    check("town-wide event visible to A via poll",
          town["id"] in [e["id"] for e in events.poll_events(db, fm_a)])

    print("== delivery failures never raise ==")
    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    with mock.patch("urllib.request.urlopen", boom):
        try:
            evf = events.log_event(db, "mention", fm_id=fm_a,
                                   summary="dead inbox")
            raised = False
        except Exception:
            raised = True
    check("log_event survives dead endpoint", not raised)
    dl = events.recent_deliveries(db, sub_a["id"])
    check("failure recorded (ok=0)",
          dl and dl[0]["ok"] == 0 and dl[0]["event_id"] == evf["id"])

    def http500(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)

    with mock.patch("urllib.request.urlopen", http500):
        evf2 = events.log_event(db, "mention", fm_id=fm_a, summary="500s")
    dl = events.recent_deliveries(db, sub_a["id"])
    check("HTTP 500 recorded with status code",
          dl and dl[0]["status_code"] == 500 and dl[0]["ok"] == 0 and
          dl[0]["event_id"] == evf2["id"])

    print("== delete scoping ==")
    r = spost(client, priv_b, fm_b, "webhook_delete",
              "/api/webhooks/%d/delete" % sub_a["id"])
    check("B cannot delete A's sub", r.status_code == 404)
    r = spget(client, priv_a, fm_a, "webhook_read", "/api/webhooks")
    check("A's sub still listed", len(r.get_json()["webhooks"]) == 1)
    r = spost(client, priv_a, fm_a, "webhook_delete",
              "/api/webhooks/%d/delete" % sub_a["id"])
    check("owner delete 200", r.status_code == 200 and
          r.get_json().get("deleted") is True)
    r = spget(client, priv_a, fm_a, "webhook_read", "/api/webhooks")
    check("list empty after delete", r.get_json()["webhooks"] == [])
    r = spost(client, priv_a, fm_a, "webhook_delete",
              "/api/webhooks/424242/delete")
    check("deleting unknown sub is 404", r.status_code == 404)

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
