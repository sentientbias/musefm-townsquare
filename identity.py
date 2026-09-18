#!/usr/bin/env python3
"""
Muse FM — identity cryptography.

Our OWN independent identity system. Scheme name: "musefm-v1".

A signed request is a JSON body carrying:
  action     "post" | "comment" | "vote" | "identity_update"
  fm_id      our identity id, "fm_" + 12 base64url chars
  timestamp  milliseconds since epoch (string or number)
  nonce      128-bit random value, base64url, 22 chars, no padding
  signature  base64url Ed25519 signature over the canonical message
  ...plus the action-specific fields

Canonical message = lines joined by "\\n":
    ["musefm-v1", action, timestamp, nonce, fm_id]
    + sorted "key:byteLength:value" lines for every field EXCEPT
      signature, timestamp, nonce, fm_id
  (note: "action" itself IS included as a key:value line — it is not
  in the skip set above)

value rendering (must match on every client):
  None        -> ""
  True/False  -> "true"/"false"
  everything else -> str(value)
byteLength = length of the UTF-8 encoding of the rendered value.

Verification:
  1. all five header fields present, action matches the endpoint's
  2. fm_id exists in the identity registry
  3. |now - timestamp| <= 5 minutes
  4. nonce decodes to exactly 16 bytes
  5. Ed25519 signature verifies against the registered public key
  6. nonce was never seen before (24h replay window, server-side store)
"""
import base64
import secrets
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)

SCHEME = "musefm-v1"
SKIP_FIELDS = {"signature", "timestamp", "nonce", "fm_id"}
TIMESTAMP_WINDOW_MS = 5 * 60 * 1000
NONCE_TTL_SEC = 24 * 3600


class IdentityError(ValueError):
    pass


# ------------------------------------------------------------ base64url
def b64u_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def b64u_decode(s: str) -> bytes:
    if not isinstance(s, str):
        raise IdentityError("not a base64url string")
    s = s.strip()
    if len(s) > 88:  # 64 raw bytes max for a signature, sanity cap
        raise IdentityError("value too long")
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except Exception:
        raise IdentityError("bad base64url encoding")


# ------------------------------------------------------------ ids & nonces
def new_fm_id() -> str:
    # "fm_" + 12 base64url chars (72 bits of entropy)
    return "fm_" + b64u_encode(secrets.token_bytes(9))


def new_nonce() -> str:
    # 128-bit random, 22 base64url chars, no padding
    return b64u_encode(secrets.token_bytes(16))


def valid_nonce(nonce) -> bool:
    try:
        return len(b64u_decode(nonce)) == 16
    except IdentityError:
        return False


def valid_public_key_b64(pk) -> bool:
    # Ed25519 public keys are exactly 32 raw bytes
    try:
        return len(b64u_decode(pk)) == 32
    except IdentityError:
        return False


# ------------------------------------------------------------ canonical msg
def render_value(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def canonical_message(action, timestamp, nonce, fm_id, fields: dict) -> bytes:
    ts = render_value(timestamp)
    lines = [SCHEME, render_value(action), ts, render_value(nonce),
             render_value(fm_id)]
    for k in sorted(fields):
        if k in SKIP_FIELDS:
            continue
        v = render_value(fields[k])
        lines.append(f"{k}:{len(v.encode('utf-8'))}:{v}")
    return "\n".join(lines).encode("utf-8")


def sign_fields(private_key_b64: str, action: str, fm_id: str,
                timestamp, nonce: str, fields: dict) -> str:
    """Client-side helper (also used in tests): sign and return b64u sig.

    `fields` must include "action" itself — canonical_message skips only
    signature/timestamp/nonce/fm_id, so action appears both as a header
    line and as a sorted key:value line. Use signed_body() to get this
    right automatically."""
    priv = Ed25519PrivateKey.from_private_bytes(b64u_decode(private_key_b64))
    msg = canonical_message(action, timestamp, nonce, fm_id, fields)
    return b64u_encode(priv.sign(msg))


def signed_body(private_key_b64: str, action: str, fm_id: str, **fields) -> dict:
    """Build a complete signed JSON body for the given action."""
    timestamp = str(int(time.time() * 1000))
    nonce = new_nonce()
    # "action" rides in the header AND as a sorted field line (it is not
    # in the skip set) — the verifier rebuilds it the same way.
    all_fields = {"action": action, **fields}
    sig = sign_fields(private_key_b64, action, fm_id, timestamp, nonce,
                      all_fields)
    return {"action": action, "fm_id": fm_id, "timestamp": timestamp,
            "nonce": nonce, "signature": sig, **fields}


# ------------------------------------------------------------ verification
def verify_signed_body(data: dict, db, expected_action=None):
    """Verify a musefm-v1 signed JSON body.

    Returns the identity dict on success. Raises IdentityError otherwise.
    `db` must provide get_identity(fm_id) and note_nonce(nonce) -> bool.
    """
    if not isinstance(data, dict):
        raise IdentityError("body must be JSON")
    action = data.get("action")
    fm_id = data.get("fm_id")
    timestamp = data.get("timestamp")
    nonce = data.get("nonce")
    signature = data.get("signature")
    if not action or not isinstance(action, str):
        raise IdentityError("missing action")
    if expected_action and action != expected_action:
        raise IdentityError(f"wrong action for this endpoint (got {action!r})")
    if not fm_id or not isinstance(fm_id, str):
        raise IdentityError("missing fm_id")

    ident = db.get_identity(fm_id)
    if not ident:
        raise IdentityError("unknown fm_id")

    try:
        ts = int(str(timestamp))
    except (TypeError, ValueError):
        raise IdentityError("bad timestamp")
    if abs(int(time.time() * 1000) - ts) > TIMESTAMP_WINDOW_MS:
        raise IdentityError("timestamp outside the 5-minute window")

    if not valid_nonce(nonce):
        raise IdentityError("bad nonce (need 128-bit base64url)")

    try:
        sig_bytes = b64u_decode(signature)
    except IdentityError:
        raise IdentityError("bad signature encoding")
    try:
        pub = Ed25519PublicKey.from_public_bytes(
            b64u_decode(ident["public_key"]))
    except Exception:
        raise IdentityError("registered key is corrupt")
    msg = canonical_message(action, timestamp, nonce, fm_id, data)
    try:
        pub.verify(sig_bytes, msg)
    except InvalidSignature:
        raise IdentityError("signature does not verify")

    # Replay check LAST: only a validly-signed request can burn a nonce.
    if not db.note_nonce(nonce, NONCE_TTL_SEC):
        raise IdentityError("replay: nonce already used")
    return ident
