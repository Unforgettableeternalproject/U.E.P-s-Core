"""記憶工具的註冊表宣告（切片 A）：``deep_recall``／``force_remember``／``member_recall``。

這裡是記憶工具 schema 與分類的**單一來源**：``uep.memory.access`` 的 ``TOOL_CLASSES``／``tool_definitions()`` 與
``uep.echo.host`` 的曝光都由註冊表衍生，不再各寫一份字面值。

``deep_recall``／``force_remember`` 的 schema 是 echo_memory ``TOOL_DECLARATIONS`` 的凍結副本（``uep/`` 不得 import
子系統，見 test_import_boundary）；``tests/uep/test_tool_registry.py`` 有漂移測試與子系統現值對照。
``member_recall`` 的 schema 取自共用契約 ``uep.stream.contracts.memory_tools``（adapter 也引用同一份）。

分類沿用 D79 的現況：沒有核准（approval_level=none）、不可中斷、並行資格不宣告（預設 serial）。
切片 E3：寫入類（``force_remember``、「關於我」三工具）走完整四階段（create_action -> approve -> execute_action ->
receipt），``approval_level=none``——使用者明確要求就直接執行，只補 ActionIntent／receipt 與稽核；分類欄未改，不升版。
"""
from __future__ import annotations

import functools
from typing import Any

from uep.adapters.tool import ToolDescriptor, ToolEntry, ToolScope, ToolSpec
from uep.stream.contracts import memory_tools as _contract
from uep.tools.registry import ToolRegistry

MEMORY_EXECUTOR = "core_memory"
# member_recall 需要的投影脈絡：場域（guild）成員名單。host 的曝光判斷與條目宣告共用這個常數。
GUILD_ROSTER_CONTEXT = "guild_roster"
# PD-12：身分與場域由 Core 每輪在 Gateway 注入（LLM 面 schema 看不到，也不能提供）；執行時 executor 只讀這些欄位
INJECTED_IDENTITY = ("actor_id", "audience", "segment_id")
INJECTED_GUILD = ("guild_id",)

# 工具結果（execute_tool 的回傳）形狀：失敗時帶 code（denied／timeout／failed）。
MEMORY_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "result": {"type": "string"},
        "code": {"type": "string"},
    },
    "required": ["success", "result"],
}

FORCE_REMEMBER_DESCRIPTION = (
    "只有在使用者明確要求記住時才呼叫（例如「記住」「幫我記」「別忘了」這類明確指示）。"
    "一般對話內容系統每輪都會自動寫入記憶，不需要也不要為此呼叫。"
    "content 寫成一句完整的第三人稱事實。"
    "Only call when the user explicitly asks you to remember something; ordinary "
    "conversation is already stored automatically every turn."
)

DEEP_RECALL_DESCRIPTION = (
    "Perform a deeper memory search beyond the auto-injected context. "
    "Use when standard context is insufficient or you need more specific memories."
)

FORCE_REMEMBER_SCHEMA: dict[str, Any] = {'type': 'object',
 'properties': {'content': {'type': 'string',
                            'description': 'The information to store in long-term memory.'},
                'reason': {'type': 'string',
                           'description': 'Why this information is important enough to '
                                          'remember.'},
                'salience': {'type': 'number',
                             'description': 'Importance score between 0.0 and 1.0 (default '
                                            '0.85). Higher means more important.'},
                'content_origin': {'type': 'string',
                                   'enum': ['user', 'agent', 'tool', 'unknown'],
                                   'description': 'Optional. Where the stored content actually '
                                                  "came from: 'user' (the user said it), "
                                                  "'agent' (your own statement or inference), "
                                                  "'tool' (a tool result), or 'unknown'. Omit "
                                                  'if you are not sure; omitted is recorded as '
                                                  "'unknown', never assumed to be the user."},
                'origin_evidence_ids': {'type': 'array',
                                        'items': {'type': 'string'},
                                        'description': 'Optional. Evidence ids (ev_...) of the '
                                                       'earlier statements this content comes '
                                                       'from, if you know them.'}},
 'required': ['content', 'reason']}

DEEP_RECALL_SCHEMA: dict[str, Any] = {'type': 'object',
 'properties': {'query': {'type': 'string',
                          'description': 'The search query to find relevant memories.'},
                'top_k': {'type': 'integer',
                          'description': 'Number of memories to retrieve (default 10, maximum '
                                         '20).'}},
 'required': ['query']}


def memory_entries() -> list[ToolEntry]:
    """記憶工具條目；順序即曝光順序（與舊版 ``think.tools`` 一致：force_remember、deep_recall、member_recall）。"""
    return [
        ToolEntry(
            name="force_remember",
            spec=ToolSpec(
                executor=MEMORY_EXECUTOR, side_effect_class="reversible_local", idempotency="none",
                presence_bound=False, tool_version=1, capability="memory.write",
            ),
            descriptor=ToolDescriptor(
                description=FORCE_REMEMBER_DESCRIPTION, input_schema=FORCE_REMEMBER_SCHEMA,
                injected_params=INJECTED_IDENTITY,
            ),
            scope=ToolScope(),
            output_schema=MEMORY_OUTPUT_SCHEMA,
        ),
        ToolEntry(
            name="deep_recall",
            spec=ToolSpec(
                executor=MEMORY_EXECUTOR, side_effect_class="internal_read", idempotency="native",
                presence_bound=False, tool_version=1, capability="memory.read",
            ),
            descriptor=ToolDescriptor(
                description=DEEP_RECALL_DESCRIPTION, input_schema=DEEP_RECALL_SCHEMA,
                injected_params=INJECTED_IDENTITY,
            ),
            scope=ToolScope(),
            output_schema=MEMORY_OUTPUT_SCHEMA,
        ),
        ToolEntry(
            name=_contract.MEMBER_RECALL,
            spec=ToolSpec(
                executor=MEMORY_EXECUTOR, side_effect_class="internal_read", idempotency="native",
                presence_bound=False, tool_version=1, capability="memory.read",
            ),
            descriptor=ToolDescriptor(
                description=_contract.MEMBER_RECALL_DESCRIPTION,
                input_schema=_contract.MEMBER_RECALL_SCHEMA["parameters"],
                injected_params=INJECTED_IDENTITY + INJECTED_GUILD,
            ),
            # 取代 host._refresh_tools 內「有 guild 才曝光」的硬編碼；投影在切片 B 啟用。
            scope=ToolScope(requires_context=frozenset({GUILD_ROSTER_CONTEXT})),
            output_schema=MEMORY_OUTPUT_SCHEMA,
        ),
        *(_about_me_entry(name) for name in ABOUT_ME_REGISTERED),
    ]


# correct_about_me 不註冊（DQ-16 不可逆）：見 _about_me_entry 說明
ABOUT_ME_REGISTERED = (_contract.RETRACT_ABOUT_ME, _contract.OPT_OUT_ABOUT_ME)


def _about_me_entry(name: str) -> ToolEntry:
    """「關於我」寫入類工具（切片 E3；DQ-16）。schema 取自共用契約。

    分類是 ``reversible_local``、無核准（Bernie 裁決：使用者明確要求就直接執行，只補 ActionIntent／receipt 與稽核）：
    opt_out／retract 依 DQ-16 暫定。**``correct_about_me`` 不註冊**：DQ-16 稱不可逆，應標 ``external_effect`` 以上，
    但 ``memory.write`` 的能力上限（``CAPABILITY_CEILING``）是 ``reversible_local``，註冊表會拒絕這個組合；
    不為了通過而調低分類或改上限，所以保持不可達（開放與否另卡，G1）。
    身分與場域由 Core 注入（PD-12）；沒有 guild 脈絡就不投出（對象是場域共享記憶裡關於說話者的旁證）。"""
    declaration = _contract.ABOUT_ME_SCHEMAS[name]
    return ToolEntry(
        name=name,
        spec=ToolSpec(
            executor=MEMORY_EXECUTOR, side_effect_class="reversible_local", idempotency="none",
            presence_bound=False, tool_version=1, capability="memory.write",
        ),
        descriptor=ToolDescriptor(
            description=declaration["description"], input_schema=declaration["parameters"],
            injected_params=INJECTED_IDENTITY + INJECTED_GUILD,
        ),
        scope=ToolScope(requires_context=frozenset({GUILD_ROSTER_CONTEXT})),
        output_schema=MEMORY_OUTPUT_SCHEMA,
    )


@functools.lru_cache(maxsize=1)
def memory_registry() -> ToolRegistry:
    """記憶工具註冊表（建構即驗證 RA-1～16 並凍結）。``access.TOOL_CLASSES``／``tool_definitions()`` 由它衍生。
    lockfile 的比對由 CI 測試負責（tests/uep/test_tool_registry.py），這裡不做執行期 I/O。"""
    return ToolRegistry.build(memory_entries())
