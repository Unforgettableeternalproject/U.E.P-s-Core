"""測試用工具組的註冊表宣告（``UEP_TOOL_TESTKIT``；Agent 架構提案 §5、§6.4、§8.2）。

目的：讓人在 Console 以完整系統（``UEP_TOOL_PATH=agent``＋``UEP_AGENT_LOOP_ENTRY=1``＋``/loop``）實測工具層契約，
涵蓋多輪呼叫、並行、取消／逾時、核准 park。四個工具都是 ``builtin``／``core_local``／global，沒有任何外部效果：

- ``test_echo``：``internal_read``、``parallel_safe``，回傳輸入文字。
- ``test_clock``：``internal_read``、``parallel_safe``，回傳目前時間（UTC 與本地）。
- ``test_sleep``：``internal_read``、``parallel_one_shot``、``interruptible``、``cancellable``；睡指定秒數。``timeout_s`` 刻意小於
  秒數上限，睡超過 ``SLEEP_TIMEOUT_S`` 秒會走逾時路徑（loop 呼叫 ``executor.cancel``），``/cancel`` 或 Supervisor 也會取消它。
- ``test_approval_echo``：``external_effect``＋``approval_level=per_action``、serial；真實 ``execute_action`` 以
  ``side_effect_class`` 判定是否要核准，所以這個工具**如實借用** ``external_effect`` 來觸發 park／核准流程，
  實際沒有任何外部效果（description 也寫明）。

工具名用底線而非點：provider（OpenAI function calling）的函式名只收 ``[a-zA-Z0-9_-]``，帶點的名稱會被 API 拒絕，Console 實測會直接失敗。
能力標籤 ``test.read``（上限 ``internal_read``）與 ``test.approval``（上限 ``external_effect``）在 ``CAPABILITY_CEILING``；
決策層的 route 表不含它們，所以只在開關開啟時由組裝層併入 ``/loop`` 的 capability_tags（見 ``uep.echo.agent_wiring.build_loop_entry``）。
開關關閉時這些工具不進任何執行期註冊表，標籤也不出現在任何投影。

另有兩個 composite（Agent 架構提案 §5.7.4、RA-14；串起上面的工具，用來在 Console 驗 composite 的展開與 park／resume）：

- ``test_clock_echo``：``test_clock`` -> ``test_echo``，第二步的 ``text`` 引用第一步 receipt 的 ``result_ref``
  （receipt 持久，跨重啟可續跑；不引用回合範圍的 ``text``）。因此 ``test_clock``／``test_echo`` 的 ``output_schema`` 宣告 ``result_ref``。
- ``test_echo_then_approve``：``test_echo`` -> ``test_approval_echo``，第二步需要核准；用來驗 composite 子步驟 park -> ``/approve`` -> 續跑，
  且已成功的 ``test_echo`` 不重播。分類取子步驟最大值（external_effect／per_action），composite 自身 serial。
"""
from __future__ import annotations

from typing import Any

from uep.adapters.tool import CompositeSpec, CompositeStep, ToolDescriptor, ToolEntry, ToolScope, ToolSpec
from uep.tools.registry import ToolRegistry

TESTKIT_EXECUTOR = "core_testkit"
TEST_READ_TAG = "test.read"
TEST_APPROVAL_TAG = "test.approval"
TESTKIT_TAGS: frozenset[str] = frozenset({TEST_READ_TAG, TEST_APPROVAL_TAG})

TOOL_ECHO = "test_echo"
TOOL_CLOCK = "test_clock"
TOOL_SLEEP = "test_sleep"
TOOL_APPROVAL_ECHO = "test_approval_echo"
TOOL_CLOCK_ECHO = "test_clock_echo"
TOOL_ECHO_THEN_APPROVE = "test_echo_then_approve"
TESTKIT_TOOLS: tuple[str, ...] = (
    TOOL_ECHO, TOOL_CLOCK, TOOL_SLEEP, TOOL_APPROVAL_ECHO, TOOL_CLOCK_ECHO, TOOL_ECHO_THEN_APPROVE,
)

ECHO_TEXT_MAX = 500
SLEEP_MAX_S = 30.0
# test_sleep 的 executor 總時限：小於 SLEEP_MAX_S，睡超過這個秒數就會被 loop 判逾時並 cancel（用來驗逾時路徑）
SLEEP_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 5.0

ECHO_DESCRIPTION = (
    "測試工具：把輸入文字原樣回傳，沒有任何副作用。Test tool: returns the given text unchanged; no side effects."
)
CLOCK_DESCRIPTION = (
    "測試工具：回傳目前時間（UTC 與本地時間）。Test tool: returns the current time (UTC and local); no side effects."
)
SLEEP_DESCRIPTION = (
    f"測試工具：等待指定秒數（0 到 {SLEEP_MAX_S:g}）後回傳，可被取消；超過 {SLEEP_TIMEOUT_S:g} 秒會觸發逾時（用來驗證取消與逾時）。"
    "Test tool: waits for the given seconds; cancellable, and times out beyond the tool time limit; no side effects."
)
APPROVAL_ECHO_DESCRIPTION = (
    "測試工具（實際沒有任何外部效果）：把訊息原樣回傳，但被標註為需要使用者核准的外部效果，用來測試等待核准與核准後續行的流程。"
    "Test tool with NO real external effect: echoes the message, but is classified as an approval-required external effect "
    "so the approval flow can be exercised."
)
CLOCK_ECHO_DESCRIPTION = (
    "測試工具（composite）：先取得目前時間，再把第一步結果的參考（result_ref）用回聲工具回傳，沒有任何副作用。"
    "Test composite: reads the clock, then echoes the first step's result reference; no side effects."
)
ECHO_THEN_APPROVE_DESCRIPTION = (
    "測試工具（composite，實際沒有任何外部效果）：先回聲輸入文字，再執行需要使用者核准的測試步驟，用來測試 composite 的等待核准與核准後續行。"
    "Test composite with NO real external effect: echoes the text, then runs an approval-required test step."
)

ECHO_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string", "description": "要回傳的文字。", "maxLength": ECHO_TEXT_MAX}},
    "required": ["text"],
    "additionalProperties": False,
}
ECHO_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"echo": {"type": "string"}, "length": {"type": "integer"}, "result_ref": {"type": "string"}},
    "required": ["echo", "length"],
    "additionalProperties": False,
}
CLOCK_INPUT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}
CLOCK_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "utc": {"type": "string"}, "local": {"type": "string"}, "timezone": {"type": "string"}, "result_ref": {"type": "string"},
    },
    "required": ["utc", "local", "timezone"],
    "additionalProperties": False,
}
SLEEP_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "seconds": {"type": "number", "description": "等待的秒數。", "minimum": 0, "maximum": SLEEP_MAX_S},
    },
    "required": ["seconds"],
    "additionalProperties": False,
}
SLEEP_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string"}, "requested_seconds": {"type": "number"}, "slept_seconds": {"type": "number"},
    },
    "required": ["status", "requested_seconds", "slept_seconds"],
    "additionalProperties": False,
}
APPROVAL_ECHO_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"message": {"type": "string", "description": "要回傳的訊息。", "maxLength": ECHO_TEXT_MAX}},
    "required": ["message"],
    "additionalProperties": False,
}
APPROVAL_ECHO_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"echo": {"type": "string"}, "note": {"type": "string"}},
    "required": ["echo", "note"],
    "additionalProperties": False,
}
ECHO_THEN_APPROVE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string", "description": "要回聲並送核准的文字。", "maxLength": ECHO_TEXT_MAX}},
    "required": ["text"],
    "additionalProperties": False,
}


def _read_spec(*, interruptible: str = "non_interruptible") -> ToolSpec:
    return ToolSpec(
        executor=TESTKIT_EXECUTOR, side_effect_class="internal_read", idempotency="native", presence_bound=False,
        tool_version=1, capability=TEST_READ_TAG, interruptible=interruptible,  # type: ignore[arg-type]
    )


def testkit_entries() -> list[ToolEntry]:
    """測試工具條目（順序即曝光順序）。"""
    return [
        ToolEntry(
            name=TOOL_ECHO, spec=_read_spec(),
            descriptor=ToolDescriptor(description=ECHO_DESCRIPTION, input_schema=ECHO_INPUT_SCHEMA),
            scope=ToolScope(), output_schema=ECHO_OUTPUT_SCHEMA, concurrency="parallel_safe", timeout_s=READ_TIMEOUT_S,
        ),
        ToolEntry(
            name=TOOL_CLOCK, spec=_read_spec(),
            descriptor=ToolDescriptor(description=CLOCK_DESCRIPTION, input_schema=CLOCK_INPUT_SCHEMA),
            scope=ToolScope(), output_schema=CLOCK_OUTPUT_SCHEMA, concurrency="parallel_safe", timeout_s=READ_TIMEOUT_S,
        ),
        ToolEntry(
            name=TOOL_SLEEP, spec=_read_spec(interruptible="interruptible"),
            descriptor=ToolDescriptor(description=SLEEP_DESCRIPTION, input_schema=SLEEP_INPUT_SCHEMA),
            scope=ToolScope(), output_schema=SLEEP_OUTPUT_SCHEMA, concurrency="parallel_one_shot",
            timeout_s=SLEEP_TIMEOUT_S, cancellable=True,
        ),
        ToolEntry(
            name=TOOL_APPROVAL_ECHO,
            spec=ToolSpec(
                executor=TESTKIT_EXECUTOR, side_effect_class="external_effect", idempotency="via_action_id",
                presence_bound=False, tool_version=1, capability=TEST_APPROVAL_TAG, approval_level="per_action",
            ),
            descriptor=ToolDescriptor(description=APPROVAL_ECHO_DESCRIPTION, input_schema=APPROVAL_ECHO_INPUT_SCHEMA),
            scope=ToolScope(), output_schema=APPROVAL_ECHO_OUTPUT_SCHEMA,
        ),
        ToolEntry(
            name=TOOL_CLOCK_ECHO, spec=_read_spec(),
            descriptor=ToolDescriptor(description=CLOCK_ECHO_DESCRIPTION, input_schema=CLOCK_INPUT_SCHEMA),
            scope=ToolScope(), output_schema=ECHO_OUTPUT_SCHEMA, timeout_s=READ_TIMEOUT_S,
            composite=CompositeSpec(steps=(
                CompositeStep(TOOL_CLOCK, {}),
                CompositeStep(TOOL_ECHO, {"text": "$.steps[0].output.result_ref"}),
            )),
        ),
        ToolEntry(
            name=TOOL_ECHO_THEN_APPROVE,
            spec=ToolSpec(
                executor=TESTKIT_EXECUTOR, side_effect_class="external_effect", idempotency="via_action_id",
                presence_bound=False, tool_version=1, capability=TEST_APPROVAL_TAG, approval_level="per_action",
            ),
            descriptor=ToolDescriptor(
                description=ECHO_THEN_APPROVE_DESCRIPTION, input_schema=ECHO_THEN_APPROVE_INPUT_SCHEMA,
            ),
            scope=ToolScope(), output_schema=APPROVAL_ECHO_OUTPUT_SCHEMA,
            composite=CompositeSpec(steps=(
                CompositeStep(TOOL_ECHO, {"text": "$.input.text"}),
                CompositeStep(TOOL_APPROVAL_ECHO, {"message": "$.input.text"}),
            )),
        ),
    ]


def with_testkit(registry: ToolRegistry) -> ToolRegistry:
    """在既有註冊表之上併入測試工具，回傳新的（已驗證、已凍結）註冊表；不改動傳入的實例。
    已含測試工具者原樣回傳（冪等）。既有條目已凍結且 ``schema_hash`` 已驗證，重建不改變其宣告。"""
    existing = registry.entries()
    if any(name in existing for name in TESTKIT_TOOLS):
        return registry
    # 同套件內取 known_modules（沒有公開存取子；測試工具沒有 requires，這裡只為不改變既有條目的 readiness）
    known = getattr(registry, "_known_modules", frozenset())
    return ToolRegistry.build([*existing.values(), *testkit_entries()], known_modules=known)
