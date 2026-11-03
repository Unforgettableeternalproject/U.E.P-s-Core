"""``DecisionHub``：同步 ``decide(kind, request)``（提案 §3.4 sync 模式）。葉節點。

中樞本身不持有權威狀態：只依 ``decision_kind`` 找註冊的 engine 並轉呼叫。預算、busy、fallback、shadow
等語意由 engine 負責（``turn.reply`` 沿用 ``ProposerGuard``）。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Protocol

from uep.decision.contracts import DecisionRequest, DecisionResult
from uep.decision.registry import DecisionPointRegistry, DecisionPointSpec

logger = logging.getLogger("uep.decision.hub")

# 觀察者：decide 成功後以（request, result, spec, elapsed_ms）呼叫；必須非阻塞（如只丟進佇列），例外一律吞掉
DecisionObserver = Callable[[DecisionRequest, DecisionResult, DecisionPointSpec, int], None]


class DecisionEngine(Protocol):
    def decide(self, request: DecisionRequest) -> DecisionResult: ...


class DecisionHub:
    def __init__(self, registry: DecisionPointRegistry | None = None) -> None:
        self.registry = registry or DecisionPointRegistry()
        self._engines: dict[str, DecisionEngine] = {}
        self._observers: list[DecisionObserver] = []

    def add_observer(self, observer: DecisionObserver) -> None:
        """登記決策結果觀察者（DL-3 決策日誌）；觀察者的任何失敗都不影響 ``decide`` 的結果。"""
        self._observers.append(observer)

    def engine(self, kind: str) -> DecisionEngine | None:
        return self._engines.get(kind)

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
        started = time.perf_counter()
        result = self._engines[kind].decide(request)
        if self._observers:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            for observer in tuple(self._observers):
                try:
                    observer(request, result, spec, elapsed_ms)
                except Exception:
                    logger.exception("決策觀察者失敗")
        return result
