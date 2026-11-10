"""切片 E3：force_remember 與「關於我」寫入類工具走完整四階段（approval_level=none）。

前半（Access Layer、executor）不需 DB；標示「DB」的測試需 PostgreSQL（未設 UEP_TEST_DATABASE_URL 時 skip）：
ActionIntent（act_ id、reversible_local）、receipt、沒有核准等待、重送同 action_id 不重複寫入、失敗 receipt 記錄錯誤碼。
"""
from __future__ import annotations

import asyncio
import copy
import types

import pytest
from sqlalchemy import select

from uep.memory.access import MemoryAccessLayer
from uep.memory.identity import IdentityDirectory
from uep.stream.contracts import memory_tools as contract
from uep.tools.catalog.memory import memory_registry
from uep.tools.outcomes import TurnOutcomeCache

ABOUT_ME_REGISTERED = ("retract_about_me", "opt_out_about_me")
WRITE_TOOLS = ("force_remember", *ABOUT_ME_REGISTERED)
GROUP = "scope_group_g123"


# ---- 註冊表分類 ----

def test_write_tools_are_reversible_local_without_approval_and_memory_write():
    registry = memory_registry()
    for name in WRITE_TOOLS:
        spec = registry.entry(name).spec
        assert spec.side_effect_class == "reversible_local" and spec.approval_level == "none", name
        assert spec.capability == "memory.write" and spec.idempotency == "none", name  # 重送冪等由 action_id 保證


def test_about_me_tools_need_guild_context_and_hide_injected_params():
    registry = memory_registry()
    for name in ABOUT_ME_REGISTERED:
        entry = registry.entry(name)
        assert entry.scope.requires_context == frozenset({"guild_roster"}), name
        expected = {"actor_id", "audience", "segment_id", "guild_id"} | ({"run_id"} if name == "retract_about_me" else set())
        assert set(entry.descriptor.injected_params) == expected, name  # retract 的 ref 以 Run 為範圍
        assert "guild_id" not in entry.descriptor.input_schema["properties"], name


# ---- Access Layer：「關於我」寫入 ----

class FakeAdapter:
    def __init__(self, result=None, raises=None):
        self.calls: list[tuple] = []
        self.result, self.raises = result if result is not None else {"ok": True}, raises

    async def retract_about_me(self, scope_id, actor_uid, projection_id):
        return self._record("retract_about_me", scope_id, actor_uid, projection_id)

    async def opt_out_about_me(self, scope_id, actor_uid, enabled):
        return self._record("opt_out_about_me", scope_id, actor_uid, enabled)

    async def correct_about_me(self, scope_id, actor_uid, projection_id, text, *, evidence=None, session_id=None):
        return self._record("correct_about_me", scope_id, actor_uid, projection_id, text, evidence, session_id)

    def _record(self, *call):
        self.calls.append(call)
        if self.raises:
            raise self.raises
        return dict(self.result)


class FakeRuntime:
    def __init__(self, adapter: FakeAdapter | None, engine=None):
        self._adapter, self._engine = adapter, engine

    def namespace_for_owner(self, owner_id):
        return "ns_owner"

    def namespace_for_guild(self, guild_id):
        return "ns_g123"

    def adapter(self, namespace_id):
        return self._adapter

    def adapter_call(self, namespace_id, fn, *, timeout_s, on_late=None):
        return asyncio.run(fn(self._adapter))

    def call(self, namespace_id, fn, *, timeout_s, on_late=None):
        return fn(self._engine)

    def adapter_fire(self, namespace_id, fn):
        raise AssertionError("不應呼叫")


class FakeMembership:
    def __init__(self, members=("111",)):
        self._members = list(members)

    def is_member(self, guild_id, actor_id):
        return actor_id == "actor_a"

    def members(self, guild_id):
        return list(self._members)


def make_layer(adapter: FakeAdapter | None, engine=None, members=("111",)) -> MemoryAccessLayer:
    identity = IdentityDirectory(
        platform_to_actor={"discord:111": "actor_a", "discord:222": "actor_b"},
        actor_to_owner={"actor_a": "owner", "actor_b": "other"},
    )
    return MemoryAccessLayer(FakeRuntime(adapter, engine), identity, guild_membership=FakeMembership(members))


def run_about(layer, name, args, *, actor="actor_a", audience=GROUP, guild="123", projection_id="proj_1"):
    context = {"guild_id": guild, "projection_id": projection_id} if guild else {"projection_id": projection_id}
    return layer.execute_tool(name, args, actor_id=actor, audience=audience, session_id="seg_1", context=context)


def test_access_opt_out_calls_adapter_with_guild_scope_and_actor_uid():
    adapter = FakeAdapter({"ok": True, "enabled": True})
    out = run_about(make_layer(adapter), "opt_out_about_me", {"enabled": True})
    assert out["success"] is True and "已設定退出" in out["result"]
    assert adapter.calls == [("opt_out_about_me", "guild:123", "111", True)]


def test_access_retract_uses_projection_id_from_context_not_args():
    adapter = FakeAdapter()
    layer = make_layer(adapter)
    assert run_about(layer, "retract_about_me", {"ref": 1, "projection_id": "evil"})["success"] is True
    assert adapter.calls == [("retract_about_me", "guild:123", "111", "proj_1")]
    # correct_about_me 不可逆、未註冊：Access 不處理
    denied = run_about(layer, "correct_about_me", {"ref": 1, "correction_text": "x"})
    assert denied["code"] == "denied" and len(adapter.calls) == 1


@pytest.mark.parametrize("kwargs", [
    {"audience": "scope_group_g999"},  # audience 不含這個場域
    {"guild": None},  # 缺 guild_id
    {"actor": "actor_b"},  # 不是成員
    {"actor": "actor_unknown"},  # 訪客
])
def test_access_about_me_is_fail_closed_and_never_touches_adapter(kwargs):
    adapter = FakeAdapter()
    out = run_about(make_layer(adapter), "opt_out_about_me", {"enabled": True}, **kwargs)
    assert out["success"] is False and out["code"] == "denied" and adapter.calls == []


def test_access_about_me_missing_projection_is_failure_without_write():
    adapter = FakeAdapter()
    layer = make_layer(adapter)
    assert run_about(layer, "retract_about_me", {"ref": 1}, projection_id="")["result"] == contract.ABOUT_BAD_REF
    assert adapter.calls == []


def test_access_about_me_engine_rejection_exception_and_unsupported_map_to_failure():
    rejected = run_about(make_layer(FakeAdapter({"ok": False, "reason": "not_found"})), "retract_about_me", {"ref": 1})
    assert rejected["success"] is False and "not_found" in rejected["result"]
    boom = run_about(make_layer(FakeAdapter(raises=RuntimeError("x"))), "retract_about_me", {"ref": 1})
    assert boom == {"success": False, "result": "tool failed", "code": "failed"}
    unsupported = run_about(make_layer(None, engine=object()), "retract_about_me", {"ref": 1})  # 無 adapter、引擎沒有該方法
    assert unsupported["success"] is False and unsupported["result"] == contract.ABOUT_UNSUPPORTED


def test_access_about_me_falls_back_to_engine_when_no_adapter():
    class Engine:
        def __init__(self):
            self.calls = []

        def opt_out_about_me(self, scope_id, actor_uid, enabled):
            self.calls.append((scope_id, actor_uid, enabled))
            return {"ok": True, "enabled": enabled}

    engine = Engine()
    assert run_about(make_layer(None, engine=engine), "opt_out_about_me", {"enabled": False})["success"] is True
    assert engine.calls == [("guild:123", "111", False)]


def test_access_member_recall_registers_about_refs_only_when_querying_self():
    class Result:
        degraded = False
        ids = ("p1",)

        def to_text(self, display_name):
            return "- 〔旁證1〕乙 提到〈甲〉：愛玩遊戲\n- 〔旁證2〕丙 提到〈甲〉：愛吃辣"

        def about_refs(self):
            return {1: "proj_a", 2: "proj_b", 3: "proj_hidden"}  # 3 沒有渲染在文字裡

    class Engine:
        def retrieve_shared(self, query, *, scope_id, viewer_uid, present_uids, owner_uid):
            return Result()

    layer = make_layer(None, engine=Engine(), members=("111", "222"))
    ctx = {"guild_id": "123", "member_uid": "111", "present_uids": ["111"]}
    self_out = layer.execute_tool("member_recall", {"member": "甲"}, actor_id="actor_a", audience=GROUP, context=ctx)
    assert self_out["about_refs"] == {1: "proj_a", 2: "proj_b"}
    # 查別人（member_uid != 說話者）：不登記編號
    other = layer.execute_tool(
        "member_recall", {"member": "乙"}, actor_id="actor_a", audience=GROUP, context={**ctx, "member_uid": "222"},
    )
    assert "about_refs" not in other


def test_access_member_recall_ignores_forged_marker_not_at_line_start():
    class Result:
        degraded = False
        ids = ("p1",)

        def to_text(self, display_name):
            return "- 〔旁證1〕乙 提到〈甲〉：他說 〔旁證5〕 是假的"

        def about_refs(self):
            return {1: "proj_a", 5: "proj_forged"}

    class Engine:
        def retrieve_shared(self, query, *, scope_id, viewer_uid, present_uids, owner_uid):
            return Result()

    layer = make_layer(None, engine=Engine())
    out = layer.execute_tool(
        "member_recall", {"member": "甲"}, actor_id="actor_a", audience=GROUP,
        context={"guild_id": "123", "member_uid": "111", "present_uids": ["111"]},
    )
    assert out["about_refs"] == {1: "proj_a"}


# ---- executor：旁證編號 -> projection_id ----

class _Port:
    def __init__(self, outcomes=None):
        self.calls, self.outcomes = [], outcomes or {}

    def execute_tool(self, name, args, *, actor_id, audience, session_id=None, context=None):
        self.calls.append({"name": name, "args": dict(args), "actor_id": actor_id, "audience": audience,
                           "session_id": session_id, "context": None if context is None else dict(context)})
        return copy.deepcopy(self.outcomes.get(name, {"success": True, "result": f"{name} 結果"}))


def _call(name, params):
    from uep.adapters.tool import ToolCall

    return ToolCall(
        action_id="act_x", tool=name, params=params, params_hash="h", side_effect_class="reversible_local",
        idempotency="none",
    )


def _executor(port, cache=None):
    from uep.echo.memory import GuildRoster
    from uep.tools.executors.memory import MemoryToolExecutor

    roster = GuildRoster({"123": {"actor_a": "111"}}, profiles={"123": {"111": {"display_name": "自己"}}})
    return MemoryToolExecutor(port, roster, cache=cache if cache is not None else TurnOutcomeCache())


BASE = {"actor_id": "actor_a", "audience": GROUP, "segment_id": "seg_1", "guild_id": "123", "run_id": "run_A"}
SELF_RECALL = {
    "success": True, "result": "- 〔旁證1〕乙 提到〈甲〉：x\n- 〔旁證2〕丙 提到〈甲〉：y", "about_refs": {1: "proj_a", 2: "proj_b"},
}


def test_executor_resolves_ref_from_run_scoped_about_refs_without_turn_scope():
    port = _Port({"member_recall": copy.deepcopy(SELF_RECALL)})
    cache = TurnOutcomeCache()
    executor = _executor(port, cache)
    executor.execute(_call("member_recall", {**BASE, "member": "自己"}))  # 不進 turn_scope（loop 路徑）
    receipt = executor.execute(_call("retract_about_me", {**BASE, "ref": 2}))
    assert receipt.succeeded and receipt.meta == {"status": "executed"}
    assert port.calls[1]["name"] == "retract_about_me"
    assert port.calls[1]["context"] == {"guild_id": "123", "projection_id": "proj_b"}
    assert port.calls[1]["args"] == {"ref": 2}  # run_id 是注入值，不當成業務引數
    assert "about_refs" not in (cache.get("act_x") or {})
    cache.discard_about_refs_run("run_A")
    assert cache.about_ref(("run_A", "actor_a"), 2) is None  # Run 終態清除


def test_executor_ref_does_not_cross_run_or_user_and_never_calls_port():
    port = _Port({"member_recall": copy.deepcopy(SELF_RECALL)})
    cache = TurnOutcomeCache()
    executor = _executor(port, cache)
    executor.execute(_call("member_recall", {**BASE, "member": "自己"}))
    n_calls = len(port.calls)
    other_run = executor.execute(_call("retract_about_me", {**BASE, "run_id": "run_B", "ref": 1}))  # B 的 Run 用 A 的編號
    other_user = executor.execute(_call("retract_about_me", {**BASE, "actor_id": "actor_x", "ref": 1}))
    assert other_run.succeeded is False and other_user.succeeded is False
    assert cache.get("act_x")["result"] == contract.ABOUT_NO_REF  # B 沒登記過：與「編號不存在」同樣不洩漏 A 的存在
    assert len(port.calls) == n_calls  # 不呼叫記憶引擎
    ok = executor.execute(_call("retract_about_me", {**BASE, "ref": 1}))
    assert ok.succeeded and port.calls[-1]["context"]["projection_id"] == "proj_a"


@pytest.mark.parametrize("bad", [{"run_id": ""}, {"run_id": "  "}, {"run_id": None}, {"actor_id": ""}, {"actor_id": None}])
def test_executor_empty_scope_is_fail_closed_for_register_and_resolve(bad):
    port = _Port({"member_recall": copy.deepcopy(SELF_RECALL)})
    cache = TurnOutcomeCache()
    executor = _executor(port, cache)
    executor.execute(_call("member_recall", {**BASE, **bad, "member": "自己"}))
    assert len(cache._about_by_n) == 0  # 空 scope 拒絕寫入（不落進共用桶）
    assert "〔旁證1〕" not in cache.get("act_x")["result"]  # 沒登記的編號不顯示給 LLM
    before = len(port.calls)
    res = executor.execute(_call("retract_about_me", {**BASE, **bad, "ref": 1}))
    assert res.succeeded is False and len(port.calls) == before
    assert cache.about_ref(None, 1) is None and cache.about_ref(("", "a"), 1) is None
    assert cache.assign_about_refs(("", "a"), {1: "p"}) == {}


def test_executor_about_ref_resolves_in_parallel_worker_threads():
    from concurrent.futures import ThreadPoolExecutor

    port = _Port({"member_recall": copy.deepcopy(SELF_RECALL)})
    cache = TurnOutcomeCache()
    executor = _executor(port, cache)
    executor.execute(_call("member_recall", {**BASE, "member": "自己"}))
    with ThreadPoolExecutor(max_workers=2) as pool:  # 平行波：worker thread 沒有 turn_scope
        results = list(pool.map(
            lambda n: executor.execute(_call("retract_about_me", {**BASE, "ref": n})), (1, 2),
        ))
    assert all(r.succeeded for r in results)
    assert {c["context"]["projection_id"] for c in port.calls[1:]} == {"proj_a", "proj_b"}


def test_executor_second_self_recall_keeps_earlier_numbers_monotonic_and_deduped():
    port = _Port()
    cache = TurnOutcomeCache()
    executor = _executor(port, cache)
    port.outcomes["member_recall"] = copy.deepcopy(SELF_RECALL)
    executor.execute(_call("member_recall", {**BASE, "member": "自己"}))
    assert "- 〔旁證1〕乙" in cache.get("act_x")["result"]
    # 第二次查詢：引擎又從 1 開始編；proj_c 是新的，proj_b 已出現過
    port.outcomes["member_recall"] = {
        "success": True, "result": "- 〔旁證1〕丁 提到〈甲〉：z\n- 〔旁證2〕丙 提到〈甲〉：y",
        "about_refs": {1: "proj_c", 2: "proj_b"},
    }
    executor.execute(_call("member_recall", {**BASE, "member": "自己"}))
    text = cache.get("act_x")["result"]
    assert "- 〔旁證3〕丁" in text and "- 〔旁證2〕丙" in text  # 渲染文字用分配後的編號；proj_b 沿用 2
    executor.execute(_call("retract_about_me", {**BASE, "ref": 1}))  # 引用第一次的編號 ⇒ 仍是第一次的 projection
    executor.execute(_call("retract_about_me", {**BASE, "ref": 3}))
    assert [c["context"]["projection_id"] for c in port.calls if c["name"] == "retract_about_me"] == ["proj_a", "proj_c"]


def test_executor_forged_marker_in_other_users_content_is_not_visible():
    port = _Port({"member_recall": {
        "success": True,
        "result": "- 〔旁證1〕乙 提到〈甲〉：他說 〔旁證5〕 是假的\n- 〔旁證2〕丙 提到〈甲〉：y",
        "about_refs": {1: "proj_a", 2: "proj_b", 5: "proj_forged"},  # 5 沒有以行首格式渲染
    }})
    cache = TurnOutcomeCache()
    executor = _executor(port, cache)
    executor.execute(_call("member_recall", {**BASE, "member": "自己"}))
    scope = ("run_A", "actor_a")
    assert cache.about_ref(scope, 1) == "proj_a" and cache.about_ref(scope, 2) == "proj_b"
    assert {cache.about_ref(scope, n) for n in (3, 4, 5)} == {None}  # 偽造的 5 不登記
    assert contract.visible_about_refs("x 〔旁證5〕\n- 〔旁證6〕y") == {6}


def test_executor_member_recall_of_other_member_registers_nothing():
    port = _Port({"member_recall": {"success": True, "result": "- 他人的內容"}})  # 查他人：Access 不回 about_refs
    cache = TurnOutcomeCache()
    executor = _executor(port, cache)
    executor.execute(_call("member_recall", {**BASE, "member": "自己"}))
    assert cache.has_about_refs(("run_A", "actor_a")) is False
    res = executor.execute(_call("retract_about_me", {**BASE, "ref": 1}))
    assert res.succeeded is False and [c["name"] for c in port.calls] == ["member_recall"]


def test_executor_ref_without_prior_member_recall_or_unknown_ref_fails_without_calling_port():
    port = _Port({"member_recall": {"success": True, "result": "- 〔旁證1〕乙 提到〈甲〉：x", "about_refs": {1: "proj_a"}}})
    cache = TurnOutcomeCache()
    executor = _executor(port, cache)
    none_yet = executor.execute(_call("retract_about_me", {**BASE, "ref": 1}))
    executor.execute(_call("member_recall", {**BASE, "member": "自己"}))
    unknown = executor.execute(_call("retract_about_me", {**BASE, "ref": 9}))
    assert none_yet.succeeded is False and none_yet.error_code == "failed"
    assert cache.get("act_x")["result"] == contract.ABOUT_BAD_REF  # 已有登記但編號不在其中
    assert unknown.succeeded is False
    assert [c["name"] for c in port.calls] == ["member_recall"]


def test_executor_truncated_member_recall_only_registers_complete_lines():
    lines = [f"- 〔旁證{n}〕乙 提到〈甲〉：{'長' * 30}" for n in (1, 2, 3)]
    port = _Port({"member_recall": {"success": True, "result": "\n".join(lines), "about_refs": {1: "a", 2: "b", 3: "c"}}})
    from uep.echo.memory import GuildRoster
    from uep.tools.executors.memory import MemoryToolExecutor

    cache = TurnOutcomeCache()
    roster = GuildRoster({"123": {"actor_a": "111"}}, profiles={"123": {"111": {"display_name": "自己"}}})
    executor = MemoryToolExecutor(port, roster, cache=cache, max_chars=len(lines[0]) + 10)
    executor.execute(_call("member_recall", {**BASE, "member": "自己"}))
    scope = ("run_A", "actor_a")
    assert cache.about_ref(scope, 1) == "a"
    assert cache.about_ref(scope, 2) is None and cache.about_ref(scope, 3) is None  # 被截斷的半行不登記
    assert "〔旁證2〕" not in cache.get("act_x")["result"] and "〔旁證3〕" not in cache.get("act_x")["result"]


def test_executor_about_me_requires_guild_and_opt_out_needs_no_ref():
    port = _Port()
    executor = _executor(port)
    no_guild = {k: v for k, v in BASE.items() if k != "guild_id"}
    assert executor.execute(_call("opt_out_about_me", {**no_guild, "enabled": True})).error_code == "denied"
    assert port.calls == []
    ok = executor.execute(_call("opt_out_about_me", {**BASE, "enabled": True}))
    assert ok.succeeded and port.calls[0]["args"] == {"enabled": True}
    assert port.calls[0]["context"] == {"guild_id": "123"}


def test_executor_about_me_timeout_marks_unknown():
    from uep.adapters.tool import ToolResponseLost

    port = _Port({"opt_out_about_me": {"success": False, "result": "tool timeout", "code": "timeout"}})
    executor = _executor(port)
    with pytest.raises(ToolResponseLost):
        executor.execute(_call("opt_out_about_me", {**BASE, "enabled": True}))


def test_legacy_tool_definitions_never_expose_about_me():
    class Backend:
        def definitions(self):
            return [{"type": "function", "function": {"name": n, "parameters": {}}}
                    for n in ("deep_recall", "force_remember", *contract.ABOUT_ME_TOOLS)]

    layer = MemoryAccessLayer(
        FakeRuntime(None), IdentityDirectory(platform_to_actor={}, actor_to_owner={}), tool_backend=Backend(),
        guild_membership=FakeMembership(),
    )
    names = [d["function"]["name"] for d in layer.tool_definitions()]
    assert names == ["deep_recall", "force_remember", "member_recall"]
    assert not set(names) & set(contract.ABOUT_ME_TOOLS)


# ---- DB：四階段端到端 ----

@pytest.fixture
def db_rig(session_factory):
    from uep.agent.bridge import AgentToolTurns
    from uep.events.bus import EventBus
    from uep.events.publisher import EventPublisher
    from uep.tasks.manager import TaskManager

    cache = TurnOutcomeCache()
    port = _Port({"member_recall": {
        "success": True, "result": "- 〔旁證1〕乙 提到〈甲〉：x", "about_refs": {1: "proj_a"},
    }})
    executor = _executor(port, cache)
    tm = TaskManager(session_factory, EventPublisher(session_factory, EventBus()),
                     {"core_memory": executor}, tool_resolver=memory_registry())
    # direct_chat 仍只讀；寫入類只在 agent_loop 的標籤集合內才投出，這裡以明示標籤模擬
    turns = AgentToolTurns(tm, memory_registry(), cache, capability_tags={"memory.read", "memory.write"})
    return types.SimpleNamespace(tm=tm, turns=turns, port=port, cache=cache, sf=session_factory)


def _ctx(turn_id, **extra):
    from uep.ids import new_id

    return {"actor_id": "actor_a", "audience": "scope_private_owner", "segment_id": new_id("seg"),
            "ticket_ref": turn_id, "conversation_id": new_id("conv"), "event_scope": "scope_private_owner", **extra}


def _guild_ctx(turn_id):
    return _ctx(turn_id, audience=GROUP, guild_id="123", event_scope=GROUP)


def _rows(sf):
    from uep.tasks.models import ActionIntentRow, ApprovalRow, ReceiptRow

    with sf() as s:
        return (list(s.scalars(select(ActionIntentRow).order_by(ActionIntentRow.created_at))),
                list(s.scalars(select(ReceiptRow))), list(s.scalars(select(ApprovalRow))))


@pytest.mark.usefixtures("session_factory")
def test_db_force_remember_four_stage_has_action_receipt_no_approval(db_rig):
    from uep.ids import is_valid_id, new_id
    from uep.tasks.states import ActionStatus

    turn_id = new_id("turn")
    out = db_rig.turns.run_tool("force_remember", {"content": "使用者喜歡貓", "reason": "明確要求"}, _ctx(turn_id))
    assert out == {"success": True, "result": "force_remember 結果"}
    actions, receipts, approvals = _rows(db_rig.sf)
    assert len(actions) == 1 and is_valid_id(actions[0].action_id, "act")
    assert actions[0].side_effect_class == "reversible_local" and actions[0].status == ActionStatus.SUCCEEDED.value
    assert len(receipts) == 1 and receipts[0].succeeded is True and receipts[0].error_code is None
    assert approvals == []  # approval_level=none：沒有核准列、沒有等待
    assert db_rig.port.calls[0]["args"] == {"content": "使用者喜歡貓", "reason": "明確要求"}
    assert db_rig.port.calls[0]["actor_id"] == "actor_a"
    from uep.events.models import EventRow

    with db_rig.sf() as s:
        events = list(s.scalars(select(EventRow).where(EventRow.turn_id == turn_id, EventRow.event_type == "action.executed")))
    assert len(events) == 1 and events[0].payload["kind"] == "reversible_local"
    db_rig.turns.finish_turn(turn_id)
    from uep.tasks.models import TaskRow

    with db_rig.sf() as s:
        assert s.scalar(select(TaskRow)).status == "SUCCEEDED"  # 沒有停在 WAITING_APPROVAL


@pytest.mark.usefixtures("session_factory")
def test_db_resend_same_action_id_does_not_write_memory_twice(db_rig):
    from uep.agent.core import AgentCore
    from uep.ids import new_id
    from uep.tools.gateway import ToolGateway
    from uep.tools.projection import ProjectionContext

    turn_id = new_id("turn")
    ctx = _ctx(turn_id)
    args = {"content": "使用者喜歡貓", "reason": "明確要求"}
    db_rig.turns.run_tool("force_remember", args, ctx)
    db = db_rig.turns._turns[turn_id].db
    core = AgentCore(None, db_rig.tm, ToolGateway(memory_registry()), worker_epoch=db.epoch,
                     content_lookup=db_rig.cache.text)
    caps = memory_registry().project({"memory.write"}, ProjectionContext())
    injected = {"actor_id": "actor_a", "audience": "scope_private_owner", "segment_id": ctx["segment_id"]}
    with db_rig.cache.turn_scope(turn_id):  # 重送：同 run／step／ordinal -> 同一個 act_
        record, view = core.run_intent(
            run_id=db.run_id, step_id=db.step_id, ordinal=0, capability_set=caps, tool="force_remember",
            args=args, injected=injected,
        )
    assert record.status == "ok" and view["status"] == "ok"
    assert record.action_id == db.action_ids[0]
    assert len(db_rig.port.calls) == 1  # 沒有第二次寫入
    actions, receipts, _ = _rows(db_rig.sf)
    assert len(actions) == 1 and len(receipts) == 1
    db_rig.turns.finish_turn(turn_id)


@pytest.mark.usefixtures("session_factory")
def test_db_failure_records_error_in_receipt_and_action_failed(db_rig):
    from uep.ids import new_id
    from uep.tasks.states import ActionStatus

    db_rig.port.outcomes["force_remember"] = {"success": False, "result": "tool failed", "code": "failed"}
    turn_id = new_id("turn")
    out = db_rig.turns.run_tool("force_remember", {"content": "x", "reason": "y"}, _ctx(turn_id))
    assert out["success"] is False
    actions, receipts, approvals = _rows(db_rig.sf)
    assert actions[0].status == ActionStatus.FAILED.value
    assert receipts[0].succeeded is False and receipts[0].error_code == "failed"
    assert approvals == []
    db_rig.turns.finish_turn(turn_id)


@pytest.mark.usefixtures("session_factory")
def test_db_timeout_marks_write_action_unknown_and_blocks_blind_retry(db_rig):
    from uep.ids import new_id
    from uep.tasks.states import ActionStatus

    db_rig.port.outcomes["force_remember"] = {"success": False, "result": "tool timeout", "code": "timeout"}
    turn_id = new_id("turn")
    ctx = _ctx(turn_id)
    assert db_rig.turns.run_tool("force_remember", {"content": "x", "reason": "y"}, ctx)["success"] is False
    actions, _, _ = _rows(db_rig.sf)
    assert actions[0].status == ActionStatus.UNKNOWN.value  # 副作用不確定：不當成沒寫
    db_rig.port.outcomes["force_remember"] = {"success": True, "result": "ok"}
    again = db_rig.turns.run_tool("force_remember", {"content": "x", "reason": "y"}, ctx)
    assert again["success"] is False  # TC-3：寫入類 UNKNOWN 擋住後續非唯讀呼叫，不盲目重寫
    assert len(db_rig.port.calls) == 1
    db_rig.turns.finish_turn(turn_id)


@pytest.mark.usefixtures("session_factory")
def test_db_about_me_tools_four_stage_with_ref_pipeline(db_rig):
    from uep.ids import new_id
    from uep.tasks.states import ActionStatus

    turn_id = new_id("turn")
    ctx = _guild_ctx(turn_id)
    assert db_rig.turns.run_tool("opt_out_about_me", {"enabled": True}, ctx)["success"] is True
    assert db_rig.turns.run_tool("member_recall", {"member": "自己"}, ctx)["success"] is True
    assert db_rig.turns.run_tool("retract_about_me", {"ref": 1}, ctx)["success"] is True
    assert db_rig.turns.run_tool("correct_about_me", {"ref": 1, "correction_text": "我不玩"}, ctx)["success"] is False
    calls = {c["name"]: c for c in db_rig.port.calls}
    assert calls["retract_about_me"]["context"]["projection_id"] == "proj_a"
    assert "correct_about_me" not in calls  # 未註冊、不可達：沒有呼叫記憶
    assert calls["opt_out_about_me"]["context"] == {"guild_id": "123"}
    actions, receipts, approvals = _rows(db_rig.sf)
    write_actions = [a for a in actions if a.tool != "member_recall"]
    assert {a.tool for a in write_actions} == set(ABOUT_ME_REGISTERED)
    assert all(a.side_effect_class == "reversible_local" and a.status == ActionStatus.SUCCEEDED.value for a in write_actions)
    assert len(receipts) == 3 and all(r.succeeded for r in receipts) and approvals == []
    db_rig.turns.finish_turn(turn_id)


@pytest.mark.usefixtures("session_factory")
def test_db_bridge_about_refs_are_run_scoped_and_cleared_when_turn_ends(db_rig):
    from uep.ids import new_id

    turn_a, turn_b = new_id("turn"), new_id("turn")
    ctx_a, ctx_b = _guild_ctx(turn_a), _guild_ctx(turn_b)
    assert db_rig.turns.run_tool("member_recall", {"member": "自己"}, ctx_a)["success"] is True
    run_a = db_rig.turns._turns[turn_a].db.run_id
    before = len(db_rig.port.calls)
    # 另一個回合（另一個 Run）用同編號：解析不到，不呼叫記憶
    assert db_rig.turns.run_tool("retract_about_me", {"ref": 1}, ctx_b)["success"] is False
    assert len(db_rig.port.calls) == before
    assert db_rig.turns.run_tool("retract_about_me", {"ref": 1}, ctx_a)["success"] is True
    assert db_rig.cache.has_about_refs((run_a, "actor_a")) is True
    db_rig.turns.finish_turn(turn_a)  # Run 終態
    assert db_rig.cache.has_about_refs((run_a, "actor_a")) is False
    db_rig.turns.finish_turn(turn_b)


@pytest.mark.usefixtures("session_factory")
def test_db_about_me_not_offered_without_guild_context(db_rig):
    from uep.ids import new_id

    turn_id = new_id("turn")
    out = db_rig.turns.run_tool("opt_out_about_me", {"enabled": True}, _ctx(turn_id))  # 私人 audience、沒有 guild
    assert out["success"] is False and db_rig.port.calls == []
    actions, receipts, _ = _rows(db_rig.sf)
    assert actions == [] and receipts == []  # 工具不在可見集合：Gateway 拒絕，不建 action
    db_rig.turns.finish_turn(turn_id)


def test_correct_about_me_is_not_registered_nor_projected_anywhere():
    registry = memory_registry()
    assert "correct_about_me" not in registry.entries()
    every_tag = {"memory.read", "memory.write", "decision.consult", "chatroom.post", "lore.write", "desktop.file_write"}
    from uep.tools.projection import ProjectionContext

    projected = registry.project(every_tag, ProjectionContext(available_context=frozenset({"guild_roster"})))
    assert "correct_about_me" not in {t["tool"] for t in projected["tools"]}
    from uep.memory.access import TOOL_CLASSES

    assert "correct_about_me" not in TOOL_CLASSES
    from uep.agent.bridge import DIRECT_CHAT_TAGS
    from uep.decision.capability import ROUTE_DEFAULT_CAPABILITIES

    assert "memory.write" not in DIRECT_CHAT_TAGS  # direct_chat 維持只讀（Q-E）
    assert all("memory.write" not in tags for tags in ROUTE_DEFAULT_CAPABILITIES.values())
