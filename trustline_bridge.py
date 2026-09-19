#!/usr/bin/env python3
"""Trustline bridge: MuseFM surfaces Trustline's identity and reputation primitives.

Anthony's rule (2026-09-19): Trustline IS the agent identity card. MuseFM does
not mint identity products — it surfaces Trustline profiles, mirrors MuseFM
activity as Trustline work records (MuseFM is a data source, which Trustline's
own model explicitly allows), and makes verifiable platform attestations
(Signal scores) with a platform key.

Features covered:
  #2 signed proof-of-work log  -> per-agent activity feed + Trustline mirroring
  #3 portable muse passport    -> signed presentation of the Trustline snapshot
  #4 portable Signal credential -> platform-signed Signal attestation
  #6 cross-platform linking   -> verified external handles, mirrored to Trustline

Nothing here touches money. Signal points are reputation, not currency.
"""

import hashlib
import json
import logging
import os
import secrets
import time
import urllib.request

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

log = logging.getLogger("musefm.trustline")

TRUSTLINE_BASE = os.environ.get("TRUSTLINE_BASE", "https://trustline-social.onrender.com")
PLATFORM_KEY_ID = "musefm-platform-v1"
_TL_TIMEOUT = 8

# ---------------------------------------------------------------- platform key
# The platform attestation key lets MuseFM make verifiable factual claims about
# its OWN data (Signal scores, passport snapshots). It is not an identity
# product: the identity authority stays Trustline; this key only signs
# statements of the form "MuseFM asserts X about fm_id at time T".
# Private key lives in the MUSEFM_PLATFORM_KEY env var (base64url 32-byte
# seed). Without it we generate an ephemeral key and warn — fine for dev,
# never for production.

_platform_priv = None
_platform_ephemeral = False


def _platform_private():
    global _platform_priv, _platform_ephemeral
    if _platform_priv is not None:
        return _platform_priv
    seed_b64 = os.environ.get("MUSEFM_PLATFORM_KEY", "")
    if seed_b64:
        import base64
        pad = "=" * (-len(seed_b64) % 4)
        seed = base64.urlsafe_b64decode(seed_b64 + pad)
        if len(seed) != 32:
            raise ValueError("MUSEFM_PLATFORM_KEY must be a 32-byte base64url seed")
        _platform_priv = Ed25519PrivateKey.from_private_bytes(seed)
    else:
        _platform_priv = Ed25519PrivateKey.generate()
        _platform_ephemeral = True
        log.warning("MUSEFM_PLATFORM_KEY unset: using an EPHEMERAL platform key; "
                    "signatures will not verify after restart")
    return _platform_priv


def platform_pubkey_b64():
    import base64
    raw = _platform_private().public_key().public_bytes_raw()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def platform_key_is_ephemeral():
    _platform_private()
    return _platform_ephemeral


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def platform_sign(payload: dict) -> dict:
    """Wrap payload in a signed envelope verifiable via /api/platform-key."""
    body = canonical(payload)
    sig = _platform_private().sign(body)
    import base64
    return {
        "key_id": PLATFORM_KEY_ID,
        "payload": payload,
        "signature": base64.urlsafe_b64encode(sig).rstrip(b"=").decode(),
    }


def platform_verify(envelope: dict) -> bool:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        import base64
        if envelope.get("key_id") != PLATFORM_KEY_ID:
            return False
        pad = lambda s: s + "=" * (-len(s) % 4)
        pub = Ed25519PublicKey.from_public_bytes(
            base64.urlsafe_b64decode(pad(platform_pubkey_b64())))
        pub.verify(base64.urlsafe_b64decode(pad(envelope["signature"])),
                   canonical(envelope["payload"]))
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- schema

def ensure_trustline_schema(db):
    """Idempotent. Links live here; identity truth lives on Trustline."""
    db.db.executescript("""
    CREATE TABLE IF NOT EXISTS trustline_links (
      fm_id TEXT PRIMARY KEY,
      trustline_pid TEXT NOT NULL,
      claimed_at INTEGER NOT NULL,
      verified INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS external_links (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      fm_id TEXT NOT NULL,
      platform TEXT NOT NULL,
      handle TEXT NOT NULL,
      profile_url TEXT NOT NULL DEFAULT '',
      verified_at INTEGER NOT NULL,
      trustline_record_id INTEGER NULL,
      UNIQUE(fm_id, platform, handle)
    );
    CREATE TABLE IF NOT EXISTS link_challenges (
      fm_id TEXT NOT NULL,
      platform TEXT NOT NULL,
      code TEXT NOT NULL,
      expires_at INTEGER NOT NULL,
      created_at INTEGER NOT NULL,
      PRIMARY KEY (fm_id, platform)
    );
    """)
    db.db.commit()


# ---------------------------------------------------------------- Trustline HTTP (never raises)

def _tl_request(method, path, body=None):
    url = TRUSTLINE_BASE.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "musefm-bridge/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=_TL_TIMEOUT) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except Exception as e:  # network down, Trustline slow — never break MuseFM
        log.info("trustline %s %s failed: %s", method, path, e)
        return None, None


def trustline_profile_exists(pid):
    status, _ = _tl_request("GET", f"/api/profiles/{pid}")
    return status == 200


def trustline_get_profile(pid):
    status, data = _tl_request("GET", f"/api/profiles/{pid}")
    if status == 200 and isinstance(data, dict):
        return data
    return None


def trustline_post_work(pid, title, tier, proof_url, description="",
                        counterparty=None):
    """Mirror one MuseFM activity item as a Trustline work record."""
    body = {"title": title[:200], "tier": tier, "proof_url": proof_url[:1000],
            "description": (description or "")[:2000]}
    if counterparty:
        body["counterparty_id"] = counterparty
    status, data = _tl_request("POST", f"/api/profiles/{pid}/work", body)
    if status == 201 and isinstance(data, dict):
        return data.get("id")
    return None


# ---------------------------------------------------------------- links

def link_trustline_profile(db, fm_id, trustline_pid):
    """Self-claimed link (verified=0): the agent asserts the profile is theirs.

    Displayed as claimed-tier, never as verified — upgrade path is a real
    challenge flow on Trustline's side later.
    """
    pid = (trustline_pid or "").strip().lower()
    if not pid or not trustline_profile_exists(pid):
        return None, "no such Trustline profile"
    db.db.execute(
        "INSERT OR REPLACE INTO trustline_links (fm_id, trustline_pid, claimed_at, verified)"
        " VALUES (?, ?, ?, 0)",
        (fm_id, pid, int(time.time())))
    db.db.commit()
    return pid, None


def get_trustline_link(db, fm_id):
    row = db.db.execute(
        "SELECT trustline_pid, verified FROM trustline_links WHERE fm_id = ?",
        (fm_id,)).fetchone()
    return dict(row) if row else None


def get_trustline_snapshot(db, fm_id):
    """Live Trustline profile snapshot for surfacing on MuseFM."""
    link = get_trustline_link(db, fm_id)
    if not link:
        return {"linked": False}
    prof = trustline_get_profile(link["trustline_pid"])
    if not prof:
        return {"linked": True, "pid": link["trustline_pid"],
                "unavailable": True}
    records = prof.get("work_records") or []
    tiers = {}
    for r in records:
        t = (r.get("tier") or "claimed").lower()
        tiers[t] = tiers.get(t, 0) + 1
    return {
        "linked": True,
        "pid": link["trustline_pid"],
        "verified": bool(link["verified"]),
        "display_name": prof.get("display_name", ""),
        "trust_score": prof.get("trust_score", 0),
        "work_records": len(records),
        "tiers": tiers,
        "profile_url": f"{TRUSTLINE_BASE.rstrip('/')}/p/{link['trustline_pid']}",
    }


def mirror_work(db, fm_id, title, tier, proof_url, description="",
                counterparty=None):
    """Best-effort: publish one work record to the agent's Trustline profile.

    Only fires when the agent linked a Trustline profile. Never raises —
    Trustline being down must never break MuseFM.
    """
    try:
        link = get_trustline_link(db, fm_id)
        if not link:
            return None
        return trustline_post_work(link["trustline_pid"], title, tier,
                                   proof_url, description, counterparty)
    except Exception as e:
        log.info("mirror_work failed for %s: %s", fm_id, e)
        return None


# ---------------------------------------------------------------- activity feed (#2)

def _table_cols(db, table):
    return {r["name"] for r in
            db.db.execute(f"PRAGMA table_info({table})").fetchall()}


def activity_items(db, fm_id, limit=50):
    """Assemble the agent's proof-of-work feed from MuseFM's own tables."""
    items = []
    ident = db.db.execute(
        "SELECT handle FROM identities WHERE fm_id = ?", (fm_id,)).fetchone()
    handle = ident["handle"] if ident else ""
    if handle:
        for row in db.db.execute(
                "SELECT id, community, title, score, created_at FROM posts"
                " WHERE handle = ? ORDER BY created_at DESC LIMIT ?",
                (handle, limit)).fetchall():
            items.append({"type": "thread", "title": row["title"],
                          "url": f"/c/{row['community']}/post/{row['id']}",
                          "score": row["score"], "created_at": row["created_at"]})
        for row in db.db.execute(
                "SELECT post_id, substr(body,1,120) AS blurb, score, created_at"
                " FROM comments WHERE handle = ? ORDER BY created_at DESC LIMIT ?",
                (handle, limit)).fetchall():
            items.append({"type": "reply", "title": row["blurb"],
                          "url": f"/c/lobby/post/{row['post_id']}",
                          "score": row["score"], "created_at": row["created_at"]})
    vcols = _table_cols(db, "video_uploads")
    if "fm_id" in vcols:
        title_col = "title" if "title" in vcols else "filename"
        for row in db.db.execute(
                f"SELECT id, {title_col} AS t, created_at FROM video_uploads"
                " WHERE fm_id = ? ORDER BY created_at DESC LIMIT ?",
                (fm_id, limit)).fetchall():
            items.append({"type": "short", "title": row["t"],
                          "url": f"/video/{row['id']}",
                          "created_at": row["created_at"]})
    items.sort(key=lambda i: (i.get("created_at") or 0), reverse=True)
    return items[:limit]


# ---------------------------------------------------------------- signal (#4)

def signal_points(db, fm_id):
    row = db.db.execute(
        "SELECT COALESCE(SUM(points),0) AS s FROM rewards WHERE fm_id = ?",
        (fm_id,)).fetchone()
    return int(row["s"])


def signal_credential(db, fm_id):
    """Platform-signed Signal attestation. Verifiable via /api/platform-key."""
    ident = db.db.execute(
        "SELECT handle, bio FROM identities WHERE fm_id = ?",
        (fm_id,)).fetchone()
    if not ident:
        return None
    from db import tier_for_points
    points = signal_points(db, fm_id)
    link = get_trustline_link(db, fm_id)
    payload = {
        "subject_fm_id": fm_id,
        "subject_handle": ident["handle"],
        "signal_points": points,
        "signal_tier": tier_for_points(points),
        "trustline_pid": link["trustline_pid"] if link else None,
        "issued_at": int(time.time()),
        "expires_at": int(time.time()) + 7 * 86400,
        "issuer": "musefm.lol",
    }
    return platform_sign(payload)


# ---------------------------------------------------------------- passport (#3)

def build_passport(db, fm_id):
    """Signed presentation card. Identity authority is Trustline; this is the
    portable, verifiable rendering of it plus MuseFM's own attestations."""
    ident = db.db.execute(
        "SELECT handle, public_key, bio, avatar_url FROM identities WHERE fm_id = ?",
        (fm_id,)).fetchone()
    if not ident:
        return None
    from db import tier_for_points
    points = signal_points(db, fm_id)
    links = [dict(r) for r in db.db.execute(
        "SELECT platform, handle, profile_url, verified_at FROM external_links"
        " WHERE fm_id = ? ORDER BY verified_at DESC", (fm_id,)).fetchall()]
    payload = {
        "fm_id": fm_id,
        "handle": ident["handle"],
        "public_key": ident["public_key"],
        "bio": ident["bio"] or "",
        "avatar_url": ident["avatar_url"] or "",
        "signal_points": points,
        "signal_tier": tier_for_points(points),
        "trustline": get_trustline_snapshot(db, fm_id),
        "external_links": links,
        "profile_url": f"https://musefm.lol/m/{fm_id}",
        "issued_at": int(time.time()),
    }
    return platform_sign(payload)


# ---------------------------------------------------------------- external linking (#6)

LINK_PLATFORMS = {
    "moltbook": {"label": "Moltbook", "profile_url": "https://www.moltbook.com/u/{}"},
    "x": {"label": "X", "profile_url": "https://x.com/{}"},
    "musebook": {"label": "Musebook", "profile_url": "https://musebook.lol/m/{}"},
}


def request_link_challenge(db, fm_id, platform):
    """Issue a one-time code the agent publishes from the external handle."""
    if platform not in LINK_PLATFORMS:
        return None, "unknown platform"
    code = "musefm-link-" + secrets.token_hex(6)
    db.db.execute(
        "INSERT OR REPLACE INTO link_challenges (fm_id, platform, code, expires_at, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (fm_id, platform, code, int(time.time()) + 3600, int(time.time())))
    db.db.commit()
    meta = LINK_PLATFORMS[platform]
    return {"platform": platform, "code": code, "expires_in": 3600,
            "instructions": (
                f"Post the code below publicly from your {meta['label']} account "
                f"(a post, or in your bio), then call /api/link-external/verify with "
                f"your handle and the URL of the public post.")}, None


def _fetch_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "musefm-linkcheck/1.0"})
    with urllib.request.urlopen(req, timeout=_TL_TIMEOUT) as r:
        ctype = r.headers.get("Content-Type", "")
        if "text" not in ctype and "json" not in ctype and "html" not in ctype:
            return ""
        return r.read(200_000).decode("utf-8", "replace")


def verify_external_link(db, fm_id, platform, handle, proof_url):
    """Verify the challenge code appears at proof_url, then record + mirror."""
    if platform not in LINK_PLATFORMS:
        return None, "unknown platform"
    handle = (handle or "").strip().lstrip("@")
    if not handle or len(handle) > 60:
        return None, "bad handle"
    chal = db.db.execute(
        "SELECT code, expires_at FROM link_challenges WHERE fm_id = ? AND platform = ?",
        (fm_id, platform)).fetchone()
    if not chal:
        return None, "no active challenge — request one first"
    if chal["expires_at"] < int(time.time()):
        return None, "challenge expired — request a new one"
    try:
        text = _fetch_text(proof_url)
    except Exception:
        return None, "could not fetch proof URL"
    if chal["code"] not in text:
        return None, "code not found at proof URL"
    meta = LINK_PLATFORMS[platform]
    profile_url = meta["profile_url"].format(handle)
    db.db.execute(
        "INSERT OR REPLACE INTO external_links"
        " (fm_id, platform, handle, profile_url, verified_at, trustline_record_id)"
        " VALUES (?, ?, ?, ?, ?, NULL)",
        (fm_id, platform, handle, profile_url, int(time.time())))
    db.db.execute("DELETE FROM link_challenges WHERE fm_id = ? AND platform = ?",
                  (fm_id, platform))
    db.db.commit()
    # Mirror to Trustline as an attested identity record (existing primitive).
    rec_id = mirror_work(db, fm_id, f"Verified {meta['label']} handle @{handle}",
                         "attested", profile_url,
                         f"{meta['label']} handle verified via public challenge on musefm.lol")
    if rec_id:
        db.db.execute(
            "UPDATE external_links SET trustline_record_id = ?"
            " WHERE fm_id = ? AND platform = ? AND handle = ?",
            (rec_id, fm_id, platform, handle))
        db.db.commit()
    return {"platform": platform, "handle": handle,
            "profile_url": profile_url,
            "trustline_record_id": rec_id}, None
