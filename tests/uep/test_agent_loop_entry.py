"""route=agent_loop 的 production 入口（DL-4 PR4）：enabled_tags、capability_tags_for、組裝旗標與雙重回覆鎖定。

離線部分不需要 DB；``test_db_*`` 需 PostgreSQL（沒有 UEP_TEST_DATABASE_URL 時自動 skip）。
"""
from __future__ import annotations

import threading
from collections import Counter
from types import SimpleNamespace

import pytest

from tests.uep.test_tool_registry import make_entry
from uep.adapters.tool import CAPABILITY_CEILING
from uep.assembly import AssemblyError, assemble_core
from uep.decision.capability import (
    COUNT_HINT_DROPPED,
    COUNT_HINT_EMPTY_MODEL,
    REASON_CAPABILITY_EMPTY,
    narrow_capability,
)
from uep.echo.agent_wiring import LoopEntry, make_capability_tags_for
from uep.tools.catalog.memory import memory_registry
from uep.tools.registry import ToolRegistry
from uep.turn.models import Proposal, ReplyMode

TICKET = SimpleNamespace(turn_id="turn_x")


def _proposal(route="agent_loop", hint=None) -> Proposal:
    return Proposal(reply_mode=ReplyMode.RESPOND, route=route, capability_hint=hint)


# ---- ToolRegistry.enabled_tags ----

def test_enabled_tags_of_production_registry_is_inside_the_closed_set():
    tags = memory_registry().enabled_tags()
    assert tags and tags <= frozenset(CAPABILITY_CEILING)
    assert "memory.read" in tags


def test_enabled_tags_excludes_disabled_and_unavailable_but_keeps_warming():
    reg = ToolRegistry.build(
        [
            make_entry("read.ok", spec={"capability": "memory.read"}),
            make_entry("write.off", spec={"capability": "memory.write", "side_effect_class": "reversible_local"}, disabled=True),
            make_entry("lore.gone", spec={"capability": "lore.write", "side_effect_class": "reversible_local"}, requires=("gone",)),
            make_entry("chat.warm", spec={"capability": "chatroom.read"}, requires=("gpu:x",), warm_required=True),
        ],
        known_modules=["gpu:x"],
    )
    # disabled 不算；requires 未知 -> unavailable 不算；warming 算（在投影裡是 pending，等轉 ready）
    assert reg.readiness("chat.warm") == "warming" and reg.readiness("lore.gone") == "unavailable"
    assert reg.enabled_tags() == frozenset({"memory.read", "chatroom.read"})
    reg.apply_module_event("module.state_changed", {"module_id": "gpu:x", "revision": 1, "readiness": "unavailable"})
    assert reg.enabled_tags() == frozenset({"memory.read"})


def test_enabled_tags_does_not_mutate_or_read_outside_memory():
    reg = memory_registry()
    assert reg.enabled_tags() == reg.enabled_tags()


# ---- capability_tags_for（交給 narrow_capability） ----

def test_tags_for_agent_loop_is_route_table_intersect_enabled():
    metrics: Counter[str] = Counter()
    tags_for = make_capability_tags_for(memory_registry(), metrics)
    # 生產註冊表尚未註冊 decision.consult 工具 -> consult 不在快照內 -> 只剩 memory.read
    assert tags_for(TICKET, _proposal()) == frozenset({"memory.read"})
    reg = ToolRegistry.build([
        make_entry("read.ok", spec={"capability": "memory.read"}),
        make_entry("consult", spec={"capability": "decision.consult"}),
    ])
    assert make_capability_tags_for(reg)(TICKET, _proposal()) == frozenset({"memory.read", "decision.consult"})


def test_tags_for_hint_only_narrows_and_never_widens():
    reg = ToolRegistry.build([
        make_entry("read.ok", spec={"capability": "memory.read"}),
        make_entry("consult", spec={"capability": "decision.consult"}),
        make_entry("write.s", spec={"capability": "memory.write", "side_effect_class": "reversible_local"}),
    ])
    metrics: Counter[str] = Counter()
    tags_for = make_capability_tags_for(reg, metrics)
    # hint 要求 memory.write（表外於 agent_loop base）-> 不會新增；consult 保留 -> 只剩 consult -> 視為空
    assert tags_for(TICKET, _proposal(hint=("memory.write",))) == frozenset()
    assert tags_for(TICKET, _proposal(hint=("memory.read",))) == frozenset({"memory.read", "decision.consult"})
    assert tags_for(TICKET, _proposal(hint=("memory.read", "no.such.tag"))) == frozenset({"memory.read", "decision.consult"})
    assert metrics[COUNT_HINT_DROPPED] == 1  # 表外標籤被丟棄並計數


def test_tags_for_unknown_route_is_empty_and_loop_entry_does_not_launch(caplog):
    launched: list = []
    entry = LoopEntry(
        "agent", capability_tags_for=make_capability_tags_for(memory_registry()),
        launch=lambda trig, t: launched.append(trig), policy_ref="p", owner_for=lambda t: "o",
    )
    assert make_capability_tags_for(memory_registry())(TICKET, _proposal(route="no_such_route")) == frozenset()
    assert entry(SimpleNamespace(turn_id="t", audience_scope="a", snapshot_version=1), _proposal(route="direct_chat")) is None
    assert launched == []


def test_tags_for_empty_snapshot_fails_closed_with_reason():
    result = narrow_capability(
        frozenset({"memory.read", "decision.consult"}), None, frozenset(), CAPABILITY_CEILING.keys(), route="agent_loop",
    )
    assert result.empty and result.route == "direct_chat" and result.reason == REASON_CAPABILITY_EMPTY


def test_model_empty_hint_is_dropped_and_counted():
    """D4-6 由 narrow_capability 負責；Proposal 目前沒有 hint 來源欄位，wiring 一律以 rule 處理（空 hint 要求空集合）。"""
    result = narrow_capability(
        frozenset({"memory.read", "decision.consult"}), (), frozenset({"memory.read", "decision.consult"}),
        CAPABILITY_CEILING.keys(), route="agent_loop", hint_source="model",
    )
    assert result.tags == frozenset({"memory.read", "decision.consult"}) and result.counters()[COUNT_HINT_EMPTY_MODEL] == 1


# ---- 旗標與組裝 ----

def test_flag_defaults_off_and_legacy_refuses_to_enable():
    from uep.config import UepSettings

    assert UepSettings.model_fields["agent_loop_entry"].default is False


def test_assemble_refuses_agent_loop_entry_under_legacy(session_factory, tmp_path):
    from tests.uep.test_assembly import kwargs

    with pytest.raises(AssemblyError, match="UEP_AGENT_LOOP_ENTRY"):
        assemble_core(session_factory, **kwargs(session_factory, tmp_path, agent_loop_entry=True))


# ---- orchestrator 認領 ----

def test_orchestrator_claims_only_when_entry_returns_trigger():
    from uep.turn.orchestrator import TurnOrchestrator

    orch = TurnOrchestrator.__new__(TurnOrchestrator)
    orch._lock = threading.Lock()
    orch._loop_claims = __import__("collections").OrderedDict()
    orch.metrics = Counter()
    ticket = SimpleNamespace(turn_id="t1")
    orch._loop_entry = lambda t, p: None
    orch._enter_loop(ticket, _proposal())
    assert not orch.loop_claimed("t1")
    orch._loop_entry = lambda t, p: object()
    orch._enter_loop(ticket, _proposal())
    assert orch.loop_claimed("t1") and orch.metrics["turn.loop_claimed"] == 1
    boom_ticket = SimpleNamespace(turn_id="t2")

    def boom(t, p):
        raise RuntimeError("x")

    orch._loop_entry = boom
    orch._enter_loop(boom_ticket, _proposal())  # 例外：回合退回 direct_chat，不認領
    assert not orch.loop_claimed("t2") and orch.metrics["turn.loop_entry_failed"] == 1
    orch._enter_loop(SimpleNamespace(turn_id="t3"), _proposal(route="direct_chat"))
    assert not orch.loop_claimed("t3")


# ---- 啟動器（離線）：UNKNOWN 不算已交付、投影預檢、並發上限、取消判定 ----

def _stub_launcher(registry=None, delivery=None, **kw):
    from uep.echo.agent_wiring import LoopLauncher
    from uep.echo.loop_text import LoopTextStore

    orch = SimpleNamespace(
        metrics=Counter(), cancel_reason_of=lambda t: None, is_ended=lambda t: False, on_turn_ended=lambda *a: None,
    )
    tasks = SimpleNamespace(
        create_task=lambda **k: pytest.fail("不應建 Task"), start_run=lambda *a, **k: pytest.fail("不應建 Run"),
    )
    return LoopLauncher(
        tasks=tasks, delivery=delivery, orchestrator=orch, registry=registry or memory_registry(), model_port=object(),
        text_store=LoopTextStore(), **kw,
    )


def _ticket():
    from datetime import UTC, datetime, timedelta

    auth = SimpleNamespace(
        persona_id="uep", endpoint_id="ep", lease_id="l", fencing_epoch=1, core_epoch=1, presence_generation=1,
    )
    return SimpleNamespace(
        turn_id="turn_u", auth=auth, turn_generation=1, audience_scope="scope_private_owner", conversation_id="c",
        segment_id="s", user_text="hi", actor_id="a", snapshot_version=1,
        valid_until=datetime.now(UTC) + timedelta(minutes=1),
    )


def _trigger():
    from uep.agent.trigger import LoopTrigger

    return LoopTrigger("user_turn", "t", "g", frozenset({"memory.read"}), "scope_private_owner", "o", "p", "snap_1")


def test_unknown_delivery_is_not_delivered():
    from uep.echo.agent_wiring import _RunCtx
    from uep.output.delivery import DeliveryStatus

    sent: list = []

    class Delivery:
        def prepare(self, intent):
            sent.append(intent)

        def dispatch(self, intent):
            return SimpleNamespace(status=DeliveryStatus.UNKNOWN)

    launcher = _stub_launcher(delivery=Delivery())
    ctx = _RunCtx(_ticket(), "task", SimpleNamespace(run_id="run_u", worker_epoch=1))
    assert launcher._deliver(ctx, "答案") is False  # 結果未明 -> 不算已交付，也不補發失敗訊息
    assert ctx.delivery_uncertain == "loop_delivery_unknown" and not ctx.delivered
    assert len(sent) == 1 and sent[0].kind.value == "text" and sent[0].origin.value == "persona"
    assert launcher._texts.get(sent[0].content_ref) == "答案"


def test_projection_precheck_refuses_before_creating_task_when_all_warming():
    from uep.echo.agent_wiring import LoopLaunchRefused

    reg = ToolRegistry.build(
        [make_entry("read.warm", spec={"capability": "memory.read"}, requires=("gpu:x",), warm_required=True)],
        known_modules=["gpu:x"],
    )
    launcher = _stub_launcher(registry=reg)
    with pytest.raises(LoopLaunchRefused, match="projection_empty"):
        launcher(_trigger(), _ticket())  # tasks 的 create_task／start_run 會 pytest.fail：沒有建任何東西
    assert launcher.active == 0  # 名額已釋放


def test_concurrency_limit_refuses_before_creating_task():
    from uep.echo.agent_wiring import LoopLaunchRefused

    launcher = _stub_launcher(max_concurrent=1)
    launcher._active = 1
    with pytest.raises(LoopLaunchRefused, match="loop_busy"):
        launcher(_trigger(), _ticket())
    assert launcher.active == 1


def test_cancelled_uses_public_orchestrator_state_and_closing_flag():
    state = {"ended": False, "reason": None}
    launcher = _stub_launcher()
    launcher._orchestrator.is_ended = lambda t: state["ended"]
    launcher._orchestrator.cancel_reason_of = lambda t: state["reason"]
    assert launcher._cancelled("t") is False
    state["reason"] = "presence_lost"
    assert launcher._cancelled("t") is True
    state.update(reason=None, ended=True)
    assert launcher._cancelled("t") is True
    state["ended"] = False
    launcher._closing.set()
    assert launcher._cancelled("t") is True


def test_loop_text_store_is_bounded_and_discards_by_turn():
    from uep.echo.loop_text import LoopTextStore

    store = LoopTextStore(max_entries=2)
    store.put("a", "t1", "x")
    store.put("b", "t1", "y")
    store.put("c", "t2", "z")
    assert store.get("a") is None and store.get("c") == "z"
    store.discard_turn("t1")
    assert len(store) == 1


def test_in_memory_store_discard_run_clears_child_and_parent_entries():
    from uep.agent.loop_context import ParentSnapshot, fork_child, merge_back
    from uep.fakes.loop_context import InMemoryLoopContextStore

    store = InMemoryLoopContextStore()
    fork_child(store, "run_1", parent=ParentSnapshot("p", 0), system_text="s", goal_code="g", persona_version=1, scope_ref="x")
    merge_back(store, run_id="run_1", parent_id="p", scope_ref="x", state="completed", reason_code=None, summary="a", result="a")
    assert store.children and store.parent_entries
    store.discard_run("run_1")
    assert not store.children and not store.parent_entries and not store._keys


def test_host_tolerates_orchestrator_without_loop_claimed():
    """舊式替身 orchestrator 沒有 loop_claimed：宿主用 getattr 防禦，視為未認領（照常啟動 ES）。"""
    from uep.echo.host import EchoPipelineHost

    started: list = []
    host = EchoPipelineHost.__new__(EchoPipelineHost)
    host.orchestrator = SimpleNamespace()
    host._es = SimpleNamespace(call_soon=lambda fn, *a: started.append(a))
    host.metrics = SimpleNamespace(inc=lambda *a: None)
    item = SimpleNamespace(ticket=SimpleNamespace(turn_id="t"), fact=object())
    host._dispatch_results([item], None)
    assert len(started) == 1


@pytest.mark.parametrize("name", [s.name for s in __import__("uep.output.delivery", fromlist=["DeliveryStatus"]).DeliveryStatus])
def test_delivered_is_a_whitelist_over_every_delivery_status(name):
    """只有 SENT／ACCEPTED／RENDERED_PLAYED 算已交付；UNKNOWN／ENQUEUED 不確定（不補發失敗訊息）；其餘走失敗路徑並補發系統訊息。"""
    from uep.echo.agent_wiring import _RunCtx
    from uep.output.delivery import DeliveryStatus

    status = DeliveryStatus[name]
    sent: list = []

    class Delivery:
        def prepare(self, intent):
            sent.append(intent)

        def dispatch(self, intent):
            return SimpleNamespace(status=status)

    launcher = _stub_launcher(delivery=Delivery())
    ctx = _RunCtx(_ticket(), "task", SimpleNamespace(run_id="run_u", worker_epoch=1))
    delivered = launcher._deliver(ctx, "答案")
    assert delivered is (status in {DeliveryStatus.SENT, DeliveryStatus.ACCEPTED, DeliveryStatus.RENDERED_PLAYED})
    assert ctx.delivered is delivered
    expect_uncertain = {DeliveryStatus.UNKNOWN: "loop_delivery_unknown", DeliveryStatus.ENQUEUED: "loop_delivery_pending"}
    assert ctx.delivery_uncertain == expect_uncertain.get(status)
    # 收尾：Run／Task 以 stub 處理；失敗路徑補發一則系統訊息，已交付與不確定的都不補發
    launcher._settle = lambda *a, **k: None
    sent.clear()
    facts = launcher._conclude(ctx, SimpleNamespace(state="completed" if delivered else "failed", reason_code=None), False)
    if delivered:
        assert facts.status == "completed" and sent == []
    elif ctx.delivery_uncertain is not None:
        assert facts.status == "failed" and facts.reason_code == ctx.delivery_uncertain and sent == []
    else:
        assert facts.status == "failed" and len(sent) == 1
        assert sent[0].kind.value == "system_notice" and sent[0].origin.value == "system"


def test_parked_run_sends_no_failure_notice_and_uses_dedicated_reason():
    from uep.echo.agent_wiring import LOOP_PARKED_REASON, _RunCtx

    sent: list = []

    class Delivery:
        def prepare(self, intent):
            sent.append(intent)

        def dispatch(self, intent):
            pytest.fail("park 時不該交付任何東西")

    launcher = _stub_launcher(delivery=Delivery())
    launcher._settle = lambda *a, **k: pytest.fail("park 的 Run 保留，不收尾")
    ctx = _RunCtx(_ticket(), "task", SimpleNamespace(run_id="run_u", worker_epoch=1))
    facts = launcher._conclude(ctx, SimpleNamespace(state="awaiting_approval", reason_code="approval_required"), False)
    assert (facts.status, facts.reason_code) == ("failed", LOOP_PARKED_REASON) and LOOP_PARKED_REASON == "loop_parked_awaiting_approval"
    assert sent == []


def test_run_deadline_is_loop_budget_plus_supervisor_grace():
    from datetime import UTC, datetime, timedelta

    from uep.agent.budget import LoopBudget
    from uep.tasks.supervisor import DEFAULT_DEADLINE_GRACE

    launcher = _stub_launcher(budget=LoopBudget(run_deadline_s=100.0))
    before = datetime.now(UTC)
    deadline = launcher._run_deadline()
    assert before + timedelta(seconds=100) + DEFAULT_DEADLINE_GRACE <= deadline <= datetime.now(UTC) + timedelta(seconds=100) + DEFAULT_DEADLINE_GRACE
    unbounded = _stub_launcher(budget=LoopBudget(run_deadline_s=None))
    assert unbounded._run_deadline() > datetime.now(UTC) + timedelta(seconds=500)  # 沒有牆鐘上限：保底值，仍有收尾者


def test_endpoint_send_replay_is_idempotent_before_resolving_content_ref():
    """同一 dispatch_id 重播：先查冪等紀錄，不再解析 content_ref（文字可能已清除）、不重複渲染。"""
    from collections import OrderedDict, deque

    from uep.adapters.output import SendResult
    from uep.contracts.presence import GateDecision
    from uep.echo.endpoint import EchoEndpoint
    from uep.output.delivery import DeliveryEvidence
    from uep.output.intent import OutputIntent, OutputKind, OutputOrigin

    endpoint = EchoEndpoint.__new__(EchoEndpoint)
    endpoint._lock = threading.RLock()
    endpoint._sent = OrderedDict()
    endpoint._texts = deque()
    calls: list = []
    endpoint._text_resolver = lambda ref: calls.append(ref)  # 解析器被呼叫就記下來（並回 None -> 會是 SendFailed）
    endpoint._on_text = None
    previous = SendResult("d1", DeliveryEvidence.TRANSPORT_SENT, __import__("datetime").datetime.now(__import__("datetime").UTC))
    endpoint._sent["d1"] = previous
    t = _ticket()
    a = t.auth
    intent = OutputIntent(
        output_id="out_1", sequence=0, kind=OutputKind.TEXT, origin=OutputOrigin.PERSONA, persona_id=a.persona_id,
        endpoint_id=a.endpoint_id, lease_id=a.lease_id, fencing_epoch=1, core_epoch=1, presence_generation=1,
        turn_id=t.turn_id, turn_generation=1, attempt_id="att_1", audience_scope=t.audience_scope, content_ref="loop:gone",
        valid_until=t.valid_until,
    )
    assert endpoint.send(intent, "d1") is previous and calls == [] and len(endpoint._texts) == 0
    assert GateDecision.ADMIT  # 僅確認匯入可用


# ---- parallel_one_shot：executors 傳進 AgentLoop（逾時／取消要能 cancel）----

def test_launcher_passes_executors_to_agent_loop():
    from uep.echo.agent_wiring import _RunCtx

    sentinel = object()
    launcher = _stub_launcher(executors={"x": sentinel})
    ctx = _RunCtx(_ticket(), "task", SimpleNamespace(run_id="run_u", worker_epoch=1))
    assert launcher._build_loop(ctx)._executors == {"x": sentinel}


def test_assembly_rejects_one_shot_registry_without_cancellable_executor():
    from tests.uep.test_agent_loop import one_shot_registry
    from uep.assembly import AssemblyError, _check_loop_one_shot_executors
    from uep.fakes.tool import FakeToolExecutor

    registry = one_shot_registry()
    with pytest.raises(AssemblyError, match="once.a"):
        _check_loop_one_shot_executors(registry, {})  # AgentLoop 拿不到 executor
    with pytest.raises(AssemblyError, match="once.b"):
        _check_loop_one_shot_executors(registry, {"fake": object()})  # 沒有 cancel
    _check_loop_one_shot_executors(registry, {"fake": FakeToolExecutor()})
    _check_loop_one_shot_executors(memory_registry(), {})  # production 目錄沒有 one_shot：不要求


# ---- 卡：agent_loop turn 收尾寫記憶（與 direct_chat 同一條路徑）----

def _run_work(launcher, facts, cancel_reason=None):
    from uep.echo.agent_wiring import _RunCtx

    launcher._tasks.heartbeat = lambda *a, **k: None
    launcher._orchestrator.cancel_reason_of = lambda t: cancel_reason
    launcher._build_loop = lambda ctx: SimpleNamespace(run=lambda *a, **k: None)
    launcher._conclude = lambda ctx, outcome, raised: facts
    ctx = _RunCtx(_ticket(), "task", SimpleNamespace(run_id="run_m", worker_epoch=1))
    launcher._work(_trigger(), ctx)


def test_completed_agent_loop_turn_is_stored_to_memory():
    from uep.turn.models import TurnEndFacts

    stored: list = []
    launcher = _stub_launcher(memory_store=lambda ticket, facts: stored.append((ticket.turn_id, facts.spoken_text)))
    _run_work(launcher, TurnEndFacts(status="completed", generation_ok=True, spoken_text="答案"))
    assert stored == [("turn_u", "答案")]


def test_rejected_agent_loop_turn_is_not_stored_and_store_failure_is_tolerated():
    from uep.turn.models import TurnEndFacts
    from uep.turn.orchestrator import RETRACT_REASONS

    stored: list = []
    launcher = _stub_launcher(memory_store=lambda ticket, facts: stored.append(1))
    _run_work(launcher, TurnEndFacts(status="completed", generation_ok=True, spoken_text="答案"), next(iter(RETRACT_REASONS)))
    assert stored == []  # 被拒／撤銷的輪不寫記憶（與 direct_chat 一致）

    def boom(ticket, facts):
        raise RuntimeError("store down")

    launcher = _stub_launcher(memory_store=boom)
    _run_work(launcher, TurnEndFacts(status="completed", generation_ok=True, spoken_text="答案"))  # 不得外洩例外


def test_host_store_turn_memory_uses_direct_chat_store_path():
    from uep.echo.host import EchoPipelineHost
    from uep.turn.models import TurnEndFacts

    calls: list = []
    host = EchoPipelineHost.__new__(EchoPipelineHost)
    host._store_memory = lambda ticket, facts, ctx: calls.append((ticket.turn_id, facts.spoken_text, ctx))
    host.store_turn_memory(_ticket(), TurnEndFacts(status="completed", generation_ok=True, spoken_text="x"))
    assert calls == [("turn_u", "x", {})]
