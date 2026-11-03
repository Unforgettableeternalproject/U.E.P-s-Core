"""``turn.reply`` 決策點（DL-1 P0 包裝）：把既有 proposer 插槽包成中樞 engine，行為零變化。

內部沿用 ``ProposerGuard``（單執行緒、busy 立即回預設）、``fallback_proposal``、``validate_proposal`` 與 shadow 機制
（``ShadowRecord`` 由 Orchestrator 搬到這裡，Orchestrator 仍 re-export）。這是 ``uep/decision/`` 內唯一可以 import
``uep.turn`` 的模組。計數鍵（``proposer.busy／timeout／error／invalid``、``shadow.*``）與包裝前一致，
寫進呼叫端傳入的 ``metrics``。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from concurrent.futures import wait as futures_wait
from dataclasses import dataclass, replace

from uep.decision.contracts import (
    DecisionEngineInfo,
    DecisionPlan,
    DecisionReply,
    DecisionRequest,
    DecisionResult,
)
from uep.decision.registry import DecisionPointRegistry, DecisionPointSpec
from uep.ids import new_id
from uep.turn.models import Proposal, ReplyMode, TurnCandidate
from uep.turn.proposer import (
    ProposerGuard,
    TurnProposer,
    fallback_proposal,
    validate_proposal,
)

logger = logging.getLogger("uep.decision.turn_reply")

KIND_TURN_REPLY = "turn.reply"
ON_TIMEOUT_REASON = "proposer_timeout"
ON_BUSY_REASON = "proposer_busy"
ON_ERROR_REASON = "proposer_error"
ON_INVALID_REASON = "proposal_invalid"


@dataclass(frozen=True)
class ShadowRecord:
    """shadow proposer 與主 proposer 的比對（只在記憶體與 metrics，不寫 journal、不提 CC-7）。"""

    candidate_id: str
    shadow_id: str
    primary_mode: str
    primary_reason: str
    shadow_mode: str | None  # None = 例外或驗證失敗
    shadow_reason: str | None
    agree: bool
    status: str  # ok／error／late
    elapsed_ms: int


def turn_reply_spec(budget_ms: int) -> DecisionPointSpec:
    return DecisionPointSpec(
        kind=KIND_TURN_REPLY, mode="sync", owner="turn",
        outcomes=tuple(m.value for m in ReplyMode),
        budget_ms=budget_ms, on_timeout_reason=ON_TIMEOUT_REASON, on_busy_reason=ON_BUSY_REASON,
        on_error_reason=ON_ERROR_REASON, on_invalid_reason=ON_INVALID_REASON, shadow=True, allow_native=True,
    )


def proposal_to_result(proposal: Proposal, request: DecisionRequest, *, fallback: bool = False) -> DecisionResult:
    """Proposal -> DecisionResult。呼叫前須已通過 ``validate_proposal``（fallback 本身永遠合法）。

    每次呼叫產生唯一 ``decision_id``（``dec_<ULID>``），重新評估同一 ``request_id`` 也不會撞號；
    ``request_id`` 保持原樣作為關聯鍵。
    """
    return DecisionResult(
        decision_id=new_id("dec"), request_id=request.request_id, kind=request.kind,
        outcome=proposal.reply_mode.value,
        plan=DecisionPlan(
            route=proposal.route,
            reply=DecisionReply(
                mode=proposal.reply_mode.value, interaction_to=proposal.interaction_to, addressed=proposal.addressed
            ),
        ),
        confidence=proposal.confidence, reason_code=proposal.reason_code,
        engine=DecisionEngineInfo(
            tier="fallback" if fallback else "rule", id=proposal.proposer_id, version=proposal.proposer_version
        ),
        fallback=fallback,
    )


def result_to_proposal(result: DecisionResult) -> Proposal:
    """DecisionResult -> Proposal（P0 的 plan 只含 route、reply.mode、interaction_to，另帶 addressed）。"""
    plan = result.plan
    reply = plan.reply if plan is not None else None
    mode = reply.mode if reply is not None else result.outcome
    return Proposal(
        reply_mode=ReplyMode(mode),
        addressed=reply.addressed if reply is not None else True,
        confidence=result.confidence,
        route=plan.route if plan is not None and plan.route is not None else "direct_chat",
        interaction_to=reply.interaction_to if reply is not None else None,
        reason_code=result.reason_code,
        proposer_id=result.engine.id, proposer_version=result.engine.version,
    )


class TurnReplyEngine:
    """``turn.reply`` 的 engine：主 proposer（預算內）＋ shadow proposers。"""

    def __init__(
        self,
        proposer: TurnProposer,
        *,
        registry: DecisionPointRegistry,
        metrics: Counter[str],
        shadow_proposers: Sequence[TurnProposer] = (),
        on_shadow: Callable[[ShadowRecord], None] | None = None,
    ) -> None:
        self.proposer = proposer
        self._registry = registry
        self.metrics = metrics
        self.guard = ProposerGuard("primary")
        self.shadows = [(sp, ProposerGuard(f"shadow{i}")) for i, sp in enumerate(shadow_proposers)]
        self.on_shadow = on_shadow
        self.shadow_log: deque[ShadowRecord] = deque(maxlen=256)
        self._shadow_pending: set[Future[object]] = set()
        self._lock = threading.Lock()

    @property
    def spec(self) -> DecisionPointSpec:
        """``turn.reply`` 的規格：預算與各 fallback reason 的唯一來源（讀 registry，不另存一份）。"""
        return self._registry.get(KIND_TURN_REPLY)

    @property
    def budget_s(self) -> float | None:
        budget_ms = self.spec.budget_ms
        return budget_ms / 1000.0 if budget_ms is not None and budget_ms > 0 else None

    @budget_s.setter
    def budget_s(self, value: float | None) -> None:
        budget_ms = 0 if value is None else round(value * 1000)
        self._registry.register(replace(self.spec, budget_ms=budget_ms), replace=True)

    def close(self) -> None:
        self.guard.close()
        for _, guard in self.shadows:
            guard.close()

    def wait_shadow(self, timeout: float | None = None) -> bool:
        """等所有進行中的 shadow 比對完成（測試與診斷用）；回傳是否全部完成。"""
        with self._lock:
            pending = set(self._shadow_pending)
        if not pending:
            return True
        _done, not_done = futures_wait(pending, timeout=timeout)
        return not not_done

    def decide(self, request: DecisionRequest) -> DecisionResult:
        """在 Core 端、預算內執行 proposer；逾時、busy、例外、驗證失敗一律回 D80 預設並計 telemetry。

        模型輸出視為不受信資料：只接受 schema 內的列舉與安全字元集（``validate_proposal``）。
        """
        candidate = request.native
        if not isinstance(candidate, TurnCandidate):
            raise TypeError("turn.reply 的 request.native 必須是 TurnCandidate")
        spec = self.spec
        proposal, status = self.guard.call(self.proposer.propose, candidate, self.budget_s)
        fallback = True
        if status == "busy":
            # 上一個呼叫還沒回來（卡住）：立即回預設，不排隊也不等預算
            self.metrics["proposer.busy"] += 1
            result = fallback_proposal(candidate, spec.on_busy_reason)
        elif status == "timeout":
            self.metrics["proposer.timeout"] += 1
            result = fallback_proposal(candidate, spec.on_timeout_reason)
        elif status != "ok":
            self.metrics["proposer.error"] += 1
            result = fallback_proposal(candidate, spec.on_error_reason)
        else:
            bad = validate_proposal(proposal)
            if bad is None:
                result, fallback = proposal, False
            else:
                logger.warning("Proposal 驗證失敗（%s），回預設", bad)
                self.metrics["proposer.invalid"] += 1
                result = fallback_proposal(candidate, spec.on_invalid_reason)
        self._run_shadows(candidate, result)
        return proposal_to_result(result, request, fallback=fallback)

    # ---- shadow mode ----

    def _run_shadows(self, candidate: TurnCandidate, primary: Proposal) -> None:
        """shadow proposer 與主決策同候選並行執行，只記錄差異：不寫 journal、不提 CC-7、不影響 ticket。"""
        for shadow, guard in self.shadows:
            started = time.monotonic()
            future = guard.submit(shadow.propose, candidate)
            if future is None:
                self.metrics["shadow.skipped_busy"] += 1  # 上一個 shadow 呼叫還在跑：跳過，佇列有界
                continue
            with self._lock:
                self._shadow_pending.add(future)
            future.add_done_callback(
                lambda f, g=shadow, c=candidate, p=primary, t=started: self._record_shadow(f, g, c, p, t)
            )

    def _record_shadow(
        self, future: Future[object], shadow: TurnProposer, candidate: TurnCandidate, primary: Proposal, started: float
    ) -> None:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        shadow_id = getattr(shadow, "proposer_id", type(shadow).__name__)
        mode: str | None = None
        reason: str | None = None
        status = "ok"
        try:
            result = future.result()
            if validate_proposal(result) is not None:
                status = "error"
            else:
                assert isinstance(result, Proposal)
                mode, reason = result.reply_mode.value, result.reason_code
                if result.proposer_id != "unknown":
                    shadow_id = result.proposer_id
                if self.budget_s is not None and elapsed_ms > self.budget_s * 1000:
                    status = "late"
        except BaseException:  # noqa: BLE001 - shadow 的任何失敗都只記錄
            status = "error"
        record = ShadowRecord(
            candidate_id=candidate.candidate_id, shadow_id=str(shadow_id)[:64],
            primary_mode=primary.reply_mode.value, primary_reason=primary.reason_code,
            shadow_mode=mode, shadow_reason=reason, agree=mode == primary.reply_mode.value, status=status,
            elapsed_ms=elapsed_ms,
        )
        self.shadow_log.append(record)
        self.metrics[f"shadow.{'agree' if record.agree else 'disagree'}"] += 1
        if status != "ok":
            self.metrics[f"shadow.{status}"] += 1
        if self.on_shadow is not None:
            try:
                self.on_shadow(record)
            except Exception:
                logger.exception("on_shadow 回呼失敗")
        with self._lock:
            self._shadow_pending.discard(future)
