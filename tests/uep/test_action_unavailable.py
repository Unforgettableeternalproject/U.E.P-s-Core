"""PR-R 決策層：task_request 降級時帶 ``degraded="action_unavailable"`` 事實，不讓她假裝已經做了（Chain 契約 Q-S）。

不需資料庫、不呼叫真 LLM：Orchestrator 用假 publisher 與假入口；生成端用真的 ``LLMThinkStage`` 搭假 backend，
檢查降級事實確實進到送給模型的 messages、而且 commit 後不進歷史。
「回覆是否真的不含已完成語氣」取決於模型，無法在這裡保證；本檔只保證事實有送達生成端。
chain 段落降級（驗收 c）：chain 路徑尚未接進 Orchestrator（``uep/decision/chain.py`` 目前只有決策日誌），無從測試，待接線後補。
"""
from __future__ import annotations

import dataclasses
import types
from types import SimpleNamespace

from uep.contracts.presence import AuthorizationTuple
from uep.echo.agent_wiring import (
    LoopCapabilityUnavailable,
    LoopEntry,
    LoopLaunchRefused,
)
from uep.echo.config import EchoOutputConfig
from uep.echo.host import DEGRADED_TURN_CONTEXT, EchoPipelineHost
from uep.echo.memory import GuildRoster
from uep.ids import new_id
from uep.stream.adapters.think_llm import LLMThinkStage
from uep.stream.contracts.cancellation import CancellationToken
from uep.turn.decision_config import DecisionConfig
from uep.turn.generations import TurnGenerations
from uep.turn.models import (
    CoreTicket,
    Proposal,
    ReplyMode,
    SessionContext,
    UtteranceFact,
)
from uep.turn.orchestrator import TurnOrchestrator

CTX = SessionContext(
    conversation_id=new_id("conv"), segment_id=new_id("seg"), audience_scope="scope_private_owner", session_ref="sess",
)
AUTH = AuthorizationTuple("uep", "ep", "lease_1", 1, 1, 1)
REMINDER = "明天早上八點提醒我開會"


class _Proposer:
    proposer_id = "test.proposer"

    def __init__(self, proposal: Proposal) -> None:
        self._proposal = proposal

    def propose(self, candidate):
        return self._proposal


class _Publisher:
    def publish(self, draft, trusted):
        return SimpleNamespace(duplicate=False, event=SimpleNamespace(event_id=f"evt_{draft.event_type}", trace_id="trace_1"))


def _orch(proposal: Proposal, entry) -> TurnOrchestrator:
    orch = TurnOrchestrator(
        _Publisher(), TurnGenerations(), auth_provider=lambda: AUTH,  # type: ignore[arg-type]
        proposer=_Proposer(proposal), decision=DecisionConfig(),
    )
    orch.bind_loop_entry(entry)
    return orch


def _proposal(mode: ReplyMode, route: str = "task_request", **kw) -> Proposal:
    return Proposal(reply_mode=mode, route=route, reason_code="called", proposer_id="test.proposer", proposer_version="1", **kw)


def _entry(tags, launched: list) -> LoopEntry:
    return LoopEntry(
        "agent", capability_tags_for=lambda t, p: frozenset(tags), launch=lambda trig, t: launched.append(trig),
        policy_ref="policy_default", owner_for=lambda t: "actor_owner",
    )


def _raiser(exc):
    def entry(ticket, proposal):
        raise exc
    return entry


def _utter(orch: TurnOrchestrator, text: str = REMINDER):
    return orch.on_utterance(CTX, UtteranceFact(speaker_ref="discord:1", text=text, seq=1))


class _Backend:
    """假 LLM backend：記錄收到的 messages，吐固定文字。"""

    api = "responses"
    reasoning_effort = None

    def __init__(self) -> None:
        self.calls: list[list] = []

    async def stream_query(self, messages, system_instruction=None, **kwargs):
        self.calls.append(list(messages))
        yield "我做不到。"

    def get_last_stream_usage(self) -> dict[str, int]:
        return {"input_tokens": 1, "cached_tokens": 0, "output_tokens": 1}


def _request_of(ticket, think=None):
    """宿主把 CoreTicket 轉成生成請求（與 test_agent_tool_path 同法：以替身呼叫真的 ``_build_request``）。"""
    stub = types.SimpleNamespace(
        _session=types.SimpleNamespace(guild_id=None), roster=GuildRoster(), output=EchoOutputConfig(),
        _make_sink=lambda *a: None, _on_think_start=lambda *a: None, _on_generation_done=lambda *a: None,
        _tool_path="legacy", _voice_retrieve_timeout_s=None, think=think if think is not None else LLMThinkStage(_Backend()),
        metrics=SimpleNamespace(inc=lambda *a, **k: None),
    )
    stub._agent_tool_path = types.MethodType(EchoPipelineHost._agent_tool_path, stub)
    fact = types.SimpleNamespace(language="zh", confidence=1.0, legacy_ref=None)
    return EchoPipelineHost._build_request(stub, ticket, fact)


# ---- 事實真的送達生成端（M2）：turn_context 進 messages、commit 後歷史只有 utterance.text ----

async def test_degraded_note_reaches_llm_messages_and_never_enters_history():
    orch = _orch(_proposal(ReplyMode.RESPOND), _entry((), []))
    try:
        ticket = _utter(orch).ticket
    finally:
        orch.close()
    assert ticket is not None and ticket.degraded == "action_unavailable"
    backend = _Backend()
    stage = LLMThinkStage(backend, system_prompt="人設")
    request = _request_of(ticket, think=stage)
    assert request.utterance.metadata["degraded"] == "action_unavailable"
    assert request.ticket.meta["degraded"] == "action_unavailable"
    note = DEGRADED_TURN_CONTEXT["action_unavailable"]
    assert request.utterance.metadata["turn_context"] == note

    async for _ in stage.stream(request.utterance, CancellationToken()):
        pass
    last = backend.calls[0][-1]
    content = getattr(last, "content", None) or last["content"]
    assert content.startswith(note) and content.endswith(REMINDER)  # 註記在本輪 user 訊息開頭，原話在後
    await stage.commit(request.utterance.turn_id, "我做不到。", "我做不到。")
    assert [m["content"] for m in stage.history] == [REMINDER, "我做不到。"]  # 歷史只有原話與回覆，沒有註記
    assert all(note not in m["content"] for m in stage.history)
    assert getattr(backend.calls[0][-1], "content", REMINDER) != REMINDER  # 確認這輪確實有附註記（對照）


def test_host_warns_and_counts_when_think_stage_has_no_turn_context(caplog):
    seen: list[str] = []
    ticket = CoreTicket(
        turn_id="turn_x", turn_generation=1, source_turn_ref=None, conversation_id="conv_x", segment_id="seg_x",
        audience_scope="scope_private_owner", event_scope="scope_private_owner", reply_mode="respond", auth=AUTH,
        valid_until=None, snapshot_version=1, speaker_ref="u", actor_id="a", user_text="hi",  # type: ignore[arg-type]
        degraded="action_unavailable",
    )
    stage = SimpleNamespace(prepare=None)  # 沒有 supports_turn_context
    stub = types.SimpleNamespace(
        _session=types.SimpleNamespace(guild_id=None), roster=GuildRoster(), output=EchoOutputConfig(),
        _make_sink=lambda *a: None, _on_think_start=lambda *a: None, _on_generation_done=lambda *a: None,
        _tool_path="legacy", _voice_retrieve_timeout_s=None, think=stage,
        metrics=SimpleNamespace(inc=lambda key, *a, **k: seen.append(key)),
    )
    stub._agent_tool_path = types.MethodType(EchoPipelineHost._agent_tool_path, stub)
    with caplog.at_level("WARNING"):
        EchoPipelineHost._build_request(stub, ticket, types.SimpleNamespace(language="zh", confidence=1.0, legacy_ref=None))
    assert seen == ["echo.degraded_context_unsupported"] and "不支援 turn_context" in caplog.text


# ---- (a) 空標籤的提醒類請求 ----

def test_reminder_with_empty_labels_degrades_and_generation_input_carries_fact():
    launched: list = []
    orch = _orch(_proposal(ReplyMode.RESPOND), _entry((), launched))
    try:
        ticket = _utter(orch).ticket
        assert ticket is not None and launched == []
        assert ticket.degraded == "action_unavailable"
        assert orch.ticket_of(ticket.turn_id).degraded == "action_unavailable"  # live ticket 同步
        assert orch.metrics["turn.action_unavailable"] == 1 and not orch.loop_claimed(ticket.turn_id)
        meta = _request_of(ticket).utterance.metadata
        assert meta["degraded"] == "action_unavailable" and meta["turn_context"] == DEGRADED_TURN_CONTEXT["action_unavailable"]
    finally:
        orch.close()


def test_agent_loop_degrades_only_for_empty_capability_or_projection():
    orch = _orch(_proposal(ReplyMode.RESPOND, "agent_loop"), _raiser(LoopCapabilityUnavailable("capability_projection_empty")))
    try:
        assert _utter(orch).ticket.degraded == "action_unavailable"
    finally:
        orch.close()
    for exc in (LoopLaunchRefused("loop_busy"), RuntimeError("入口壞了")):
        orch = _orch(_proposal(ReplyMode.RESPOND, "agent_loop"), _raiser(exc))
        try:
            assert _utter(orch).ticket.degraded is None  # 額滿、故障不是「沒有工具」；direct_chat 的唯讀工具仍可能處理
        finally:
            orch.close()


def test_task_request_degrades_whenever_it_falls_back_to_direct_chat():
    for exc in (
        LoopCapabilityUnavailable("capability_projection_empty"), LoopLaunchRefused("loop_busy"),
        LoopLaunchRefused("loop_closing"), RuntimeError("入口壞了"),
    ):
        orch = _orch(_proposal(ReplyMode.RESPOND, "task_request"), _raiser(exc))
        try:
            assert _utter(orch).ticket.degraded == "action_unavailable", repr(exc)  # direct_chat 沒有寫入工具
        finally:
            orch.close()


def test_claimed_run_and_direct_chat_have_no_degraded_flag():
    launched: list = []
    orch = _orch(_proposal(ReplyMode.RESPOND, "agent_loop"), _entry(("lore.read",), launched))
    try:
        ticket = _utter(orch).ticket
        assert ticket.degraded is None and len(launched) == 1
    finally:
        orch.close()
    orch = _orch(_proposal(ReplyMode.RESPOND, "direct_chat"), _entry((), []))
    try:
        ticket = _utter(orch).ticket
        assert ticket.degraded is None
        meta = _request_of(ticket).utterance.metadata
        assert "degraded" not in meta and "turn_context" not in meta
    finally:
        orch.close()


# ---- 沒綁 loop 入口：task_request 標降級，agent_loop 不標 ----

def test_task_request_without_entry_is_degraded_but_agent_loop_is_not():
    for route, expected in (("task_request", "action_unavailable"), ("agent_loop", None), ("direct_chat", None)):
        orch = _orch(_proposal(ReplyMode.RESPOND, route), None)
        try:
            assert _utter(orch).ticket.degraded == expected, route
        finally:
            orch.close()


# ---- LoopEntry.has_capability 與 __call__ 同條件（M1） ----

def test_has_capability_mirrors_the_route_condition_of_call():
    launched: list = []
    entry = _entry(("lore.write",), launched)
    for route in ("direct_chat", "ignore"):  # 入口不處理的 route：__call__ 回 None，查核也是 False
        proposal = _proposal(ReplyMode.RESPOND, route)
        assert entry(SimpleNamespace(turn_id="t"), proposal) is None and launched == []
        assert entry.has_capability(proposal) is False, route
    for route in ("agent_loop", "task_request"):
        assert entry.has_capability(_proposal(ReplyMode.RESPOND, route)) is True, route
        assert _entry((), []).has_capability(_proposal(ReplyMode.RESPOND, route)) is False, route


# ---- (b) defer＋task_request＋capability_empty ----

def test_defer_with_empty_capability_skips_confirmation_and_degrades():
    launched: list = []
    orch = _orch(_proposal(ReplyMode.DEFER, "agent_loop"), _entry((), launched))
    try:
        result = _utter(orch)
        ticket = result.ticket
        assert ticket is not None, "能力不足時不應停在 defer 確認（沒有 ticket = 沒有回覆也沒有確認）"
        assert ticket.degraded == "action_unavailable" and ticket.reply_mode == ReplyMode.RESPOND.value
        assert launched == [] and orch.metrics["turn.defer_action_unavailable"] == 1
        assert result.proposal.reason_code == "action_unavailable" and result.proposal.original_reason_code == "called"
        assert _request_of(ticket).utterance.metadata["degraded"] == "action_unavailable"
    finally:
        orch.close()


def test_defer_task_request_with_empty_capability_skips_confirmation_and_degrades():
    """task_request 沒有可用標籤：不能先問確認再說做不到，直接降級。"""
    orch = _orch(_proposal(ReplyMode.DEFER, "task_request"), _entry((), []))
    try:
        ticket = _utter(orch).ticket
        assert ticket is not None and ticket.degraded == "action_unavailable"
    finally:
        orch.close()


def test_defer_task_request_with_capability_keeps_defer():
    orch = _orch(_proposal(ReplyMode.DEFER, "task_request"), _entry(("lore.write",), []))
    try:
        result = _utter(orch)
        assert result.ticket is None and result.reason_code == "called"  # 有能力：照原本的 defer 確認流程
    finally:
        orch.close()


def test_defer_with_capability_keeps_defer():
    orch = _orch(_proposal(ReplyMode.DEFER, "agent_loop"), _entry(("lore.write",), []))
    try:
        result = _utter(orch)
        assert result.ticket is None and result.reason_code == "called"  # 有能力：原本的 defer 流程不變
    finally:
        orch.close()


def test_defer_untouched_for_direct_chat_unaddressed_and_agent_loop_without_entry():
    cases = (
        (_proposal(ReplyMode.DEFER, "direct_chat"), _entry((), [])),
        (_proposal(ReplyMode.DEFER, "agent_loop", addressed=False), _entry((), [])),  # 沒被叫到：不開 turn
        (_proposal(ReplyMode.DEFER, "task_request", addressed=False), None),
        (_proposal(ReplyMode.DEFER, "agent_loop"), None),
    )
    for proposal, entry in cases:
        orch = _orch(proposal, entry)
        try:
            assert _utter(orch).ticket is None, proposal
        finally:
            orch.close()
    orch = _orch(_proposal(ReplyMode.DEFER, "task_request"), None)
    try:
        assert _utter(orch).ticket.degraded == "action_unavailable"  # task_request 沒入口：寫入類動作做不到
    finally:
        orch.close()


def test_capability_check_runs_before_interaction_request_and_result_is_revalidated():
    seen: list[ReplyMode] = []
    orch = _orch(_proposal(ReplyMode.DEFER, "agent_loop"), _entry((), []))
    original = orch._apply_interaction
    orch._apply_interaction = lambda cand, prop: (seen.append(prop.reply_mode), original(cand, prop))[1]  # type: ignore[method-assign]
    try:
        assert _utter(orch).ticket.degraded == "action_unavailable"
        assert seen == [ReplyMode.RESPOND]  # CC-7／稽核看到的是 respond，不是 defer
    finally:
        orch.close()
    # 轉換後驗證失敗（例如原因碼含不安全字元以外的情形）就維持 defer：直接餵一個無法通過驗證的提議
    orch = _orch(_proposal(ReplyMode.DEFER, "agent_loop"), _entry((), []))
    try:
        bad = dataclasses.replace(_proposal(ReplyMode.DEFER, "agent_loop"), proposer_id="含 空白")
        proposal, degraded = orch._defer_to_unavailable(bad)
        assert degraded is None and proposal is bad and orch.metrics["turn.defer_conversion_invalid"] == 1
    finally:
        orch.close()


def test_ticket_carries_enum_only_no_user_visible_text():
    ticket = CoreTicket(
        turn_id="t", turn_generation=1, source_turn_ref=None, conversation_id="c", segment_id="s", audience_scope="a",
        event_scope="e", reply_mode="respond", auth=AUTH, valid_until=None, snapshot_version=0,  # type: ignore[arg-type]
        speaker_ref="u", actor_id="x", degraded="action_unavailable",
    )
    assert ticket.degraded.isascii() and " " not in ticket.degraded
    assert CoreTicket.__dataclass_fields__["degraded"].default is None
