"""靜態記憶體工具註冊表：實作 ``ToolSpecResolver``（契約 TG-1、D54）。

建構後凍結（內部複製成唯讀映射）；``resolve`` 每次都查這份當前資料。TaskManager 只依賴
``uep.adapters.tool`` 的協定，不 import 本模組。
"""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from uep.adapters.tool import ToolSpec
from uep.tasks.manager import PolicyViolation

TOOL_UNKNOWN = "tool_unknown"


def unknown_tool(tool: str) -> PolicyViolation:
    """解析不到的統一例外（resolver 實作共用；訊息不含參數內容）。"""
    return PolicyViolation(f"未註冊的工具：{tool}", code=TOOL_UNKNOWN)


class StaticToolRegistry:
    def __init__(self, specs: Mapping[str, ToolSpec]) -> None:
        for name, spec in specs.items():
            if not isinstance(name, str) or not name:
                raise ValueError("工具名必須是非空字串")
            if not isinstance(spec, ToolSpec):
                raise TypeError(f"{name}: 必須是 ToolSpec")
            if isinstance(spec.tool_version, bool) or not isinstance(spec.tool_version, int) or spec.tool_version < 1:
                raise ValueError(f"{name}: tool_version 必須是 >= 1 的整數")
        self._specs: Mapping[str, ToolSpec] = MappingProxyType(dict(specs))

    def resolve(self, tool: str) -> ToolSpec:
        spec = self._specs.get(tool)
        if spec is None:
            raise unknown_tool(tool)
        return spec

    def specs(self) -> Mapping[str, ToolSpec]:
        return self._specs
