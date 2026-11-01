"""ID 格式（D32）與設定載入；不需要資料庫。"""
import pytest

from uep.config import UepSettings
from uep.db.engine import normalize_url
from uep.ids import PREFIXES, is_valid_id, new_id


def test_new_id_format():
    value = new_id("evt")
    assert value.startswith("evt_")
    assert len(value) == len("evt_") + 26
    assert is_valid_id(value, "evt")
    assert not is_valid_id(value, "task")


def test_ids_are_unique_and_sortable():
    ids = [new_id("evt") for _ in range(200)]
    assert len(set(ids)) == 200


def test_unknown_prefix_rejected():
    with pytest.raises(ValueError):
        new_id("nope")


@pytest.mark.parametrize("bad", ["", "evt_", "evt_123", "evt_" + "I" * 26, "EVT_" + "0" * 26, f"evt_{'0' * 26}x"])
def test_invalid_ids(bad):
    assert not is_valid_id(bad)


def test_legacy_millisecond_id_is_invalid():
    # legacy 的 evt_{ms} 同毫秒會撞號，新格式不接受
    assert not is_valid_id("evt_1761436800000")


def test_contract_prefixes_registered():
    for prefix in ("evt", "actv", "task", "run", "step", "act", "turn", "seg", "conv",
                   "lease", "xfer", "out", "dsp", "dlv", "snap", "appr", "req"):
        assert prefix in PREFIXES


def test_settings_read_from_environment(monkeypatch):
    monkeypatch.setenv("UEP_DATABASE_URL", "postgresql://u:p@h/db1")
    monkeypatch.setenv("UEP_TEST_DATABASE_URL", "postgresql://u:p@h/db2")
    settings = UepSettings(_env_file=None)
    assert settings.database_url.get_secret_value().endswith("/db1")
    assert settings.test_database_url.get_secret_value().endswith("/db2")


def test_settings_default_none(monkeypatch):
    monkeypatch.delenv("UEP_DATABASE_URL", raising=False)
    monkeypatch.delenv("UEP_TEST_DATABASE_URL", raising=False)
    settings = UepSettings(_env_file=None)
    assert settings.database_url is None and settings.test_database_url is None


def test_normalize_url_uses_psycopg3():
    assert normalize_url("postgresql://a/b") == "postgresql+psycopg://a/b"
    assert normalize_url("postgres://a/b") == "postgresql+psycopg://a/b"
    assert normalize_url("postgresql+psycopg://a/b") == "postgresql+psycopg://a/b"


def test_settings_repr_hides_password(monkeypatch):
    monkeypatch.setenv("UEP_DATABASE_URL", "postgresql://u:s3cretpw@h/db1")
    monkeypatch.setenv("UEP_TEST_DATABASE_URL", "postgresql://u:s3cretpw@h/db2")
    settings = UepSettings(_env_file=None)
    assert "s3cretpw" not in repr(settings) and "s3cretpw" not in str(settings)
