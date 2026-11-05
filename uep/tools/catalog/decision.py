"""``decision.consult`` 的註冊表條目（Agent 架構提案 §4.9；原屬切片 G，提前到 D2，只做這一條）。

- ``internal_read``／``approval_level=none``／``scope=global``／``concurrency=serial``／``idempotency=native``，
  能力標籤 ``decision.consult``（走標準投影：只有 Run 凍結標籤含它才會被投出）。
- ``timeout_s=0.5`` 是 executor 總時限，必須大於中樞 ``decide()`` 的 300 ms 預算（CN-5）。
- LLM 面的 input schema 由 DL-C1 的各 sub_kind ``input_schema`` 合併而來（扣掉迴圈注入的欄位）。``placement`` 在 D2 不投出，
  所以 enum 不含它；迴圈自己的呼叫也不會用它。其餘欄位（run_id／step_id／ordinal／凍結與 snapshot 範圍／計數）由 Gateway 注入，
  模型不可提供（``injected_params``）。
"""
from __future__ import annotations

from typing import Any

from uep.adapters.tool import ToolDescriptor, ToolEntry, ToolScope, ToolSpec
from uep.decision.loop_consult import SUB_KINDS, input_schema, output_schema

DECISION_EXECUTOR = "decision_consult"
CONSULT_TOOL = "decision.consult"
CONSULT_TAG = "decision.consult"
CONSULT_TIMEOUT_S = 0.5
# D2 不投出（DL-C1 對 placement 一律 escalate）
NOT_OFFERED_SUB_KINDS = frozenset({"placement"})
OFFERED_SUB_KINDS = tuple(k for k in SUB_KINDS if k not in NOT_OFFERED_SUB_KINDS)

# 由 Gateway 注入、模型看不到：身分（決定性 decision_id）、凍結範圍與本次 snapshot（modalities／platforms 由迴圈補上）、loop_end 的計數
INJECTED_CONSULT = (
    "run_id", "step_id", "ordinal",
    "frozen_capability_tags", "frozen_modalities", "frozen_platforms",
    "snapshot_capability_tags", "snapshot_modalities", "snapshot_platforms",
    "consults_used", "no_progress_count",
)
_COMMON_FACTS = frozenset({"sub_kind", "run_id", "step_id", "ordinal", "frozen", "capability_snapshot", "consults_used", "no_progress_count"})

CONSULT_DESCRIPTION = (
    "Ask the decision hub for advice on a local decision (speak, output_modality, ask_user, loop_end, followup). "
    "The answer is advice only: it never grants new tools or permissions."
)


def consult_input_schema() -> dict[str, Any]:
    """LLM 面 schema：``sub_kind`` 加各 kind 的事實旗標（扣掉注入欄位）。"""
    props: dict[str, Any] = {"sub_kind": {"type": "string", "enum": list(OFFERED_SUB_KINDS)}}
    for kind in OFFERED_SUB_KINDS:
        for name, definition in input_schema(kind)["properties"].items():
            if name in _COMMON_FACTS:
                continue
            definition = dict(definition)
            if "type" not in definition:
                definition["type"] = "string"
            if isinstance(definition.get("items"), dict) and "type" not in definition["items"]:
                definition["items"] = {**definition["items"], "type": "string"}
            props.setdefault(name, definition)
    return {"type": "object", "properties": props, "required": ["sub_kind"]}


def consult_output_schema() -> dict[str, Any]:
    """工具結果形狀（宣告用，RA-16）：DL-C1 回傳欄位的合併，advice 為物件或省略。"""
    props = dict(output_schema(OFFERED_SUB_KINDS[0])["properties"])
    props["advice"] = {"type": "object"}
    props["sub_kind"] = {"type": "string"}
    props["default"] = {"type": "string"}
    return {"type": "object", "properties": props}


def consult_entry() -> ToolEntry:
    return ToolEntry(
        name=CONSULT_TOOL,
        spec=ToolSpec(
            executor=DECISION_EXECUTOR, side_effect_class="internal_read", idempotency="native", presence_bound=False,
            tool_version=1, capability=CONSULT_TAG,
        ),
        descriptor=ToolDescriptor(
            description=CONSULT_DESCRIPTION, input_schema=consult_input_schema(), injected_params=INJECTED_CONSULT,
        ),
        scope=ToolScope(),  # global
        output_schema=consult_output_schema(),
        timeout_s=CONSULT_TIMEOUT_S,
    )
