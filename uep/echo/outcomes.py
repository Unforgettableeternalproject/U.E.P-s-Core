"""ES 的 ``TurnOutcome`` -> Core 的 ``TurnEndFacts``（與 ES 無關的字串）。

只有這個檔案與轉接層認識 ES 的 outcome 形狀；Orchestrator 不依賴它。
"""
from __future__ import annotations

from echo_stream.contracts.output import OutcomeStatus, TurnOutcome

from uep.turn.models import TurnEndFacts

_STATUS = {
    OutcomeStatus.COMPLETED: "completed",
    OutcomeStatus.SKIPPED: "skipped",
    OutcomeStatus.CANCELLED: "cancelled",
    OutcomeStatus.VOIDED: "voided",
    OutcomeStatus.FAILED: "failed",
    OutcomeStatus.DROPPED_BUSY: "dropped_busy",
}
_GENERATED = (OutcomeStatus.COMPLETED, OutcomeStatus.SKIPPED, OutcomeStatus.CANCELLED)


def _code(value: object, default: str | None) -> str | None:
    """原因碼只取冒號之前的部分（ES 的 ``play_failed: <例外>`` 之類不能把例外文字帶進 journal）。"""
    if not value:
        return default
    return str(value).split(":", 1)[0].strip()[:64] or default


def facts_from_outcome(outcome: TurnOutcome) -> TurnEndFacts:
    meta = outcome.metadata or {}
    status = _STATUS[outcome.status]
    reason: str | None
    if outcome.status is OutcomeStatus.COMPLETED:
        reason = None
    elif outcome.status is OutcomeStatus.SKIPPED:
        reason = _code(outcome.skipped_reason, None)
    elif outcome.status is OutcomeStatus.FAILED:
        reason = _code(meta.get("reason_code"), "upstream_error")
    elif outcome.status is OutcomeStatus.DROPPED_BUSY:
        reason = "dropped_busy"
    else:
        reason = _code(meta.get("reason_code") or outcome.cancel_reason, status)
    # 「文字確實生成完」只供回合後的快照登記與記憶 store 判斷；generation_completed 事件走 on_generation_done
    generation_ok = outcome.status in _GENERATED and outcome.error is None and not outcome.aborted
    return TurnEndFacts(
        status=status, reason_code=reason, generation_ok=generation_ok,
        generated_len=len(outcome.generated_text or ""),
        spoken_text=(outcome.spoken_text or "") if outcome.spoken_known else "",
        attempt_no=outcome.attempt_id, snapshot_version=outcome.snapshot_version,
        barge_in=bool(outcome.barge_in),
    )
