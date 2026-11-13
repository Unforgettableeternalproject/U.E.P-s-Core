"""Console 簽發 source_endpoint_id（UEP_CONSOLE_INGRESS_ENDPOINT）：離線測試，不連資料庫、不啟動 Host。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from uep.config import UepSettings
from uep.console import real_rig
from uep.console.commands import Console
from uep.console.rig import CONSOLE_ENDPOINT_ID, CONSOLE_SPEAKER, ConsoleRuntime
from uep.contracts.presence import PresenceLocation


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UEP_CONSOLE_INGRESS_ENDPOINT", raising=False)


def _console(endpoint_id: str | None) -> tuple[Console, list]:
    """假 runtime／host：只記錄 submit_utterance 收到的 fact。"""
    facts: list = []
    host = SimpleNamespace(location="console", submit_utterance=lambda fact: facts.append(fact) or SimpleNamespace())
    rt = SimpleNamespace(host=host, seq=0, ingress_endpoint_id=endpoint_id)
    console = Console(rt, out=lambda _t: None)  # type: ignore[arg-type]
    console._report_turn = lambda _result: None  # type: ignore[method-assign]
    return console, facts


def test_flag_off_has_no_endpoint() -> None:
    console, facts = _console(None)
    console._submit("你好")
    assert facts[0].source_endpoint_id is None and facts[0].speaker_ref == CONSOLE_SPEAKER


def test_flag_off_drops_external_endpoint() -> None:
    console, facts = _console(None)
    console._submit("你好", source_endpoint_id="x")
    assert facts[0].source_endpoint_id is None


def test_flag_on_issues_console_endpoint() -> None:
    console, facts = _console(CONSOLE_ENDPOINT_ID)
    console._submit("你好")
    assert facts[0].source_endpoint_id == "test_console"


def test_flag_on_non_local_source_location_gets_no_endpoint() -> None:
    console, facts = _console(CONSOLE_ENDPOINT_ID)
    console._submit("你好", source_location=PresenceLocation.DISCORD)
    assert facts[0].source_endpoint_id is None


def test_flag_on_explicit_console_source_location_still_issued() -> None:
    console, facts = _console(CONSOLE_ENDPOINT_ID)
    console._submit("你好", source_location=PresenceLocation.CONSOLE)
    assert facts[0].source_endpoint_id == "test_console"


def test_flag_on_ignores_external_endpoint_value() -> None:
    console, facts = _console(CONSOLE_ENDPOINT_ID)
    console._submit("你好", source_endpoint_id="evil")
    assert facts[0].source_endpoint_id == "test_console"
    console._submit("你好", source_endpoint_id="evil", source_location=PresenceLocation.WEBSITE)
    assert facts[1].source_endpoint_id is None


def test_runtime_field_defaults_to_none() -> None:
    assert ConsoleRuntime.__dataclass_fields__["ingress_endpoint_id"].default is None


def test_stt_stage_endpoint_only_when_flag_on() -> None:
    off = real_rig.build_stt_stage(UepSettings(_env_file=None), {})
    assert getattr(off, "source_endpoint_id", None) is None
    on = real_rig.build_stt_stage(UepSettings(_env_file=None, UEP_CONSOLE_INGRESS_ENDPOINT=True), {})
    assert on.source_endpoint_id == "test_console"


def test_flag_default_off_and_takes_effect_from_dotenv_file(tmp_path: Path) -> None:
    assert UepSettings(_env_file=None).console_ingress_endpoint is False
    env_file = tmp_path / ".env"
    env_file.write_text("UEP_CONSOLE_INGRESS_ENDPOINT=true\n", encoding="utf-8")
    assert UepSettings(_env_file=env_file).console_ingress_endpoint is True


def test_flag_takes_effect_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UEP_CONSOLE_INGRESS_ENDPOINT", "1")
    assert UepSettings(_env_file=None).console_ingress_endpoint is True


def test_flag_on_desktop_source_location_is_not_local_console() -> None:
    console, facts = _console(CONSOLE_ENDPOINT_ID)
    console._submit("你好", source_location=PresenceLocation.DESKTOP)
    assert facts[0].source_endpoint_id is None


def test_default_source_location_is_console() -> None:
    console, facts = _console(None)
    console._submit("你好")
    assert facts[0].source_location == PresenceLocation.CONSOLE


def test_stt_stage_declares_owner_speaker_with_endpoint() -> None:
    on = real_rig.build_stt_stage(UepSettings(_env_file=None, UEP_CONSOLE_INGRESS_ENDPOINT=True), {})
    assert on.source_endpoint_speaker == CONSOLE_SPEAKER
    off = real_rig.build_stt_stage(UepSettings(_env_file=None), {})
    assert getattr(off, "source_endpoint_speaker", None) is None


def test_explicit_non_owner_speaker_gets_no_endpoint() -> None:
    console, facts = _console(CONSOLE_ENDPOINT_ID)
    console._submit("你好", speaker_ref="discord:123")
    assert facts[0].speaker_ref == "discord:123" and facts[0].source_endpoint_id is None
    console._submit("你好", speaker_ref=CONSOLE_SPEAKER)
    assert facts[1].source_endpoint_id == "test_console"


def _stage_fact(stage, speaker: str):
    from uep.echo.host import EchoPipelineHost

    facts: list = []
    fake = SimpleNamespace(_seq=0, _input_stage=stage, submit_utterance=facts.append)
    utt = SimpleNamespace(speaker_id=speaker, text="嗨", ended_at=1.0, started_at=0.0, language="zh", confidence=0.9,
                          turn_id="t1")
    EchoPipelineHost._intake_stage_utterance(fake, utt)  # type: ignore[arg-type]
    return facts[0]


def test_host_signs_stage_endpoint_only_for_declared_owner_speaker() -> None:
    stage = SimpleNamespace(source_location=PresenceLocation.CONSOLE, source_endpoint_id="test_console",
                            source_endpoint_speaker=CONSOLE_SPEAKER)
    assert _stage_fact(stage, CONSOLE_SPEAKER).source_endpoint_id == "test_console"
    assert _stage_fact(stage, "discord:123").source_endpoint_id is None
    undeclared = SimpleNamespace(source_location=PresenceLocation.CONSOLE, source_endpoint_id="test_console")
    assert _stage_fact(undeclared, CONSOLE_SPEAKER).source_endpoint_id is None  # 未宣告 owner speaker：fail-closed
