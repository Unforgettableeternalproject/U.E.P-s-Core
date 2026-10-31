"""FakeToolSpecResolver：測試用的可註冊 ``ToolSpecResolver``。

``register(tool, **spec)`` 可隨時登記或覆寫（模擬註冊表升版、改分類）；``resolve`` 每次查當前資料。
未給的欄位用測試友善預設（executor="fake"、reversible_local、via_action_id、非 presence_bound、v1）。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from uep.adapters.tool import ToolSpec
from uep.tools.registry import unknown_tool

_DEFAULTS: dict[str, Any] = {
    "executor": "fake", "side_effect_class": "reversible_local", "idempotency": "via_action_id",
    "presence_bound": False, "tool_version": 1,
}


class FakeToolSpecResolver:
    def __init__(self, **tools: Mapping[str, Any]) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for name, spec in tools.items():
            self.register(name, **spec)

    def register(self, tool: str, **spec: Any) -> ToolSpec:
        built = ToolSpec(**{**_DEFAULTS, **spec})
        self._specs[tool] = built
        return built

    def unregister(self, tool: str) -> None:
        self._specs.pop(tool, None)

    def resolve(self, tool: str) -> ToolSpec:
        spec = self._specs.get(tool)
        if spec is None:
            raise unknown_tool(tool)
        return spec

    def specs(self) -> Mapping[str, ToolSpec]:
        return dict(self._specs)
