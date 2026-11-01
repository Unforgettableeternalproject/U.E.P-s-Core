"""DeliveryRecord 與狀態機（契約 CC-5 §5.2、O-1／O-2／O-3）。

狀態只能依證據單向推進，不得跳級：
- text 等無合成的 kind：generated -> enqueued -> sent -> accepted -> rendered_played
- audio_stream：generated -> synthesized -> enqueued -> sent -> accepted -> rendered_played
- 任何非終態可轉 failed／unknown／cancelled；rejected 只自初始 generated（Gate 拒絕，尚未進佇列）；
  suppressed 只自送出前的狀態（不得觸發實際送出）。
- 終態：rendered_played、failed、cancelled、rejected、suppressed，之後不再變動。
  晚到的證據（例如已 cancelled 後才收到播放回報）不改狀態，由呼叫端另行記錄。
- unknown 是查核前的暫態，只能解析為 sent（查得已送出；證據須為 transport_sent 以上，
  若證據為強證據，之後可依序走 accepted -> rendered_played，不跳級）或 failed（查核確認未送出，須附 failure）。
- 證據強度只升不降（unknown < transport_sent < local_playback／remote_render）。
- 目標狀態等於當前狀態且證據不降級時，advance 為 no-op（冪等，回傳自身）。
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from uep.output.intent import OutputKind


class DeliveryStatus(StrEnum):
    GENERATED = "generated"
    SYNTHESIZED = "synthesized"
    ENQUEUED = "enqueued"
    SENT = "sent"
    ACCEPTED = "accepted"
    RENDERED_PLAYED = "rendered_played"
    FAILED = "failed"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    SUPPRESSED = "suppressed"


class DeliveryEvidence(StrEnum):
    LOCAL_PLAYBACK = "local_playback"
    REMOTE_RENDER = "remote_render"
    TRANSPORT_SENT = "transport_sent"
    UNKNOWN = "unknown"


S = DeliveryStatus
_TEXT_PATH = (S.GENERATED, S.ENQUEUED, S.SENT, S.ACCEPTED, S.RENDERED_PLAYED)
_AUDIO_PATH = (S.GENERATED, S.SYNTHESIZED, S.ENQUEUED, S.SENT, S.ACCEPTED, S.RENDERED_PLAYED)
TERMINAL = frozenset({S.RENDERED_PLAYED, S.FAILED, S.CANCELLED, S.REJECTED, S.SUPPRESSED})
_PRE_SEND = frozenset({S.GENERATED, S.SYNTHESIZED, S.ENQUEUED})
_STRONG_EVIDENCE = frozenset({DeliveryEvidence.LOCAL_PLAYBACK, DeliveryEvidence.REMOTE_RENDER})
_EVIDENCE_RANK = {
    DeliveryEvidence.UNKNOWN: 0,
    DeliveryEvidence.TRANSPORT_SENT: 1,
    DeliveryEvidence.LOCAL_PLAYBACK: 2,
    DeliveryEvidence.REMOTE_RENDER: 2,
}
_RANGE_KEYS = frozenset({"from_sample", "to_sample", "from_chunk", "to_chunk", "chunk", "sample", "complete"})
_FAILURE_KEYS = frozenset({"code", "retryable"})
_MAX_CODE_LEN = 64


def evidence_rank(evidence: DeliveryEvidence) -> int:
    return _EVIDENCE_RANK[evidence]


def _check_range(name: str, value: dict[str, Any] | None) -> None:
    """位置類欄位只收白名單鍵與非負整數／布林，不夾帶文字內容（D33）。"""
    if value is None:
        return
    extra = set(value) - _RANGE_KEYS
    if extra:
        raise ValueError(f"{name} 含未允許的欄位：{sorted(extra)}")
    for key, item in value.items():
        ok = isinstance(item, bool) or (isinstance(item, int) and item >= 0)
        if not ok:
            raise ValueError(f"{name}.{key} 必須是非負整數或布林")


class IllegalDeliveryTransition(ValueError):
    """違反 CC-5 合法路徑或證據規則。"""


def path_for(kind: OutputKind) -> tuple[DeliveryStatus, ...]:
    return _AUDIO_PATH if kind is OutputKind.AUDIO_STREAM else _TEXT_PATH


def check_transition(
    kind: OutputKind, current: DeliveryStatus, to: DeliveryStatus, evidence: DeliveryEvidence
) -> None:
    """檢查單步轉換；不合法即拋 IllegalDeliveryTransition。"""
    if current in TERMINAL:
        raise IllegalDeliveryTransition(f"終態 {current.value} 不得再改為 {to.value}")
    if current is S.UNKNOWN and to not in (S.SENT, S.FAILED):
        raise IllegalDeliveryTransition("unknown 只能由查核結果解析為 sent 或 failed")
    # O-2：accepted／rendered_played 需強證據；sent 需 transport_sent 以上；證據 unknown 一律 unknown
    if to in (S.ACCEPTED, S.RENDERED_PLAYED) and evidence not in _STRONG_EVIDENCE:
        raise IllegalDeliveryTransition(
            f"{to.value} 需 local_playback／remote_render 證據，收到 {evidence.value}"
        )
    if to is S.SENT and evidence is DeliveryEvidence.UNKNOWN:
        raise IllegalDeliveryTransition("sent 需 transport_sent 以上證據；證據 unknown 一律 unknown")
    if to in (S.FAILED, S.UNKNOWN, S.CANCELLED):
        return
    if to is S.REJECTED:
        if current is not S.GENERATED:
            raise IllegalDeliveryTransition("rejected 只在 Output Gate 拒絕時成立（自 generated）")
        return
    if to is S.SUPPRESSED:
        if current not in _PRE_SEND:
            raise IllegalDeliveryTransition("suppressed 不得在送出後成立（不得觸發實際送出）")
        return
    if current is S.UNKNOWN:  # unknown -> sent，證據已於上方檢查
        return
    path = path_for(kind)
    if to not in path:
        raise IllegalDeliveryTransition(f"{kind.value} 沒有狀態 {to.value}")
    if path.index(to) != path.index(current) + 1:
        raise IllegalDeliveryTransition(f"不得跳級或倒退：{current.value} -> {to.value}")


class DeliveryRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    delivery_id: str
    output_id: str
    sequence: int
    kind: OutputKind
    dispatch_id: str
    status: DeliveryStatus = S.GENERATED
    delivery_evidence: DeliveryEvidence = DeliveryEvidence.UNKNOWN
    played_range: dict[str, Any] | None = None
    cancel_position: dict[str, Any] | None = None
    # 實際說出的文字存於可刪 blob（D33），這裡只留引用與雜湊（O-9：與 generated_text 分開）
    spoken_text_ref: str | None = None
    spoken_text_hash: str | None = None
    failure: dict[str, Any] | None = None  # 只收 {code, retryable}
    sent_at: datetime | None = None  # 尚未送出前為 None
    reported_at: datetime | None = None
    claimed_by: str | None = None  # 單一送出者的 claim（由 claim_delivery 設定）
    claimed_at: datetime | None = None

    @model_validator(mode="after")
    def _check_values(self) -> DeliveryRecord:
        _check_range("played_range", self.played_range)
        _check_range("cancel_position", self.cancel_position)
        if self.failure is not None:
            extra = set(self.failure) - _FAILURE_KEYS
            code = self.failure.get("code")
            if extra or not isinstance(code, str) or not code or len(code) > _MAX_CODE_LEN:
                raise ValueError("failure 只收 {code: 短字串, retryable: bool}")
            if not isinstance(self.failure.get("retryable", False), bool):
                raise ValueError("failure.retryable 必須是布林")
        return self

    def with_progress(self, played_range: dict[str, Any]) -> DeliveryRecord:
        """狀態不變、只更新播放進度（O-16）：位置只能往前，倒退、同值或無法比較者回傳自身（no-op）。

        只在 accepted 有意義（sent 尚無播放證據，終態不再變動）。位置以 ``to_sample`` 為準，
        沒有時用 ``to_chunk``；新舊紀錄的位置鍵不一致無法比較時，保留舊值。``complete`` 不得由 true 倒退。
        """
        if self.status is not S.ACCEPTED:
            return self
        _check_range("played_range", played_range)
        old = self.played_range
        if old is not None:
            key = next((k for k in ("to_sample", "to_chunk") if k in old and k in played_range), None)
            if key is None or played_range[key] <= old[key]:
                return self
            if old.get("complete") is True and played_range.get("complete") is not True:
                return self
        return type(self).model_validate({**self.model_dump(), "played_range": played_range})

    def advance(
        self,
        to: DeliveryStatus,
        *,
        evidence: DeliveryEvidence | None = None,
        sent_at: datetime | None = None,
        reported_at: datetime | None = None,
        played_range: dict[str, Any] | None = None,
        cancel_position: dict[str, Any] | None = None,
        spoken_text_ref: str | None = None,
        spoken_text_hash: str | None = None,
        failure: dict[str, Any] | None = None,
    ) -> DeliveryRecord:
        """回傳推進後的新紀錄；不合法即拋 IllegalDeliveryTransition。"""
        ev = evidence if evidence is not None else self.delivery_evidence
        if evidence_rank(ev) < evidence_rank(self.delivery_evidence):
            raise IllegalDeliveryTransition(
                f"證據只升不降：{self.delivery_evidence.value} -> {ev.value}"
            )
        if to is self.status:
            return self  # 冪等 no-op（證據未降級）
        if self.status is S.UNKNOWN and to is S.FAILED and failure is None:
            raise IllegalDeliveryTransition("unknown 解析為 failed 須附查核結果 failure")
        check_transition(self.kind, self.status, to, ev)
        update = {
            "status": to,
            "delivery_evidence": ev,
            "sent_at": sent_at or self.sent_at,
            "reported_at": reported_at or self.reported_at,
            "played_range": played_range if played_range is not None else self.played_range,
            "cancel_position": cancel_position if cancel_position is not None else self.cancel_position,
            "spoken_text_ref": spoken_text_ref or self.spoken_text_ref,
            "spoken_text_hash": spoken_text_hash or self.spoken_text_hash,
            "failure": failure if failure is not None else self.failure,
        }
        return type(self).model_validate({**self.model_dump(), **update})
