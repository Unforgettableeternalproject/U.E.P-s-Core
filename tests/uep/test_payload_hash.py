"""payload_hash 改 per-install 密鑰 HMAC（D42、E-16a；T-23）。"""
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from uep.config import UepSettings, get_settings
from uep.events import journal
from uep.events.bus import EventBus
from uep.events.envelope import EventDraft, TrustedContext
from uep.events.models import EventRow, PayloadBlob
from uep.events.payload_hash import (
    HMAC_PREFIX,
    PayloadHasher,
    PayloadHashKeyMissing,
    algorithm_of,
    resolve_hasher,
    sha256_hex,
)
from uep.events.publisher import EventPublisher

KEY_A = SecretStr("key-aaaaaaaaaaaaaaaaaaaa")
KEY_B = SecretStr("key-bbbbbbbbbbbbbbbbbbbb")
EXTERNAL = TrustedContext(source="adapter.discord", actor_id="guest_1", scope_ref="scope_public", external=True)


# ---- 不需要資料庫 ----

def test_same_content_different_keys_hash_differently():
    data = "好".encode()  # 低熵短文
    a, b = PayloadHasher(KEY_A), PayloadHasher(KEY_B)
    assert a.hash(data) != b.hash(data)
    assert a.hash(data) == PayloadHasher(KEY_A).hash(data)  # 同密鑰決定性
    assert a.hash(data).startswith(HMAC_PREFIX) and a.hash(data) != sha256_hex(data)


def test_verify_success_and_failure():
    hasher = PayloadHasher(KEY_A)
    stored = hasher.hash(b"hello")
    assert hasher.verify(b"hello", stored)
    assert not hasher.verify(b"hellp", stored)
    assert not PayloadHasher(KEY_B).verify(b"hello", stored)  # 密鑰不同即驗證失敗


def test_legacy_sha256_is_verified_by_its_algorithm():
    legacy = sha256_hex(b"old")
    assert algorithm_of(legacy) == "sha256" and algorithm_of(PayloadHasher(KEY_A).hash(b"x")) == "hmac-sha256"
    hasher = PayloadHasher(KEY_A)
    assert hasher.verify(b"old", legacy) and not hasher.verify(b"new", legacy)


def test_short_key_rejected():
    with pytest.raises(PayloadHashKeyMissing):
        PayloadHasher(SecretStr("short"))


def test_resolve_without_key_fails_closed(monkeypatch):
    monkeypatch.setattr(get_settings(), "payload_hash_key", None)
    with pytest.raises(PayloadHashKeyMissing):
        resolve_hasher()


def test_resolve_reads_settings_key():
    assert resolve_hasher().hash(b"x").startswith(HMAC_PREFIX)


def test_settings_reads_env(monkeypatch):
    monkeypatch.setenv("UEP_PAYLOAD_HASH_KEY", "env-key-0123456789abcdef")
    settings = UepSettings()
    assert settings.payload_hash_key.get_secret_value() == "env-key-0123456789abcdef"
    assert "env-key" not in repr(settings)  # SecretStr 不外洩


# ---- 需要資料庫 ----

@pytest.fixture
def publisher(session_factory):
    return EventPublisher(session_factory, EventBus())


def _draft(blob: bytes | None, key: str = "k1"):
    return EventDraft(event_type="input.text_final", idempotency_key=key, payload={"len": 1}, payload_blob=blob)


def _count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def test_blob_write_without_key_is_refused_and_leaves_nothing(session_factory, monkeypatch):
    monkeypatch.setattr(get_settings(), "payload_hash_key", None)
    publisher = EventPublisher(session_factory, EventBus())
    with pytest.raises(PayloadHashKeyMissing):
        publisher.publish(_draft("私人".encode()), EXTERNAL)
    with session_factory() as s:
        assert _count(s, EventRow) == 0 and _count(s, PayloadBlob) == 0
    # 沒有私人 blob 的事件不受影響
    assert publisher.publish(_draft(None, "k2"), EXTERNAL).event.payload_hash is None


def test_published_hash_is_hmac_in_both_blob_and_event(session_factory, publisher):
    secret = "好".encode()
    event = publisher.publish(_draft(secret), EXTERNAL).event
    expected = PayloadHasher(get_settings().payload_hash_key).hash(secret)
    assert event.payload_hash == expected
    with session_factory() as s:
        assert s.get(PayloadBlob, event.payload_ref).content_hash == expected
    assert journal.verify_payload(secret, event.payload_hash)
    assert not journal.verify_payload("壞".encode(), event.payload_hash)


def test_tombstone_keeps_only_hmac_and_still_verifies(session_factory, publisher):
    secret = "只剩雜湊".encode()
    event = publisher.publish(_draft(secret), EXTERNAL).event
    with session_factory() as s, s.begin():
        journal.delete_blob(s, event.payload_ref, "使用者要求刪除")
    with session_factory() as s:
        row = s.get(EventRow, event.event_id)
        assert row.tombstone_at is not None and row.payload_hash.startswith(HMAC_PREFIX)
        assert s.get(PayloadBlob, event.payload_ref).content_hash.startswith(HMAC_PREFIX)
        assert journal.verify_payload(secret, row.payload_hash)  # 持有原文者可驗證
        # 字典攻擊者沒有密鑰，純 sha256 對不上
        assert sha256_hex(secret) not in row.payload_hash


def test_resend_with_same_blob_is_not_flagged_as_mismatch(session_factory, publisher, caplog):
    publisher.publish(_draft("相同".encode()), EXTERNAL)
    with caplog.at_level("WARNING", logger="uep.events.journal"):
        again = publisher.publish(_draft("相同".encode()), EXTERNAL)
    assert again.duplicate and "無法以目前密鑰驗證" not in caplog.text
    with caplog.at_level("WARNING", logger="uep.events.journal"):
        publisher.publish(_draft("不同".encode()), EXTERNAL)
    assert "無法以目前密鑰驗證（可能已輪替" in caplog.text


def test_resend_without_key_returns_original_and_logs_unverifiable(session_factory, publisher, monkeypatch, caplog):
    first = publisher.publish(_draft("相同".encode()), EXTERNAL)
    monkeypatch.setattr(get_settings(), "payload_hash_key", None)
    with caplog.at_level("WARNING", logger="uep.events.journal"):
        again = publisher.publish(_draft("相同".encode()), EXTERNAL)  # 不得拋例外
    assert again.duplicate and again.event.event_id == first.event.event_id
    assert "無法驗證重送內容" in caplog.text


def test_legacy_sha256_hash_still_verifiable_with_key():
    secret = "舊資料".encode()
    legacy = sha256_hex(secret)
    assert journal.verify_payload(secret, legacy)
    assert not journal.verify_payload("別的".encode(), legacy)


def test_explicit_hasher_overrides_settings(session_factory):
    hasher = PayloadHasher(KEY_B)
    publisher = EventPublisher(session_factory, EventBus(), hasher=hasher)
    event = publisher.publish(_draft(b"abc"), EXTERNAL).event
    assert event.payload_hash == hasher.hash(b"abc")
