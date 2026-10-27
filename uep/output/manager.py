"""最小 Delivery Manager（契約 CC-5 5.4、O-1／O-2／O-6）。

職責：建立 delivery、呼叫 OutputAdapter.send、依 Adapter 回報的證據推進狀態。
- 以 ``(output_id, sequence)`` 與 ``dispatch_id`` 冪等：重複呼叫不會產生第二筆 delivery，
  已送出（或已終態）者不會再次呼叫 send。
- 單一送出者：送出前先以列鎖 CAS 取得 claim（claimed_by／claimed_at），輸家回現況、不送。
  claim 超過 ``claim_ttl`` 視為前一個送出者已崩潰，可被接手；接手時先 ``lookup`` 再決定是否重送。
- SendResponseLost 標 unknown；``resolve_unknown`` 以 lookup 解析：
  found -> sent（強證據續推）；confirmed_not_sent 且 Adapter 宣告權威 -> failed；其餘維持 unknown（O-6）。
- 強證據只在 played_range 標示 complete（或 text 類且未提供 range）才推到 rendered_played，
  否則停在 accepted（INV-05：不把部分播放寫成完整已說出）。
- Output Gate（選用，CC-4 P-8／O-5）：送出前檢查授權六元組。於 ``generated`` 被拒記 ``rejected``；
  已越過 generated（audio 已合成、或 claim 後送出前再檢查）被拒則記 ``cancelled``，
  兩者皆不送出、不重送、不遞補到其他 endpoint；failure.code 為 Gate 結果碼。未注入 Gate 時不檢查。
- I/O 不在交易內進行；每個狀態推進各自一個短交易。
- ``crash_hook(point)`` 與 TaskManager 同形，供測試在指定點拋 SimulatedCrash：
  ``delivery.after_prepare``、``delivery.after_claim``、``delivery.after_send``（send 後、commit 前）。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from uep.adapters.output import OutputAdapter, SendFailed, SendResponseLost, SendResult
from uep.contracts.presence import GateDecision
from uep.ids import new_id
from uep.output.delivery import (
    TERMINAL,
    DeliveryEvidence,
    DeliveryRecord,
    path_for,
)
from uep.output.delivery import DeliveryStatus as S
from uep.output.intent import OutputIntent
from uep.output.models import (
    DeliveryRow,
    advance_delivery,
    claim_delivery,
    create_delivery,
    load_delivery,
    load_intent,
    record_from_row,
    save_intent,
)
from uep.presence.gate import OutputGate

logger = logging.getLogger("uep.output.manager")

_STRONG = frozenset({DeliveryEvidence.LOCAL_PLAYBACK, DeliveryEvidence.REMOTE_RENDER})


class IntentMismatchError(ValueError):
    """dispatch 的 intent 與已持久化的同 ``(output_id, sequence)`` intent 不一致（拒絕送出）。"""


class DeliveryManager:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        adapter: OutputAdapter,
        *,
        claim_ttl: timedelta = timedelta(seconds=30),
        crash_hook: Callable[[str], None] | None = None,
        gate: OutputGate | None = None,
    ) -> None:
        self._sf = session_factory
        self._adapter = adapter
        self._gate = gate
        self._ttl = claim_ttl
        self._crash = crash_hook or (lambda _point: None)

    def prepare(self, intent: OutputIntent) -> DeliveryRecord:
        """寫入 intent 並建立 generated 狀態的 delivery；已存在則回傳既有（冪等）。"""
        with self._sf.begin() as s:
            existing = s.execute(
                select(DeliveryRow).where(
                    DeliveryRow.output_id == intent.output_id, DeliveryRow.sequence == intent.sequence
                )
            ).scalar_one_or_none()
            if existing is not None:
                return record_from_row(existing)
            if load_intent(s, intent.output_id, intent.sequence) is None:
                save_intent(s, intent)
            record = DeliveryRecord(
                delivery_id=new_id("dlv"), output_id=intent.output_id, sequence=intent.sequence,
                kind=intent.kind, dispatch_id=new_id("dsp"),
            )
            create_delivery(s, record)
            return record

    def mark_synthesized(self, delivery_id: str) -> DeliveryRecord:
        with self._sf.begin() as s:
            return advance_delivery(s, delivery_id, S.SYNTHESIZED)

    def dispatch(self, intent: OutputIntent) -> DeliveryRecord:
        """送出一筆輸出；回傳最新 delivery。audio 須先 ``mark_synthesized``。"""
        record = self.prepare(intent)
        self._assert_same_intent(intent)  # 送出的必須是已持久化的那份，不信任呼叫端另帶的內容
        if record.status not in (S.GENERATED, S.SYNTHESIZED, S.ENQUEUED):
            return record  # 已送出、unknown 或終態：不重送
        rejected = self._gate_check(intent, record)
        if rejected is not None:
            return rejected
        with self._sf.begin() as s:
            record = advance_delivery(s, record.delivery_id, S.ENQUEUED)
        self._crash("delivery.after_prepare")
        with self._sf.begin() as s:
            claim = claim_delivery(s, record.delivery_id, new_id("req"), self._ttl)
        if not claim.won:
            return claim.record  # 另一個送出者持有送出權
        self._crash("delivery.after_claim")
        if claim.reclaimed:
            # 前一個送出者可能已送出：先查核，不重複外部效果
            looked = self._adapter.lookup(record.dispatch_id)
            if looked.outcome == "found" and looked.result is not None:
                return self._apply_result(record.delivery_id, looked.result)
            if looked.outcome == "undetermined" or not self._adapter.lookup_authoritative:
                return self._set_unknown(record.delivery_id)
        rejected = self._gate_check(intent, record)  # 端點送出前再接納一次（授權可能在 claim 期間被撤銷）
        if rejected is not None:
            return rejected
        try:
            result = self._adapter.send(intent, record.dispatch_id)
        except SendResponseLost:
            self._crash("delivery.after_send")
            return self._set_unknown(record.delivery_id)
        except SendFailed:
            with self._sf.begin() as s:
                return advance_delivery(
                    s, record.delivery_id, S.FAILED, failure={"code": "SEND_FAILED", "retryable": False}
                )
        self._crash("delivery.after_send")
        return self._apply_result(record.delivery_id, result)

    def _assert_same_intent(self, intent: OutputIntent) -> None:
        """既有 row 時載入已持久化 intent 比對（content_ref、target、sequence 等全部欄位）。"""
        with self._sf() as s:
            persisted = load_intent(s, intent.output_id, intent.sequence)
        if persisted is None or persisted != intent:
            raise IntentMismatchError(f"dispatch 的 intent 與已持久化內容不符：{intent.output_id}#{intent.sequence}")

    def resolve_unknown(self, delivery_id: str) -> DeliveryRecord:
        """解析 unknown：found -> sent；權威確認未送出 -> failed；其餘維持 unknown。"""
        with self._sf() as s:
            record = load_delivery(s, delivery_id)
        if record is None or record.status is not S.UNKNOWN:
            return record  # type: ignore[return-value]
        looked = self._adapter.lookup(record.dispatch_id)
        if looked.outcome == "found" and looked.result is not None:
            return self._apply_result(delivery_id, looked.result)
        if looked.outcome == "confirmed_not_sent" and self._adapter.lookup_authoritative:
            with self._sf.begin() as s:
                return advance_delivery(
                    s, delivery_id, S.FAILED, failure={"code": "NOT_SENT", "retryable": True}
                )
        return record

    def _gate_check(self, intent: OutputIntent, record: DeliveryRecord) -> DeliveryRecord | None:
        """通過回 None；被拒則依目前狀態記 rejected（generated）或 cancelled（其後），不送出。"""
        if self._gate is None:
            return None
        try:
            decision = self._gate.admit_intent(intent)
        except Exception:
            # Gate 內部錯誤（例如時間欄型別）：fail-closed 記為拒絕，不讓 delivery 卡在 generated
            logger.exception("Output Gate 接納時發生例外，記為拒絕")
            decision = None
        if decision is GateDecision.ADMIT:
            return None
        failure = {"code": decision.value if decision is not None else "GATE_ERROR", "retryable": False}
        with self._sf.begin() as s:
            current = load_delivery(s, record.delivery_id)
            target = S.REJECTED if current is not None and current.status is S.GENERATED else S.CANCELLED
            return advance_delivery(s, record.delivery_id, target, failure=failure)

    def _set_unknown(self, delivery_id: str) -> DeliveryRecord:
        with self._sf.begin() as s:
            return advance_delivery(s, delivery_id, S.UNKNOWN)

    def _apply_result(self, delivery_id: str, result: SendResult) -> DeliveryRecord:
        """依證據推進；對重放冪等：已終態為 no-op，已越過的階段略過，不拋錯。"""
        if result.evidence is DeliveryEvidence.UNKNOWN:
            return self._set_unknown(delivery_id)
        with self._sf.begin() as s:
            current = load_delivery(s, delivery_id)
            if current is None or current.status in TERMINAL:
                return current  # type: ignore[return-value]
            path = path_for(current.kind)
            steps = [S.SENT]
            if result.evidence in _STRONG:
                steps.append(S.ACCEPTED)
                if self._is_complete(current, result):
                    steps.append(S.RENDERED_PLAYED)
            rec = current
            for step in steps:
                if rec.status is not S.UNKNOWN and path.index(rec.status) >= path.index(step):
                    continue  # 重放：該階段已達成
                kwargs: dict = {"evidence": result.evidence}
                if step is S.SENT:
                    kwargs.update(sent_at=datetime.now(UTC), reported_at=result.reported_at)
                if step is S.RENDERED_PLAYED or (step is S.ACCEPTED and result.played_range):
                    kwargs["played_range"] = result.played_range
                rec = advance_delivery(s, delivery_id, step, **kwargs)
            return rec

    @staticmethod
    def _is_complete(current: DeliveryRecord, result: SendResult) -> bool:
        if result.played_range is not None:
            return result.played_range.get("complete") is True
        return current.kind.value != "audio_stream"  # 文字類無播放範圍；音訊必須有 complete
