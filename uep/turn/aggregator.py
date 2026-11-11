"""TurnAggregator Port 與預設實作（P3接入方案 §4.1、S5）。

``TurnAggregator`` 把一句句 utterance 合併成 turn：``feed`` 在每句 utterance 抵達時呼叫，``tick`` 由宿主
定期呼叫（處理等待逾時）。兩者回傳「已完成」的 ``AggregatedTurn``（0..n 個）。Orchestrator 不依賴具體實作。

- ``PassThroughAggregator``：一句一個 turn、立即交出（Web／CLI、單一 STT 已切好 turn 的路徑）。
- ``VoiceRoutedAggregator``：依 ``Segment.voice`` 分流：語音交給 ``BufferedAggregator``，文字逐句通過（不被緩衝延遲）。
- ``BufferedAggregator``：搬自 bot ``echo_decision.aggregator.turn_buffer``：per-speaker 緩衝，
  段落間隔超過 merge_gap 或等待超過 max_wait 即完成；主動模式改用較短的 0.25s 門檻；被叫到的 turn 也用短門檻。
  時間由注入的 ``clock`` 提供。

敘述中保持聆聽（narrative hold，搬自 bot ``turn_buffer`` 的 ``_track_chain``／``_narrative_done``，僅主動模式）：
同一人連續 ``narrative_min_segments`` 段或累積語音 ``narrative_min_speech_s`` 以上，整串合併成一個 turn，
等說話者安靜 ``narrative_end_s`` 才交出。「安靜」從 ``note_voice`` 回報的最後出聲時刻起算（宿主在 VAD 有聲事件時呼叫），
沒有可信的聲音活動時退回最後一段抵達時刻。

刻意沒有搬：bot middleware 的 ``_is_skippable_fragment``（他人敘述時略過短片段）屬決策層，不在 aggregator。
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

# 以下常數逐條沿用 bot ``echo_decision/aggregator/turn_buffer.py``（行號為該檔）
# 聲音活動可信的時間窗：最後一次出聲不早於「最後一段抵達」前這麼久，才拿來當敘述中的安靜起算點（:30）
_VOICE_TRUST_WINDOW_S = 15.0
# 敘述中，最後一段抵達後超過 narrative_end_s 再加這麼久，即使仍偵測到聲音也強制收束（:32）
_NARRATIVE_HOLD_CAP_S = 20.0
# 沒有音訊長度時，用文字長度估算說話秒數（中文約每秒 4 字）（:34）
_CHARS_PER_SPEECH_S = 4.0
# 發言串與聲音活動紀錄保留時間（秒）（:36）
_CHAIN_TTL_S = 120.0


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
    voice: bool = False  # 來自語音輸入 stage（分流 aggregator 只緩衝語音）


@dataclass(frozen=True)
class AggregatedTurn:
    speaker_ref: str
    text: str = field(repr=False)
    segments: tuple[Segment, ...]
    state: AggregateState = AggregateState.COMPLETE
    narrative: bool = False  # 這個 turn 是「敘述中」整串合併而成

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


class _Chain:
    """同一位說話者的連續發言串（跨 turn 追蹤，turn 交出去後仍保留一小段時間）。"""

    __slots__ = ("last_arrival", "segments", "speech_s")

    def __init__(self, speech_s: float, now: float) -> None:
        self.last_arrival = now
        self.speech_s = speech_s
        self.segments = 1


class _Buffer:
    __slots__ = ("last_time", "narrative", "segments", "urgent_cache")

    def __init__(self, first: Segment, now: float, narrative: bool = False) -> None:
        self.segments = [first]
        self.last_time = now
        self.narrative = narrative  # 敘述中：整串合併，等說話者安靜夠久才完成
        self.urgent_cache: tuple[int, bool] | None = None  # (段數, 判斷結果)：buffer 沒長就不重判

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
        self._chains: dict[str, _Chain] = {}
        self._voice_last: dict[str, float] = {}  # speaker -> 最後一次出聲時刻（STT 執行緒回報）
        self._replied_at: dict[str, float] = {}  # speaker -> UEP 回覆完成時刻：之後的發言重新起算發言串
        self._fast = False
        self._urgent: Callable[[str], bool] | None = None

    # ---- 模式 ----

    def set_fast(self, fast: bool) -> None:
        """主動模式用較短的 merge_gap／max_wait；門檻在檢查當下才讀取，等待中的 turn 也會立刻套用。"""
        self._fast = fast

    def set_urgent_predicate(self, predicate: Callable[[str], bool] | None) -> None:
        """「這段文字已確定在叫人設」的判斷：被動模式的 merge_gap／max_wait 很長，被叫到的 turn 不必等。"""
        self._urgent = predicate
        for buf in self._buffers.values():
            buf.urgent_cache = None

    def discard_speaker(self, speaker_ref: str) -> None:
        """成員離場：丟掉他還沒成 turn 的文字（不再決策）。"""
        self._buffers.pop(speaker_ref, None)
        self._chains.pop(speaker_ref, None)
        self._voice_last.pop(speaker_ref, None)
        self._replied_at.pop(speaker_ref, None)

    def note_voice(self, speaker_ref: str, now: float | None = None) -> None:
        """回報某位說話者「現在正在出聲」（VAD 有聲事件，可從 STT 執行緒呼叫）。

        敘述中的 turn 靠它判斷說話者是否真的講完：段落文字要等整句講完加 STT 延遲才會抵達，
        只看段落抵達時間會在對方還在講的時候就誤判結束。
        """
        self._voice_last[speaker_ref] = self._clock() if now is None else now

    def note_replied(self, speaker_ref: str, now: float | None = None) -> None:
        """UEP 已回覆這位說話者：之後的發言是新的一輪，不接續回覆前的發言串。"""
        self._replied_at[speaker_ref] = self._clock() if now is None else now

    def forget_replied(self, speaker_ref: str) -> None:
        """回覆被作廢／失敗（對方沒有真的聽到）：不算「回覆過」。"""
        self._replied_at.pop(speaker_ref, None)

    @property
    def pending_wait_s(self) -> float:
        """目前模式下，最後一段抵達後最久還要等多久 turn 才會交出（被動 max_wait；主動模式含敘述中的安靜時間）。"""
        wait = self.max_wait_s
        if self._fast and self._cfg.narrative_enabled:
            wait = max(wait, self._cfg.narrative_end_s)
        return wait

    def is_narrating(self, speaker_ref: str) -> bool:
        """該說話者目前是否有進行中的敘述中 turn（僅主動模式）。"""
        buf = self._buffers.get(speaker_ref)
        return bool(buf is not None and buf.narrative and self._fast)

    def narrating_speakers(self) -> list[str]:
        return [s for s in list(self._buffers) if self.is_narrating(s)]

    @property
    def merge_gap_s(self) -> float:
        return (self._cfg.active_merge_gap_ms if self._fast else self._cfg.merge_gap_ms) / 1000.0

    @property
    def max_wait_s(self) -> float:
        return (self._cfg.active_max_wait_ms if self._fast else self._cfg.max_wait_ms) / 1000.0

    def _is_urgent(self, buf: _Buffer) -> bool:
        if self._fast or self._urgent is None:
            return False  # 主動模式本來就是短門檻
        version = len(buf.segments)
        if buf.urgent_cache is not None and buf.urgent_cache[0] == version:
            return buf.urgent_cache[1]
        try:
            urgent = bool(self._urgent(buf.text))
        except Exception:
            logger.exception("urgent predicate 失敗")
            return False  # 判斷失敗就退回一般等待（不快取，下次再試）
        buf.urgent_cache = (version, urgent)
        return urgent

    def _gap_for(self, buf: _Buffer) -> float:
        return self._cfg.active_merge_gap_ms / 1000.0 if self._is_urgent(buf) else self.merge_gap_s

    # ---- 介面 ----

    def feed(self, segment: Segment) -> list[AggregatedTurn]:
        speaker = segment.speaker_ref
        now = self._clock()
        narrative, continued = self._track_chain(segment, now)
        existing = self._buffers.get(speaker)
        if existing is None:
            self._buffers[speaker] = _Buffer(segment, now, narrative)
            return self._flush_if_called(speaker)
        # 敘述中：同一串連續發言一律併入同一個 buffer。段落抵達間隔 ≈ 段落長度＋STT 延遲，
        # 不能拿來當 merge_gap；什麼時候講完由 tick 依說話者的安靜時間判斷
        if continued and (existing.narrative or narrative):
            cap = max(self._cfg.max_duration_s, self._cfg.narrative_max_duration_s)
            if existing.duration_s < cap:
                existing.append(segment, now)
                existing.narrative = True
                return self._flush_if_called(speaker)
            done = self._flush(speaker, AggregateState.OVERFLOW)
            self._buffers[speaker] = _Buffer(segment, now, narrative)
            return done
        # 與上一段隔太久：先結束舊 turn，再開新的
        if now - existing.last_time > self._gap_for(existing):
            done = self._flush(speaker, AggregateState.COMPLETE)
            self._buffers[speaker] = _Buffer(segment, now, narrative)
            return done
        # 累積長度超過上限：先交出去（overflow）
        if existing.duration_s >= self._cfg.max_duration_s:
            done = self._flush(speaker, AggregateState.OVERFLOW)
            self._buffers[speaker] = _Buffer(segment, now, narrative)
            return done
        existing.append(segment, now)
        return []

    def tick(self) -> list[AggregatedTurn]:
        now = self._clock()
        self._prune(now)
        active_wait = self._cfg.active_max_wait_ms / 1000.0
        expired: list[str] = []
        for speaker, buf in list(self._buffers.items()):
            if buf.narrative and self._fast:
                # 敘述中：不看一般的 max_wait，改看說話者是否已安靜夠久
                if self._narrative_done(speaker, buf, now):
                    expired.append(speaker)
                continue
            idle = now - buf.last_time
            if idle > self.max_wait_s or (idle > active_wait and self._is_urgent(buf)):
                expired.append(speaker)
        out: list[AggregatedTurn] = []
        for speaker in expired:
            out.extend(self._flush(speaker, AggregateState.TIMEOUT))
        return out

    # ---- 敘述中 ----

    @staticmethod
    def _speech_s(segment: Segment) -> float:
        """該段的說話秒數：優先用 segment 的時間長度，沒有就依文字長度估算。"""
        duration = segment.end_ts - segment.start_ts
        if duration > 0:
            return duration
        return len(segment.text.strip()) / _CHARS_PER_SPEECH_S

    def _track_chain(self, segment: Segment, now: float) -> tuple[bool, bool]:
        """更新該說話者的連續發言串，回傳 ``(是否敘述中, 是否接續上一段)``。

        只在主動模式追蹤。段落抵達時刻 ≈ 說完＋STT 延遲，所以「段間停頓」用
        （本段抵達 - 本段長度）- 上一段抵達 估算；連續被切段（沒有停頓）時為負值，照樣算接續。
        """
        cfg = self._cfg
        speaker = segment.speaker_ref
        if not (self._fast and cfg.narrative_enabled):
            self._chains.pop(speaker, None)
            return False, False
        speech_s = self._speech_s(segment)
        chain = self._chains.get(speaker)
        continued = (
            chain is not None
            and (now - speech_s) - chain.last_arrival <= cfg.narrative_gap_s
            and self._replied_at.get(speaker, float("-inf")) <= chain.last_arrival
        )
        if continued:
            chain.speech_s += speech_s
            chain.segments += 1
            chain.last_arrival = now
        else:
            chain = self._chains[speaker] = _Chain(speech_s, now)
        narrative = chain.speech_s >= cfg.narrative_min_speech_s or chain.segments >= cfg.narrative_min_segments
        return narrative, continued

    def _text_called(self, text: str) -> bool:
        if self._urgent is None:
            return False
        try:
            return bool(self._urgent(text))
        except Exception:
            logger.exception("urgent predicate 失敗")
            return False  # 判斷失敗就當作沒叫到

    def _flush_if_called(self, speaker: str) -> list[AggregatedTurn]:
        """敘述中的 turn，最新一段直接叫 UEP（喚醒詞命中）-> 不等講完，立刻交出去。"""
        buf = self._buffers.get(speaker)
        if buf is None or not (buf.narrative and self._fast):
            return []
        if self._text_called(buf.segments[-1].text):
            return self._flush(speaker, AggregateState.COMPLETE)
        return []

    def _narrative_done(self, speaker: str, buf: _Buffer, now: float) -> bool:
        """敘述中的 turn 是否已講完：說話者安靜 narrative_end_s。

        有聲音活動紀錄且可信時，從「最後一次出聲」起算（段落文字抵達前對方若又開口就不會收束）；
        否則從最後一段抵達起算。
        """
        end_s = self._cfg.narrative_end_s
        last_seg = buf.last_time
        if now - last_seg >= end_s + _NARRATIVE_HOLD_CAP_S:
            return True  # 保險：一直偵測到聲音卻沒有任何新段落
        ref = last_seg
        voice = self._voice_last.get(speaker)
        if voice is not None and voice >= last_seg - _VOICE_TRUST_WINDOW_S:
            ref = voice
        return now - ref >= end_s

    def _prune(self, now: float) -> None:
        """清掉過期的發言串與聲音活動紀錄。"""
        for uid in [u for u, c in list(self._chains.items()) if now - c.last_arrival > _CHAIN_TTL_S]:
            self._chains.pop(uid, None)
        for uid in [u for u, t in list(self._voice_last.items()) if now - t > _CHAIN_TTL_S]:
            self._voice_last.pop(uid, None)

    def flush_all(self) -> list[AggregatedTurn]:
        out: list[AggregatedTurn] = []
        for speaker in list(self._buffers):
            out.extend(self._flush(speaker, AggregateState.FLUSHED))
        return out

    def clear(self) -> None:
        self._buffers.clear()
        self._chains.clear()
        self._voice_last.clear()
        self._replied_at.clear()

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
        return [AggregatedTurn(speaker, text, tuple(buf.segments), state, buf.narrative)]


class VoiceRoutedAggregator:
    """依輸入來源分流：``Segment.voice`` 的句子交給 ``voice``（通常是 ``BufferedAggregator``），其餘交給 ``text``（預設逐句通過）。

    文字輸入（``/say``、``submit_utterance`` 的文字）不能被緩衝：被動模式的等待是數秒，打字的人不該等。
    宿主與 orchestrator 用到的選用入口（模式、urgent、聲音活動、回覆回饋、離場）一律轉給 ``voice``；
    ``isinstance(..., PassThroughAggregator)`` 為假，因此 ``needs_tick`` 成立、tick 執行緒會啟動。
    """

    def __init__(self, voice: TurnAggregator, text: TurnAggregator | None = None) -> None:
        self.voice = voice
        self.text: TurnAggregator = text if text is not None else PassThroughAggregator()

    def _pick(self, segment: Segment) -> TurnAggregator:
        return self.voice if segment.voice else self.text

    def feed(self, segment: Segment) -> list[AggregatedTurn]:
        return self._pick(segment).feed(segment)

    def tick(self) -> list[AggregatedTurn]:
        return [*self.voice.tick(), *self.text.tick()]

    def flush_all(self) -> list[AggregatedTurn]:
        return [*self.voice.flush_all(), *self.text.flush_all()]

    # ---- 選用入口：轉給語音聚合器（沒有該入口的實作視為無操作）----

    def set_fast(self, fast: bool) -> None:
        hook = getattr(self.voice, "set_fast", None)
        if callable(hook):
            hook(fast)

    def set_urgent_predicate(self, predicate: Callable[[str], bool] | None) -> None:
        hook = getattr(self.voice, "set_urgent_predicate", None)
        if callable(hook):
            hook(predicate)

    def note_voice(self, speaker_ref: str, now: float | None = None) -> None:
        hook = getattr(self.voice, "note_voice", None)
        if callable(hook):
            hook(speaker_ref, now)

    def note_replied(self, speaker_ref: str, now: float | None = None) -> None:
        hook = getattr(self.voice, "note_replied", None)
        if callable(hook):
            hook(speaker_ref, now)

    def forget_replied(self, speaker_ref: str) -> None:
        hook = getattr(self.voice, "forget_replied", None)
        if callable(hook):
            hook(speaker_ref)

    def discard_speaker(self, speaker_ref: str) -> None:
        for part in (self.voice, self.text):
            hook = getattr(part, "discard_speaker", None)
            if callable(hook):
                hook(speaker_ref)

    @property
    def pending_wait_s(self) -> float:
        """語音緩衝最久還要等多久（console 追 turn 的等待上限用）；語音聚合器沒有緩衝時為 0。"""
        return float(getattr(self.voice, "pending_wait_s", 0.0))
