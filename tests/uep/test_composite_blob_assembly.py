"""D110：assembly 注入 composite blob store 的條件（離線；不連 DB）。"""
from __future__ import annotations

import logging

import pytest
from pydantic import SecretStr

from uep import assembly
from uep.events.composite_blobs import DbCompositeBlobStore
from uep.events.payload_hash import MIN_KEY_BYTES


def _settings(monkeypatch, *, key, max_bytes=65536, ttl=86400.0):
    s = type("S", (), {})()
    s.payload_hash_key, s.composite_blob_max_bytes, s.composite_blob_ttl_s = key, max_bytes, ttl
    monkeypatch.setattr(assembly, "get_settings", lambda: s)


def test_valid_key_injects_store_and_limit(monkeypatch, caplog):
    _settings(monkeypatch, key=SecretStr("k" * MIN_KEY_BYTES), max_bytes=1234)
    with caplog.at_level(logging.WARNING):
        kwargs = assembly._composite_blob_kwargs(object())
    assert isinstance(kwargs["composite_blobs"], DbCompositeBlobStore) and kwargs["composite_blob_max_bytes"] == 1234
    assert not caplog.records


@pytest.mark.parametrize("key", [SecretStr("k" * (MIN_KEY_BYTES - 1)), SecretStr(" " * 40), SecretStr("")], ids=["short", "blank", "empty"])
def test_short_or_blank_key_is_not_injected_and_warns_once(monkeypatch, caplog, key):
    _settings(monkeypatch, key=key)
    with caplog.at_level(logging.WARNING):
        assert assembly._composite_blob_kwargs(object()) == {}
    assert len([r for r in caplog.records if "UEP_PAYLOAD_HASH_KEY" in r.getMessage()]) == 1


def test_missing_key_or_disabled_limit_is_silent(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING):
        _settings(monkeypatch, key=None)
        assert assembly._composite_blob_kwargs(object()) == {}
        _settings(monkeypatch, key=SecretStr("k" * 40), max_bytes=0)
        assert assembly._composite_blob_kwargs(object()) == {}
    assert not caplog.records
