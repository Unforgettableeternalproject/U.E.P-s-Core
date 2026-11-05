"""``turn.interject`` 決策點（DL-P1 第一點）：把被動模式插話（``TickingProposer.tick``）收編進決策中樞。

P1 的規則是「primary 行為零變化、新路徑只以 shadow 並跑並記錄比對」：
- primary 仍由 Orchestrator 對主 proposer 呼叫 ``tick``（同一個 guard、同一個預算），本模組不介入、不改時序；
  Orchestrator 之後把結果交給 ``TurnInterjectEngine.on_tick`` 轉成 ``DecisionResult``／``DecisionPlan`` 記錄。
- shadow proposer 若也實作 ``tick``，與 primary 同一輪在各自的 guard 上非阻塞地 tick，只比對「會不會插話」。
- 靜默的 tick（primary 與 shadow 都沒有插話）不寫日誌，只計數；否則每 250ms 一列會灌爆決策日誌。
- 任何失敗（engine 例外、shadow 逾時／例外）只計數與記 log，不影響插話。日誌不存原文，只存 ID 與英文原因碼。

本模組是 ``uep/decision/`` 內第二個可以 import ``uep.turn`` 的轉接層。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from concurrent.futures import wait as futures_wait
from dataclasses import replace
from typing import Any

from uep.decision.contracts import (
    DecisionEngineInfo,
    DecisionPlan,
    DecisionReply,
    DecisionRequest,
    DecisionResult,
)
from uep.decision.registry import DecisionPointRegistry, DecisionPointSpec
from uep.decision.turn_reply import ShadowRecord, TurnReplyEngine
from uep.ids import new_id
from uep.turn.models import ReplyMode
from uep.turn.proposer import Interjection, TickingProposer, validate_proposal

logger = logging.getLogger("uep.decision.turn_interject")

KIND_TURN_INTERJECT = "turn.interject"
ON_TIMEOUT_REASON = "interject_timeout"
ON_BUSY_REASON = "interject_busy"
ON_ERROR_REASON = "interject_error"
ON_INVALID_REASON = "interject_invalid"
NO_INTERJECT_REASON = "no_interject"

_PASS = ReplyMode.PASS.value
_QUIET_MODES = frozenset({ReplyMode.PASS, ReplyMode.DEFER})


def turn_interject_spec(budget_ms: int) -> DecisionPointSpec:
    """``turn.interject`` 的規格。mode 為 async（提案 §3.4）；``budget_ms`` 沿用 primary tick 現行使用的
    proposer 預算，在這裡只當 shadow tick 的「過慢」門檻，不限制 primary。不開 native。"""
    return DecisionPointSpec(
        kind=KIND_TURN_INTERJECT, mode="async", owner="turn",
        outcomes=tuple(m.value for m in ReplyMode),
        budget_ms=budget_ms, on_timeout_reason=ON_TIMEOUT_REASON, on_busy_reason=ON_BUSY_REASON,
        on_error_reason=ON_ERROR_REASON, on_invalid_reason=ON_INVALID_REASON, shadow=True, allow_native=False,
    )


def summarize_interjection(ij: object) -> dict[str, Any]:
    """Interjection -> 不含原文的摘要（``request.facts`` 用）。不受信的欄位驗證失敗時只留 ``valid=False``。"""
    proposal = getattr(ij, "proposal", None)
    bad = validate_proposal(proposal)
    if bad is not None:
        return {"valid": False, "effective": False, "invalid_reason": bad}
    reply_mode = proposal.reply_mode
    return {
        "valid": True, "effective": reply_mode not in _QUIET_MODES,
        "speaker_ref": ij.speaker_ref, "reply_mode": reply_mode.value, "reason_code": proposal.reason_code,
        "confidence": proposal.confidence, "route": proposal.route, "interaction_to": proposal.interaction_to,
        "addressed": proposal.addressed, "proposer_id": proposal.proposer_id,
        "proposer_version": proposal.proposer_version, "utterance_refs": tuple(ij.utterance_refs),
    }


def _first_effective(summaries: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    return next((s for s in summaries if s.get("effective")), None)


def interject_outcome(summaries: Sequence[dict[str, Any]]) -> tuple[str, str]:
    """摘要 -> ``(outcome, reason_code)``：取第一個會實際插話的提議；沒有就是 pass。"""
    first = _first_effective(summaries)
    if first is None:
        return _PASS, NO_INTERJECT_REASON
    return str(first["reply_mode"]), str(first["reason_code"])


class _TickState:
    """同一輪 tick 的 primary 結果：延遲到第一次需要記錄時才提交給中樞（只提交一次）。"""

    def __init__(self, request: DecisionRequest, outcome: str, reason: str) -> None:
        self.request = request
        self.outcome = outcome
        self.reason = reason
        self._lock = threading.Lock()
        self._result: DecisionResult | None = None

    def emit(self, submit: Callable[[str, DecisionRequest], DecisionResult]) -> DecisionResult:
        with self._lock:
            if self._result is None:
                self._result = submit(KIND_TURN_INTERJECT, self.request)
            return self._result


class TurnInterjectEngine:
    """``turn.interject`` 的 engine：primary 結果轉 DecisionResult＋對 shadow proposer 並行 tick 比對。"""

    def __init__(
        self,
        reply_engine: TurnReplyEngine,
        *,
        registry: DecisionPointRegistry,
        metrics: Counter[str],
        submit: Callable[[str, DecisionRequest], DecisionResult],
        run_id: str,
        on_shadow: Callable[[ShadowRecord], None] | None = None,
    ) -> None:
        # 與 turn.reply 共用 proposer／guard／shadows：有狀態的 proposer 的 propose 與 tick 必須在同一條執行緒序列化
        self._reply = reply_engine
        self._registry = registry
        self.metrics = metrics
        self._submit = submit
        self._run_id = run_id
        self.on_shadow = on_shadow
        self.shadow_log: deque[ShadowRecord] = deque(maxlen=256)
        self._shadow_pending: set[Future[Any]] = set()
        self._lock = threading.Lock()
        self._seq = 0

    @property
    def spec(self) -> DecisionPointSpec:
        """預算與各 fallback reason 的唯一來源（讀 registry）。"""
        return self._registry.get(KIND_TURN_INTERJECT)

    @property
    def budget_s(self) -> float | None:
        budget_ms = self.spec.budget_ms
        return budget_ms / 1000.0 if budget_ms is not None and budget_ms > 0 else None

    @budget_s.setter
    def budget_s(self, value: float | None) -> None:
        budget_ms = 0 if value is None else round(value * 1000)
        self._registry.register(replace(self.spec, budget_ms=budget_ms), replace=True)

    def wait_shadow(self, timeout: float | None = None) -> bool:
        """等所有進行中的 shadow tick 比對完成（測試與診斷用）。"""
        with self._lock:
            pending = set(self._shadow_pending)
        if not pending:
            return True
        _done, not_done = futures_wait(pending, timeout=timeout)
        return not not_done

    # ---- DecisionEngine：request.facts -> DecisionResult（純轉換，無副作用）----

    def decide(self, request: DecisionRequest) -> DecisionResult:
        spec = self.spec
        facts = request.facts
        status = str(facts.get("status", "ok"))
        summaries: Sequence[dict[str, Any]] = facts.get("interjections", ())
        fallback_reason = {
            "timeout": spec.on_timeout_reason, "busy": spec.on_busy_reason, "error": spec.on_error_reason,
        }.get(status)
        if fallback_reason is None and summaries and not any(s.get("valid") for s in summaries):
            fallback_reason = spec.on_invalid_reason  # 全部提議都驗證失敗：與現行行為一樣丟棄
        if fallback_reason is not None:
            return DecisionResult(
                decision_id=new_id("dec"), request_id=request.request_id, kind=request.kind, outcome=_PASS,
                plan=DecisionPlan(route="ignore", reply=DecisionReply(mode=_PASS, addressed=False)),
                confidence=0.0, reason_code=fallback_reason,
                engine=DecisionEngineInfo(tier="fallback", id="core.fallback", version="d80"), fallback=True,
            )
        first = _first_effective(summaries)
        if first is None:
            return DecisionResult(
                decision_id=new_id("dec"), request_id=request.request_id, kind=request.kind, outcome=_PASS,
                plan=DecisionPlan(route="ignore", reply=DecisionReply(mode=_PASS, addressed=False)),
                confidence=0.0, reason_code=NO_INTERJECT_REASON,
                engine=DecisionEngineInfo(
                    tier="rule", id=str(facts.get("proposer_id", "unknown")),
                    version=str(facts.get("proposer_version", "unknown")),
                ),
            )
        return DecisionResult(
            decision_id=new_id("dec"), request_id=request.request_id, kind=request.kind,
            outcome=str(first["reply_mode"]),
            plan=DecisionPlan(
                route=first["route"],
                reply=DecisionReply(
                    mode=str(first["reply_mode"]), interaction_to=first["interaction_to"],
                    addressed=bool(first["addressed"]),
                ),
            ),
            confidence=float(first["confidence"]), reason_code=str(first["reason_code"]),
            engine=DecisionEngineInfo(tier="rule", id=str(first["proposer_id"]), version=str(first["proposer_version"])),
        )

    # ---- Orchestrator.tick 的掛點 ----

    def on_tick(
        self, interjections: Sequence[Interjection] | None, status: str, elapsed_ms: int, *,
        primary_id: str = "unknown", primary_version: str = "unknown",
    ) -> None:
        """primary 這一輪 tick 結束後呼叫：記錄 primary 結果並啟動 shadow tick。呼叫端須吞掉任何例外。"""
        self._seq += 1
        summaries = [summarize_interjection(ij) for ij in interjections or ()]
        outcome, reason = interject_outcome(summaries)
        refs: tuple[str, ...] = ()
        first = _first_effective(summaries)
        if first is not None:
            refs = tuple(first["utterance_refs"])
        request = DecisionRequest(
            request_id=f"{self._run_id}:tick:{self._seq}", kind=KIND_TURN_INTERJECT, trigger="tick",
            facts={"status": status, "elapsed_ms": elapsed_ms, "interjections": tuple(summaries),
                   "proposer_id": primary_id, "proposer_version": primary_version},
            text_refs=refs, allowed_outcomes=self.spec.outcomes,
        )
        state = _TickState(request, outcome, reason)
        if status != "ok" or outcome != _PASS or (summaries and not any(s["valid"] for s in summaries)):
            state.emit(self._submit)  # primary 有插話或失敗：一定記錄
        else:
            self.metrics["interject.quiet"] += 1
        self._run_shadow_ticks(state)

    # ---- shadow ----

    def _run_shadow_ticks(self, state: _TickState) -> None:
        for shadow, guard in tuple(self._reply.shadows):
            if not isinstance(shadow, TickingProposer):
                continue
            started = time.monotonic()
            future = guard.submit(lambda _, s=shadow: s.tick(), None)
            if future is None:
                self.metrics["shadow.tick_skipped_busy"] += 1  # 上一個 shadow 呼叫還在跑：跳過，佇列有界
                continue
            with self._lock:
                self._shadow_pending.add(future)
            future.add_done_callback(lambda f, s=shadow, st=state, t=started: self._record_shadow(f, s, st, t))

    def _record_shadow(self, future: Future[Any], shadow: object, state: _TickState, started: float) -> None:
        try:
            self._record_shadow_inner(future, shadow, state, started)
        except Exception:
            self.metrics["interject.shadow_record_error"] += 1
            logger.exception("shadow tick 比對記錄失敗")
        finally:
            with self._lock:
                self._shadow_pending.discard(future)

    def _record_shadow_inner(self, future: Future[Any], shadow: object, state: _TickState, started: float) -> None:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        shadow_id = str(getattr(shadow, "proposer_id", type(shadow).__name__))
        mode: str | None = None
        reason: str | None = None
        status = "ok"
        try:
            summaries = [summarize_interjection(ij) for ij in future.result() or ()]
            if summaries and not any(s["valid"] for s in summaries):
                status = "error"
            else:
                mode, reason = interject_outcome(summaries)
                first = _first_effective(summaries)
                if first is not None:
                    shadow_id = str(first["proposer_id"])
                if self.budget_s is not None and elapsed_ms > self.budget_s * 1000:
                    status = "late"
        except BaseException:  # noqa: BLE001 - shadow 的任何失敗都只記錄
            status = "error"
        if status == "ok" and mode == _PASS and state.outcome == _PASS:
            self.metrics["interject.shadow_quiet"] += 1  # 雙方都沒有插話：不寫日誌
            return
        primary = state.emit(self._submit)
        record = ShadowRecord(
            candidate_id=state.request.request_id, shadow_id=shadow_id[:64],
            primary_mode=state.outcome, primary_reason=state.reason,
            shadow_mode=mode, shadow_reason=reason, agree=mode == state.outcome, status=status,
            elapsed_ms=elapsed_ms, decision_id=new_id("dec"), primary_decision_id=primary.decision_id,
            request_id=state.request.request_id, kind=KIND_TURN_INTERJECT,
        )
        self.shadow_log.append(record)
        self.metrics[f"shadow.interject_{'agree' if record.agree else 'disagree'}"] += 1
        if status != "ok":
            self.metrics[f"shadow.interject_{status}"] += 1
        if self.on_shadow is not None:
            try:
                self.on_shadow(record)
            except Exception:
                logger.exception("on_shadow 回呼失敗")
