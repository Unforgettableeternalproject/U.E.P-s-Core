"""FakeOutputAdapter：實作 ``uep.adapters.output.OutputAdapter`` 協定的可腳本化替身。

- ``sends`` 記錄每次 ``send`` 呼叫；``send_count(output_id, sequence)`` 為呼叫次數。
- ``effect_count(output_id, sequence)`` 為實際外部效果次數（與呼叫次數分開）。
  ``idempotent_by_dispatch=True`` 時，同 dispatch_id 重送只產生一次效果並回既有結果。
- 注入點 ``send_behavior(output_id, sequence, mode)``（亦可用 ``default_mode``）：
  * ``played``：回報 local_playback 證據與 played_range。
  * ``transport_only``：只回 transport_sent，之後不會有播放回報（VC-5-2）。
  * ``lose_ack``：實際送出並記帳後拋 SendResponseLost（VC-5-3）。
  * ``fail``：未送出，拋 SendFailed。
- ``lookup_authoritative``（預設 True，可設定）：非權威時沒紀錄只能回 undetermined。
- ``lookup_lost``：把 ``lookup`` 設為 undetermined；``lookup_outcome(dispatch_id, outcome)`` 可強制
  found／confirmed_not_sent／undetermined（用於模擬查核誤報）。
- ``played_partial`` 模式：回報 local_playback 但 played_range 未標 complete。
- crash 點：``output.before_send``、``output.after_send``（已送出記帳、尚未回傳）。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from uep.adapters.output import (
    SendFailed,
    SendLookup,
    SendLookupOutcome,
    SendResponseLost,
    SendResult,
)
from uep.fakes.crash import CrashInjector
from uep.output.delivery import DeliveryEvidence
from uep.output.intent import ControlSignal, OutputIntent

SendMode = Literal["played", "played_partial", "transport_only", "lose_ack", "fail"]


class FakeOutputAdapter:
    def __init__(
        self, *, default_mode: SendMode = "played", idempotent_by_dispatch: bool = False,
        crash: CrashInjector | None = None,
    ) -> None:
        self.default_mode: SendMode = default_mode
        self.idempotent_by_dispatch = idempotent_by_dispatch
        self.crash = crash or CrashInjector()
        self.sends: list[tuple[OutputIntent, str]] = []
        self.effects: list[tuple[str, int, str]] = []  # (output_id, sequence, dispatch_id)
        self.delivered: dict[str, SendResult] = {}  # dispatch_id -> 實際送出的結果
        self.controls: list[ControlSignal] = []
        self.stop_count = 0
        self.lookup_lost = False
        self.lookup_authoritative = True
        self._forced_lookup: dict[str, SendLookupOutcome] = {}
        self._modes: dict[tuple[str, int], SendMode] = {}
        self._seen_controls: set[str] = set()

    def send_behavior(self, output_id: str, sequence: int, mode: SendMode) -> None:
        self._modes[(output_id, sequence)] = mode

    def send_count(self, output_id: str, sequence: int) -> int:
        return sum(1 for i, _ in self.sends if i.output_id == output_id and i.sequence == sequence)

    def effect_count(self, output_id: str, sequence: int) -> int:
        return sum(1 for o, q, _ in self.effects if o == output_id and q == sequence)

    def send(self, intent: OutputIntent, dispatch_id: str) -> SendResult:
        self.sends.append((intent, dispatch_id))
        if self.idempotent_by_dispatch and dispatch_id in self.delivered:
            return self.delivered[dispatch_id]
        mode = self._modes.get((intent.output_id, intent.sequence), self.default_mode)
        if mode == "fail":
            raise SendFailed(f"{intent.output_id}#{intent.sequence}: 送出失敗")
        self.crash.hit("output.before_send")
        now = datetime.now(UTC)
        if mode in ("played", "played_partial"):
            played = {"from_chunk": 0, "to_chunk": intent.sequence + 1}
            if mode == "played":
                played["complete"] = True
            res = SendResult(dispatch_id, DeliveryEvidence.LOCAL_PLAYBACK, now, played_range=played)
        else:
            res = SendResult(dispatch_id, DeliveryEvidence.TRANSPORT_SENT, now)
        self.delivered[dispatch_id] = res
        self.effects.append((intent.output_id, intent.sequence, dispatch_id))
        self.crash.hit("output.after_send")
        if mode == "lose_ack":
            raise SendResponseLost(f"{dispatch_id}: 已送出但 ACK 遺失")
        return res

    def control(self, signal: ControlSignal) -> None:
        if signal.control_id in self._seen_controls:
            return  # 冪等
        self._seen_controls.add(signal.control_id)
        self.controls.append(signal)
        if signal.kind.value == "stop":
            self.stop_count += 1

    def lookup_outcome(self, dispatch_id: str, outcome: SendLookupOutcome) -> None:
        self._forced_lookup[dispatch_id] = outcome

    def lookup(self, dispatch_id: str) -> SendLookup:
        found = self.delivered.get(dispatch_id)
        forced = self._forced_lookup.get(dispatch_id)
        if forced == "found" and found is None:
            raise ValueError(f"{dispatch_id} 尚未送出，無結果可回")
        if self.lookup_lost or forced == "undetermined":
            return SendLookup("undetermined")
        if forced == "confirmed_not_sent":
            return SendLookup("confirmed_not_sent")
        if found is not None:
            return SendLookup("found", found)
        return SendLookup("confirmed_not_sent" if self.lookup_authoritative else "undetermined")
