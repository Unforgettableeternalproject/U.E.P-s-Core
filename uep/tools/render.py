"""``render_tools``：送給 provider 的 tools JSON 的**唯一產生點**（Agent 架構提案 §6.2，D60）。

送給 provider 的 tools 恰等於 ``capability_set`` 的渲染；除註冊表投影外，任何程式碼不得產生 tools JSON。
靜態測試（tests/uep/test_tool_projection.py）檢查 ``uep/`` 內沒有手寫的 ``{"type": "function", ...}`` 清單，
白名單只有這個檔案與 S-A 保留的舊衍生路徑。只輸出 ``type=function``，不產生 hosted／built-in 工具。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from uep.tools.schema import thaw

DIALECTS = frozenset({"openai_chat"})  # Responses／Ollama 等方言隨 Spike 1 的模型層加入


def render_tools(capability_set: Mapping[str, Any], dialect: str = "openai_chat") -> list[dict[str, Any]]:
    """``capability_set`` -> provider 的 tools 清單（順序同投影順序；深複本，呼叫端改不到投影結果）。
    capability_set 形狀不合（缺欄位、重複工具名）拋 ``ValueError``，不靜默跳過。"""
    if dialect not in DIALECTS:
        raise ValueError(f"不支援的 dialect：{dialect!r}")
    tools = capability_set.get("tools") if isinstance(capability_set, Mapping) else None
    if not isinstance(tools, (list, tuple)):
        raise ValueError("capability_set 必須帶 tools 清單")  # noqa: TRY004
    rendered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in tools:
        if not isinstance(item, Mapping):
            raise ValueError("capability_set.tools 的項目必須是映射")  # noqa: TRY004
        name, description, schema = item.get("tool"), item.get("description"), item.get("input_schema")
        if not isinstance(name, str) or not name or not isinstance(description, str) or not isinstance(schema, Mapping):
            raise ValueError("capability_set.tools 項目必須帶 tool／description／input_schema")
        if name in seen:
            raise ValueError(f"capability_set 含重複工具：{name}")
        seen.add(name)
        rendered.append({
            "type": "function",
            "function": {"name": name, "description": description, "parameters": thaw(schema)},
        })
    return rendered
