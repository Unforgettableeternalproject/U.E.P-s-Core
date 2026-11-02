"""記憶工具契約：Access Layer（``uep.memory.access``）與 Memory adapter（``uep.stream.adapters.memory_echo``）共用的單一來源。

N5 去重：``member_recall`` 的 schema、成員標籤常數、工具結果字數上限與簽名檢查原本在兩邊各寫一份（靠測試對照防漂移）。
這裡不 import 任何子系統，也不依賴 ``uep`` 其他部分；Access Layer 為權威消費端並再匯出，adapter 只引用。
"""
from __future__ import annotations

import inspect
from typing import Any

MEMBER_RECALL = "member_recall"
MEMBER_RECALL_DESCRIPTION = (
    "查詢同一個伺服器裡某位成員在共享記憶中可被看到的內容（例如使用者問「你認識某某嗎？"
    "他最近在玩什麼？」）。member 填成員的名稱；query 可選，填想了解的主題，留空則取最近的內容。"
    "只在使用者明確問到特定成員時呼叫；查不到只代表目前沒有可用的共享內容。"
    "Look up what is shared about one named member of this server; call only when the "
    "user asks about that member."
)
MEMBER_RECALL_SCHEMA: dict[str, Any] = {
    "name": MEMBER_RECALL,
    "description": MEMBER_RECALL_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "member": {"type": "string", "description": "成員名稱（顯示名稱或暱稱）。"},
            "query": {"type": "string", "description": "想了解的主題，可留空。"},
        },
        "required": ["member"],
    },
}
UNKNOWN_MEMBER_LABEL = "這位成員"
MEMBER_LABEL_MAX_CHARS = 40
DEFAULT_TOOL_MAX_CHARS = 1500
"""工具結果回給 LLM 的字數上限。deep_recall 的 top_k 可到 20、門檻比自動注入鬆（0.2 vs 0.45），
不截會把第二段續寫的 prompt 塞爆。"""


def declares_param(fn: Any, name: str) -> bool:
    """``fn`` 的簽名是否明確宣告名為 ``name`` 的參數；只有 ``**kwargs`` 或簽名讀不到都算否。"""
    try:
        param = inspect.signature(fn).parameters.get(name)
    except (TypeError, ValueError):
        return False
    return param is not None and param.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY,
    )
