from __future__ import annotations

import hashlib

from odyssey_store.auth import hash_api_key


def test_hash_api_key_is_sha256_hex():
    assert hash_api_key("sk-test") == hashlib.sha256(b"sk-test").hexdigest()


def test_hash_api_key_is_deterministic():
    assert hash_api_key("sk-test") == hash_api_key("sk-test")


def test_hash_api_key_differs_for_different_keys():
    assert hash_api_key("sk-a") != hash_api_key("sk-b")
