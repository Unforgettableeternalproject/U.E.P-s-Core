"""測試用工具組（UEP_TOOL_TESTKIT）：宣告、executor、AgentLoop（假 Port、離線）與開關。

離線（不需 DB）：宣告與投影、executor 單元、以真實 ``TestkitToolExecutor`` 驅動的 AgentLoop
（多輪、並行、取消、逾時、核准 park／resume、開關關閉）。真實 TaskManager／組裝層／Console 的案例在 ``test_tool_testkit_db.py``。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from tests.uep.loop_support import FakeTasks, Round, projector_for
from tests.uep.test_agent_loop import RUN, Rig, _crash, _park_checkpoint, trigger
from uep.adapters.tool import CAPABILITY_CEILING, ToolCall, supports_cancel
from uep.agent.budget import LoopBudget
from uep.agent.core import stable_action_id
from uep.echo.agent_wiring import with_extra_capability_tags
from uep.tasks.manager import ActionOutcome
from uep.tasks.states import ActionStatus
from uep.tools.catalog.memory import memory_registry
from uep.tools.catalog.testkit import (
    SLEEP_MAX_S,
    SLEEP_TIMEOUT_S,
    TEST_APPROVAL_TAG,
    TEST_READ_TAG,
    TESTKIT_EXECUTOR,
    TESTKIT_TAGS,
    TESTKIT_TOOLS,
    TOOL_APPROVAL_ECHO,
    TOOL_CLOCK,
    TOOL_CLOCK_ECHO,
    TOOL_ECHO,
    TOOL_ECHO_THEN_APPROVE,
    TOOL_SLEEP,
    with_testkit,
)
from uep.tools.catalog.testkit import (
    testkit_entries as kit_entries,
)
from uep.tools.executors.testkit import APPROVAL_NOTE, TestkitToolExecutor
from uep.tools.gateway import ToolGateway
from uep.tools.lockfile import current_registry
from uep.tools.projection import ProjectionContext
from uep.tools.registry import ToolRegistry, load_lockfile

REGISTRY = with_testkit(memory_registry())
CONSOLE = ProjectionContext.for_platform("console")  # testkit 屬 console 平台工具
ALL_TAGS = frozenset(TESTKIT_TAGS)


def names(capability_set) -> list[str]:
    return [t["tool"] for t in capability_set["tools"]]


def kit_call(tool: str, params: dict, action_id: str = "act_1", registry: ToolRegistry = REGISTRY) -> ToolCall:
    spec = registry.entry(tool).spec
    return ToolCall(
        action_id=action_id, tool=tool, params=params, params_hash="h", side_effect_class=spec.side_effect_class,
        idempotency=spec.idempotency,
    )


def result_of(kit: TestkitToolExecutor, action_id: str) -> dict:
    return json.loads(kit.cache.text(action_id))


# ───────────────────────── 宣告 ─────────────────────────

def test_testkit_entries_are_builtin_core_local_console_only_with_full_schemas():
    entries = {e.name: e for e in kit_entries()}
    assert tuple(entries) == TESTKIT_TOOLS
    for entry in entries.values():
        assert (entry.spec.origin, entry.spec.targeting, entry.spec.provider) == ("builtin", "core_local", "core")
        assert entry.scope.platforms == frozenset({"console"}) and entry.scope.presence == "any"  # 僅 console 平台
        assert entry.descriptor.input_schema["additionalProperties"] is False
        assert entry.output_schema is not None and entry.output_schema["additionalProperties"] is False
        assert entry.spec.executor == TESTKIT_EXECUTOR and entry.descriptor.injected_params == ()
    assert [(e.spec.side_effect_class, e.concurrency) for e in (entries[TOOL_ECHO], entries[TOOL_CLOCK])] == [
        ("internal_read", "parallel_safe")] * 2
    sleep = entries[TOOL_SLEEP]
    assert (sleep.concurrency, sleep.spec.interruptible, sleep.cancellable) == ("parallel_one_shot", "interruptible", True)
    assert sleep.timeout_s == SLEEP_TIMEOUT_S < SLEEP_MAX_S  # 睡超過總時限才會觸發逾時路徑
    assert sleep.descriptor.input_schema["properties"]["seconds"]["maximum"] == SLEEP_MAX_S
    approval = entries[TOOL_APPROVAL_ECHO]
    assert (approval.spec.side_effect_class, approval.spec.approval_level, approval.concurrency) == (
        "external_effect", "per_action", "serial")
    assert "沒有任何外部效果" in approval.descriptor.description and "NO real external effect" in approval.descriptor.description


def test_testkit_tags_are_in_the_closed_set_with_ceilings():
    assert CAPABILITY_CEILING[TEST_READ_TAG] == "internal_read"
    assert CAPABILITY_CEILING[TEST_APPROVAL_TAG] == "external_effect"
    assert {e.spec.capability for e in kit_entries()} == set(TESTKIT_TAGS)


def test_testkit_is_in_lockfile_and_registry_builds_against_it():
    lock = load_lockfile()
    assert set(TESTKIT_TOOLS) <= set(lock)
    ToolRegistry.build(kit_entries(), lockfile=lock)
    assert set(TESTKIT_TOOLS) <= set(current_registry().entries())


def test_with_testkit_is_additive_idempotent_and_leaves_the_memory_registry_untouched():
    base = memory_registry()
    assert not set(TESTKIT_TOOLS) & set(base.entries())
    assert with_testkit(REGISTRY) is REGISTRY
    assert list(REGISTRY.entries())[: len(base.entries())] == list(base.entries())
    for name, entry in base.entries().items():  # 既有條目的宣告與 hash 不變
        assert REGISTRY.entry(name).schema_hash == entry.schema_hash and REGISTRY.entry(name).spec == entry.spec


# ───────────────────────── 開關：投影與標籤 ─────────────────────────

def test_projection_offers_testkit_tools_only_when_registered():
    ctx = CONSOLE
    assert names(REGISTRY.project({TEST_READ_TAG}, ctx)) == [TOOL_ECHO, TOOL_CLOCK, TOOL_SLEEP, TOOL_CLOCK_ECHO]
    assert names(REGISTRY.project({TEST_APPROVAL_TAG}, ctx)) == [TOOL_APPROVAL_ECHO, TOOL_ECHO_THEN_APPROVE]
    assert not set(TESTKIT_TOOLS) & set(names(REGISTRY.project({"memory.read", "memory.write"}, ctx)))
    assert TESTKIT_TAGS <= REGISTRY.enabled_tags()


def test_without_the_switch_no_projection_or_snapshot_contains_testkit():
    base = memory_registry()  # 開關關閉＝組裝層不併入測試工具，執行期註冊表就是記憶註冊表
    assert not TESTKIT_TAGS & base.enabled_tags()
    assert names(base.project(ALL_TAGS | {"memory.read"}, CONSOLE)) == ["deep_recall"]
    with pytest.raises(Exception, match="未註冊"):
        base.entry(TOOL_ECHO)


def test_extra_tags_are_added_after_narrowing_only_for_agent_loop_and_only_when_nonempty():
    base = frozenset({"memory.read", "decision.consult"})
    route = {"route": "agent_loop"}
    ticket = SimpleNamespace(turn_id="t1")

    def tags_for(_ticket, proposal):
        return base if proposal.route == "agent_loop" else frozenset()

    wrapped = with_extra_capability_tags(tags_for, ALL_TAGS, REGISTRY)
    assert wrapped(ticket, SimpleNamespace(**route)) == base | ALL_TAGS
    assert wrapped(ticket, SimpleNamespace(route="direct_chat")) == frozenset()  # 空即不啟動，不被繞過
    nonloop = with_extra_capability_tags(lambda *_: base, ALL_TAGS, REGISTRY)
    assert nonloop(ticket, SimpleNamespace(route="direct_chat")) == base  # 非 agent_loop 不併入
    # 註冊表沒有測試工具時（開關未開）即使被要求也不會帶出沒有工具的標籤
    assert with_extra_capability_tags(tags_for, ALL_TAGS, memory_registry())(ticket, SimpleNamespace(**route)) == base
    # 閉集外的標籤不併入
    assert with_extra_capability_tags(tags_for, frozenset({"bogus.tag"}), REGISTRY)(ticket, SimpleNamespace(**route)) == base


# ───────────────────────── executor 單元 ─────────────────────────

def test_echo_returns_the_text_and_replay_does_not_run_twice():
    kit = TestkitToolExecutor()
    receipt = kit.execute(kit_call(TOOL_ECHO, {"text": "你好 hi"}))
    assert receipt.succeeded and receipt.error_code is None and receipt.meta["status"] == "executed"
    assert result_of(kit, "act_1") == {"echo": "你好 hi", "length": 5}
    assert kit.execute(kit_call(TOOL_ECHO, {"text": "你好 hi"})) is receipt and kit.executions == ["act_1"]
    assert kit.lookup_receipt("act_1").outcome == "found" and kit.lookup_receipt("nope").outcome == "undetermined"
    assert kit.lookup_authoritative is False


def test_clock_returns_utc_and_local_time():
    fixed = datetime(2026, 11, 11, 3, 4, 5, tzinfo=UTC)
    kit = TestkitToolExecutor(clock=lambda: fixed)
    assert kit.execute(kit_call(TOOL_CLOCK, {})).succeeded
    body = result_of(kit, "act_1")
    assert body["utc"] == "2026-11-11T03:04:05+00:00" and datetime.fromisoformat(body["local"]) == fixed
    assert set(body) == {"utc", "local", "timezone"}


def test_approval_echo_executes_without_any_external_effect_and_says_so():
    kit = TestkitToolExecutor()
    assert kit.execute(kit_call(TOOL_APPROVAL_ECHO, {"message": "m"})).succeeded
    assert result_of(kit, "act_1") == {"echo": "m", "note": APPROVAL_NOTE}


@pytest.mark.parametrize("tool,params", [
    (TOOL_ECHO, {}), (TOOL_ECHO, {"text": 1}), (TOOL_APPROVAL_ECHO, {"message": "x" * 501}),
    (TOOL_SLEEP, {"seconds": -1}), (TOOL_SLEEP, {"seconds": SLEEP_MAX_S + 1}), (TOOL_SLEEP, {"seconds": True}),
])
def test_invalid_arguments_fail_closed_with_a_failed_receipt(tool, params):
    kit = TestkitToolExecutor()
    receipt = kit.execute(kit_call(tool, params))
    assert (receipt.succeeded, receipt.error_code) == (False, "failed")


def test_unknown_tool_is_denied():
    receipt = TestkitToolExecutor().execute(ToolCall(
        action_id="a", tool="other", params={}, params_hash="h", side_effect_class="internal_read", idempotency="native",
    ))
    assert (receipt.succeeded, receipt.error_code) == (False, "denied")


def test_sleep_completes_and_reports_duration():
    kit = TestkitToolExecutor()
    assert kit.execute(kit_call(TOOL_SLEEP, {"seconds": 0.05})).succeeded
    body = result_of(kit, "act_1")
    assert body["status"] == "completed" and body["requested_seconds"] == 0.05 and body["slept_seconds"] >= 0.04


def test_sleep_cancel_wakes_it_and_signs_a_cancelled_failure_receipt():
    kit = TestkitToolExecutor()
    assert supports_cancel(kit)
    box = {}
    worker = threading.Thread(target=lambda: box.update(r=kit.execute(kit_call(TOOL_SLEEP, {"seconds": 20}))), daemon=True)
    started = time.monotonic()
    worker.start()
    time.sleep(0.15)
    assert kit.cancel("act_1") == "stopped"
    worker.join(3)
    assert not worker.is_alive() and time.monotonic() - started < 3
    receipt = box["r"]
    assert (receipt.succeeded, receipt.error_code, receipt.meta["status"]) == (False, "cancelled", "cancelled")


def test_cancel_before_execute_and_for_unknown_actions():
    kit = TestkitToolExecutor()
    assert kit.cancel("act_1") == "stopped" and kit.cancel("never_seen") == "stopped"
    started = time.monotonic()
    receipt = kit.execute(kit_call(TOOL_SLEEP, {"seconds": 20}))  # cancel 先到：不睡
    assert time.monotonic() - started < 1 and receipt.error_code == "cancelled"


# ───────────────────────── AgentLoop（假 Port，真實 executor） ─────────────────────────

class KitTasks(FakeTasks):
    """FakeTasks 的測試工具變體：測試工具改走真實的 ``TestkitToolExecutor``（保留它簽回的 receipt，含取消的失敗 receipt）。"""

    def __init__(self, registry: ToolRegistry, kit: TestkitToolExecutor) -> None:
        super().__init__(registry)
        self.kit = kit

    def _run_action(self, action_id, tool, params):
        if tool not in TESTKIT_TOOLS:
            return super()._run_action(action_id, tool, params)
        receipt = self.kit.execute(kit_call(tool, dict(params), action_id, self.registry))
        with self.lock:
            if action_id in self.unknown:
                return ActionOutcome(action_id, ActionStatus.UNKNOWN, True, None)
            if receipt.succeeded:
                self.executions.append((action_id, tool))
                self.contents[action_id] = self.kit.cache.text(action_id)
            outcome = ActionOutcome(
                action_id, ActionStatus.SUCCEEDED if receipt.succeeded else ActionStatus.FAILED, True, receipt,
            )
            self.outcomes[action_id] = outcome
            return outcome


def kit_rig(rounds, *, registry: ToolRegistry = REGISTRY, kit: TestkitToolExecutor | None = None, **kw) -> Rig:
    kit = kit or TestkitToolExecutor()
    kw.setdefault("budget", LoopBudget(max_parallel=2, repeat_limit=99))
    kw.setdefault("executors", {TESTKIT_EXECUTOR: kit})
    kw.setdefault("cancel_grace_s", 1.0)
    kw.setdefault("projector", projector_for(registry, "console"))  # testkit 只在 console 平台投影
    rig = Rig(rounds, registry=registry, **kw)
    rig.tasks = KitTasks(registry, kit)
    rig.loop = rig.new_loop()  # 以換上的 tasks（含它的 contents）重建 loop
    rig.kit = kit
    return rig


def act(ordinal: int, step: int = 1) -> str:
    return stable_action_id(RUN, f"step_{step}", ordinal)


def fed(rig: Rig) -> list[dict]:
    return [v for _, v in next(iter(rig.runtime.submitted.values()))]


def tool_texts(rig: Rig) -> list[str]:
    return [m.content for m in rig.child() if m.role == "tool"]


def test_loop_multi_round_chain_with_real_executor():
    rig = kit_rig([
        Round("先回聲", ((TOOL_ECHO, {"text": "第一輪"}),)),
        Round("再看時間", ((TOOL_CLOCK, {}),)),
        Round("再回聲", ((TOOL_ECHO, {"text": "第三輪"}),)),
        Round("全部完成"),
    ])
    out = rig.run(trigger=trigger(tags={TEST_READ_TAG}))
    assert (out.state, out.text, out.steps, out.tool_calls) == ("completed", "全部完成", 4, 3)
    assert [t for _, t in rig.tasks.executions] == [TOOL_ECHO, TOOL_CLOCK, TOOL_ECHO]
    texts = tool_texts(rig)
    assert json.loads(texts[0]) == {"echo": "第一輪", "length": 3} and json.loads(texts[2]) == {"echo": "第三輪", "length": 3}
    assert set(json.loads(texts[1])) == {"utc", "local", "timezone"}
    assert all(s["status"] == "ok" for s in [v for v in fed(rig)])


def test_loop_runs_two_parallel_safe_items_of_one_batch_concurrently():
    barrier = threading.Barrier(2, timeout=3)

    def meet() -> datetime:
        barrier.wait()  # 兩個 test_clock 必須同時在執行，否則逾時失敗
        return datetime.now(UTC)

    rig = kit_rig(
        [Round("", ((TOOL_CLOCK, {}), (TOOL_CLOCK, {}))), Round("好")], kit=TestkitToolExecutor(clock=meet),
    )
    out = rig.run(trigger=trigger(tags={TEST_READ_TAG}))
    assert out.state == "completed" and len(rig.tasks.executions) == 2
    assert [v["status"] for v in fed(rig)] == ["ok", "ok"]


def test_loop_run_cancel_stops_a_sleeping_tool_via_executor_cancel():
    flag = threading.Event()
    rig = kit_rig([Round("", ((TOOL_SLEEP, {"seconds": 20}),)), Round("不該到這裡")], cancelled=flag.is_set)
    timer = threading.Timer(0.2, flag.set)
    timer.start()
    started = time.monotonic()
    try:
        out = rig.run(trigger=trigger(tags={TEST_READ_TAG}))
    finally:
        timer.cancel()
    assert out.state == "cancelled" and time.monotonic() - started < 5  # 沒有睡滿 20 秒
    outcome = rig.tasks.outcomes[act(0)]
    assert outcome.status is ActionStatus.FAILED and outcome.receipt.error_code == "cancelled"
    assert rig.tasks.marked_unknown == []  # executor 回報 stopped：不是結果未明


def test_loop_sleep_beyond_its_time_limit_times_out_and_the_run_continues():
    entries = [replace(e, timeout_s=0.3) if e.name == TOOL_SLEEP else e for e in kit_entries()]
    registry = ToolRegistry.build(entries)
    rig = kit_rig([Round("", ((TOOL_SLEEP, {"seconds": 5}),)), Round("逾時後仍完成")], registry=registry)
    started = time.monotonic()
    out = rig.run(trigger=trigger(tags={TEST_READ_TAG}))
    assert out.state == "completed" and time.monotonic() - started < 4
    (view,) = fed(rig)
    assert (view["status"], view.get("reason_code")) == ("failed", "timeout")
    assert rig.tasks.outcomes[act(0)].receipt.error_code == "cancelled" and rig.tasks.marked_unknown == []


def test_loop_short_sleep_completes_inside_its_time_limit():
    rig = kit_rig([Round("", ((TOOL_SLEEP, {"seconds": 0.05}),)), Round("好")])
    assert rig.run(trigger=trigger(tags={TEST_READ_TAG})).state == "completed"
    assert json.loads(tool_texts(rig)[0])["status"] == "completed"


def test_loop_approval_echo_parks_then_resumes_after_approval():
    rig = kit_rig([Round("要核准", ((TOOL_APPROVAL_ECHO, {"message": "請放行"}),)), Round("核准後完成")])
    tags = {TEST_READ_TAG, TEST_APPROVAL_TAG}
    out = rig.run(trigger=trigger(tags=tags))
    assert (out.state, out.pending_action_ids) == ("awaiting_approval", (act(0),))
    assert rig.kit.executions == [] and rig.tasks.executions == []  # 核准前 executor 一次都沒被呼叫
    checkpoint = _park_checkpoint(rig)
    rig.tasks.grant("appr_1", act(0))
    resumed = _crash(rig, rounds=[Round("核准後完成")]).resume(
        RUN, checkpoint, trigger=trigger(tags=tags), parent_id="ctx_parent", approvals={act(0): "appr_1"},
    )
    assert (resumed.state, resumed.text) == ("completed", "核准後完成")
    assert rig.kit.executions == [act(0)] and rig.tasks.approvals["appr_1"].consumed_by == act(0)  # 單次消耗、只執行一次
    assert json.loads(tool_texts(rig)[0]) == {"echo": "請放行", "note": APPROVAL_NOTE}


def test_loop_without_the_approval_tag_cannot_call_approval_echo():
    rig = kit_rig([Round("", ((TOOL_APPROVAL_ECHO, {"message": "x"}),)), Round("好")])
    out = rig.run(trigger=trigger(tags={TEST_READ_TAG}))  # 只凍結 test.read：test_approval_echo 不在可見集合
    assert out.state == "completed" and fed(rig)[0]["status"] == "rejected"
    assert fed(rig)[0]["reason_code"] == "capability_missing" and rig.kit.executions == []


def test_gateway_rejects_bad_testkit_arguments_before_any_action():
    gateway = ToolGateway(REGISTRY)
    caps = REGISTRY.project(ALL_TAGS, CONSOLE)
    from uep.tools.gateway import GatewayRejected

    for tool, args in ((TOOL_ECHO, {"text": "x", "extra": 1}), (TOOL_SLEEP, {"seconds": SLEEP_MAX_S + 1}), (TOOL_CLOCK, {"x": 1})):
        with pytest.raises(GatewayRejected) as exc:
            gateway.check_intent(tool, args, caps)
        assert exc.value.code == "invalid_tool_args"
    assert gateway.check_intent(TOOL_SLEEP, {"seconds": 1}, caps) == {"seconds": 1}


# ───────────────────────── composite 測試工具 ─────────────────────────

def sub_act(ordinal: int, sub: int, step: int = 1) -> str:
    return stable_action_id(RUN, f"step_{step}", ordinal, sub)


def test_composite_testkit_entries_follow_ra14_and_reference_only_persistent_outputs():
    entries = {e.name: e for e in kit_entries()}
    clock_echo, echo_approve = entries[TOOL_CLOCK_ECHO], entries[TOOL_ECHO_THEN_APPROVE]
    for comp in (clock_echo, echo_approve):
        assert comp.concurrency == "serial" and comp.composite is not None  # 自身 serial（RA-14）
        assert all(s.tool in TESTKIT_TOOLS for s in comp.composite.steps)
    assert [s.tool for s in clock_echo.composite.steps] == [TOOL_CLOCK, TOOL_ECHO]
    assert clock_echo.composite.steps[1].params == {"text": "$.steps[0].output.result_ref"}  # 只引用 receipt 持久的欄位
    assert (clock_echo.spec.side_effect_class, clock_echo.spec.approval_level, clock_echo.spec.capability) == (
        "internal_read", "none", TEST_READ_TAG)
    assert [s.tool for s in echo_approve.composite.steps] == [TOOL_ECHO, TOOL_APPROVAL_ECHO]
    assert (echo_approve.spec.side_effect_class, echo_approve.spec.approval_level, echo_approve.spec.capability) == (
        "external_effect", "per_action", TEST_APPROVAL_TAG)  # 分類取子步驟最大值
    for tool in (TOOL_CLOCK, TOOL_ECHO):  # 被引用的 result_ref 必須在 output_schema 內（RA-14 靜態檢查）
        assert "result_ref" in entries[tool].output_schema["properties"]
        assert "result_ref" not in entries[tool].output_schema["required"]


def test_composite_testkit_tools_are_projected_by_their_tags_and_hidden_without_the_switch():
    ctx = CONSOLE
    assert TOOL_CLOCK_ECHO in names(REGISTRY.project({TEST_READ_TAG}, ctx))
    assert TOOL_ECHO_THEN_APPROVE not in names(REGISTRY.project({TEST_READ_TAG}, ctx))
    assert TOOL_ECHO_THEN_APPROVE in names(REGISTRY.project({TEST_APPROVAL_TAG}, ctx))
    assert not {TOOL_CLOCK_ECHO, TOOL_ECHO_THEN_APPROVE} & set(names(memory_registry().project(ALL_TAGS, ctx)))


def test_loop_clock_echo_passes_the_first_receipt_result_ref_to_the_second_step():
    rig = kit_rig([Round("組合", ((TOOL_CLOCK_ECHO, {}),)), Round("完成")])
    out = rig.run(trigger=trigger(tags={TEST_READ_TAG}))
    clock_id, echo_id = sub_act(0, 0), sub_act(0, 1)
    assert (out.state, out.text) == ("completed", "完成")
    assert rig.kit.executions == [clock_id, echo_id] and not rig.tasks.approvals  # composite 本身不建 action
    assert result_of(rig.kit, echo_id) == {"echo": f"blob_{clock_id}", "length": len(f"blob_{clock_id}")}
    view = fed(rig)[0]
    assert [(i["tool"], i["status"]) for i in view["items"]] == [(TOOL_CLOCK, "ok"), (TOOL_ECHO, "ok")]


def test_loop_echo_then_approve_parks_at_the_approval_step_then_resumes_without_replaying_the_echo():
    rig = kit_rig([Round("要核准", ((TOOL_ECHO_THEN_APPROVE, {"text": "請放行"}),)), Round("核准後完成")])
    tags = {TEST_READ_TAG, TEST_APPROVAL_TAG}
    out = rig.run(trigger=trigger(tags=tags))
    echo_id, approval_id = sub_act(0, 0), sub_act(0, 1)
    assert (out.state, out.pending_action_ids) == ("awaiting_approval", (approval_id,))
    assert rig.kit.executions == [echo_id]  # 第一步已執行；核准步驟尚未
    checkpoint = _park_checkpoint(rig)
    assert checkpoint["composite_cursor"]["next"] == 1
    rig.tasks.grant("appr_1", approval_id)
    resumed = _crash(rig, rounds=[Round("核准後完成")]).resume(
        RUN, checkpoint, trigger=trigger(tags=tags), parent_id="ctx_parent", approvals={approval_id: "appr_1"},
    )
    assert (resumed.state, resumed.text) == ("completed", "核准後完成")
    assert rig.kit.executions == [echo_id, approval_id]  # echo 沒有重播；核准步驟只執行一次
    assert rig.tasks.approvals["appr_1"].consumed_by == approval_id
    assert json.loads(tool_texts(rig)[0]) == {"echo": "請放行", "note": APPROVAL_NOTE}


def test_loop_clock_echo_is_invisible_without_the_test_read_tag():
    rig = kit_rig([Round("", ((TOOL_CLOCK_ECHO, {}),)), Round("好")])
    out = rig.run(trigger=trigger(tags={TEST_APPROVAL_TAG}))
    assert out.state == "completed" and fed(rig)[0]["reason_code"] == "capability_missing" and rig.kit.executions == []


# ───────────────────────── 平台限定：只在 console 投影 ─────────────────────────

@pytest.mark.parametrize("platform", [None, "desktop", "discord", "website"])
def test_testkit_is_not_projected_outside_console_and_gateway_blocks_before_execution(platform):
    from uep.tools.gateway import GatewayRejected

    ctx = ProjectionContext.for_platform(platform)
    caps = REGISTRY.project(ALL_TAGS | {"memory.read"}, ctx)
    assert not set(TESTKIT_TOOLS) & set(names(caps))
    assert "deep_recall" in names(caps)  # 原生工具任何平台可用
    with pytest.raises(GatewayRejected):
        ToolGateway(REGISTRY).check_intent(TOOL_ECHO, {"text": "x"}, caps)


def test_testkit_is_projected_on_console_alongside_native_tools():
    caps = REGISTRY.project(ALL_TAGS | {"memory.read"}, CONSOLE)
    assert set(TESTKIT_TOOLS) <= set(names(caps)) and "deep_recall" in names(caps)


def test_loop_outside_console_refuses_to_start_with_only_testkit_tags():
    rig = kit_rig([Round("", ((TOOL_ECHO, {"text": "x"}),)), Round("好")], projector=projector_for(REGISTRY, "desktop"))
    out = rig.run(trigger=trigger(tags={TEST_READ_TAG}))  # 投影為空：Run 拒絕啟動，工具一次都不執行
    assert (out.state, out.reason_code) == ("rejected", "capability_empty") and rig.kit.executions == []
