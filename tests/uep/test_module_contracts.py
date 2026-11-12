"""Module Coordinator PR-1：契約與事件定名（D108 R1～R3）。離線，不需 DB。

只測契約與命名：型別、七態／三態對照、轉移表、payload 驗證、事件註冊、暫定名清除、註冊表端讀 readiness、
lore 不可用語意。coordinator 本體（runtime）不在此 PR。
"""
from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from tests.uep.test_tool_registry import make_entry
from uep.contracts.provision import (
    PROVISION_ACTIONS,
    PROVISION_OUTCOMES,
    ProvisionItem,
)
from uep.events.registry import EventRegistry
from uep.lifecycle import states
from uep.lifecycle.states import (
    MODULE_PROVISION_RESOLVED,
    MODULE_STATE_CHANGED,
    MODULE_STATES,
    READINESS_OF_STATE,
    TRANSITIONS,
    is_legal_transition,
    readiness_of,
    validate_provision_resolved_payload,
    validate_state_changed_payload,
)
from uep.tools.catalog.lore import LORE_DELETE_MODULE, LORE_MODULE, TOOL_RECALL, with_lore
from uep.tools.registry import MODULE_EVENT_TYPE, ToolRegistry

REPO_ROOT = Path(__file__).resolve().parents[2]


# --- ProvisionItem ---

def test_provision_item_defaults_and_actions():
    item = ProvisionItem(module_id="lore", action="warm")
    assert (item.ready_by_ms, item.urgency, item.on_not_ready) == (None, "normal", "degrade")
    assert PROVISION_ACTIONS == {"keep", "warm", "load", "unload"}  # 沒有 install（ADR-04）
    assert {"accepted", "accepted_best_effort", "deferred", "rejected", "dropped", "noop"} == PROVISION_OUTCOMES


@pytest.mark.parametrize("kwargs", [
    {"module_id": "", "action": "warm"},
    {"module_id": "lore", "action": "install"},
    {"module_id": "lore", "action": "warm", "urgency": "urgent"},
    {"module_id": "lore", "action": "warm", "on_not_ready": "ignore"},
    {"module_id": "lore", "action": "warm", "ready_by_ms": -1},
    {"module_id": "lore", "action": "warm", "ready_by_ms": True},
    {"module_id": "lore", "action": "warm", "ready_by_ms": 1.5},
])
def test_provision_item_rejects_invalid(kwargs):
    with pytest.raises(ValueError):
        ProvisionItem(**kwargs)


def test_provision_item_is_frozen():
    with pytest.raises(AttributeError):
        ProvisionItem(module_id="lore", action="keep").action = "load"  # type: ignore[misc]


# --- 七態／三態／轉移表 ---

def test_seven_states_map_to_three_readiness():
    assert MODULE_STATES == {"unloaded", "loading", "warming", "ready", "degraded", "unloading", "failed"}
    assert dict(READINESS_OF_STATE) == {
        "unloaded": "unavailable", "loading": "warming", "warming": "warming", "ready": "ready",
        "degraded": "ready", "unloading": "unavailable", "failed": "unavailable",
    }
    assert readiness_of("degraded") == "ready"
    with pytest.raises(ValueError):
        readiness_of("ready ")


def test_transition_table_exhaustive():
    legal = {
        ("unloaded", "loading"), ("loading", "warming"), ("loading", "ready"), ("warming", "ready"),
        ("ready", "degraded"), ("degraded", "ready"), ("ready", "unloading"), ("degraded", "unloading"),
        ("unloading", "unloaded"), ("failed", "loading"),
        ("ready", "loading"),  # R1：reload 掛點（v1 不啟用）
        # 任一態（failed 自身除外）-> failed
        *((s, "failed") for s in MODULE_STATES if s != "failed"),
    }
    for src, dst in itertools.product(sorted(MODULE_STATES), repeat=2):
        assert is_legal_transition(src, dst) == ((src, dst) in legal), (src, dst)
    assert set(TRANSITIONS) == MODULE_STATES
    assert not is_legal_transition("ready", "bogus") and not is_legal_transition("bogus", "ready")


# --- payload ---

def _changed(**over):
    payload = {"module_id": "lore", "revision": 1, "epoch": 0, "state": "ready", "readiness": "ready"}
    payload.update(over)
    return payload


def test_state_changed_payload_valid_cases():
    validate_state_changed_payload(_changed())
    validate_state_changed_payload(_changed(
        state="failed", readiness="unavailable", reason_code="oom", prev_state="loading", cause="health",
        decision_id="d1", attempt=2,
    ))
    validate_state_changed_payload(_changed(state="degraded", readiness="ready", reason_code="cpu_fallback"))


@pytest.mark.parametrize("over", [
    {"module_id": ""}, {"revision": -1}, {"revision": True}, {"epoch": "0"}, {"state": "paused"},
    {"readiness": "warming"},  # 與 state=ready 不一致
    {"readiness": None}, {"prev_state": "bogus"}, {"cause": "whim"}, {"attempt": 0}, {"decision_id": ""},
    {"secret": "x"},  # 白名單以外
    {"state": "failed", "readiness": "unavailable"},  # failed 缺 reason_code
    {"state": "degraded", "readiness": "ready"},  # degraded 缺 reason_code
])
def test_state_changed_payload_invalid_cases(over):
    with pytest.raises(ValueError):
        validate_state_changed_payload(_changed(**over))


def test_state_changed_payload_missing_required_keys():
    for key in ("module_id", "revision", "epoch", "state", "readiness"):
        payload = _changed()
        del payload[key]
        with pytest.raises(ValueError):
            validate_state_changed_payload(payload)


def _resolved(**over):
    payload = {"module_id": "lore", "decision_id": "d1", "plan_version": 3, "action": "warm", "outcome": "accepted"}
    payload.update(over)
    return payload


def test_provision_resolved_payload():
    validate_provision_resolved_payload(_resolved())
    validate_provision_resolved_payload(_resolved(outcome="rejected", reason_code="unknown_module"))
    for over in ({"action": "install"}, {"outcome": "maybe"}, {"plan_version": -1}, {"decision_id": ""},
                 {"outcome": "rejected"}, {"extra": 1}):
        with pytest.raises(ValueError):
            validate_provision_resolved_payload(_resolved(**over))


# --- 事件註冊 ---

def test_event_registry_knows_new_names_with_matching_whitelists():
    reg = EventRegistry()
    assert reg.lookup(MODULE_STATE_CHANGED).payload_keys >= set(states.STATE_CHANGED_KEYS)
    assert reg.lookup(MODULE_PROVISION_RESOLVED).payload_keys >= set(states.PROVISION_RESOLVED_KEYS)
    # 白名單與契約鍵一致：契約鍵都在，且沒有多出 DEFAULT 以外的未契約鍵
    from uep.events.registry import DEFAULT_PAYLOAD_KEYS
    assert reg.lookup(MODULE_STATE_CHANGED).payload_keys - DEFAULT_PAYLOAD_KEYS == set(states.STATE_CHANGED_KEYS) - DEFAULT_PAYLOAD_KEYS
    assert reg.lookup(MODULE_PROVISION_RESOLVED).payload_keys - DEFAULT_PAYLOAD_KEYS == set(states.PROVISION_RESOLVED_KEYS) - DEFAULT_PAYLOAD_KEYS
    assert MODULE_EVENT_TYPE == MODULE_STATE_CHANGED  # 註冊表端與契約同名


def test_provisional_event_names_are_gone():
    from uep.events.registry import UnregisteredEventType
    reg = EventRegistry()
    for suffix in ("ready", "warming", "unavailable"):
        with pytest.raises(UnregisteredEventType):
            reg.lookup("module." + suffix)


def test_provisional_names_purged_from_source_and_tests():
    """grep 式：uep/ 與 tests/ 內不得再出現暫定事件名（本檔以拼接組字串，不會自我命中）。"""
    banned = ["module." + s for s in ("ready", "warming", "unavailable")] + ["MODULE_EVENT" + "_STATES"]
    hits = []
    for base in (REPO_ROOT / "uep", REPO_ROOT / "tests"):
        for path in base.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            hits += [f"{path.relative_to(REPO_ROOT)}: {b}" for b in banned if b in text and path.resolve() != Path(__file__).resolve()]
    assert not hits, "\n".join(hits)


# --- 註冊表端 ---

def _registry() -> ToolRegistry:
    heavy = make_entry("speak.tool", spec={"capability": "memory.read"}, requires=("gpu:x",), warm_required=True)
    return ToolRegistry.build([heavy], known_modules=["gpu:x"])


def test_registry_reads_readiness_from_single_event_type():
    reg = _registry()
    ev = reg.apply_module_event
    assert ev(MODULE_STATE_CHANGED, {"module_id": "gpu:x", "revision": 1, "readiness": "ready", "state": "ready"})
    assert reg.readiness("speak.tool") == "ready"
    # 七態中 loading 的 readiness 為 warming；註冊表只信 payload.readiness
    assert ev(MODULE_STATE_CHANGED, {"module_id": "gpu:x", "revision": 2, "readiness": "warming", "state": "loading"})
    assert reg.readiness("speak.tool") == "warming"


@pytest.mark.parametrize("event_type,payload", [
    ("module.ready", {"module_id": "gpu:x", "revision": 9, "readiness": "ready"}),  # 舊名不再被接受
    ("module.warming", {"module_id": "gpu:x", "revision": 9, "readiness": "warming"}),
    ("module.unavailable", {"module_id": "gpu:x", "revision": 9, "readiness": "unavailable"}),
    (MODULE_STATE_CHANGED, {"module_id": "gpu:x", "revision": 9}),  # 缺 readiness
    (MODULE_STATE_CHANGED, {"module_id": "gpu:x", "revision": 9, "readiness": "degraded"}),  # 非三態值
    (MODULE_STATE_CHANGED, {"module_id": "gpu:x", "revision": 9, "readiness": ["ready"]}),
    (MODULE_STATE_CHANGED, {"module_id": "gpu:zzz", "revision": 9, "readiness": "ready"}),  # 未知模組
])
def test_registry_ignores_malformed_or_legacy_events(event_type, payload):
    reg = _registry()
    assert not reg.apply_module_event(event_type, payload)
    assert reg.readiness("speak.tool") == "warming"  # warm_required 預設，狀態不變


# --- lore 不可用語意 ---

def test_lore_unconfigured_and_delete_disabled_stay_unavailable():
    base = ToolRegistry.build([make_entry("deep_recall")], known_modules=[])
    off = with_lore(base, configured=False)
    assert off.readiness(TOOL_RECALL) == "unavailable"
    on = with_lore(base, configured=True, delete_enabled=False)
    assert on.readiness(TOOL_RECALL) == "ready"
    assert on.readiness("lore.delete") == "unavailable"  # 禁止 delete 時 lore_delete 模組不可用
    full = with_lore(base, configured=True, delete_enabled=True)
    assert full.readiness("lore.delete") == "ready"
    assert {LORE_MODULE, LORE_DELETE_MODULE} <= set(getattr(off, "_known_modules"))


# --- 審查補強 ---

def test_registry_rejects_negative_revision_or_epoch():
    reg = _registry()
    base = {"module_id": "gpu:x", "readiness": "ready"}
    assert not reg.apply_module_event(MODULE_STATE_CHANGED, {**base, "revision": -1})
    assert not reg.apply_module_event(MODULE_STATE_CHANGED, {**base, "revision": 1, "epoch": -1})
    assert reg.readiness("speak.tool") == "warming"
    assert reg.apply_module_event(MODULE_STATE_CHANGED, {**base, "revision": 0, "epoch": 0})


@pytest.mark.parametrize("state,readiness", [("failed", "ready"), ("ready", "unavailable"), ("loading", "ready"), ("degraded", "warming")])
def test_registry_rejects_state_readiness_mismatch(state, readiness):
    reg = _registry()
    assert not reg.apply_module_event(
        MODULE_STATE_CHANGED, {"module_id": "gpu:x", "revision": 1, "state": state, "readiness": readiness})
    assert reg.readiness("speak.tool") == "warming"


def test_registry_state_map_matches_lifecycle_contract():
    from uep.tools import registry as tool_registry
    assert dict(tool_registry._READINESS_OF_STATE) == dict(READINESS_OF_STATE)


def test_registry_accepts_consistent_state_for_every_state():
    for i, (state, readiness) in enumerate(sorted(READINESS_OF_STATE.items()), start=1):
        reg = _registry()
        assert reg.apply_module_event(
            MODULE_STATE_CHANGED, {"module_id": "gpu:x", "revision": i, "state": state, "readiness": readiness})
        assert reg.readiness("speak.tool") == readiness


def test_lore_payloads_pass_contract_validation():
    from uep.lifecycle.states import unavailable_payload
    for module, reason in ((LORE_MODULE, "not_configured"), (LORE_DELETE_MODULE, "delete_disabled")):
        payload = unavailable_payload(module, reason_code=reason)
        validate_state_changed_payload(payload)
        assert payload["readiness"] == "unavailable" and payload["module_id"] == module
    with pytest.raises(ValueError):
        unavailable_payload("lore", reason_code="x", state="ready")


def test_epoch_increase_restarts_revision_with_new_payload_shape():
    reg = _registry()

    def send(revision, epoch, state):
        return reg.apply_module_event(MODULE_STATE_CHANGED, {
            "module_id": "gpu:x", "revision": revision, "epoch": epoch, "state": state,
            "readiness": READINESS_OF_STATE[state],
        })

    assert send(50, 0, "ready") and reg.readiness("speak.tool") == "ready"
    assert not send(10, 0, "loading")  # 同 epoch 舊 revision
    assert send(1, 1, "loading") and reg.readiness("speak.tool") == "warming"  # epoch 變大，revision 重算
    assert not send(999, 0, "ready")  # 舊 epoch 遲到
