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

WRITE_TOOLS = ("force_remember", *contract.ABOUT_ME_TOOLS)
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
    for name in contract.ABOUT_ME_TOOLS:
        entry = registry.entry(name)
        assert entry.scope.requires_context == frozenset({"guild_roster"}), name
        assert set(entry.descriptor.injected_params) == {"actor_id", "audience", "segment_id", "guild_id"}, name
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


def test_access_retract_and_correct_use_projection_id_from_context_not_args():
    adapter = FakeAdapter()
    layer = make_layer(adapter)
    assert run_about(layer, "retract_about_me", {"ref": 1, "projection_id": "evil"})["success"] is True
    out = run_about(layer, "correct_about_me", {"ref": 1, "correction_text": " 我其實不玩 "})
    assert out["success"] is True
    assert adapter.calls[0] == ("retract_about_me", "guild:123", "111", "proj_1")
    name, scope, uid, pid, text, evidence, session = adapter.calls[1]
    assert (name, scope, uid, pid, text, evidence, session) == (
        "correct_about_me", "guild:123", "111", "proj_1", "我其實不玩", None, "seg_1",
    )


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


def test_access_about_me_missing_projection_or_empty_correction_is_failure_without_write():
    adapter = FakeAdapter()
    layer = make_layer(adapter)
    assert run_about(layer, "retract_about_me", {"ref": 1}, projection_id="")["result"] == contract.ABOUT_BAD_REF
    assert "空的" in run_about(layer, "correct_about_me", {"ref": 1, "correction_text": "  "})["result"]
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


BASE = {"actor_id": "actor_a", "audience": GROUP, "segment_id": "seg_1", "guild_id": "123"}


def test_executor_resolves_ref_from_turn_scoped_about_refs():
    port = _Port({"member_recall": {
        "success": True, "result": "- 〔旁證1〕乙 提到〈甲〉：x\n- 〔旁證2〕丙 提到〈甲〉：y", "about_refs": {1: "proj_a", 2: "proj_b"},
    }})
    cache = TurnOutcomeCache()
    executor = _executor(port, cache)
    with cache.turn_scope("turn_1"):
        executor.execute(_call("member_recall", {**BASE, "member": "自己"}))
        receipt = executor.execute(_call("retract_about_me", {**BASE, "ref": 2}))
    assert receipt.succeeded and receipt.meta == {"status": "executed"}
    assert port.calls[1]["name"] == "retract_about_me"
    assert port.calls[1]["context"] == {"guild_id": "123", "projection_id": "proj_b"}
    assert port.calls[1]["args"] == {"ref": 2}
    assert "about_refs" not in (cache.get("act_x") or {})
    cache.discard_turn("turn_1")
    assert cache.about_ref(2) is None  # 回合結束清掉


def test_executor_ref_without_prior_member_recall_or_unknown_ref_fails_without_calling_port():
    port = _Port({"member_recall": {"success": True, "result": "- 〔旁證1〕乙 提到〈甲〉：x", "about_refs": {1: "proj_a"}}})
    cache = TurnOutcomeCache()
    executor = _executor(port, cache)
    with cache.turn_scope("turn_1"):
        none_yet = executor.execute(_call("correct_about_me", {**BASE, "ref": 1, "correction_text": "z"}))
        executor.execute(_call("member_recall", {**BASE, "member": "自己"}))
        unknown = executor.execute(_call("correct_about_me", {**BASE, "ref": 9, "correction_text": "z"}))
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
    with cache.turn_scope("turn_1"):
        executor.execute(_call("member_recall", {**BASE, "member": "自己"}))
        assert cache.about_ref(1) == "a"
        assert cache.about_ref(2) is None and cache.about_ref(3) is None  # 被截斷的半行不登記


def test_executor_about_me_requires_guild_and_opt_out_needs_no_ref():
    port = _Port()
    executor = _executor(port)
    no_guild = {k: v for k, v in BASE.items() if k != "guild_id"}
    assert executor.execute(_call("opt_out_about_me", {**no_guild, "enabled": True})).error_code == "denied"
    assert port.calls == []
    ok = executor.execute(_call("opt_out_about_me", {**BASE, "enabled": True}))
    assert ok.succeeded and port.calls[0]["args"] == {"enabled": True}
    assert port.calls[0]["context"] == {"guild_id": "123"}


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
    turns = AgentToolTurns(tm, memory_registry(), cache)
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
    assert db_rig.turns.run_tool("correct_about_me", {"ref": 1, "correction_text": "我不玩"}, ctx)["success"] is True
    calls = {c["name"]: c for c in db_rig.port.calls}
    assert calls["retract_about_me"]["context"]["projection_id"] == "proj_a"
    assert calls["correct_about_me"]["args"] == {"ref": 1, "correction_text": "我不玩"}
    assert calls["opt_out_about_me"]["context"] == {"guild_id": "123"}
    actions, receipts, approvals = _rows(db_rig.sf)
    write_actions = [a for a in actions if a.tool != "member_recall"]
    assert {a.tool for a in write_actions} == set(contract.ABOUT_ME_TOOLS)
    assert all(a.side_effect_class == "reversible_local" and a.status == ActionStatus.SUCCEEDED.value for a in write_actions)
    assert len(receipts) == 4 and all(r.succeeded for r in receipts) and approvals == []
    db_rig.turns.finish_turn(turn_id)


@pytest.mark.usefixtures("session_factory")
def test_db_about_me_not_offered_without_guild_context(db_rig):
    from uep.ids import new_id

    turn_id = new_id("turn")
    out = db_rig.turns.run_tool("opt_out_about_me", {"enabled": True}, _ctx(turn_id))  # 私人 audience、沒有 guild
    assert out["success"] is False and db_rig.port.calls == []
    actions, receipts, _ = _rows(db_rig.sf)
    assert actions == [] and receipts == []  # 工具不在可見集合：Gateway 拒絕，不建 action
    db_rig.turns.finish_turn(turn_id)
