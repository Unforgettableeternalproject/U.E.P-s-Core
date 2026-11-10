"""平台 shadow 的 host 整合（不需 DB）：開啟時每個簽出的 turn 恰好 observe 一次且在 intake 鎖外；關閉時一次都不呼叫；
UEP_PLATFORM_SHADOW 預設關閉；peek 只能被 platform_context 呼叫。"""
from __future__ import annotations

import ast
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from uep.config import UepSettings
from uep.echo.host import EchoPipelineHost
from uep.turn.models import IntakeResult, SessionContext, UtteranceFact

UEP_ROOT = Path(__file__).resolve().parents[2] / "uep"


class FakeShadow:
    def __init__(self, host: EchoPipelineHost) -> None:
        self.host = host
        self.calls: list[tuple[str, str | None]] = []
        self.lock_held: list[bool] = []

    def observe(self, turn_ref, session_platform=None):
        self.calls.append((turn_ref, session_platform))
        self.lock_held.append(self.host._intake_lock.locked())


class StubOrchestrator:
    def __init__(self, results: IntakeResult) -> None:
        self.results = results

    def on_utterance(self, session, fact):
        return self.results

    def tick(self, session):
        return list(self.results.all_results())


def _ticket(turn_id: str):
    return SimpleNamespace(turn_id=turn_id)


def _fact(seq: int = 1) -> UtteranceFact:
    return UtteranceFact(speaker_ref="discord:1", text="hi", seq=seq)


def _host(results: IntakeResult, *, shadow: bool) -> tuple[EchoPipelineHost, FakeShadow | None]:
    host = EchoPipelineHost.__new__(EchoPipelineHost)  # 只組 submit 路徑需要的欄位，不碰 DB／ES
    host._intake_lock = threading.Lock()
    host._closed, host._started, host._switching = False, True, False
    host._session = SessionContext(
        conversation_id="c", segment_id="s", audience_scope="private", session_ref="r", platform="web"
    )
    host._es = SimpleNamespace(in_loop_thread=lambda: False, call_soon=lambda *a, **k: None)
    host.orchestrator = StubOrchestrator(results)
    host._yield_speaker = lambda ref: None
    host._platform_shadow = None
    fake = None
    if shadow:
        fake = FakeShadow(host)
        host.bind_platform_shadow(fake)
    return host, fake


def _results() -> IntakeResult:
    fact = _fact()
    second = IntakeResult(_ticket("t2"), "ok", fact=fact)
    return IntakeResult(_ticket("t1"), "ok", fact=fact, extra=(second,))


def test_enabled_observes_each_dispatched_turn_once_outside_lock():
    host, shadow = _host(_results(), shadow=True)
    host.submit_utterance(_fact())
    assert shadow.calls == [("t1", "web"), ("t2", "web")]
    assert shadow.lock_held == [False, False]


def test_enabled_tick_observes_once_per_turn_outside_lock():
    host, shadow = _host(_results(), shadow=True)
    host.submit_tick()
    assert [c[0] for c in shadow.calls] == ["t1", "t2"] and shadow.lock_held == [False, False]


def test_enabled_skips_results_without_ticket():
    host, shadow = _host(IntakeResult(None, "buffered"), shadow=True)
    host.submit_utterance(_fact())
    assert shadow.calls == []


def test_disabled_never_observes():
    host, shadow = _host(_results(), shadow=False)
    assert shadow is None and host._platform_shadow is None
    host.submit_utterance(_fact())
    host.submit_tick()  # 關閉時 _observe_platform 直接返回，不會有任何呼叫


def test_setting_defaults_off(monkeypatch):
    monkeypatch.delenv("UEP_PLATFORM_SHADOW", raising=False)
    assert UepSettings(_env_file=None).platform_shadow is False
    monkeypatch.setenv("UEP_PLATFORM_SHADOW", "true")
    assert UepSettings(_env_file=None).platform_shadow is True


def test_peek_is_only_called_from_platform_context():
    hits = []
    for path in UEP_ROOT.rglob("*.py"):
        rel = path.relative_to(UEP_ROOT).as_posix()
        if rel == "presence/manager.py":  # 定義處
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and node.attr == "peek":
                hits.append(rel)
    assert set(hits) == {"echo/platform_context.py"}, hits


@pytest.mark.parametrize("phrase", ["只供平台快照與 shadow 使用", "不處理過期", "不可作為 Lease 或授權判斷的依據"])
def test_peek_docstring_states_restrictions(phrase):
    from uep.presence.manager import PresenceManager

    assert phrase in (PresenceManager.peek.__doc__ or "")
