"""``memory.dream`` 決策點（DL-P1c）：把 ``DreamPlanner.decide`` 收編進決策中樞。

- engine 只是 ``DreamPlanner.decide`` 的薄包裝：使用 ``DreamLoop`` 自己的那一份 planner（純函式，狀態——退避、歷史——
  仍只存在於 ``DreamLoop`` 的 ``NamespaceHistory`` 並隨 snapshot 進出），所以走中樞與直接呼叫的結果與退避演進完全相同。
- ``DecisionResult``／``DecisionPlan`` 是給日誌與審計看的面向（只有 ID、原因碼、trigger 這類欄位，沒有原文）；
  精確的 ``DreamDecision``（含 namespace_id 與 not_before 浮點）由 engine 在同一次呼叫內留存，decider 以 request_id 取回。
- 中樞這條路失敗、逾時、忙碌或例外，一律回 wait 並帶 reason_code，不再呼叫一次 decide；``DreamLoop`` 不中斷。
- ``memory`` 不 import 本模組：``DreamLoop`` 只接受一個 ``Callable[[DreamSnapshot], DreamDecision]``。
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from itertools import count

from uep.decision.contracts import (
    DecisionEngineInfo,
    DecisionFollowup,
    DecisionLoop,
    DecisionPlan,
    DecisionRequest,
    DecisionResult,
)
from uep.decision.hub import DecisionHub
from uep.decision.registry import DecisionPointSpec
from uep.ids import new_id
from uep.memory.dream_planner import DreamDecision, DreamPlanner, DreamSnapshot

logger = logging.getLogger("uep.decision.memory_dream")

KIND_MEMORY_DREAM = "memory.dream"
ON_TIMEOUT_REASON = "dream_decide_timeout"
ON_BUSY_REASON = "dream_decide_busy"
ON_ERROR_REASON = "dream_decide_error"
ON_INVALID_REASON = "dream_decide_invalid"
OUTCOME_RUN = "run"
OUTCOME_WAIT = "wait"
_ENGINE_ID = "dream_planner"
_ENGINE_VERSION = "dl0"
_DEFAULT_BUDGET_MS = 1000


def memory_dream_spec(budget_ms: int = _DEFAULT_BUDGET_MS) -> DecisionPointSpec:
    """``memory.dream`` 規格：async、tick 30s、可 shadow、不開 native、不用 LLM。``budget_ms`` 只是 decider
    等中樞回覆的上限（<= 0 代表不設上限、同步呼叫）。"""
    return DecisionPointSpec(
        kind=KIND_MEMORY_DREAM, mode="async", owner="memory", outcomes=(OUTCOME_RUN, OUTCOME_WAIT),
        budget_ms=budget_ms, on_timeout_reason=ON_TIMEOUT_REASON, on_busy_reason=ON_BUSY_REASON,
        on_error_reason=ON_ERROR_REASON, on_invalid_reason=ON_INVALID_REASON, shadow=True, allow_native=False,
    )


def _fallback_wait(reason: str) -> DreamDecision:
    return DreamDecision("wait", None, None, reason, None)


class MemoryDreamEngine:
    """``DecisionEngine``：``request.facts["snapshot"]``（行程內的 ``DreamSnapshot``）-> ``DecisionResult``。"""

    def __init__(self, planner: DreamPlanner, spec: DecisionPointSpec | None = None) -> None:
        self._planner = planner
        self._spec = spec or memory_dream_spec()
        self._lock = threading.Lock()
        self._last: tuple[str, DreamDecision] | None = None  # 只留最新一筆（逾時的遲到結果不累積）

    def decide(self, request: DecisionRequest) -> DecisionResult:
        spec = self._spec
        snap = request.facts.get("snapshot")
        if not isinstance(snap, DreamSnapshot):
            return self._fallback(request, spec.on_invalid_reason)
        try:
            decision = self._planner.decide(snap)
        except Exception as exc:  # noqa: BLE001 - planner 例外只轉成 fallback，不影響迴圈
            logger.warning("memory.dream planner 例外：%s", type(exc).__name__)
            return self._fallback(request, spec.on_error_reason)
        if decision.action not in spec.outcomes:
            return self._fallback(request, spec.on_invalid_reason)
        with self._lock:
            self._last = (request.request_id, decision)
        return DecisionResult(
            decision_id=new_id("dec"), request_id=request.request_id, kind=request.kind, outcome=decision.action,
            plan=self._plan(decision, snap), confidence=1.0, reason_code=decision.reason,
            engine=DecisionEngineInfo(tier="rule", id=_ENGINE_ID, version=_ENGINE_VERSION),
        )

    def take(self, request_id: str) -> DreamDecision | None:
        """取回同一次 ``decide`` 留存的精確 ``DreamDecision``（取一次即清除）。"""
        with self._lock:
            last = self._last
            if last is None or last[0] != request_id:
                return None
            self._last = None
            return last[1]

    @staticmethod
    def _plan(decision: DreamDecision, snap: DreamSnapshot) -> DecisionPlan:
        """run -> loop.start＋trigger；wait -> 有 not_before 時以 followup 表示下次評估時間。不放 namespace。"""
        if decision.action == OUTCOME_RUN:
            return DecisionPlan(loop=DecisionLoop(start=True, trigger_kind=decision.trigger, goal_code="dream"))
        if decision.not_before is None:
            return DecisionPlan()
        after_ms = max(0, round((decision.not_before - snap.now) * 1000))
        return DecisionPlan(followups=(DecisionFollowup(kind=KIND_MEMORY_DREAM, after_ms=after_ms),))

    @staticmethod
    def _fallback(request: DecisionRequest, reason: str) -> DecisionResult:
        return DecisionResult(
            decision_id=new_id("dec"), request_id=request.request_id, kind=request.kind, outcome=OUTCOME_WAIT,
            plan=DecisionPlan(), confidence=0.0, reason_code=reason,
            engine=DecisionEngineInfo(tier="fallback", id="core.fallback", version="d80"), fallback=True,
        )


class MemoryDreamDecider:
    """``DreamLoop`` 的決策入口：``snapshot -> DreamDecision``，經 ``hub.submit`` 走中樞。"""

    def __init__(self, hub: DecisionHub, engine: MemoryDreamEngine, *, run_id: str | None = None) -> None:
        self._hub = hub
        self._engine = engine
        self._run_id = run_id or new_id("dec")
        self._seq = count(1)
        self._pending: Future[DecisionResult] | None = None  # 逾時而仍在跑的上一次呼叫

    def __call__(self, snapshot: DreamSnapshot) -> DreamDecision:
        try:
            spec = self._hub.registry.get(KIND_MEMORY_DREAM)
        except KeyError:
            return _fallback_wait(ON_ERROR_REASON)
        if self._pending is not None and not self._pending.done():
            return _fallback_wait(spec.on_busy_reason)
        self._pending = None
        request = DecisionRequest(
            request_id=f"{self._run_id}:tick:{next(self._seq)}", kind=KIND_MEMORY_DREAM, trigger="tick",
            facts={"snapshot": snapshot}, allowed_outcomes=spec.outcomes,
        )
        budget_s = spec.budget_ms / 1000 if spec.budget_ms and spec.budget_ms > 0 else None
        try:
            result = self._submit(request, budget_s)
        except FutureTimeout:
            return _fallback_wait(spec.on_timeout_reason)
        except Exception as exc:  # noqa: BLE001 - 中樞路徑例外：回 wait，不重呼叫 decide
            logger.warning("memory.dream 中樞呼叫失敗：%s", type(exc).__name__)
            return _fallback_wait(spec.on_error_reason)
        if result.fallback:
            return _fallback_wait(result.reason_code or spec.on_error_reason)
        decision = self._engine.take(request.request_id)
        if decision is None or decision.action != result.outcome or decision.reason != result.reason_code:
            return _fallback_wait(spec.on_invalid_reason)
        return decision

    def _submit(self, request: DecisionRequest, budget_s: float | None) -> DecisionResult:
        if budget_s is None:
            return self._hub.submit(KIND_MEMORY_DREAM, request)
        future: Future[DecisionResult] = Future()

        def run() -> None:
            try:
                future.set_result(self._hub.submit(KIND_MEMORY_DREAM, request))
            except BaseException as exc:  # noqa: BLE001 - 轉交給等待端
                future.set_exception(exc)

        threading.Thread(target=run, name="uep-dream-decide", daemon=True).start()
        try:
            return future.result(timeout=budget_s)
        except FutureTimeout:
            self._pending = future
            raise


def register_memory_dream(
    hub: DecisionHub, planner: DreamPlanner, *, spec: DecisionPointSpec | None = None, run_id: str | None = None,
) -> MemoryDreamDecider:
    """把 ``memory.dream`` 登記到中樞，回傳可注入 ``DreamLoop`` 的 decider。
    ``planner`` 必須是該 ``DreamLoop`` 自己的那一份（``DreamLoop.planner``）。"""
    spec = spec or memory_dream_spec()
    engine = MemoryDreamEngine(planner, spec)
    hub.register(spec, engine)
    return MemoryDreamDecider(hub, engine, run_id=run_id)
