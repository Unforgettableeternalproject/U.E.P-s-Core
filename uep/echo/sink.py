"""Sink 包裝：第二次接納（P-14）、晚到輸出丟棄（V07）、逐句 intent 觸發與播放範圍自算（D74）。

包在 ``SinkFactory`` 回傳的 sink 外面。規則：

- ``write`` 每個 chunk 前先 ``admit()``（本地、只讀記憶體）。不通過 -> 丟棄該 chunk、停內層 sink、通知宿主，
  **不拋例外**（拋了 coordinator 會把整輪判成 FAILED）。已停止（``stop`` 之後）的晚到 chunk 一律丟棄並計數，
  不呼叫內層、不觸發 intent（V07）。
- 每一句的第一個通過接納的 chunk 觸發 ``on_sentence``（宿主據此建 intent 並丟給工作緒 dispatch，不在此處碰 DB）。
- 選用能力（``is_active``、``first_frame_at``、``played_evidence``、``confirm_stopped``、``wait_idle``、
  ``play_error``）原樣透傳；漏透傳會被 ES 當成「無此能力」而走保守預設。內層沒有就維持沒有
  （``getattr`` 預設值語意不變）。刻意不透傳 ``reset``：包裝帶有每輪狀態，每輪必須新建。
- ``collect_evidence`` 依內層 ``played_duration_s`` 與各句累積區間算出逐句已播範圍；呼叫時機是 coordinator
  確認停止／播完之後（``on_turn_ended``），內層的實播量此時已凍結。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from echo_stream.contracts.output import PlayedEvidence, sink_played_evidence
from echo_stream.contracts.stages import AudioSink
from echo_stream.contracts.types import AudioChunk

from uep.contracts.presence import GateDecision

logger = logging.getLogger("uep.echo.sink")

_PASSTHROUGH = frozenset({
    "is_active", "first_frame_at", "played_evidence", "confirm_stopped", "wait_idle", "play_error",
})
_COMPLETE_EPSILON_S = 1e-3


@dataclass(frozen=True)
class SentenceEvidence:
    """一句的播放證據：``confirmed`` 表示內層宣告為播放確認（否則只有 transport_sent）。"""

    sentence_index: int
    confirmed: bool
    played_s: float
    duration_s: float
    complete: bool
    sample_rate: int

    def played_range(self) -> dict[str, Any]:
        return {"from_sample": 0, "to_sample": int(self.played_s * self.sample_rate), "complete": self.complete}


class AdmittingSink:
    def __init__(
        self,
        inner: AudioSink,
        *,
        turn_id: str,
        attempt_no: int,
        output_id: str,
        admit: Callable[[], GateDecision],
        on_sentence: Callable[[AdmittingSink, int, int, int], None],
        on_reject: Callable[[AdmittingSink, GateDecision], None],
        on_discard: Callable[[AdmittingSink], None],
        context: Any = None,
    ) -> None:
        self._inner = inner
        self.turn_id = turn_id
        self.attempt_no = attempt_no
        self.output_id = output_id
        self.context = context  # 宿主附帶的 ticket 等（回呼用）
        self._admit = admit
        self._on_sentence = on_sentence
        self._on_reject = on_reject
        self._on_discard = on_discard
        self._lock = threading.Lock()
        self._stopped = False
        self.rejected: GateDecision | None = None
        self._written_s = 0.0
        self._spans: dict[int, list[float]] = {}  # index -> [start_s, duration_s, sample_rate]
        self.delivery_ids: dict[int, str] = {}  # 工作緒 dispatch 後填入：sentence index -> delivery_id
        self.discarded = 0

    # ---- AudioSink ----

    async def write(self, chunk: AudioChunk) -> None:
        if self._stopped:
            self._discard()
            return
        decision = self._admit()
        if decision is not GateDecision.ADMIT:
            self._reject(decision)
            return
        await self._inner.write(chunk)
        if self._stopped:
            return  # 寫入期間被停止：這個 chunk 內層可能已丟棄，不記入句界
        self._record(chunk)

    def stop(self) -> None:
        """同步、冪等、任何執行緒（barge-in 等不了 event loop）。"""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        try:
            self._inner.stop()
        except Exception:
            logger.exception("內層 sink 停止失敗")

    async def drain(self) -> None:
        await self._inner.drain()

    @property
    def played_duration_s(self) -> float:
        return self._inner.played_duration_s

    def __getattr__(self, name: str) -> Any:
        if name in _PASSTHROUGH:
            return getattr(self._inner, name)  # 內層沒有就 AttributeError，維持「無此能力」
        raise AttributeError(name)

    # ---- 內部 ----

    @property
    def stopped(self) -> bool:
        return self._stopped

    def _discard(self) -> None:
        self.discarded += 1
        self._on_discard(self)

    def _reject(self, decision: GateDecision) -> None:
        first = False
        with self._lock:
            if self.rejected is None:
                self.rejected = decision
                first = True
        self.stop()
        if first:
            try:
                self._on_reject(self, decision)
            except Exception:
                logger.exception("sink 拒絕通知失敗")
        else:
            self._discard()

    def _record(self, chunk: AudioChunk) -> None:
        index = chunk.sentence_index
        new_sentence = index not in self._spans
        if new_sentence:
            self._spans[index] = [self._written_s, 0.0, float(chunk.sample_rate)]
        self._spans[index][1] += chunk.duration_s
        self._written_s += chunk.duration_s
        if new_sentence:
            try:
                self._on_sentence(self, index, chunk.sample_rate, chunk.channels)
            except Exception:
                logger.exception("逐句 intent 觸發失敗")

    # ---- 證據 ----

    def collect_evidence(self) -> list[SentenceEvidence]:
        played = float(self._inner.played_duration_s or 0.0)
        confirmed = sink_played_evidence(self._inner) is PlayedEvidence.PLAYBACK_CONFIRMED
        result: list[SentenceEvidence] = []
        for index, (start, duration, rate) in sorted(self._spans.items()):
            played_i = min(max(played - start, 0.0), duration)
            if played_i <= 0.0:
                continue
            result.append(SentenceEvidence(
                index, confirmed, played_i, duration, played_i >= duration - _COMPLETE_EPSILON_S, int(rate),
            ))
        return result
