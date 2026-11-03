"""``DecisionHub``：同步 ``decide(kind, request)``（提案 §3.4 sync 模式）。葉節點。

中樞本身不持有權威狀態：只依 ``decision_kind`` 找註冊的 engine 並轉呼叫。預算、busy、fallback、shadow
等語意由 engine 負責（``turn.reply`` 沿用 ``ProposerGuard``）。
"""
from __future__ import annotations

from typing import Protocol

from uep.decision.contracts import DecisionRequest, DecisionResult
from uep.decision.registry import DecisionPointRegistry, DecisionPointSpec


class DecisionEngine(Protocol):
    def decide(self, request: DecisionRequest) -> DecisionResult: ...


class DecisionHub:
    def __init__(self, registry: DecisionPointRegistry | None = None) -> None:
        self.registry = registry or DecisionPointRegistry()
        self._engines: dict[str, DecisionEngine] = {}

    def register(self, spec: DecisionPointSpec, engine: DecisionEngine, *, replace: bool = False) -> None:
        self.registry.register(spec, replace=replace)
        self._engines[spec.kind] = engine

    def decide(self, kind: str, request: DecisionRequest) -> DecisionResult:
        spec = self.registry.get(kind)
        if spec.mode != "sync":
            raise ValueError(f"decide 只支援 sync 決策點：{kind}（{spec.mode}）")
        if request.kind != kind:
            raise ValueError(f"request.kind（{request.kind}）與 kind（{kind}）不符")
        if request.native is not None and not spec.allow_native:
            raise ValueError(f"決策點 {kind} 不接受 native（只有標記 allow_native 的決策點可帶）")
        return self._engines[kind].decide(request)
