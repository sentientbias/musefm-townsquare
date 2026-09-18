#!/usr/bin/env python3
"""Agent-API test harness for the Muse FM tester loop (AGENT TESTER persona).

Exercises musefm-v1 signed flows against http://127.0.0.1:PORT and prints
a JSONL-style report of every finding. Write to scratch DB only.
"""
import base64, hashlib, io, json, os, struct, sys, time, urllib.request, zlib

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8473
BASE = f"http://127.0.0.1:{PORT}"

sys.path.insert(0, os.path.expanduser("~/workspace/musefm-townsquare"))
from identity import (b64u_encode, b64u_decode, new_fm_id, new_nonce,
                      signed_body, sign_fields, canonical_message)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

findings = []
notes = []

def finding(sev, title, request, response, expected):
    findings.append({"severity": sev, "title": title, "request": request,
                     "response": response, "expected": expected})
    print(f"[{sev}] {title}", flush=True)

def note(msg):
    notes.append(msg)
    print(f"  note: {msg}", flush=True)

# ------------------------------------------------------------- http helpers
def http(method, path, body=None, files=None, fields=None):
    """body: dict -> JSON. files: (fieldname, filename, bytes) multipart."""
    if files:
        boundary = "harness-boundary-xyz"
        buf = io.BytesIO()
        for k, v in (fields or {}).items():
            buf.write(f"--{boundary}\r\n".encode())
            buf.write(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
            buf.write(f"{v}\r\n".encode())
        fn, fname, raw = files
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{fn}"; filename="{fname}"\r\n'.encode())
        buf.write(b"Content-Type: application/octet-stream\r\n\r\n")
        buf.write(raw + b"\r\n")
        buf.write(f"--{boundary}--\r\n".encode())
        data = buf.getvalue()
        req = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            txt = r.read().decode("utf-8", "replace")
            try:
                j = json.loads(txt)
            except Exception:
                j = None
            return r.status, j or txt
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        try:
            j = json.loads(txt)
        except Exception:
            j = None
        return e.code, j or txt

def expect(status, resp, ok_when, sev, title, req_desc, expected_desc):
    ok = (resp.get("ok") if isinstance(resp, dict) else None) == ok_when
    if ok_when is True:
        ok = ok and status == 200
    if ok_when is False:
        ok = (not (isinstance(resp, dict) and resp.get("ok"))) and status >= 400
    if not ok:
        finding(sev, title, req_desc, {"http": status, "body": resp}, expected_desc)
    return ok

# ------------------------------------------------------------------- setup
priv = Ed25519PrivateKey.generate()
pub = priv.public_key()
priv_b64 = b64u_encode(priv.private_bytes_raw())
pub_b64 = b64u_encode(pub.public_bytes_raw())

status, resp = http("POST", "/api/identity/register",
                    {"handle": "qa_agent_1", "public_key": pub_b64,
                     "bio": "test agent", "avatar_url": ""})
if not expect(status, resp, True, "P0", "identity registration failed",
              "POST /api/identity/register {handle, public_key}", "ok:true + fm_id"):
    print(json.dumps(findings, indent=2)); sys.exit(2)
fm_id = resp["fm_id"]
note(f"registered {fm_id}")

def sign(action, **fields):
    return signed_body(priv_b64, action, fm_id, **fields)

def sign_other_key(action, **fields):
    """Sign with a DIFFERENT key than the registered one."""
    other = Ed25519PrivateKey.generate()
    ob64 = b64u_encode(other.private_bytes_raw())
    return signed_body(ob64, action, fm_id, **fields)

# ------------------------------------------------------- 1. signed post (good)
body = sign("post", community="lobby", title="Hello from the agent tester",
            body="This post carries a valid musefm-v1 signature.",
            flair="discussion")
status, resp = http("POST", "/api/forum/post", body)
expect(status, resp, True, "P0", "signed post rejected",
       "POST /api/forum/post with valid signature", "ok:true, post id")
pid = resp.get("id") if isinstance(resp, dict) else None
note(f"posted id={pid}")

# ------------------------------------------------------- 2. replay same body
status2, resp2 = http("POST", "/api/forum/post", body)
expect(status2, resp2, False, "P1", "replay accepted — nonce reuse NOT rejected",
       "POST /api/forum/post with the EXACT same signed body again",
       "401 ok:false with replay error")
note(f"replay status={status2} body={str(resp2)[:120]}")

# ------------------------------------------------------- 3. tampered payload
t = sign("post", community="lobby", title="legit title", body="legit body", flair="discussion")
t["title"] = "TAMPERED TITLE"
status, resp = http("POST", "/api/forum/post", t)
expect(status, resp, False, "P0", "tampered payload accepted",
       "sign fields then change title after signing", "401 signature does not verify")
note(f"tamper status={status} body={str(resp)[:120]}")

# ------------------------------------------------------- 4. wrong-signature key
w = sign_other_key("post", community="lobby", title="wrong key", body="x", flair="discussion")
status, resp = http("POST", "/api/forum/post", w)
expect(status, resp, False, "P0", "wrong-key signature accepted",
       "signed with a keypair not registered to the fm_id", "401 signature does not verify")

# ------------------------------------------------------- 5. expired timestamp
old_ts = str(int(time.time() * 1000) - 10 * 60 * 1000)  # 10 min ago
n = new_nonce()
allf = {"action": "post", "community": "lobby", "title": "old", "body": "old", "flair": "discussion"}
sig = sign_fields(priv_b64, "post", fm_id, old_ts, n, allf)
expired = {"action": "post", "fm_id": fm_id, "timestamp": old_ts, "nonce": n,
           "signature": sig, "community": "lobby", "title": "old", "body": "old", "flair": "discussion"}
status, resp = http("POST", "/api/forum/post", expired)
expect(status, resp, False, "P1", "expired timestamp accepted (>5min window)",
       "timestamp 10 minutes in the past, otherwise valid", "401 timestamp outside the 5-minute window")

# ------------------------------------------------------- 6. future timestamp
fut_ts = str(int(time.time() * 1000) + 10 * 60 * 1000)
n2 = new_nonce()
sig2 = sign_fields(priv_b64, "post", fm_id, fut_ts, n2, allf)
future = {"action": "post", "fm_id": fm_id, "timestamp": fut_ts, "nonce": n2,
          "signature": sig2, "community": "lobby", "title": "fut", "body": "fut", "flair": "discussion"}
status, resp = http("POST", "/api/forum/post", future)
expect(status, resp, False, "P2", "far-future timestamp accepted",
       "timestamp 10 minutes in the future", "401 timestamp outside window")
note(f"future-ts body={str(resp)[:120]}")

# ------------------------------------------------------- 7. missing signature
m = sign("post", community="lobby", title="nosig", body="x", flair="discussion")
del m["signature"]
status, resp = http("POST", "/api/forum/post", m)
expect(status, resp, False, "P1", "missing signature accepted",
       "drop the signature field, keep everything else", "401")

# ------------------------------------------------------- 8. missing nonce
m = sign("post", community="lobby", title="nononce", body="x", flair="discussion")
del m["nonce"]
status, resp = http("POST", "/api/forum/post", m)
expect(status, resp, False, "P1", "missing nonce accepted",
       "drop the nonce field", "401")

# ------------------------------------------------------- 9. bad nonce (not 16 bytes)
m = sign("post", community="lobby", title="badnonce", body="x", flair="discussion")
m["nonce"] = b64u_encode(b"short")
# must re-sign so only nonce validity is being tested
n3 = m["nonce"]
allf9 = {"action": "post", "community": "lobby", "title": "badnonce", "body": "x", "flair": "discussion"}
sig9 = sign_fields(priv_b64, "post", fm_id, m["timestamp"], n3, allf9)
m["signature"] = sig9
status, resp = http("POST", "/api/forum/post", m)
expect(status, resp, False, "P1", "short nonce accepted",
       "nonce decodes to 5 bytes, correctly signed over it", "401 bad nonce")

# ------------------------------------------------------- 10. unknown fm_id
m = sign("post", community="lobby", title="u", body="x", flair="discussion")
m["fm_id"] = "fm_" + "A" * 12
status, resp = http("POST", "/api/forum/post", m)
expect(status, resp, False, "P0", "unknown fm_id accepted",
       "swap fm_id for an unregistered one (sig over original id)", "401 unknown fm_id")

# ------------------------------------------------------- 11. wrong action for endpoint
m = sign("post", community="lobby", title="actionmix", body="x", flair="discussion")
status, resp = http("POST", "/api/forum/vote", {**m, "target_type": "post", "target_id": pid or 1, "value": 1})
expect(status, resp, False, "P1", "wrong-action signature accepted on vote endpoint",
       "action='post' signed body sent to /api/forum/vote", "401 wrong action for this endpoint")
note(f"action-mix body={str(resp)[:140]}")

# ------------------------------------------------------- 12. signed comment (good)
c = sign("comment", post_id=pid, body="Agent tester comment — signed and valid.")
status, resp = http("POST", "/api/forum/comment", c)
expect(status, resp, True, "P0", "signed comment rejected",
       "POST /api/forum/comment valid signature", "ok:true, comment id")
cid = resp.get("id") if isinstance(resp, dict) else None
note(f"comment id={cid}")

# ------------------------------------------------------- 13. signed vote (good)
v = sign("vote", target_type="post", target_id=pid, value=1)
status, resp = http("POST", "/api/forum/vote", v)
expect(status, resp, True, "P1", "signed vote rejected",
       "POST /api/forum/vote target_type=post target_id=<pid> value=1", "ok:true, score")
note(f"vote -> {str(resp)[:120]}")

# ------------------------------------------------------- 14. double vote (same body = replay)
status, resp = http("POST", "/api/forum/vote", v)
expect(status, resp, False, "P2", "vote replay not nonce-rejected",
       "re-send the identical signed vote body", "401 replay (nonce already used)")
note(f"double-vote body={str(resp)[:120]}")

# ------------------------------------------------------- 15. vote toggle semantics
v2 = sign("vote", target_type="post", target_id=pid, value=1)
status, r1 = http("POST", "/api/forum/vote", v2)
v3 = sign("vote", target_type="post", target_id=pid, value=1)
status, r2 = http("POST", "/api/forum/vote", v3)
s1 = r1.get("score") if isinstance(r1, dict) else None
s2 = r2.get("score") if isinstance(r2, dict) else None
note(f"vote toggle: score after two +1 votes on same post = {s1} then {s2} (toggle behavior? up-down vote)")

# ------------------------------------------------------- 16. signed react (good)
r = sign("react", target_type="post", target_id=pid, emoji="🚀")
status, resp = http("POST", "/api/forum/react", r)
expect(status, resp, True, "P1", "signed react rejected",
       "POST /api/forum/react target_type=post target_id=<pid> emoji=🚀", "ok:true")
note(f"react -> {str(resp)[:160]}")

# ------------------------------------------------------- 17. react with bad target_type
r = sign("react", target_type="nope", target_id=99999, emoji="🚀")
status, resp = http("POST", "/api/forum/react", r)
expect(status, resp, False, "P2", "react with invalid target_type accepted or 500",
       "target_type=nope (invalid)", "400 ok:false")
note(f"bad-target react -> {status} {str(resp)[:160]}")

# ------------------------------------------------------- 18. PNG upload (signed multipart, ai_generated=true)
def png_1x1():
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    raw = struct.pack(">B", 0) + bytes([200, 30, 30])
    idat = chunk(b"IDAT", zlib.compress(raw))
    return b"\x89PNG\r\n\x1a\n" + ihdr + idat + chunk(b"IEND", b"")

png = png_1x1()
sha = hashlib.sha256(png).hexdigest()
ub = sign("upload", file_sha256=sha, ai_generated="true")
status, resp = http("POST", "/api/upload/image",
                    files=("image", "test.png", png), fields=ub)
expect(status, resp, True, "P0", "signed image upload rejected",
       "multipart /api/upload/image with valid sig + correct sha256 + ai_generated=true",
       "ok:true, image_url")
img_url = resp.get("image_url") if isinstance(resp, dict) else None
note(f"image_url={img_url} ai={resp.get('ai_generated') if isinstance(resp, dict) else None}")

# ------------------------------------------------------- 19. upload with sha256 mismatch
ub = sign("upload", file_sha256="0" * 64, ai_generated="true")
status, resp = http("POST", "/api/upload/image",
                    files=("image", "test.png", png), fields=ub)
expect(status, resp, False, "P0", "upload accepted despite file_sha256 mismatch",
       "sign over wrong sha256, upload real bytes", "401 file_sha256 does not match")

# ------------------------------------------------------- 20. upload unsigned
status, resp = http("POST", "/api/upload/image",
                    files=("image", "test.png", png),
                    fields={"file_sha256": sha})
expect(status, resp, False, "P0", "upload accepted with NO signature",
       "multipart upload with sha256 field but no signature", "401")

# ------------------------------------------------------- 21. upload non-image bytes
junk = b"this is not a png file, obviously"
ub = sign("upload", file_sha256=hashlib.sha256(junk).hexdigest(), ai_generated="false")
status, resp = http("POST", "/api/upload/image",
                    files=("image", "fake.png", junk), fields=ub)
expect(status, resp, False, "P1", "non-image bytes accepted as image upload",
       "upload text bytes with valid signature", "400 not an image")

# ------------------------------------------------------- 22. GIF upload (signed multipart, minimal valid GIF)
gif = (b"GIF89a" + bytes([1, 0, 1, 0, 0x80, 0, 0, 0, 0, 0, 0xff, 0xff, 0xff,
                          0x21, 0xf9, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00,
                          0x2c, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00,
                          0x00, 0x02, 0x02, 0x44, 0x01, 0x00, 0x3b]))
sha_g = hashlib.sha256(gif).hexdigest()
ub = sign("upload", file_sha256=sha_g)
status, resp = http("POST", "/api/upload/gif",
                    files=("gif", "test.gif", gif), fields=ub)
expect(status, resp, True, "P1", "signed GIF upload rejected",
       "multipart /api/upload/gif with valid minimal GIF89a", "ok:true, gif_url")
gif_url = resp.get("gif_url") if isinstance(resp, dict) else None
note(f"gif_url={gif_url}")

# ------------------------------------------------------- 23. fake gif (png bytes as gif)
ub = sign("upload", file_sha256=sha)
status, resp = http("POST", "/api/upload/gif",
                    files=("gif", "fake.gif", png), fields=ub)
expect(status, resp, False, "P1", "PNG bytes accepted as GIF upload (magic bytes ignored?)",
       "upload PNG bytes under the gif field, valid signature", "400 not a gif")

# ------------------------------------------------------- 24. post referencing uploaded image + gif
p2 = sign("post", community="lobby", title="Agent tester: media attachments",
          body="image and gif attached via the signed upload flow.",
          flair="discussion", image_url=img_url or "", image_ai="true",
          gif_url=gif_url or "")
status, resp = http("POST", "/api/forum/post", p2)
expect(status, resp, True, "P1", "post with attached media URLs rejected",
       "image_url + gif_url from uploads passed back into a signed post",
       "ok:true")
note(f"media post -> {str(resp)[:160]}")

# ------------------------------------------------------- 25. post with external (non-whitelisted) image url
p3 = sign("post", community="lobby", title="ext img", body="x",
          flair="discussion", image_url="https://evil.example.com/x.png", image_ai="false")
status, resp = http("POST", "/api/forum/post", p3)
expect(status, resp, False, "P2", "external image URL accepted in post",
       "image_url=https://evil.example.com/x.png", "400 bad image url")
note(f"ext-img post -> {status} {str(resp)[:160]}")

# ------------------------------------------------------- 26. unsigned forum post (agent_key fallback absent)
status, resp = http("POST", "/api/forum/post",
                    {"community": "lobby", "title": "nosig post", "body": "x"})
expect(status, resp, False, "P0", "unsigned post accepted",
       "plain JSON post with no signature fields at all", "401 musefm-v1 auth failed")

# ------------------------------------------------------- 27. identity update (signed flow)
u = sign("identity_update", bio="updated bio from agent tester", avatar_url="")
status, resp = http("POST", "/api/identity/update", u)
expect(status, resp, True, "P1", "signed identity update rejected",
       "POST /api/identity/update valid signature", "ok:true")
note(f"identity update -> {str(resp)[:160]}")

# ------------------------------------------------------- 28. heartbeat (signed)
h = sign("heartbeat")
status, resp = http("POST", "/api/rewards/heartbeat", h)
expect(status, resp, True, "P2", "signed heartbeat rejected",
       "POST /api/rewards/heartbeat valid signature", "ok:true")
note(f"heartbeat -> {str(resp)[:160]}")

# ------------------------------------------------------- 29. profile read (read-only)
status, resp = http("GET", f"/api/identity/{fm_id}")
if not (status == 200 and isinstance(resp, dict) and resp.get("ok")):
    finding("P2", "identity profile read failed",
            f"GET /api/identity/{fm_id}", {"http": status, "body": str(resp)[:200]},
            "200 ok:true with public profile")
else:
    note(f"profile read ok; visible fields: {list(resp['identity'].keys())}")

# ------------------------------------------------------- 30. video upload (valid minimal MP4 w/ moov)
def minimal_mp4():
    # ftyp box + minimal moov box (>= 4096 bytes total to pass structure check)
    def box(typ, payload):
        return struct.pack(">I", 8 + len(payload)) + typ + payload
    ftyp = box(b"ftyp", b"isom" + b"\x00\x00\x00\x01" + b"isomiso2")
    moov = box(b"moov", b"\x00" * 4600)  # padded moov
    return ftyp + moov
mp4 = minimal_mp4()
sha_v = hashlib.sha256(mp4).hexdigest()
ub = sign("upload", file_sha256=sha_v, ai_generated="true", title="agent test clip")
status, resp = http("POST", "/api/upload/video",
                    files=("video", "test.mp4", mp4), fields=ub)
expect(status, resp, True, "P1", "signed video upload rejected",
       "multipart /api/upload/video with structurally valid minimal MP4", "ok:true, video_url")
vid_url = resp.get("video_url") if isinstance(resp, dict) else None
note(f"video_url={vid_url} duration={resp.get('duration_secs') if isinstance(resp, dict) else None}")

# ------------------------------------------------------- 31. truncated video (ftyp + zeros) rejected
trunc = minimal_mp4()[:len(struct.pack(">I",0))]  # tiny: below MIN_VIDEO_BYTES
trunc = struct.pack(">I", 16) + b"ftyp" + b"\x00" * 100  # 112 bytes < 4096
ub = sign("upload", file_sha256=hashlib.sha256(trunc).hexdigest())
status, resp = http("POST", "/api/upload/video",
                    files=("video", "trunc.mp4", trunc), fields=ub)
expect(status, resp, False, "P1", "truncated video accepted",
       "ftyp + zeros below minimum size", "400 corrupt or truncated")

# ------------------------------------------------------- 32. serve uploaded media back (read-only)
for name, url in [("image", img_url), ("gif", gif_url), ("video", vid_url)]:
    if url:
        status, resp = http("GET", url)
        if status != 200:
            finding("P1", f"uploaded {name} not servable", f"GET {url}",
                    {"http": status}, "200 with bytes")
        else:
            note(f"GET {url} -> 200 ({len(resp) if isinstance(resp, str) else 'bytes'} )")

# ------------------------------------------------------- 33. non-canonical field types (int vs str timestamp)
m = sign("post", community="lobby", title="int-ts", body="x", flair="discussion")
m["timestamp"] = int(m["timestamp"])  # int timestamp, still signed over str -> signature differs
status, resp = http("POST", "/api/forum/post", m)
note(f"int-timestamp (sig over str ts) -> {status} {str(resp)[:120]} (expected 401: canonical form mismatch)")

# ------------------------------------------------------- 34. duplicate nonce with DIFFERENT valid signature
n_a = new_nonce()
ts_a = str(int(time.time() * 1000))
fa = {"action": "post", "community": "lobby", "title": "dupA", "body": "a", "flair": "discussion"}
sig_a = sign_fields(priv_b64, "post", fm_id, ts_a, n_a, fa)
req_a = {"action": "post", "fm_id": fm_id, "timestamp": ts_a, "nonce": n_a,
         "signature": sig_a, **fa}
status, resp = http("POST", "/api/forum/post", req_a)
ok1 = status == 200
fb = {"action": "post", "community": "lobby", "title": "dupB", "body": "b", "flair": "discussion"}
sig_b = sign_fields(priv_b64, "post", fm_id, ts_a, n_a, fb)  # same nonce, new valid sig
req_b = {"action": "post", "fm_id": fm_id, "timestamp": ts_a, "nonce": n_a,
         "signature": sig_b, **fb}
status, resp = http("POST", "/api/forum/post", req_b)
expect(status, resp, False, "P1", "nonce reuse across different valid signatures accepted",
       "same nonce, second request correctly signed", "401 replay: nonce already used")
note(f"nonce-reuse first={ok1} second={status} {str(resp)[:120]}")

# ------------------------------------------------------- 35. attestation check on audio upload path
# (audio requires ai_generated signed + file bytes; read the route contract)
import importlib
try:
    import ai_audio  # noqa
    note("ai_audio module present; skipping raw audio bytes test (format-specific)")
except Exception as e:
    note(f"ai_audio import: {e}")

print("\n=== FINDINGS:", len(findings))
for f in findings:
    print(json.dumps(f))
