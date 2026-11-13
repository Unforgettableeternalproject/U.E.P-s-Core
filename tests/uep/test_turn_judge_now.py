"""turn.reply 注入在場判決忙碌事實（owner 限定；離線，不需資料庫、不呼叫真 LLM）。"""
from __future__ import annotations

import threading
import types
from types import SimpleNamespace

import pytest

from tests.uep.test_action_unavailable import AUTH, CTX, _request_of
from tests.uep.test_presence_sinks import OWNER, Capture
from uep.contracts.presence import PresenceLocation
from uep.echo.host import DEGRADED_TURN_CONTEXT, PRESENCE_BUSY_TURN_CONTEXT
from uep.memory.identity import IdentityDirectory
from uep.stream.adapters.think_llm import LLMThinkStage
from uep.turn.generations import TurnGenerations
from uep.turn.models import CoreTicket, Proposal, ReplyMode, UtteranceFact
from uep.turn.orchestrator import TurnOrchestrator, _is_busy_judgment

BUSY = SimpleNamespace(outcome="REJECTED", reason_code="work_in_progress")


class _Proposer:
    proposer_id = "t.fixed"

    def __init__(self, mode: ReplyMode) -> None:
        self.mode = mode

    def propose(self, candidate):
        return Proposal(reply_mode=self.mode, reason_code="called", proposer_id="t.fixed", proposer_version="1")


class _Judge:
    """假 judge_now：記錄每次呼叫，回傳固定判決。"""

    def __init__(self, result=BUSY, exc: Exception | None = None) -> None:
        self.result, self.exc, self.calls = result, exc, []

    def __call__(self, **kw):
        self.calls.append(kw)
        if self.exc is not None:
            raise self.exc
        return self.result


def _orch(judge, mode: ReplyMode = ReplyMode.RESPOND, **kw) -> TurnOrchestrator:
    return TurnOrchestrator(
        Capture(), TurnGenerations(), auth_provider=lambda: AUTH,  # type: ignore[arg-type]
        proposer=_Proposer(mode), judge_now=judge, run_id="run1",
        identity=IdentityDirectory({OWNER: "actor_owner", "discord:200": "actor_guest"}, {"actor_owner": "owner1"}),
        **kw,
    )


def _say(orch: TurnOrchestrator, speaker: str = OWNER, endpoint: str | None = "ep", seq: int = 1):
    return orch.on_utterance(CTX, UtteranceFact(speaker_ref=speaker, text="你在做什麼", seq=seq, source_endpoint_id=endpoint))


def test_mapping_accepts_only_busy_combinations():
    assert _is_busy_judgment(BUSY)
    assert _is_busy_judgment(SimpleNamespace(outcome="DEFERRED", reason_code="uninterruptible_work"))
    assert _is_busy_judgment(SimpleNamespace(outcome="DEFERRED", reason_code="interaction_active"))
    assert not _is_busy_judgment(None)
    assert not _is_busy_judgment(SimpleNamespace(outcome="ACCEPTED_TRANSFER", reason_code="accepted"))
    assert not _is_busy_judgment(SimpleNamespace(outcome="REJECTED", reason_code="owner_only"))
    assert not _is_busy_judgment(SimpleNamespace(outcome="DEFERRED", reason_code="work_in_progress"))


def test_owner_busy_sets_fact_and_queries_with_platform_target():
    judge = _Judge()
    orch = _orch(judge)
    try:
        ticket = _say(orch).ticket
    finally:
        orch.close()
    assert ticket is not None and ticket.presence_busy is True
    assert judge.calls == [
        {"target": CTX.platform, "is_owner": True, "actor_ref": "actor_owner", "actor_authenticated": True}]
    assert orch.metrics["turn.judge_now.busy"] == 1


def test_declared_source_platform_is_the_target():
    judge = _Judge()
    orch = _orch(judge)
    try:
        orch.on_utterance(CTX, UtteranceFact(
            speaker_ref=OWNER, text="嗨", seq=1, source_endpoint_id="ep", source_location=PresenceLocation.DISCORD))
    finally:
        orch.close()
    assert judge.calls[0]["target"] == "discord" != CTX.platform


@pytest.mark.parametrize("result", [None, SimpleNamespace(outcome="ACCEPTED_TRANSFER", reason_code="accepted")])
def test_none_and_accepted_do_not_inject(result):
    judge = _Judge(result)
    orch = _orch(judge)
    try:
        ticket = _say(orch).ticket
    finally:
        orch.close()
    assert ticket is not None and ticket.presence_busy is False and len(judge.calls) == 1
    assert orch.metrics["turn.judge_now.busy"] == 0
    assert orch.metrics["turn.judge_now.none" if result is None else "turn.judge_now.not_busy"] == 1


@pytest.mark.parametrize("case", ["guest", "unmapped", "unauthenticated", "pass", "passive", "vetoed"])
def test_ineligible_turns_never_call_judge_now(case):
    judge = _Judge()
    kw = {}
    mode = ReplyMode.RESPOND
    speaker, endpoint = OWNER, "ep"
    if case == "guest":
        speaker = "discord:200"  # 映射到 guest actor（非 owner）
    elif case == "unmapped":
        speaker = "discord:999"
    elif case == "unauthenticated":
        endpoint = None
    elif case == "pass":
        mode = ReplyMode.PASS
    elif case == "passive":
        mode = ReplyMode.PASSIVE
    elif case == "vetoed":
        kw["veto"] = lambda ctx: "audience_blocked"
    orch = _orch(judge, mode, **kw)
    try:
        ticket = _say(orch, speaker, endpoint).ticket
    finally:
        orch.close()
    assert judge.calls == [] and (ticket is None or ticket.presence_busy is False)
    assert not any(k.startswith("turn.judge_now") for k in orch.metrics)


def test_timeout_is_treated_as_unknown(monkeypatch):
    import uep.turn.orchestrator as mod

    monkeypatch.setattr(mod, "_JUDGE_NOW_TIMEOUT_S", 0.01)
    release = threading.Event()

    def slow(**kw):
        release.wait(2)
        return BUSY

    orch = _orch(slow)
    try:
        ticket = _say(orch).ticket
        assert ticket is not None and ticket.presence_busy is False
        assert orch.metrics["turn.judge_now.timeout"] == 1
    finally:
        release.set()
        orch.close()


def test_previous_query_still_running_skips_new_one(monkeypatch):
    import uep.turn.orchestrator as mod

    monkeypatch.setattr(mod, "_JUDGE_NOW_TIMEOUT_S", 0.01)
    release, calls = threading.Event(), []

    def slow(**kw):
        calls.append(kw)
        release.wait(2)
        return BUSY

    orch = _orch(slow)
    try:
        _say(orch, seq=1)  # 逾時，worker 仍在跑
        ticket = _say(orch, seq=2).ticket
        assert ticket is not None and ticket.presence_busy is False
        assert orch.metrics["turn.judge_now.skipped_inflight"] == 1 and len(calls) == 1
        release.set()
        orch._judge_executor.shutdown(wait=True)  # 等第一筆結束後釋放旗標
        ticket = _say(orch, seq=3).ticket
        assert ticket is not None and ticket.presence_busy is False  # executor 已關閉：不拋出、當作未知
        assert len(calls) == 1
    finally:
        release.set()
        orch.close()


def test_exception_is_treated_as_unknown_and_turn_continues():
    judge = _Judge(exc=RuntimeError("db down"))
    orch = _orch(judge)
    try:
        ticket = _say(orch).ticket
    finally:
        orch.close()
    assert ticket is not None and ticket.presence_busy is False
    assert orch.metrics["turn.judge_now.none"] == 1


def test_judge_now_none_keeps_behavior_unchanged():
    orch = _orch(None)
    try:
        ticket = _say(orch).ticket
        assert ticket is not None and ticket.presence_busy is False
        assert orch._judge_executor is None
        assert not any(k.startswith("turn.judge_now") for k in orch.metrics)
    finally:
        orch.close()


# ---- Host 端：busy 事實進 turn_context，並與降級註記合併 ----

def _ticket(**kw) -> CoreTicket:
    return CoreTicket(
        turn_id="turn_x", turn_generation=1, source_turn_ref=None, conversation_id="conv_x", segment_id="seg_x",
        audience_scope="scope_private_owner", event_scope="scope_private_owner", reply_mode="respond", auth=AUTH,
        valid_until=None, snapshot_version=1, speaker_ref="u", actor_id="a", user_text="hi", **kw,  # type: ignore[arg-type]
    )


def test_host_puts_busy_note_into_turn_context():
    request = _request_of(_ticket(presence_busy=True))
    meta = request.utterance.metadata
    assert meta["turn_context"] == PRESENCE_BUSY_TURN_CONTEXT and meta["presence_busy"] is True
    assert "degraded" not in meta


def test_host_merges_busy_and_degraded_notes_keeping_both():
    request = _request_of(_ticket(presence_busy=True, degraded="action_unavailable"))
    note = request.utterance.metadata["turn_context"]
    assert DEGRADED_TURN_CONTEXT["action_unavailable"] in note and PRESENCE_BUSY_TURN_CONTEXT in note


def test_host_without_busy_is_unchanged():
    assert "turn_context" not in _request_of(_ticket()).utterance.metadata
    only_degraded = _request_of(_ticket(degraded="action_unavailable")).utterance.metadata
    assert only_degraded["turn_context"] == DEGRADED_TURN_CONTEXT["action_unavailable"]
    assert "presence_busy" not in only_degraded


def test_host_warns_when_think_stage_lacks_turn_context_for_busy_note(caplog):
    from tests.uep.test_action_unavailable import (
        EchoOutputConfig,
        EchoPipelineHost,
        GuildRoster,
    )

    seen: list[str] = []
    stub = types.SimpleNamespace(
        _session=types.SimpleNamespace(guild_id=None), roster=GuildRoster(), output=EchoOutputConfig(),
        _make_sink=lambda *a: None, _on_think_start=lambda *a: None, _on_generation_done=lambda *a: None,
        _tool_path="legacy", _voice_retrieve_timeout_s=None, think=SimpleNamespace(prepare=None),
        metrics=SimpleNamespace(inc=lambda key, *a, **k: seen.append(key)),
    )
    stub._agent_tool_path = types.MethodType(EchoPipelineHost._agent_tool_path, stub)
    with caplog.at_level("WARNING"):
        EchoPipelineHost._build_request(
            stub, _ticket(presence_busy=True), types.SimpleNamespace(language="zh", confidence=1.0, legacy_ref=None))
    assert seen == ["echo.degraded_context_unsupported"] and "不支援 turn_context" in caplog.text


def test_busy_note_reaches_llm_messages():
    request = _request_of(_ticket(presence_busy=True), think=LLMThinkStage(_FakeBackend(), system_prompt="人設"))
    assert request.utterance.metadata["turn_context"] == PRESENCE_BUSY_TURN_CONTEXT


class _FakeBackend:
    api = "responses"
    reasoning_effort = None

    async def stream_query(self, messages, system_instruction=None, **kwargs):
        yield "好"

    def get_last_stream_usage(self) -> dict[str, int]:
        return {"input_tokens": 1, "cached_tokens": 0, "output_tokens": 1}
