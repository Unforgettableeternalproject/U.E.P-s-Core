"""切片 C2：MemoryToolExecutor 與 CoreToolExecutor 的逐欄對照、agent 路徑投影、legacy 黃金位元組（皆不需 DB）。"""
from __future__ import annotations

import asyncio
import copy
import json
import types
from pathlib import Path

import pytest

from uep.adapters.tool import ToolCall, ToolExecutor, ToolResponseLost
from uep.config import get_settings
from uep.echo.host import EchoPipelineHost
from uep.echo.memory import GuildRoster
from uep.echo.tools import CoreToolExecutor
from uep.memory.access import TOOL_CLASSES
from uep.tools.catalog.memory import memory_registry
from uep.tools.executors.memory import MemoryToolExecutor

GOLDEN = Path(__file__).parent / "fixtures" / "think_tools_golden.json"


class RecordingPort:
    """最小 MemoryPort 替身：記錄 execute_tool 的完整呼叫，結果依工具名或例外腳本給。"""

    def __init__(self, outcomes=None, raises: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.outcomes = outcomes or {}
        self.raises = raises

    def execute_tool(self, name, args, *, actor_id, audience, session_id=None, context=None):
        self.calls.append({"name": name, "args": dict(args), "actor_id": actor_id, "audience": audience,
                           "session_id": session_id, "context": context})
        if self.raises is not None:
            raise self.raises
        return copy.deepcopy(self.outcomes.get(name, {"success": True, "result": "ok"}))


def make_roster() -> GuildRoster:
    return GuildRoster({"123": {"actor_a": "111", "actor_b": "222"}}, profiles={
        "123": {"222": {"display_name": "測試乙", "aliases": ["小乙"]}, "111": {"display_name": "測試甲"}}})


CTX = {"actor_id": "actor_a", "audience": "scope_group_g123", "segment_id": "seg_x", "ticket_ref": "turn_x",
       "guild_id": "123", "present_uids": ["111", "222"]}
LONG = "字" * 50

# (id, 工具, 引數, context 修改, 埠結果, 埠例外, audience_ok, max_chars)
CASES = [
    ("deep_recall_ok", "deep_recall", {"query": "q"}, {}, None, None, None, None),
    ("force_remember_ok", "force_remember", {"content": "c", "reason": "r"}, {}, None, None, None, None),
    ("member_recall_ok", "member_recall", {"member": " 小乙 ", "query": "近況"}, {},
     {"member_recall": {"success": True, "result": "投影", "shared_ids": ["p1", "p2", "p1"]}}, None, None, None),
    ("member_recall_not_found", "member_recall", {"member": "不存在"}, {}, None, None, None, None),
    ("member_recall_empty_name", "member_recall", {}, {}, None, None, None, None),
    ("member_recall_no_guild", "member_recall", {"member": "測試甲"}, {"guild_id": None}, None, None, None, None),
    ("not_whitelisted", "trigger_dream", {}, {}, None, None, None, None),
    ("no_context", "deep_recall", {}, "NONE", None, None, None, None),
    ("no_audience", "deep_recall", {}, {"audience": None}, None, None, None, None),
    ("empty_audience", "deep_recall", {}, {"audience": ""}, None, None, None, None),
    ("audience_narrowed", "deep_recall", {}, {}, None, None, lambda a: False, None),
    ("audience_ok_passes", "deep_recall", {}, {}, None, None, lambda a: True, None),
    ("args_not_mapping", "deep_recall", ["x"], {}, None, None, None, None),
    ("tool_reports_failure", "deep_recall", {}, {}, {"deep_recall": {"success": False, "result": "壞了"}}, None, None, None),
    ("tool_reports_failed_code", "deep_recall", {}, {},
     {"deep_recall": {"success": False, "result": "x", "code": "failed"}}, None, None, None),
    ("tool_timeout", "deep_recall", {}, {},
     {"deep_recall": {"success": False, "result": "tool timeout", "code": "timeout"}}, None, None, None),
    ("port_raises", "deep_recall", {}, {}, None, RuntimeError("boom"), None, None),
    ("truncated_drops_ids", "member_recall", {"member": "測試甲"}, {},
     {"member_recall": {"success": True, "result": LONG, "shared_ids": ["p1"]}}, None, None, 10),
    ("non_str_actor", "deep_recall", {}, {"actor_id": 5, "segment_id": 7}, None, None, None, None),
]


def _ctx(mod):
    if mod == "NONE":
        return None
    ctx = copy.deepcopy(CTX)
    for k, v in mod.items():
        if v is None:
            ctx.pop(k, None)
        else:
            ctx[k] = v
    return ctx


def _params(args, ctx):
    """Gateway 注入後的 params：LLM 引數 + Core 的身分／場域（PD-12）；args 不是映射時原樣（無從注入）。"""
    if not isinstance(args, dict):
        return args
    injected = {k: ctx[k] for k in ("actor_id", "audience", "segment_id", "guild_id") if ctx and k in ctx}
    return {**args, **injected}


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_memory_tool_executor_matches_core_tool_executor(case):
    _id, tool, args, mod, outcomes, raises, audience_ok, max_chars = case
    kw = {} if max_chars is None else {"max_chars": max_chars}
    legacy_calls: list[tuple] = []

    legacy_port, new_port = RecordingPort(outcomes, raises), RecordingPort(outcomes, raises)
    legacy_ctx, new_ctx = _ctx(mod), _ctx(mod)
    legacy = CoreToolExecutor(legacy_port, make_roster(), on_action=lambda *a: legacy_calls.append(a),
                              audience_ok=audience_ok, **kw)
    new = MemoryToolExecutor(new_port, make_roster(), audience_ok=audience_ok, **kw)
    assert isinstance(new, ToolExecutor)

    expected = asyncio.run(legacy(tool, args, context=legacy_ctx))
    call = ToolCall(action_id="act_1", tool=tool, params=_params(args, new_ctx), params_hash="h",
                    side_effect_class="internal_read", idempotency="native")
    lost = False
    try:
        receipt = new.execute(call)
    except ToolResponseLost:
        lost, receipt = True, None

    got = new.outcome("act_1")
    shared = got.pop("shared_ids", None)
    assert got == expected  # 結果逐欄一致（shared_ids 另比）
    assert new_port.calls == legacy_port.calls  # 傳給 Access Layer 的身分、audience、context、引數完全一致
    assert (shared or None) == ((legacy_ctx or {}).get("member_recall_ids") or None)  # 投影 id 改由結果回傳
    assert new_ctx == _ctx(mod)  # executor 不改任何共用狀態
    legacy_status = legacy_calls[0][3] if legacy_calls else None
    if lost:
        assert legacy_status == "unknown" and receipt is None
        assert new.lookup_receipt("act_1").outcome == "undetermined"
    else:
        assert receipt.result_ref == "blob_act_1" and receipt.source_verified
        assert receipt.succeeded == bool(expected.get("success")) and legacy_status != "unknown"
        assert receipt.meta["status"] == (legacy_status or ("denied" if not expected.get("success") else "not_found"))
        assert new.lookup_receipt("act_1").receipt is receipt


def test_llm_cannot_override_injected_scope_fields_at_executor():
    """injected 欄位只能來自 Gateway；executor 不把它們當業務引數傳給 Access Layer。"""
    port = RecordingPort()
    ex = MemoryToolExecutor(port, make_roster())
    params = {"query": "q", **_params({}, CTX)}
    ex.execute(ToolCall("act_s", "deep_recall", params, "h", "internal_read", "native"))
    assert port.calls[0]["args"] == {"query": "q"} and port.calls[0]["actor_id"] == "actor_a"


def test_denied_cases_never_reach_access_layer():
    for tool, mod, ok in (("trigger_dream", {}, None), ("deep_recall", "NONE", None),
                          ("deep_recall", {"audience": None}, None), ("deep_recall", {}, lambda a: False)):
        port = RecordingPort()
        ctx = _ctx(mod)
        ex = MemoryToolExecutor(port, make_roster(), audience_ok=ok)
        receipt = ex.execute(ToolCall("act_d", tool, _params({}, ctx), "h", "internal_read", "native"))
        assert receipt.succeeded is False and receipt.error_code == "denied" and receipt.meta == {"status": "denied"}
        assert port.calls == []


def test_lookup_is_not_authoritative_and_idempotent_replay_returns_same_receipt():
    port = RecordingPort()
    ex = MemoryToolExecutor(port, make_roster())
    assert ex.lookup_authoritative is False
    assert ex.lookup_receipt("act_none").outcome == "undetermined"
    call = ToolCall("act_i", "deep_recall", _params({"query": "q"}, CTX), "h", "internal_read", "native")
    first = ex.execute(call)
    assert ex.execute(call) is first and len(port.calls) == 1
    redo = ToolCall("act_n", "force_remember", _params({"content": "c", "reason": "r"}, CTX), "h",
                    "reversible_local", "none")
    ex.execute(redo)
    ex.execute(redo)  # idempotency=none：不沿用舊 receipt（與 FakeToolExecutor 同）
    assert len(port.calls) == 3


def test_outcome_cache_is_turn_scoped_and_not_in_receipt():
    port = RecordingPort({"deep_recall": {"success": True, "result": "私人結果原文"}})
    ex = MemoryToolExecutor(port, make_roster())
    receipt = ex.execute(ToolCall("act_c", "deep_recall", _params({}, CTX), "h", "internal_read", "native"))
    assert "私人結果原文" not in repr(receipt)  # D33：原文不進 receipt
    assert ex.cache.text("act_c") == "私人結果原文"
    ex.cache.discard(["act_c"])
    assert ex.cache.text("act_c") is None and ex.outcome("act_c") is None


# ---- host：agent 路徑投影／legacy 黃金 ----

def _host_stub(guild_id, audience="scope_private_owner", path="legacy"):
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    class Memory:
        def tool_definitions(self):
            return copy.deepcopy(golden)

    stub = types.SimpleNamespace(
        _memory=Memory(), tool_executor=object(), think=types.SimpleNamespace(tools=None),
        _session=types.SimpleNamespace(guild_id=guild_id, audience_scope=audience), _tool_path=path,
    )
    stub._agent_tool_path = types.MethodType(EchoPipelineHost._agent_tool_path, stub)  # 路徑由建構參數注入
    stub._refresh_tools_agent = types.MethodType(EchoPipelineHost._refresh_tools_agent, stub)
    stub._DIRECT_CHAT_CAPABILITY_TAGS = EchoPipelineHost._DIRECT_CHAT_CAPABILITY_TAGS
    return stub


def _names(tools):
    return [t["function"]["name"] for t in tools]


def test_default_tool_path_is_legacy():
    assert get_settings().tool_path == "legacy"


@pytest.mark.parametrize("guild_id", [None, "123"])
def test_legacy_path_bytes_unchanged(guild_id):
    stub = _host_stub(guild_id)
    EchoPipelineHost._refresh_tools(stub)
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    available = {"member_recall"} if guild_id else set()
    expected = [d for d in golden if d["function"]["name"] != "member_recall" or "member_recall" in available]
    assert json.dumps(stub.think.tools, ensure_ascii=False) == json.dumps(expected, ensure_ascii=False)
    assert "force_remember" in _names(stub.think.tools)  # legacy 行為不變：仍曝光 force_remember


@pytest.mark.parametrize("guild_id", [None, "123"])
def test_agent_path_direct_chat_projection_includes_write_tools(guild_id):
    """E3：direct_chat 加上 memory.write（approval_level=none 的寫入類走四階段）；「關於我」只在有場域時投出。"""
    stub = _host_stub(guild_id, audience="scope_group_g123" if guild_id else "scope_private_owner", path="agent")
    EchoPipelineHost._refresh_tools(stub)
    names = _names(stub.think.tools)
    assert names == (
        ["force_remember", "deep_recall", "member_recall", "retract_about_me", "opt_out_about_me", "correct_about_me"]
        if guild_id else ["force_remember", "deep_recall"]
    )
    assert {n for n in names if TOOL_CLASSES[n] != "internal_read"} == (
        {"force_remember", "retract_about_me", "opt_out_about_me", "correct_about_me"} if guild_id else {"force_remember"}
    )
    assert all(t["type"] == "function" and "parameters" in t["function"] for t in stub.think.tools)


def test_agent_path_direct_chat_tags_come_from_route_table(monkeypatch):
    """黃金：經 Host 組裝的 direct_chat 投影輸入 == {memory.read, memory.write}（E3），來源是決策層 route 表（DL-4 PR3）。"""
    from uep.agent.bridge import DIRECT_CHAT_TAGS
    from uep.decision.capability import ROUTE_DEFAULT_CAPABILITIES

    assert EchoPipelineHost._DIRECT_CHAT_CAPABILITY_TAGS == frozenset({"memory.read", "memory.write"})
    assert DIRECT_CHAT_TAGS == ROUTE_DEFAULT_CAPABILITIES["direct_chat"]  # 橋接層與決策層同一份
    assert EchoPipelineHost._DIRECT_CHAT_CAPABILITY_TAGS == ROUTE_DEFAULT_CAPABILITIES["direct_chat"]
    seen: list[frozenset[str]] = []
    real = memory_registry()

    class Spy:
        def project(self, tags, ctx):
            seen.append(frozenset(tags))
            return real.project(tags, ctx)

    monkeypatch.setattr("uep.echo.host.memory_registry", lambda: Spy())
    stub = _host_stub(None, audience="scope_private_owner", path="agent")
    EchoPipelineHost._refresh_tools(stub)
    assert seen == [frozenset({"memory.read", "memory.write"})]
    assert _names(stub.think.tools) == ["force_remember", "deep_recall"]


def test_agent_path_fails_closed_on_projection_error():
    stub = _host_stub(None, audience="not-an-audience", path="agent")
    EchoPipelineHost._refresh_tools(stub)
    assert stub.think.tools == []


def test_registry_member_recall_needs_guild_context():
    assert memory_registry().entry("member_recall").scope.requires_context


def test_gateway_outcome_view_takes_content_from_cache_or_returns_unknown():
    from datetime import UTC, datetime

    from uep.adapters.tool import ToolReceipt
    from uep.tasks.manager import ActionOutcome
    from uep.tasks.states import ActionStatus
    from uep.tools.gateway import ToolGateway
    from uep.tools.outcomes import TurnOutcomeCache

    cache = TurnOutcomeCache()
    receipt = ToolReceipt("rcpt_1", "act_1", True, "blob_act_1", datetime.now(UTC), True)
    outcome = ActionOutcome("act_1", ActionStatus.SUCCEEDED, True, receipt)
    gateway = ToolGateway()
    assert gateway.outcome_view(outcome, content_lookup=cache.text)["status"] == "unknown"  # 找不到：不編造
    cache.put("act_1", {"success": True, "result": "內容"})
    view = gateway.outcome_view(outcome, content_lookup=cache.text)
    assert view["status"] == "ok" and view["items"][0]["content"] == "內容"
    assert "content" not in gateway.outcome_view(outcome)["items"][0]  # 沒給 lookup：形狀不變
