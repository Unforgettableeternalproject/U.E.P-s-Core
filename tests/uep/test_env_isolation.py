"""tests/uep 對 repo 根 ``.env`` 的隔離：UepSettings 不讀 env_file；DB URL 只走白名單。"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.uep.conftest import PROD_DB_KEY, TEST_DB_KEY, resolve_db_urls
from uep.config import UepSettings


def _dotenv(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_settings_class_does_not_read_env_file() -> None:
    assert UepSettings.model_config.get("env_file") is None


def test_explicit_env_file_still_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """明確傳 _env_file= 的測試（驗 .env 行為）不受全域隔離影響。"""
    monkeypatch.delenv("UEP_SUPERVISOR_INTERVAL_S", raising=False)
    path = _dotenv(tmp_path, "UEP_SUPERVISOR_INTERVAL_S=7\n")
    assert UepSettings(_env_file=path).supervisor_interval_s == 7


def test_env_overrides_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TEST_DB_KEY, "postgresql://u:p@h/from_env_test")
    path = _dotenv(tmp_path, f"{TEST_DB_KEY}=postgresql://u:p@h/from_file_test\n")
    url, source, _ = resolve_db_urls(path)
    assert source == "env" and url.endswith("/from_env_test")


def test_empty_env_means_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TEST_DB_KEY, "")
    path = _dotenv(tmp_path, f"{TEST_DB_KEY}=postgresql://u:p@h/from_file_test\n")
    url, source, _ = resolve_db_urls(path)
    assert url is None and source == "env"


def test_dotenv_used_when_env_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TEST_DB_KEY, raising=False)
    path = _dotenv(tmp_path, f"{TEST_DB_KEY}=postgresql://u:p@h/from_file_test\n")
    url, source, _ = resolve_db_urls(path)
    assert source == ".env" and url.endswith("/from_file_test")


def test_none_when_nothing_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TEST_DB_KEY, raising=False)
    monkeypatch.delenv(PROD_DB_KEY, raising=False)
    url, source, prod = resolve_db_urls(tmp_path / "missing.env")
    assert (url, source, prod) == (None, "none", None)


def test_only_whitelisted_keys_are_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """.env 的其他鍵不會進設定，也不影響白名單解析。"""
    monkeypatch.delenv(TEST_DB_KEY, raising=False)
    monkeypatch.delenv("UEP_MODULE_VRAM_HEADROOM_MB", raising=False)
    monkeypatch.delenv("UEP_SUPERVISOR_INTERVAL_S", raising=False)
    path = _dotenv(tmp_path, "UEP_MODULE_VRAM_HEADROOM_MB=12345\nUEP_SUPERVISOR_INTERVAL_S=99\n")
    url, source, _ = resolve_db_urls(path)
    assert (url, source) == (None, "none")
    # 對照組：env_file 指向這份檔時確實會讀到
    monkeypatch.setitem(UepSettings.model_config, "env_file", path)
    assert UepSettings().module_vram_headroom_mb == 12345
    # 隔離狀態（env_file=None）：不傳 _env_file 也不讀，VRAM 維持未設、其他鍵維持預設
    monkeypatch.setitem(UepSettings.model_config, "env_file", None)
    st = UepSettings()
    assert st.module_vram_headroom_mb is None
    assert st.supervisor_interval_s == 30.0


def test_prod_url_from_dotenv_fallback_for_comparison(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PROD_DB_KEY, raising=False)
    path = _dotenv(tmp_path, f"{PROD_DB_KEY}=postgresql://u:p@h/prod\n")
    assert resolve_db_urls(path)[2] == "postgresql://u:p@h/prod"
    monkeypatch.setenv(PROD_DB_KEY, "postgresql://u:p@h/env_prod")
    assert resolve_db_urls(path)[2] == "postgresql://u:p@h/env_prod"


def test_report_header_survives_malformed_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """畸形 URL（非數字埠號）時 header 不拋例外，也不印出帳密。"""
    from tests.uep.conftest import pytest_report_header

    monkeypatch.setenv(TEST_DB_KEY, "postgresql://u:s3cretpw@h:badport/x_test")
    line = pytest_report_header(None)
    assert "s3cretpw" not in line and "source=env" in line
