#!/usr/bin/env python3
"""Agent-API test harness v2 — in-process Flask TestClient against the CURRENT
checkout (hermetic DB under /tmp/agent-tester-hermetic, isolated REMOTE_ADDR
so rate-limit buckets are fresh)."""
import base64, hashlib, io, json, os, shutil, struct, sys, time, zlib

WORK = "/tmp/agent-tester-hermetic"
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(WORK)
os.environ["TOWNSQUARE_DB"] = os.path.join(WORK, "agent.db")
os.environ["DATA_DIR"] = WORK

sys.path.insert(0, os.path.expanduser("~/workspace/musefm-townsquare"))
import app as appmod
from identity import (b64u_encode, new_nonce, signed_body, sign_fields)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

client = appmod.app.test_client()
ENV = {"REMOTE_ADDR": "10.99.0.77"}

findings = []
notes = []

def finding(sev, title, request, response, expected):
    findings.append({"severity": sev, "title": title, "request": request,
                     "response": response, "expected": expected})
    print(f"[{sev}] {title}", flush=True)

def note(msg):
    notes.append(msg)
    print(f"  note: {msg}", flush=True)

def http(method, path, body=None, files=None, fields=None):
    kw = {"environ_base": ENV}
    if files:
        fn, fname, raw = files
        data = dict(fields or {})
        data[fn] = (io.BytesIO(raw), fname)
        r = client.post(path, data=data, content_type="multipart/form-data", **kw)
    else:
        if body is not None:
            r = client.open(path, method=method, json=body, **kw)
        else:
            r = client.open(path, method=method, **kw)
    try:
        j = r.get_json()
    except Exception:
        j = None
    return r.status_code, (j if j is not None else r.get_data(as_text=True)[:300])

def expect(status, resp, ok_when, sev, title, req_desc, expected_desc):
    if ok_when is True:
        ok = status == 200 and isinstance(resp, dict) and resp.get("ok") is True
    else:
        ok = status >= 400 and not (isinstance(resp, dict) and resp.get("ok"))
    if not ok:
        finding(sev, title, req_desc, {"http": status, "body": resp}, expected_desc)
    return ok

# --- keypair + register
priv = Ed25519PrivateKey.generate()
priv_b64 = b64u_encode(priv.private_bytes_raw())
pub_b64 = b64u_encode(priv.public_key().public_bytes_raw())

status, resp = http("POST", "/api/identity/register",
                    {"handle": "qa_hermetic", "public_key": pub_b64, "bio": "t"})
assert status == 200 and resp["ok"], resp
fm_id = resp["fm_id"]
note(f"registered {fm_id}")

def sign(action, **fields):
    return signed_body(priv_b64, action, fm_id, **fields)

def sign_other(action, **fields):
    o = Ed25519PrivateKey.generate()
    return signed_body(b64u_encode(o.private_bytes_raw()), action, fm_id, **fields)

# 1. valid post
b = sign("post", community="lobby", title="Hermetic signed post",
         body="valid musefm-v1 signature, hermetic client.", flair="discussion")
status, resp = http("POST", "/api/forum/post", b)
expect(status, resp, True, "P0", "valid signed post rejected",
       "POST /api/forum/post, fresh identity, valid sig", "ok:true + id")
pid = resp.get("id") if isinstance(resp, dict) else None
note(f"post id={pid}")

# 2. replay
status, resp = http("POST", "/api/forum/post", b)
expect(status, resp, False, "P0", "replay accepted",
       "identical signed body re-sent", "401 replay: nonce already used")

# 3. tampered
t = sign("post", community="lobby", title="legit", body="x", flair="discussion")
t["title"] = "TAMPERED"
status, resp = http("POST", "/api/forum/post", t)
expect(status, resp, False, "P0", "tampered payload accepted",
       "title mutated after signing", "401 signature does not verify")

# 4. wrong key
status, resp = http("POST", "/api/forum/post",
                    sign_other("post", community="lobby", title="wk", body="x", flair="discussion"))
expect(status, resp, False, "P0", "wrong-key signature accepted",
       "signed with key not registered to fm_id", "401 signature does not verify")

# 5. expired timestamp (10 min old)
ts = str(int(time.time()*1000) - 10*60*1000); n = new_nonce()
f5 = {"action": "post", "community": "lobby", "title": "old", "body": "x", "flair": "discussion"}
req5 = {"action": "post", "fm_id": fm_id, "timestamp": ts, "nonce": n,
        "signature": sign_fields(priv_b64, "post", fm_id, ts, n, f5), **f5}
status, resp = http("POST", "/api/forum/post", req5)
expect(status, resp, False, "P1", "expired timestamp accepted",
       "timestamp 10 min in the past", "401 timestamp outside the 5-minute window")

# 6. future timestamp
ts = str(int(time.time()*1000) + 10*60*1000); n = new_nonce()
req6 = dict(req5); req6.update(timestamp=ts, nonce=n,
        signature=sign_fields(priv_b64, "post", fm_id, ts, n, f5))
status, resp = http("POST", "/api/forum/post", req6)
expect(status, resp, False, "P2", "far-future timestamp accepted",
       "timestamp 10 min in the future", "401 timestamp outside window")
note(f"future-ts: {status} {str(resp)[:100]}")

# 7/8. missing signature / nonce
for drop in ("signature", "nonce"):
    m = sign("post", community="lobby", title="d", body="x", flair="discussion")
    del m[drop]
    status, resp = http("POST", "/api/forum/post", m)
    expect(status, resp, False, "P1", f"missing {drop} accepted",
           f"drop '{drop}'", "401")

# 9. short nonce (5 bytes), correctly signed
m = sign("post", community="lobby", title="bn", body="x", flair="discussion")
f9 = {"action": "post", "community": "lobby", "title": "bn", "body": "x", "flair": "discussion"}
m["nonce"] = b64u_encode(b"short")
m["signature"] = sign_fields(priv_b64, "post", fm_id, m["timestamp"], m["nonce"], f9)
status, resp = http("POST", "/api/forum/post", m)
expect(status, resp, False, "P1", "short nonce accepted",
       "nonce decodes to 5 bytes", "401 bad nonce")

# 10. unknown fm_id
m = sign("post", community="lobby", title="u", body="x", flair="discussion")
m["fm_id"] = "fm_" + "B"*12
status, resp = http("POST", "/api/forum/post", m)
expect(status, resp, False, "P0", "unknown fm_id accepted",
       "fm_id swapped for unregistered id", "401 unknown fm_id")

# 11. action mismatch: action=post body -> vote endpoint
m = sign("post", community="lobby", title="am", body="x", flair="discussion")
m.update(target_type="post", target_id=pid, value=1)
status, resp = http("POST", "/api/forum/vote", m)
expect(status, resp, False, "P1", "wrong-action body accepted on vote endpoint",
       "action='post' signed body sent to /api/forum/vote", "401 wrong action")

# 12. valid comment
c = sign("comment", post_id=pid, body="Hermetic signed comment.")
status, resp = http("POST", "/api/forum/comment", c)
expect(status, resp, True, "P0", "valid signed comment rejected",
       "POST /api/forum/comment valid sig", "ok:true + id")
cid = resp.get("id") if isinstance(resp, dict) else None
note(f"comment id={cid}")

# 13. comment on nonexistent post
c = sign("comment", post_id=999999999, body="ghost thread")
status, resp = http("POST", "/api/forum/comment", c)
expect(status, resp, False, "P2", "comment on nonexistent post accepted-or-500",
       "post_id=999999999", "400 ok:false")
note(f"ghost comment: {status} {str(resp)[:140]}")

# 14. valid vote
v = sign("vote", target_type="post", target_id=pid, value=1)
status, resp = http("POST", "/api/forum/vote", v)
expect(status, resp, True, "P1", "valid signed vote rejected",
       "vote +1 on own post", "ok:true + score")
note(f"vote: {str(resp)[:120]}")

# 15. vote replay (same body)
status, resp = http("POST", "/api/forum/vote", v)
expect(status, resp, False, "P2", "vote replay not nonce-rejected",
       "identical vote body re-sent", "401 replay")

# 16. vote toggle: two fresh +1 votes
s = []
for _ in range(2):
    vv = sign("vote", target_type="post", target_id=pid, value=1)
    status, resp = http("POST", "/api/forum/vote", vv)
    s.append(resp.get("score") if isinstance(resp, dict) else None)
note(f"two +1 votes on same post -> scores {s} (toggle semantics?)")

# 17. vote bad value
vv = sign("vote", target_type="post", target_id=pid, value=99)
status, resp = http("POST", "/api/forum/vote", vv)
expect(status, resp, False, "P2", "out-of-range vote value accepted-or-500",
       "value=99", "400 ok:false")
note(f"value=99 vote: {status} {str(resp)[:140]}")

# 18. valid react
r = sign("react", target_type="post", target_id=pid, emoji="🚀")
status, resp = http("POST", "/api/forum/react", r)
expect(status, resp, True, "P1", "valid signed react rejected",
       "react 🚀 on post", "ok:true")
note(f"react: {status} {str(resp)[:160]}")

# 19. react invalid target_type
r = sign("react", target_type="nope", target_id=pid, emoji="🚀")
status, resp = http("POST", "/api/forum/react", r)
expect(status, resp, False, "P2", "react invalid target_type accepted-or-500",
       "target_type=nope", "400 ok:false")
note(f"bad-target react: {status} {str(resp)[:160]}")

# --- media helpers
def png_1x1():
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    idat = chunk(b"IDAT", zlib.compress(struct.pack(">B", 0) + bytes([10, 200, 30])))
    return b"\x89PNG\r\n\x1a\n" + ihdr + idat + chunk(b"IEND", b"")

png = png_1x1()

# 20. valid image upload, ai_generated=true
sha = hashlib.sha256(png).hexdigest()
ub = sign("upload", file_sha256=sha, ai_generated="true")
status, resp = http("POST", "/api/upload/image", files=("image", "t.png", png), fields=ub)
expect(status, resp, True, "P0", "valid signed image upload rejected",
       "multipart image upload, correct sha256, ai_generated=true", "ok:true + image_url")
img_url = resp.get("image_url") if isinstance(resp, dict) else None
note(f"image_url={img_url} ai={resp.get('ai_generated') if isinstance(resp, dict) else None}")

# 21. sha256 mismatch
ub = sign("upload", file_sha256="0"*64)
status, resp = http("POST", "/api/upload/image", files=("image", "t.png", png), fields=ub)
expect(status, resp, False, "P0", "sha256 mismatch accepted",
       "signed sha256 of zeros, real bytes", "401 file_sha256 does not match")

# 22. unsigned upload
status, resp = http("POST", "/api/upload/image", files=("image", "t.png", png),
                    fields={"file_sha256": sha})
expect(status, resp, False, "P0", "unsigned upload accepted",
       "no signature fields at all", "401")

# 23. non-image bytes
junk = b"definitely not an image"
ub = sign("upload", file_sha256=hashlib.sha256(junk).hexdigest())
status, resp = http("POST", "/api/upload/image", files=("image", "f.png", junk), fields=ub)
expect(status, resp, False, "P1", "non-image bytes accepted as image",
       "text bytes as PNG", "400")

# 24. valid gif upload
gif = (b"GIF89a" + bytes([1,0,1,0,0x80,0,0,0,0,0,0xff,0xff,0xff,0x21,0xf9,0x04,
       0x00,0x00,0x00,0x00,0x00,0x2c,0x00,0x00,0x00,0x00,0x01,0x00,0x01,0x00,
       0x00,0x02,0x02,0x44,0x01,0x00,0x3b]))
ub = sign("upload", file_sha256=hashlib.sha256(gif).hexdigest())
status, resp = http("POST", "/api/upload/gif", files=("gif", "t.gif", gif), fields=ub)
expect(status, resp, True, "P1", "valid signed GIF upload rejected",
       "minimal valid GIF89a", "ok:true + gif_url")
gif_url = resp.get("gif_url") if isinstance(resp, dict) else None
note(f"gif_url={gif_url}")

# 25. png bytes as gif
ub = sign("upload", file_sha256=sha)
status, resp = http("POST", "/api/upload/gif", files=("gif", "f.gif", png), fields=ub)
expect(status, resp, False, "P1", "PNG bytes accepted as GIF (magic bytes ignored)",
       "PNG bytes under gif field", "400 not a gif")

# 26. post with attached media
p2 = sign("post", community="lobby", title="Hermetic media post",
          body="attached via signed uploads.", flair="discussion",
          image_url=img_url or "", image_ai="true", gif_url=gif_url or "")
status, resp = http("POST", "/api/forum/post", p2)
expect(status, resp, True, "P1", "post with valid media URLs rejected",
       "image_url + gif_url from uploads", "ok:true")
note(f"media post: {status} {str(resp)[:140]}")

# 27. external image url in post
p3 = sign("post", community="lobby", title="x", body="x", flair="discussion",
          image_url="https://evil.example.com/x.png", image_ai="false")
status, resp = http("POST", "/api/forum/post", p3)
expect(status, resp, False, "P2", "external image URL accepted in post",
       "image_url=https://evil.example.com/x.png", "400")
note(f"ext-img: {status} {str(resp)[:140]}")

# 28. javascript: URL in post image field
p4 = sign("post", community="lobby", title="x", body="x", flair="discussion",
          image_url="javascript:alert(1)", image_ai="false")
status, resp = http("POST", "/api/forum/post", p4)
expect(status, resp, False, "P1", "javascript: URL accepted as image_url",
       "image_url=javascript:alert(1)", "400")
note(f"js-url: {status} {str(resp)[:140]}")

# 29. unsigned forum post
status, resp = http("POST", "/api/forum/post",
                    {"community": "lobby", "title": "nosig", "body": "x"})
expect(status, resp, False, "P0", "unsigned post accepted",
       "no signature fields", "401 musefm-v1 auth failed")

# 30. identity update
u = sign("identity_update", bio="hermetic bio", avatar_url="")
status, resp = http("POST", "/api/identity/update", u)
expect(status, resp, True, "P1", "valid identity update rejected",
       "signed identity_update", "ok:true")

# 31. heartbeat
h = sign("heartbeat")
status, resp = http("POST", "/api/rewards/heartbeat", h)
expect(status, resp, True, "P2", "valid heartbeat rejected", "signed heartbeat", "ok:true")

# 32. valid video upload (minimal MP4 w/ moov)
def minimal_mp4():
    def box(typ, payload):
        return struct.pack(">I", 8 + len(payload)) + typ + payload
    return box(b"ftyp", b"isom" + b"\x00\x00\x00\x01" + b"isomiso2") + box(b"moov", b"\x00"*4600)
mp4 = minimal_mp4()
ub = sign("upload", file_sha256=hashlib.sha256(mp4).hexdigest(),
          ai_generated="true", title="hermetic clip")
status, resp = http("POST", "/api/upload/video", files=("video", "t.mp4", mp4), fields=ub)
expect(status, resp, True, "P1", "valid signed video upload rejected",
       "structurally valid minimal MP4", "ok:true + video_url")
vid_url = resp.get("video_url") if isinstance(resp, dict) else None
note(f"video_url={vid_url}")

# 33. TRUNCATED video (ftyp + zeros, 108 bytes) — the key regression test
trunc = struct.pack(">I", 16) + b"ftyp" + b"\x00"*100
ub = sign("upload", file_sha256=hashlib.sha256(trunc).hexdigest())
status, resp = http("POST", "/api/upload/video", files=("video", "tr.mp4", trunc), fields=ub)
expect(status, resp, False, "P1", "truncated video accepted",
       "108-byte ftyp+zeros upload", "400 corrupt or truncated")
note(f"truncated video on CURRENT checkout: {status} {str(resp)[:140]}")

# 34. WebM without Segment element
webm = b"\x1a\x45\xdf\xa3" + b"\x00"*5000
ub = sign("upload", file_sha256=hashlib.sha256(webm).hexdigest())
status, resp = http("POST", "/api/upload/video", files=("video", "w.webm", webm), fields=ub)
expect(status, resp, False, "P2", "WebM without Segment accepted",
       "EBML header + zeros, no Segment", "400 corrupt or truncated")
note(f"segmentless webm: {status} {str(resp)[:140]}")

# 35. serve media back
for name, url in [("image", img_url), ("gif", gif_url), ("video", vid_url)]:
    if url:
        r = client.get(url, environ_base=ENV)
        if r.status_code != 200:
            finding("P1", f"uploaded {name} not servable", f"GET {url}",
                    {"http": r.status_code}, "200")
        else:
            note(f"GET {url} -> 200 ({len(r.get_data())} bytes)")

# 36. replay nonce across DIFFERENT valid signatures
ts = str(int(time.time()*1000)); n = new_nonce()
fA = {"action": "post", "community": "lobby", "title": "dupA", "body": "a", "flair": "discussion"}
reqA = {"action": "post", "fm_id": fm_id, "timestamp": ts, "nonce": n,
        "signature": sign_fields(priv_b64, "post", fm_id, ts, n, fA), **fA}
sA, _ = http("POST", "/api/forum/post", reqA)
fB = dict(fA); fB.update(title="dupB", body="b")
reqB = {"action": "post", "fm_id": fm_id, "timestamp": ts, "nonce": n,
        "signature": sign_fields(priv_b64, "post", fm_id, ts, n, fB), **fB}
sB, rB = http("POST", "/api/forum/post", reqB)
expect(sB, rB, False, "P1", "nonce reuse across valid signatures accepted",
       "same nonce, second correctly-signed request", "401 replay: nonce already used")
note(f"nonce-reuse: first={sA} second={sB}")

# 37. malformed JSON body
r = client.post("/api/forum/post", data="{not json", content_type="application/json",
                environ_base=ENV)
if r.status_code < 400:
    finding("P2", "malformed JSON not rejected", "POST /api/forum/post '{not json'",
            {"http": r.status_code}, "400")
else:
    note(f"malformed JSON -> {r.status_code}")

# 38. attestation: ai_generated flag round-trip on video
if vid_url:
    uid = int(vid_url.rsplit("/", 1)[1])
    import videos as vmod
    # read via app's own DB handle
    row = appmod.db._one("SELECT ai_generated, title FROM video_uploads WHERE id=?", (uid,))
    note(f"video row: ai_generated={row['ai_generated']} title={row['title']!r} (signed ai_generated=true -> stored 1?)")
    if row["ai_generated"] != 1:
        finding("P1", "ai_generated attestation not persisted",
                "video upload signed ai_generated=true", {"row": dict(row)},
                "video_uploads.ai_generated = 1")

# 39. empty-string signature
m = sign("post", community="lobby", title="es", body="x", flair="discussion")
m["signature"] = ""
status, resp = http("POST", "/api/forum/post", m)
expect(status, resp, False, "P2", "empty-string signature accepted",
       "signature=''", "401")

print(f"\n=== FINDINGS: {len(findings)}")
for f in findings:
    print(json.dumps(f))
