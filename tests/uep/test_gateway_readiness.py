"""切片 F-1：Gateway 執行前再檢查 readiness（契約 §5.8）。離線，不需 DB。

已投出（在 capability_set 內）的工具，之後被事件轉成 warming／unavailable ⇒ ``check_intent`` 以 ``tool_not_ready`` 拒絕、
不建立 action；沒有任何事件時一律不擋；事件恢復 ready 後可再通過；沒有 registry／registry 沒有 ``readiness`` 時舊路徑不變。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.uep.loop_support import TAGS, Round, projector_for
from tests.uep.test_agent_loop import Rig, call
from tests.uep.test_tool_registry import make_entry
from uep.agent.budget import LoopBudget
from uep.agent.core import AgentCore
from uep.tools.catalog.decision import consult_entry
from uep.tools.catalog.memory import memory_entries
from uep.tools.gateway import TOOL_NOT_READY, GatewayRejected, ToolGateway
from uep.tools.registry import StaticToolRegistry, ToolRegistry

MODULES = ["mod:a", "mod:b"]


def build() -> ToolRegistry:
    return ToolRegistry.build([
        make_entry("mod.cold", requires=("mod:a",), load_cost="heavy", warm_required=True),
        make_entry("mod.plain", requires=("mod:b",)),
        make_entry("mod.none"),
    ], known_modules=MODULES)


def cap_of(registry: ToolRegistry) -> dict:
    return projector_for(registry)(TAGS, None)


def event(registry: ToolRegistry, kind: str, module: str, revision: int) -> None:
    assert registry.apply_module_event(f"module.{kind}", {"module_id": module, "revision": revision})


# ---- 投出後才轉非 ready ----

@pytest.mark.parametrize("state", ["unavailable", "warming"])
def test_tool_turned_not_ready_after_projection_is_rejected(state):
    registry = build()
    cap = cap_of(registry)
    assert "mod.plain" in {t["tool"] for t in cap["tools"]}  # 投出時是 ready
    event(registry, state, "mod:b", 1)
    gateway = ToolGateway(registry)
    with pytest.raises(GatewayRejected) as exc:
        gateway.check_intent("mod.plain", {"path": "x"}, cap)
    assert exc.value.code == TOOL_NOT_READY == "tool_not_ready"
    view = gateway.rejection_view(exc.value)
    assert view["reason_code"] == "tool_not_ready" and "fields" not in view  # 不含值、不含欄位
    assert view["status"] == "blocked"  # §5.8：tool_not_ready 是 blocked


def test_not_ready_check_runs_before_argument_checks():
    """順序：capability -> readiness -> 其他。連壞參數也先回 tool_not_ready（不洩漏 schema 細節給未就緒的工具）。"""
    registry = build()
    cap = cap_of(registry)
    event(registry, "unavailable", "mod:b", 1)
    with pytest.raises(GatewayRejected) as exc:
        ToolGateway(registry).check_intent("mod.plain", "不是物件", cap)
    assert exc.value.code == TOOL_NOT_READY
    with pytest.raises(GatewayRejected) as missing:  # capability 仍在 readiness 之前
        ToolGateway(registry).check_intent("mod.plain", {"path": "x"}, {"tools": []})
    assert missing.value.code == "capability_missing"


def test_not_ready_does_not_create_an_action():
    registry = build()
    cap = cap_of(registry)
    event(registry, "unavailable", "mod:b", 1)

    class NoCreate:
        def create_action(self, **_kw):
            raise AssertionError("tool_not_ready 不得建立 action")

        execute_action = create_action

    core = AgentCore(None, NoCreate(), ToolGateway(registry), worker_epoch=1)  # type: ignore[arg-type]
    record, view = core.run_intent(run_id="run_1", step_id="s", ordinal=0, capability_set=cap, tool="mod.plain", args={"path": "x"})
    assert record.action_id is None and view["reason_code"] == "tool_not_ready"
    assert (record.status, view["status"]) == ("blocked", "blocked")
    screened = core.screen_intent(run_id="run_1", step_id="s", ordinal=1, capability_set=cap, tool="mod.plain", args={"path": "x"})
    assert screened.view["reason_code"] == "tool_not_ready" and screened.record.action_id is None
    assert (screened.record.status, screened.view["status"]) == ("blocked", "blocked")


def test_recovered_tool_passes_again():
    registry = build()
    cap = cap_of(registry)
    gateway = ToolGateway(registry)
    event(registry, "unavailable", "mod:b", 1)
    with pytest.raises(GatewayRejected):
        gateway.check_intent("mod.plain", {"path": "x"}, cap)
    event(registry, "ready", "mod:b", 2)
    assert gateway.check_intent("mod.plain", {"path": "x"}, cap) == {"path": "x"}


def test_warm_required_tool_passes_once_the_first_ready_event_arrives():
    registry = build()
    gateway = ToolGateway(registry)
    cap = {"tools": [{"tool": "mod.cold", "version": 1}]}
    with pytest.raises(GatewayRejected) as exc:  # 已知模組、warm_required：第一個 ready 事件前算 warming
        gateway.check_intent("mod.cold", {"path": "x"}, cap)
    assert exc.value.code == TOOL_NOT_READY
    event(registry, "ready", "mod:a", 1)
    assert gateway.check_intent("mod.cold", {"path": "x"}, cap) == {"path": "x"}


# ---- 沒有任何事件時不擋 ----

def test_nothing_is_blocked_without_events():
    registry = build()
    gateway = ToolGateway(registry)
    cap = {"tools": [{"tool": "mod.plain", "version": 1}, {"tool": "mod.none", "version": 1}]}
    for tool in ("mod.plain", "mod.none"):  # requires 為空、已知模組且非 warm_required
        assert registry.readiness(tool) == "ready"
        assert gateway.check_intent(tool, {"path": "x"}, cap) == {"path": "x"}


def test_every_real_catalog_entry_is_ready_and_not_blocked_without_events():
    registry = ToolRegistry.build([*memory_entries(), consult_entry()])
    assert registry.entries()
    gateway = ToolGateway(registry)
    for name in registry.entries():
        assert registry.readiness(name) == "ready", name
        cap = {"tools": [{"tool": name, "version": registry.resolve(name).tool_version}]}
        try:
            gateway.check_intent(name, {}, cap)
        except GatewayRejected as exc:  # 空參數可能不合 schema，但絕不能是 tool_not_ready
            assert exc.code != TOOL_NOT_READY, name


def test_unknown_module_requirement_stays_fail_closed():
    """requires 含 coordinator 不認得的模組 ⇒ readiness 一律 unavailable（RA-15），Gateway 同樣擋。"""
    registry = ToolRegistry.build([make_entry("mod.orphan", requires=("mod:ghost",))], known_modules=MODULES)
    with pytest.raises(GatewayRejected) as exc:
        ToolGateway(registry).check_intent("mod.orphan", {"path": "x"}, {"tools": [{"tool": "mod.orphan", "version": 1}]})
    assert exc.value.code == TOOL_NOT_READY


# ---- 舊路徑不變 ----

def test_no_registry_keeps_the_old_path():
    gateway = ToolGateway()
    assert gateway.check_intent("any.tool", {"a": 1}, {"tools": [{"tool": "any.tool"}]}) == {"a": 1}
    view = gateway.rejection_view(GatewayRejected(TOOL_NOT_READY))
    assert view == gateway.result_view("rejected")  # 舊形狀：不帶 reason_code


def test_registry_without_readiness_is_not_checked():
    registry = build()
    static = StaticToolRegistry({"mod.plain": registry.resolve("mod.plain")})
    assert not hasattr(static, "readiness")
    fake = SimpleNamespace(
        entry=registry.entry, effective_input_schema=registry.effective_input_schema,  # 有 entry 但沒有 readiness
    )
    gateway = ToolGateway(fake)  # type: ignore[arg-type]
    event(registry, "unavailable", "mod:b", 1)
    assert gateway.check_intent("mod.plain", {"path": "x"}, {"tools": [{"tool": "mod.plain", "version": 1}]}) == {"path": "x"}


# ---- 無進展預算 ----

def test_not_ready_rejections_count_toward_no_progress():
    """AgentLoop 把 screen 階段的拒絕一律以 ``rejected`` 記入 observe_call（loop.py 的 _handle_batch），
    連續 rejected_limit 次就 ``FAILED(no_progress)``；tool_not_ready 也不例外。"""
    registry = ToolRegistry.build([make_entry("mod.plain", requires=("mod:b",))], known_modules=MODULES)
    cap = cap_of(registry)  # 過期的投影（投出時 ready）
    event(registry, "unavailable", "mod:b", 1)
    rig = Rig(
        [Round("", (call("mod.plain", "1"), call("mod.plain", "2"), call("mod.plain", "3"))), Round("好")],
        registry=registry, projector=lambda tags, prior: cap, budget=LoopBudget(),
    )
    out = rig.run()
    assert (out.state, out.reason_code) == ("failed", "no_progress")
    assert rig.tasks.actions == {} and rig.tasks.executions == []  # 沒有建立任何 action


def test_isolated_not_ready_rejection_is_fed_back_and_loop_continues():
    registry = ToolRegistry.build([make_entry("mod.plain", requires=("mod:b",))], known_modules=MODULES)
    cap = cap_of(registry)
    event(registry, "unavailable", "mod:b", 1)
    rig = Rig([Round("", (call("mod.plain", "1"),)), Round("好")], registry=registry, projector=lambda tags, prior: cap)
    out = rig.run()
    assert out.state == "completed" and rig.tasks.actions == {}
    (fed,) = next(iter(rig.runtime.submitted.values()))
    assert fed[1]["reason_code"] == "tool_not_ready" and fed[1]["status"] == "blocked"  # §5.8


# ---- 其他 GatewayRejected 的 code 仍是 rejected ----

@pytest.mark.parametrize(("tool", "args", "cap_tools", "code"), [
    ("mod.none", {"path": "x"}, [], "capability_missing"),
    ("mod.none", "不是物件", [{"tool": "mod.none", "version": 1}], "args_not_mapping"),
    ("mod.none", {}, [{"tool": "mod.none", "version": 1}], "invalid_tool_args"),  # 缺必填 path
    ("mod.none", {"path": "x", "多餘": 1}, [{"tool": "mod.none", "version": 1}], "invalid_tool_args"),
    ("mod.ghost", {"path": "x"}, [{"tool": "mod.ghost", "version": 1}], "tool_unknown"),
])
def test_other_gateway_rejections_stay_rejected(tool, args, cap_tools, code):
    registry = build()
    gateway = ToolGateway(registry)
    with pytest.raises(GatewayRejected) as exc:
        gateway.check_intent(tool, args, {"tools": cap_tools})
    assert exc.value.code == code
    assert gateway.rejection_status(exc.value) == "rejected" and gateway.rejection_view(exc.value)["status"] == "rejected"
    core = AgentCore(None, SimpleNamespace(), gateway, worker_epoch=1)  # type: ignore[arg-type]
    screened = core.screen_intent(run_id="run_1", step_id="s", ordinal=0, capability_set={"tools": cap_tools}, tool=tool, args=args)
    assert screened.record.status == "rejected" and screened.view["status"] == "rejected" and screened.record.action_id is None


def test_injected_param_missing_stays_rejected():
    from uep.adapters.tool import ToolDescriptor

    entry = make_entry("mod.inj", descriptor=ToolDescriptor("d", {"type": "object", "properties": {}}, injected_params=("venue",)))
    registry = ToolRegistry.build([entry])
    gateway = ToolGateway(registry)
    with pytest.raises(GatewayRejected) as exc:
        gateway.check_intent("mod.inj", {}, {"tools": [{"tool": "mod.inj", "version": 1}]})
    assert exc.value.code == "injected_param_missing" and gateway.rejection_view(exc.value)["status"] == "rejected"
