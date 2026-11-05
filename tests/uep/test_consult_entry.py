"""``decision.consult`` 註冊表條目（切片 D2）：分類、投影、schema 與注入；不需 DB。"""
from __future__ import annotations

import pytest

from uep.tools.catalog.decision import (
    CONSULT_TAG,
    CONSULT_TOOL,
    DECISION_EXECUTOR,
    INJECTED_CONSULT,
    OFFERED_SUB_KINDS,
    consult_entry,
    consult_input_schema,
)
from uep.tools.catalog.memory import memory_entries
from uep.tools.gateway import GatewayRejected, ToolGateway
from uep.tools.projection import ProjectionContext
from uep.tools.registry import ToolRegistry

REGISTRY = ToolRegistry.build([*memory_entries(), consult_entry()])
INJECTED = {name: ([] if name.startswith(("frozen", "snapshot")) else 0) for name in INJECTED_CONSULT}
INJECTED.update({"run_id": "run_x", "step_id": "step_x"})


def test_entry_classification():
    entry = REGISTRY.entry(CONSULT_TOOL)
    assert entry.spec.side_effect_class == "internal_read" and entry.spec.approval_level == "none"
    assert entry.spec.capability == CONSULT_TAG and entry.spec.executor == DECISION_EXECUTOR
    assert entry.spec.idempotency == "native" and entry.concurrency == "serial"
    assert entry.scope.platforms == frozenset() and entry.scope.presence == "any"  # global
    assert entry.timeout_s == 0.5 and entry.timeout_s > 0.3  # executor 總時限 > decide 預算 300 ms（CN-5）


def test_projected_only_when_frozen_tags_contain_consult_tag():
    ctx = ProjectionContext()
    assert CONSULT_TOOL not in {t["tool"] for t in REGISTRY.project({"memory.read"}, ctx)["tools"]}
    with_tag = REGISTRY.project({"memory.read", CONSULT_TAG}, ctx)
    assert CONSULT_TOOL in {t["tool"] for t in with_tag["tools"]}
    # 標籤被 ceiling 收掉就投不出來（AL-3）
    assert CONSULT_TOOL not in {t["tool"] for t in REGISTRY.project({"memory.read", CONSULT_TAG}, ctx, ceiling={"memory.read"})["tools"]}


def test_llm_schema_hides_injected_and_excludes_placement():
    schema = consult_input_schema()
    assert schema["properties"]["sub_kind"]["enum"] == list(OFFERED_SUB_KINDS) and "placement" not in OFFERED_SUB_KINDS
    assert not set(INJECTED_CONSULT) & set(schema["properties"])
    effective = REGISTRY.effective_input_schema(CONSULT_TOOL)
    assert effective["additionalProperties"] is False


def test_gateway_injects_and_rejects_model_supplied_injected_fields_and_placement():
    gateway = ToolGateway(REGISTRY)
    cap = REGISTRY.project({CONSULT_TAG}, ProjectionContext())
    checked = gateway.check_intent(CONSULT_TOOL, {"sub_kind": "loop_end", "goal_satisfied": False}, cap, injected=INJECTED)
    assert checked["run_id"] == "run_x" and checked["sub_kind"] == "loop_end" and checked["consults_used"] == 0
    for bad in ({"sub_kind": "placement"}, {"sub_kind": "speak", "run_id": "run_evil"}, {"sub_kind": "speak", "ordinal": 3}):
        with pytest.raises(GatewayRejected) as exc:
            gateway.check_intent(CONSULT_TOOL, bad, cap, injected=INJECTED)
        assert exc.value.code == "invalid_tool_args"
