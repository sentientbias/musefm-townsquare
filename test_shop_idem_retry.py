#!/usr/bin/env python3
"""
Failing test proving: shop idempotency-key retry breaks when the spendable
balance dropped below the item price after the first purchase.

Repro (live, 2026-09-18): POST /api/shop/buy {"item":"rename_token",
"idempotency_key":"key-DDD"} twice with 35 spendable Signal. First -> 200
charged=20 (spendable 15). Second, same key -> 402 "insufficient spendable
Signal" instead of the idempotent 200 already_owned/charged=0.

Root cause: shop.buy() checks `spendable < price` BEFORE the
UNIQUE(fm_id, ref_id) insert that implements idempotency. One-time items
(accessory/bypass) check owns() first and retry fine; consumables with an
idempotency key do not.

Run:  .venv/bin/python test_shop_idem_retry.py
Throwaway SQLite db. Nothing touches townsquare.db.
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import shop
from db import Database

TEST_DB = "/tmp/test-townsquare-shop-idem-retry.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    db = Database(TEST_DB)
    priv = Ed25519PrivateKey.generate()
    pub = b64u(priv.public_key().public_bytes_raw())
    ident = db.register_identity("IdemRetry", pub)
    fm = ident["fm_id"]

    # exactly enough for one rename token (price 20)
    db.award(fm, "IdemRetry", 20, "thread", "post", "ir-seed")
    assert shop.spendable(db, fm) == 20

    first = shop.buy(db, fm, "rename_token", idempotency_key="kretry")
    check("first buy charges 20",
          first["charged"] == 20 and not first["already_owned"], first)
    check("spendable now 0", shop.spendable(db, fm) == 0)

    # retry with the SAME idempotency key: must be the idempotent no-op,
    # not a 402-style insufficient-funds error
    try:
        retry = shop.buy(db, fm, "rename_token", idempotency_key="kretry")
    except ValueError as e:
        check("retry with same key is idempotent no-op (not insufficient)",
              False, f"raised ValueError: {e}")
    else:
        check("retry with same key is idempotent no-op (not insufficient)",
              retry["already_owned"] and retry["charged"] == 0, retry)

    n = db._one("SELECT COUNT(*) c FROM shop_purchases"
                " WHERE fm_id=? AND ref_id=?", (fm, "kretry"))["c"]
    check("still exactly one purchase row for the key", n == 1, n)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
