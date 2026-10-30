"""TurnAggregator Port 與預設實作（P3接入方案 §4.1、S5）。

``TurnAggregator`` 把一句句 utterance 合併成 turn：``feed`` 在每句 utterance 抵達時呼叫，``tick`` 由宿主
定期呼叫（處理等待逾時）。兩者回傳「已完成」的 ``AggregatedTurn``（0..n 個）。Orchestrator 不依賴具體實作。

- ``PassThroughAggregator``：一句一個 turn、立即交出（Web／CLI、單一 STT 已切好 turn 的路徑）。
- ``BufferedAggregator``：搬自 bot ``echo_decision.aggregator.turn_buffer``：per-speaker 緩衝，
  段落間隔超過 merge_gap 或等待超過 max_wait 即完成；主動模式改用較短的 0.25s 門檻；被叫到的 turn 也用短門檻。
  時間由注入的 ``clock`` 提供。

刻意沒有搬：敘述（narrative）偵測與聲音活動追蹤（需要有聲事件餵進 aggregator，S6 前沒有這條管線）。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from uep.turn.decision_config import AggregatorConfig

logger = logging.getLogger("uep.turn.aggregator")


class AggregateState(StrEnum):
    COMPLETE = "complete"  # 間隔超過 merge_gap，或 passthrough
    TIMEOUT = "timeout"  # 超過 max_wait
    OVERFLOW = "overflow"  # 超過 max_duration
    FLUSHED = "flushed"  # 強制結束（離開頻道等）


@dataclass(frozen=True)
class Segment:
    """aggregator 的輸入：一句已定稿的 utterance。``text`` 為不受信資料。"""

    speaker_ref: str
    text: str = field(repr=False)
    ref: str = ""  # utterance 事件 ID
    start_ts: float = 0.0  # 秒；只用來算 turn 長度（overflow）
    end_ts: float = 0.0
    payload: object = field(default=None, repr=False, compare=False)  # 宿主私有（原始 UtteranceFact）


@dataclass(frozen=True)
class AggregatedTurn:
    speaker_ref: str
    text: str = field(repr=False)
    segments: tuple[Segment, ...]
    state: AggregateState = AggregateState.COMPLETE

    @property
    def refs(self) -> tuple[str, ...]:
        return tuple(s.ref for s in self.segments if s.ref)

    @property
    def last(self) -> Segment:
        return self.segments[-1]


class TurnAggregator(Protocol):
    def feed(self, segment: Segment) -> list[AggregatedTurn]: ...

    def tick(self) -> list[AggregatedTurn]: ...

    def flush_all(self) -> list[AggregatedTurn]: ...


@runtime_checkable
class ModeAwareAggregator(Protocol):
    """選用：宿主依 proposer 的頻道模式與喚醒判斷調整等待。"""

    def set_fast(self, fast: bool) -> None: ...

    def set_urgent_predicate(self, predicate: Callable[[str], bool] | None) -> None: ...


class PassThroughAggregator:
    def feed(self, segment: Segment) -> list[AggregatedTurn]:
        return [AggregatedTurn(segment.speaker_ref, segment.text, (segment,))]

    def tick(self) -> list[AggregatedTurn]:
        return []

    def flush_all(self) -> list[AggregatedTurn]:
        return []


class _Buffer:
    __slots__ = ("last_time", "segments")

    def __init__(self, first: Segment, now: float) -> None:
        self.segments = [first]
        self.last_time = now

    def append(self, segment: Segment, now: float) -> None:
        self.segments.append(segment)
        self.last_time = now

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments)

    @property
    def duration_s(self) -> float:
        return self.segments[-1].end_ts - self.segments[0].start_ts


class BufferedAggregator:
    def __init__(
        self, config: AggregatorConfig | None = None, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._cfg = config or AggregatorConfig()
        self._clock = clock
        self._buffers: dict[str, _Buffer] = {}
        self._fast = False
        self._urgent: Callable[[str], bool] | None = None

    # ---- 模式 ----

    def set_fast(self, fast: bool) -> None:
        """主動模式用較短的 merge_gap／max_wait；門檻在檢查當下才讀取，等待中的 turn 也會立刻套用。"""
        self._fast = fast

    def set_urgent_predicate(self, predicate: Callable[[str], bool] | None) -> None:
        """「這段文字已確定在叫人設」的判斷：被動模式的 merge_gap／max_wait 很長，被叫到的 turn 不必等。"""
        self._urgent = predicate

    @property
    def merge_gap_s(self) -> float:
        return (self._cfg.active_merge_gap_ms if self._fast else self._cfg.merge_gap_ms) / 1000.0

    @property
    def max_wait_s(self) -> float:
        return (self._cfg.active_max_wait_ms if self._fast else self._cfg.max_wait_ms) / 1000.0

    def _is_urgent(self, buf: _Buffer) -> bool:
        if self._fast or self._urgent is None:
            return False  # 主動模式本來就是短門檻
        try:
            return bool(self._urgent(buf.text))
        except Exception:
            logger.exception("urgent predicate 失敗")
            return False

    def _gap_for(self, buf: _Buffer) -> float:
        return self._cfg.active_merge_gap_ms / 1000.0 if self._is_urgent(buf) else self.merge_gap_s

    # ---- 介面 ----

    def feed(self, segment: Segment) -> list[AggregatedTurn]:
        speaker = segment.speaker_ref
        now = self._clock()
        existing = self._buffers.get(speaker)
        if existing is None:
            self._buffers[speaker] = _Buffer(segment, now)
            return []
        # 與上一段隔太久：先結束舊 turn，再開新的
        if now - existing.last_time > self._gap_for(existing):
            done = self._flush(speaker, AggregateState.COMPLETE)
            self._buffers[speaker] = _Buffer(segment, now)
            return done
        # 累積長度超過上限：先交出去（overflow）
        if existing.duration_s >= self._cfg.max_duration_s:
            done = self._flush(speaker, AggregateState.OVERFLOW)
            self._buffers[speaker] = _Buffer(segment, now)
            return done
        existing.append(segment, now)
        return []

    def tick(self) -> list[AggregatedTurn]:
        now = self._clock()
        active_wait = self._cfg.active_max_wait_ms / 1000.0
        expired = [
            speaker for speaker, buf in self._buffers.items()
            if (idle := now - buf.last_time) > self.max_wait_s or (idle > active_wait and self._is_urgent(buf))
        ]
        out: list[AggregatedTurn] = []
        for speaker in expired:
            out.extend(self._flush(speaker, AggregateState.TIMEOUT))
        return out

    def flush_all(self) -> list[AggregatedTurn]:
        out: list[AggregatedTurn] = []
        for speaker in list(self._buffers):
            out.extend(self._flush(speaker, AggregateState.FLUSHED))
        return out

    def clear(self) -> None:
        self._buffers.clear()

    @property
    def active_speakers(self) -> list[str]:
        return list(self._buffers)

    def _flush(self, speaker: str, state: AggregateState) -> list[AggregatedTurn]:
        buf = self._buffers.pop(speaker, None)
        if buf is None:
            return []
        text = buf.text
        if len(text.strip()) < self._cfg.min_text_length:
            logger.debug("turn 太短，丟棄：%s", speaker)
            return []
        return [AggregatedTurn(speaker, text, tuple(buf.segments), state)]
