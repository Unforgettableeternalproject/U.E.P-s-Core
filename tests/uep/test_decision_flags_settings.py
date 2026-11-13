"""決策層旗標（UEP_LAYA_CAPABILITIES／UEP_DECISION_LOG）改由 UepSettings 讀取：``.env`` 檔設定也要生效。"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from uep import assembly
from uep.config import UepSettings, get_settings
from uep.echo import laya_wiring
from uep.echo.laya_wiring import resolve_capabilities_mode

FLAGS = ("UEP_LAYA_CAPABILITIES", "UEP_DECISION_LOG")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in FLAGS:
        monkeypatch.delenv(name, raising=False)


def _env_file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


# ---- UEP_LAYA_CAPABILITIES ----

def test_laya_capabilities_default_is_static() -> None:
    assert resolve_capabilities_mode(UepSettings(_env_file=None).laya_capabilities) == "static"


def test_laya_capabilities_takes_effect_from_dotenv_file(tmp_path: Path) -> None:
    st = UepSettings(_env_file=_env_file(tmp_path, "UEP_LAYA_CAPABILITIES= Snapshot \n"))
    assert resolve_capabilities_mode(st.laya_capabilities) == "snapshot"


def test_laya_capabilities_env_overrides_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UEP_LAYA_CAPABILITIES", "static")
    st = UepSettings(_env_file=_env_file(tmp_path, "UEP_LAYA_CAPABILITIES=snapshot\n"))
    assert resolve_capabilities_mode(st.laya_capabilities) == "static"


def test_laya_capabilities_unknown_value_falls_back_to_static_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    st = UepSettings(_env_file=_env_file(tmp_path, "UEP_LAYA_CAPABILITIES=bogus\n"))
    with caplog.at_level(logging.WARNING, logger=laya_wiring.logger.name):
        assert resolve_capabilities_mode(st.laya_capabilities) == "static"
    assert sum("不認得" in r.getMessage() for r in caplog.records) == 1


# ---- UEP_DECISION_LOG ----

def test_decision_log_default_enabled() -> None:
    assert UepSettings(_env_file=None).decision_log_enabled is True


@pytest.mark.parametrize("value", ["0", "off", "False", " NO ", "false"])
def test_decision_log_disabled_values_from_dotenv_file(tmp_path: Path, value: str) -> None:
    st = UepSettings(_env_file=_env_file(tmp_path, f'UEP_DECISION_LOG="{value}"\n'))
    assert st.decision_log_enabled is False


@pytest.mark.parametrize("value", ["", "1", "on", "true", "bogus"])
def test_decision_log_other_values_stay_enabled(tmp_path: Path, value: str) -> None:
    st = UepSettings(_env_file=_env_file(tmp_path, f'UEP_DECISION_LOG="{value}"\n'))
    assert st.decision_log_enabled is True


def test_decision_log_env_overrides_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UEP_DECISION_LOG", "off")
    st = UepSettings(_env_file=_env_file(tmp_path, "UEP_DECISION_LOG=1\n"))
    assert st.decision_log_enabled is False


def test_assembly_reads_decision_log_through_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "decision_log", "off")
    assert assembly._decision_log_enabled(None) is False
    assert assembly._decision_log_enabled(True) is True  # 明示旗標仍優先
    monkeypatch.setattr(get_settings(), "decision_log", "")
    assert assembly._decision_log_enabled(None) is True
