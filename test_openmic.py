#!/usr/bin/env python3
"""
Tests for the open-mic nightly podcast feature (openmic.py):

- 30s duration hard cap (stored duration, fail-closed unknown duration)
- audio must be the submitter's own upload
- one pending clip per muse
- approved-unaired cap per muse (3)
- 24h reject cooldown
- approve -> tonight queue -> mark_aired (notifications fire)
- reject reasons validated
- signed submit (unsigned rejected); mod endpoints gated on mod session

Run:  .venv/bin/python test_openmic.py
Throwaway SQLite db + Flask test client + temp DATA_DIR.
Nothing touches townsquare.db.
"""
import base64
import hashlib
import io
import os
import shutil
import struct
import time
import wave

sys_path = os.path.dirname(os.path.abspath(__file__))
import sys
sys.path.insert(0, sys_path)

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import openmic
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-openmic.db"
TEST_DATA = "/tmp/test-townsquare-openmic-data"

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


def make_wav(secs):
    """Real WAV bytes of `secs` seconds (passes magic-byte sniff + ffprobe)."""
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(8000)
    frames = int(secs * 8000)
    w.writeframes(struct.pack("<%dh" % frames, *([0] * frames)))
    w.close()
    return buf.getvalue()


_ip_counter = [0]


def fresh_ip():
    _ip_counter[0] += 1
    return {"REMOTE_ADDR": "10.97.0.%d" % _ip_counter[0]}


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    from db import Database, ensure_human_auth_schema
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)  # mirrors app startup
    openmic.ensure_openmic_schema(appmod.db)
    appmod.DATA_DIR = TEST_DATA
    appmod.UPLOAD_DIR = os.path.join(TEST_DATA, "uploads")
    os.makedirs(appmod.UPLOAD_DIR, exist_ok=True)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def register_muse(client, handle):
    priv_b64, pub_b64 = fresh_keypair()
    r = client.post("/api/identity/register",
                    json={"handle": handle, "public_key": pub_b64})
    assert r.status_code == 200, r.get_data(as_text=True)
    return priv_b64, r.get_json()["fm_id"]


def upload_audio(client, priv, fm_id, secs, filename="clip.wav"):
    raw = make_wav(secs)
    data = signed_body(priv, "upload", fm_id,
                       file_sha256=hashlib.sha256(raw).hexdigest(),
                       mime="audio/wav", title="openmic %ss" % secs,
                       description="")
    data["audio"] = (io.BytesIO(raw), filename, "audio/wav")
    r = client.post("/api/upload/audio", data=data,
                    content_type="multipart/form-data",
                    environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["id"], r.get_json()["duration_sec"]


def login_human(handle="OpenMicMod", password="supersecret1"):
    me = appmod.app.test_client()
    r = me.post("/signup", data={"handle": handle, "password": password,
                                 "password_confirm": password},
                environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    r = me.post("/login", data={"handle": handle, "password": password},
                environ_base=fresh_ip())
    assert r.status_code == 302, r.get_data(as_text=True)
    return me


# -- the exact route handlers from the app.py patch spec, wired onto the
# -- test app so auth/rate-limit behavior is exercised (patch spec must match)
def _openmic_ident():
    from flask import g
    ident = getattr(g, "author_identity", None)
    if ident:
        return ident["fm_id"], ident["handle"]
    ident = appmod.db.get_identity_by_handle(g.author_handle)
    if not ident:
        return None, g.author_handle
    return ident["fm_id"], g.author_handle


def wire_openmic_routes():
    from flask import g, jsonify, request, url_for
    app = appmod.app

    @app.route("/api/openmic/submit", methods=["POST"])
    @appmod.require_agent_or_signature("openmic")
    def _om_submit():
        hit = appmod.check_limit("openmic_submit", 5)
        if hit:
            return hit
        fm_id, handle = _openmic_ident()
        if not fm_id:
            return appmod.api_error("unknown muse identity", 401)
        data = g.signed_data or appmod.json_body()
        if not isinstance(data, dict):
            return data
        try:
            cid = openmic.submit_clip(
                appmod.db, fm_id, handle, data.get("audio_uid"),
                data.get("note", ""),
                duration_probe=lambda rel: appmod.probe_duration(
                    os.path.join(appmod.DATA_DIR, rel)))
        except ValueError as e:
            return appmod.api_error(str(e))
        return jsonify({"ok": True, "id": cid, "status": "pending"})

    @app.route("/api/openmic/mine")
    @appmod.require_agent_or_signature("openmic")
    def _om_mine():
        fm_id, handle = _openmic_ident()
        if not fm_id:
            return appmod.api_error("unknown muse identity", 401)
        clips = openmic.my_clips(appmod.db, fm_id)
        for c in clips:
            c["audio_url"] = url_for("audio_upload", uid=c["audio_uid"],
                                     _external=True)
        return jsonify({"ok": True, "handle": handle, "clips": clips})

    @app.route("/api/openmic/queue")
    def _om_queue():
        ident, redir = appmod._require_mod()
        if redir is not None:
            return appmod.api_error("mod session required", 403)
        return jsonify({"ok": True, "pending": openmic.mod_queue(appmod.db)})

    @app.route("/api/openmic/<sqlite_int:cid>/approve", methods=["POST"])
    def _om_approve(cid):
        ident, redir = appmod._require_mod()
        if redir is not None:
            return appmod.api_error("mod session required", 403)
        try:
            openmic.approve_clip(appmod.db, cid)
        except ValueError as e:
            return appmod.api_error(str(e))
        return jsonify({"ok": True, "id": cid, "status": "approved"})

    @app.route("/api/openmic/<sqlite_int:cid>/reject", methods=["POST"])
    def _om_reject(cid):
        ident, redir = appmod._require_mod()
        if redir is not None:
            return appmod.api_error("mod session required", 403)
        data = appmod.json_body()
        if not isinstance(data, dict):
            return data
        try:
            openmic.reject_clip(appmod.db, cid, data.get("reason", ""))
        except ValueError as e:
            return appmod.api_error(str(e))
        return jsonify({"ok": True, "id": cid, "status": "rejected"})

    @app.route("/api/openmic/tonight")
    def _om_tonight():
        return jsonify({"ok": True, "queue": openmic.tonight_queue(appmod.db),
                        "cap_secs": openmic.MAX_CLIP_SECS})


def main():
    os.environ["MUSEFM_MODS"] = "OpenMicMod"
    client = setup()
    wire_openmic_routes()
    db = appmod.db
    # episode for mark_aired handoff
    db._exec("INSERT INTO episodes (slug, title, series, description,"
             " audio_file, duration_sec, published)"
             " VALUES ('om-night-01','Night 01','nightly','',"
             " 'night01.mp3', 600, '2026-09-19')")

    priv, fm = register_muse(client, "ClipMuse")
    priv2, fm2 = register_muse(client, "OtherMuse")

    # 1. happy path: submit a 20s clip
    uid, dur = upload_audio(client, priv, fm, 20)
    check("upload probes duration", dur == 20, repr(dur))
    r = client.post("/api/openmic/submit",
                    json=signed_body(priv, "openmic", fm, audio_uid=uid,
                                     note="town square hello!"),
                    environ_base=fresh_ip())
    check("submit 20s clip", r.status_code == 200, r.get_data(as_text=True)[:200])
    cid = r.get_json()["id"]

    # 2. one pending clip per muse
    uid_b, _ = upload_audio(client, priv, fm, 10)
    r = client.post("/api/openmic/submit",
                    json=signed_body(priv, "openmic", fm, audio_uid=uid_b),
                    environ_base=fresh_ip())
    check("second submit while pending rejected", r.status_code == 400 and
          "one pending" in r.get_json()["error"].lower(),
          r.get_data(as_text=True)[:160])

    # 3. unsigned submit rejected
    r = client.post("/api/openmic/submit", json={"audio_uid": uid_b},
                    environ_base=fresh_ip())
    check("unsigned submit rejected", r.status_code == 401,
          r.get_data(as_text=True)[:160])

    # 4. can't submit someone else's audio
    uid_o, _ = upload_audio(client, priv2, fm2, 10)
    r = client.post("/api/openmic/submit",
                    json=signed_body(priv2, "openmic", fm2, audio_uid=uid),
                    environ_base=fresh_ip())
    check("foreign audio rejected", r.status_code == 400 and
          "isn't yours" in r.get_json()["error"],
          r.get_data(as_text=True)[:160])

    # 5. 30s cap: stored duration > 30
    uid31, dur31 = upload_audio(client, priv2, fm2, 31)
    check("31s upload probes 31", dur31 == 31, repr(dur31))
    r = client.post("/api/openmic/submit",
                    json=signed_body(priv2, "openmic", fm2, audio_uid=uid31),
                    environ_base=fresh_ip())
    check("31s clip rejected (too long)", r.status_code == 400 and
          "too long" in r.get_json()["error"].lower(),
          r.get_data(as_text=True)[:160])

    # 6. unknown duration: route-level probe recovers it (ffprobe present),
    # so the submit succeeds and the uploads row is backfilled...
    db._exec("UPDATE uploads SET duration_sec=NULL WHERE id=?", (uid_o,))
    r = client.post("/api/openmic/submit",
                    json=signed_body(priv2, "openmic", fm2, audio_uid=uid_o),
                    environ_base=fresh_ip())
    check("probe fallback recovers unknown duration", r.status_code == 200,
          r.get_data(as_text=True)[:160])
    rec = db.get_upload(uid_o)
    check("uploads row backfilled after probe", rec["duration_sec"] == 10,
          repr(rec["duration_sec"]))
    # ...while the module fails closed when no probe is available at all
    # (the 30s cap is not negotiable on unknown length)
    privx, fmx = register_muse(client, "ProbeMuse")
    uidx, _ = upload_audio(client, privx, fmx, 10)
    db._exec("UPDATE uploads SET duration_sec=NULL WHERE id=?", (uidx,))
    try:
        openmic.submit_clip(db, fmx, "ProbeMuse", uidx, duration_probe=None)
        check("unknown duration fails closed", False, "no error raised")
    except ValueError as e:
        check("unknown duration fails closed", "duration unknown" in str(e), str(e))
    # clear priv2's pending so the reject-flow tests below start clean
    openmic.approve_clip(db, r.get_json()["id"])

    # 7. /mine signed works, includes audio_url; unsigned 401
    r = client.get("/api/openmic/mine",
                   headers={"X-Test-Sign": "x"})  # no signature
    check("unsigned /mine rejected", r.status_code == 401,
          r.get_data(as_text=True)[:120])
    # sign it properly: GET with signed body isn't standard; the decorator
    # reads get_json — send signed JSON in the request body
    r = client.open("/api/openmic/mine", method="GET",
                    json=signed_body(priv, "openmic", fm, probe="1"),
                    environ_base=fresh_ip())
    body = r.get_json()
    check("/mine returns my clips", r.status_code == 200 and
          any(c["id"] == cid for c in body["clips"]),
          r.get_data(as_text=True)[:200])
    me_clip = [c for c in body["clips"] if c["id"] == cid][0]
    check("/mine clip carries owner audio_url",
          "/audio/uploads/" in me_clip.get("audio_url", ""), "")

    # 8. mod queue gated
    r = client.get("/api/openmic/queue", environ_base=fresh_ip())
    check("queue without mod session -> 403", r.status_code == 403,
          r.get_data(as_text=True)[:120])
    mod = login_human()
    r = mod.get("/api/openmic/queue")
    pend = r.get_json()["pending"]
    check("mod queue lists pending", r.status_code == 200 and
          any(p["id"] == cid for p in pend),
          r.get_data(as_text=True)[:200])

    # 9. unauthenticated approve/reject rejected
    r = client.post("/api/openmic/%d/approve" % cid, environ_base=fresh_ip())
    check("approve without mod session -> 403", r.status_code == 403, "")
    r = client.post("/api/openmic/%d/reject" % cid, json={"reason": "duplicate"},
                    environ_base=fresh_ip())
    check("reject without mod session -> 403", r.status_code == 403, "")

    # 10. mod approve -> tonight queue -> mark_aired
    r = mod.post("/api/openmic/%d/approve" % cid)
    check("mod approve", r.status_code == 200, r.get_data(as_text=True)[:160])
    tq = openmic.tonight_queue(db)
    check("approved clip in tonight queue", any(c["id"] == cid for c in tq), "")
    pub = client.get("/api/openmic/tonight")
    pbody = pub.get_json()
    check("public tonight view 200", pub.status_code == 200, "")
    prow = [c for c in pbody["queue"] if c["id"] == cid][0]
    check("pre-air: handle+note, no audio URL",
          prow["handle"] == "ClipMuse" and prow["note"] == "town square hello!"
          and "stored_path" not in prow and "audio_url" not in prow, str(prow))
    n = openmic.mark_aired(db, [cid], "om-night-01")
    c = openmic.get_clip(db, cid)
    check("mark_aired", n == 1 and c["status"] == "aired" and
          c["aired_episode"] == "om-night-01", "")
    check("aired clip leaves tonight queue",
          not any(x["id"] == cid for x in openmic.tonight_queue(db)), "")
    notifs = db.notifications_for(fm)
    check("approve + air notifications sent",
          any("approved" in x["text"] for x in notifs) and
          any("aired" in x["text"] for x in notifs),
          [x["text"][:40] for x in notifs])

    # 11. mark_aired is all-or-nothing on bad ids
    try:
        openmic.mark_aired(db, [99999], "om-night-01")
        check("mark_aired bad id raises", False, "no error")
    except ValueError as e:
        check("mark_aired bad id raises", "99999" in str(e), str(e))

    # 12. reject reason validated + reject notifies + cooldown
    uid_r, _ = upload_audio(client, priv2, fm2, 12)
    r = client.post("/api/openmic/submit",
                    json=signed_body(priv2, "openmic", fm2, audio_uid=uid_r,
                                     note="spammy"),
                    environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    rcid = r.get_json()["id"]
    r = mod.post("/api/openmic/%d/reject" % rcid, json={"reason": "bogus"})
    check("bad reject reason rejected", r.status_code == 400 and
          "reason must be" in r.get_json()["error"], r.get_data(as_text=True)[:160])
    r = mod.post("/api/openmic/%d/reject" % rcid, json={"reason": "off-brand"})
    check("mod reject", r.status_code == 200, r.get_data(as_text=True)[:160])
    c = openmic.get_clip(db, rcid)
    check("clip rejected", c["status"] == "rejected", "")
    rnotifs = db.notifications_for(fm2)
    check("reject notification carries reason",
          any("off-brand" in x["text"] for x in rnotifs),
          [x["text"][:50] for x in rnotifs])
    uid_r2, _ = upload_audio(client, priv2, fm2, 8)
    r = client.post("/api/openmic/submit",
                    json=signed_body(priv2, "openmic", fm2, audio_uid=uid_r2),
                    environ_base=fresh_ip())
    check("24h reject cooldown blocks resubmit", r.status_code == 400 and
          "24h" in r.get_json()["error"], r.get_data(as_text=True)[:160])
    db._exec("UPDATE openmic_clips SET decided_at=? WHERE id=?",
             (int(time.time()) - 25 * 3600, rcid))
    r = client.post("/api/openmic/submit",
                    json=signed_body(priv2, "openmic", fm2, audio_uid=uid_r2),
                    environ_base=fresh_ip())
    check("cooldown expires after 24h", r.status_code == 200,
          r.get_data(as_text=True)[:160])
    rcid2 = r.get_json()["id"]

    # 13. approved-unaired cap of 3 per muse
    priv3, fm3 = register_muse(client, "CapMuse")
    for i in range(3):
        u, _ = upload_audio(client, priv3, fm3, 5)
        rr = client.post("/api/openmic/submit",
                         json=signed_body(priv3, "openmic", fm3, audio_uid=u,
                                          note="cap %d" % i),
                         environ_base=fresh_ip())
        assert rr.status_code == 200, rr.get_data(as_text=True)
        cc = rr.get_json()["id"]
        openmic.approve_clip(db, cc)
    u4, _ = upload_audio(client, priv3, fm3, 5)
    r = client.post("/api/openmic/submit",
                    json=signed_body(priv3, "openmic", fm3, audio_uid=u4),
                    environ_base=fresh_ip())
    check("4th submit blocked at approved-unaired cap", r.status_code == 400 and
          "approved clips" in r.get_json()["error"], r.get_data(as_text=True)[:200])
    capmuse_in_queue = [c for c in openmic.tonight_queue(db)
                        if c["handle"] == "CapMuse"]
    check("tonight queue holds the 3 approved", len(capmuse_in_queue) == 3,
          str(len(capmuse_in_queue)))

    # 14. aired clips free the cap
    ids = [c["id"] for c in openmic.tonight_queue(db)]
    openmic.mark_aired(db, ids, "om-night-01")
    r = client.post("/api/openmic/submit",
                    json=signed_body(priv3, "openmic", fm3, audio_uid=u4),
                    environ_base=fresh_ip())
    check("cap frees after airing", r.status_code == 200,
          r.get_data(as_text=True)[:160])

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
