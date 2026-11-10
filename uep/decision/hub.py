"""``DecisionHub``：同步 ``decide(kind, request)``（提案 §3.4 sync 模式）。葉節點。

中樞本身不持有權威狀態：只依 ``decision_kind`` 找註冊的 engine 並轉呼叫。預算、busy、fallback、shadow
等語意由 engine 負責（``turn.reply`` 沿用 ``ProposerGuard``）。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Protocol

from uep.decision.contracts import DecisionRequest, DecisionResult
from uep.decision.registry import DecisionPointRegistry, DecisionPointSpec

logger = logging.getLogger("uep.decision.hub")

# spec 未設 on_invalid_reason 時，非白名單區段被剝除的預設 reason_code
REASON_PLAN_SECTION_REJECTED = "plan_section_rejected"
# 需在 spec.plan_sections 宣告才允許帶的區段（policy：僅政策發布事實；capability：DL-4 hint，proactive 不宣告）
_GUARDED_SECTIONS = ("policy", "capability")

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
        return self._dispatch(kind, spec, request)

    def submit(self, kind: str, request: DecisionRequest) -> DecisionResult:
        """async 決策點的登記入口（提案 §3.4）：由 engine 以 request.facts 產出結果並通知觀察者，不帶 native。

        engine 不阻塞呼叫端；何時呼叫 ``submit``（例如只在非靜默的 tick 才提交）由 engine 的擁有者決定。
        """
        spec = self.registry.get(kind)
        if spec.mode != "async":
            raise ValueError(f"submit 只支援 async 決策點：{kind}（{spec.mode}）")
        if request.kind != kind:
            raise ValueError(f"request.kind（{request.kind}）與 kind（{kind}）不符")
        if request.native is not None:
            raise ValueError(f"決策點 {kind} 不接受 native")
        return self._dispatch(kind, spec, request)

    def publish(self, kind: str, request: DecisionRequest) -> DecisionResult:
        """publish 決策點的登記入口（提案 §3.4）：記錄別處已定案的發布事實（如政策快照），不帶 native。

        engine 只記錄與審計，不重新計算；呼叫端（發布監聽者）必須輕量。
        """
        spec = self.registry.get(kind)
        if spec.mode != "publish":
            raise ValueError(f"publish 只支援 publish 決策點：{kind}（{spec.mode}）")
        if request.kind != kind:
            raise ValueError(f"request.kind（{request.kind}）與 kind（{kind}）不符")
        if request.native is not None:
            raise ValueError(f"決策點 {kind} 不接受 native")
        return self._dispatch(kind, spec, request)

    def _dispatch(self, kind: str, spec: DecisionPointSpec, request: DecisionRequest) -> DecisionResult:
        started = time.perf_counter()
        result = self._engines[kind].decide(request)
        if result.plan is not None:
            # 中樞原則：失敗回 fallback、不拋例外；剝除未宣告的區段並照常通知觀察者（只記 kind 與 engine id，不記內容）
            for section in _GUARDED_SECTIONS:
                if getattr(result.plan, section) is not None and section not in spec.plan_sections:
                    logger.warning("決策點 %s 的 engine %s 帶了未宣告的 %s 區段，已剝除", kind, result.engine.id, section)
                    result = replace(
                        result, plan=replace(result.plan, **{section: None}), fallback=True,
                        reason_code=spec.on_invalid_reason or REASON_PLAN_SECTION_REJECTED,
                    )
        if self._observers:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            for observer in tuple(self._observers):
                try:
                    observer(request, result, spec, elapsed_ms)
                except Exception:
                    logger.exception("決策觀察者失敗")
        return result
