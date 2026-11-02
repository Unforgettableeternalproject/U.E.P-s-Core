"""聲道引擎——平台無關的「PCM → VAD → turn 判定 → 待轉錄段」狀態機與多聲道排程。

這一層刻意放在 ``core/``：Discord、主機 PC、Web 共用同一套聲道邏輯，
平台特化（取樣率轉換、來源執行緒、backend 載入）留在 adapters。
因此這裡**不 import 任何子系統或第三方套件**；backend 一律以 duck typing 接入
（``transcribe`` 回傳帶 ``text`` / ``confidence`` / ``language`` 屬性的物件或 None）。

本模組分兩層：

* :class:`VoiceChannel`——單一聲道的同步狀態機（由 ``SttInputStage._read`` 抽出，行為不變）。
* 多聲道的 :class:`TranscriberPool` 與 :class:`ChannelEngine`（見後半）。
"""

from __future__ import annotations

from collections import deque
from typing import Any

from ..contracts.turn import TurnDetector, VoiceState
from .vad import EnergyVad


class Segment:
    """一段待轉錄的音訊與它的時間標記。

    ``final=False`` 是 turn 進行中在停頓處切出的**增量段**——轉錄結果
    先經 ``on_partial`` 推給前端顯示，turn 結束時與殘餘段拼成完整轉錄。
    ``final=True`` 標記 turn 結束，``pcm`` 只含最後一段增量之後的殘餘
    （可能為空），時間標記代表**整個 turn** 的起訖。
    """

    __slots__ = ("pcm", "started_at", "ended_at", "final", "gen", "turn_end_ts")

    def __init__(
        self,
        pcm: bytes,
        started_at: float,
        ended_at: float,
        final: bool = True,
        gen: int = 0,
        turn_end_ts: float = 0.0,
    ) -> None:
        self.pcm = pcm
        self.started_at = started_at
        self.ended_at = ended_at
        self.final = final
        self.gen = gen
        """切出這段時的世代號。discard 會把世代往前推，
        舊世代的段（包含已經在轉錄中的）一律作廢。"""
        self.turn_end_ts = turn_end_ts
        """切出這段（turn 判定結束）當下的牆鐘時刻。"""


class VoiceChannel:
    """單一聲道的同步狀態機：VAD、turn 判定、preroll、增量切分。

    **不做任何 I/O、不 await**——呼叫端（單聲道 stage 的讀取迴圈或多聲道引擎的
    pump）自己負責把回傳的段送去轉錄並處理背壓。

    ``cfg`` 是 duck typing 的設定來源，需有 ``policy`` / ``preroll_s`` /
    ``partial_silence_s`` / ``partial_min_speech_s`` / ``on_partial`` /
    ``on_voice_event`` 屬性，每次 push 現讀（web 面板可即時調參）。
    """

    def __init__(
        self,
        vad: EnergyVad,
        turn_detector: TurnDetector,
        cfg: Any,
        speaker_id: str | None = None,
    ) -> None:
        self.vad = vad
        self.turn_detector = turn_detector
        self.cfg = cfg
        self.speaker_id = speaker_id
        self.gen = 0
        self._preroll: deque[bytes] = deque()
        self._preroll_len_s = 0.0
        self._buffer = bytearray()
        self._partial_mark = 0
        """buffer 中已切出去做增量轉錄的位元組數。turn 結束時只送殘餘。"""
        self._speech_start_clock: float | None = None
        """語音起點的**音訊時鐘**。時間標記一律用音訊時鐘回推、共用同一個
        牆鐘錨點——混用兩種時鐘在比即時快的來源（測試、檔案）下會讓
        started_at / ended_at 順序反轉。"""
        self._speech_in_turn_s = 0.0
        self._last_speech_clock: float | None = None

    # --- 狀態 ---

    @property
    def active(self) -> bool:
        """有進行中的語音／累積中的音訊（仍需要靜音推進時鐘才能結束 turn）。"""
        return bool(self._buffer) or self.vad.state is VoiceState.SPEECH

    def _clear_turn(self) -> None:
        self._buffer.clear()
        self._partial_mark = 0
        self._speech_start_clock = None
        self._speech_in_turn_s = 0.0
        self._last_speech_clock = None

    def reset(self) -> None:
        """丟棄累積中的狀態（世代變更時）。VAD 時鐘連續，不重置。"""
        self._clear_turn()
        self._preroll.clear()
        self._preroll_len_s = 0.0
        self.turn_detector.reset()

    def discard(self) -> None:
        """世代 +1 並清空本地狀態：此前累積與已切出的段全部作廢。

        VAD 也一併回到靜音——否則 hangover 期的靜音會被當成「被丟棄那段語音的延續」，
        帶著舊語音的時長湊出一個假 turn（多聲道的 discard 會在說話中途發生）。
        """
        self.gen += 1
        self.reset()
        self.vad.reset()

    # --- 推進 ---

    def push(self, chunk: bytes, now: float) -> list[Segment]:
        """吃一個 chunk，回傳這次切出的段（依序）。``now`` 是牆鐘錨點。"""
        segments: list[Segment] = []
        cfg = self.cfg
        policy = cfg.policy
        sample_rate = self.vad.sample_rate

        event = self.vad.process(chunk)
        event.is_voiced = event.state is VoiceState.SPEECH
        event.chunk_s = (len(chunk) // 2) / sample_rate
        if self.speaker_id is not None:
            # 多聲道：帶上說話者，時間戳改用呼叫端的牆鐘（宿主用它累積讓位／插話秒數）
            event.speaker_id = self.speaker_id
            event.timestamp = now
        callback = cfg.on_voice_event
        if callback is not None:
            try:
                callback(event)
            except Exception:  # noqa: BLE001 - 訂閱者壞掉不能斷麥克風
                pass

        chunk_s = (len(chunk) // 2) / sample_rate

        if event.state is VoiceState.SPEECH:
            if self._speech_start_clock is None:
                self._speech_start_clock = self.vad.clock_s - event.duration_s
                # 補前導：語音其實在 VAD 反應之前就開始了
                self._buffer.extend(b"".join(self._preroll))
                self._preroll.clear()
                self._preroll_len_s = 0.0
            self._buffer.extend(chunk)
            self._speech_in_turn_s += chunk_s
            self._last_speech_clock = self.vad.clock_s
        else:
            self._preroll.append(chunk)
            self._preroll_len_s += chunk_s
            while self._preroll_len_s > cfg.preroll_s and len(self._preroll) > 1:
                dropped = self._preroll.popleft()
                self._preroll_len_s -= (len(dropped) // 2) / sample_rate

            # 增量切分：講話中的自然停頓就先切一段去轉錄，
            # 不等整個 turn。條件是「停頓夠久 + 未轉錄的語音夠多」——
            # 同一次停頓切完 partial_mark 追上 buffer，不會重複觸發。
            pending_s = (len(self._buffer) - self._partial_mark) / 2 / sample_rate
            if (
                cfg.on_partial is not None
                and self._buffer
                and event.duration_s >= cfg.partial_silence_s
                and pending_s >= cfg.partial_min_speech_s
            ):
                clock = self.vad.clock_s
                end_clock = (
                    self._last_speech_clock if self._last_speech_clock is not None else clock
                )
                stamp = now - max(0.0, clock - end_clock)
                piece = bytes(self._buffer[self._partial_mark :])
                self._partial_mark = len(self._buffer)
                segments.append(
                    Segment(piece, stamp, stamp, final=False, gen=self.gen, turn_end_ts=now)
                )

        decision = self.turn_detector.evaluate(event)
        if decision.is_end_of_turn and self._buffer:
            # 兩個標記共用同一個牆鐘錨點，再各自用音訊時鐘回推。
            # ended_at 回推到語音實際停止的時刻——靜音等待是
            # turn 判定的成本，不是使用者的說話時間。
            clock = self.vad.clock_s
            end_clock = self._last_speech_clock if self._last_speech_clock is not None else clock
            start_clock = (
                self._speech_start_clock if self._speech_start_clock is not None else end_clock
            )
            # 增量切過的部分不重轉——final 段只帶殘餘，
            # 時間標記仍代表整個 turn
            segment = Segment(
                pcm=bytes(self._buffer[self._partial_mark :]),
                started_at=now - max(0.0, clock - start_clock),
                ended_at=now - max(0.0, clock - end_clock),
                final=True,
                gen=self.gen,
                turn_end_ts=now,
            )
            self._clear_turn()
            self.turn_detector.reset()
            segments.append(segment)
        elif (
            self._buffer
            and event.state is VoiceState.SILENCE
            and event.duration_s >= policy.silence_threshold_s
            and decision.reason == "too_short"
        ):
            # 偵測器判定太短（咳嗽、關門聲、零碎噪音 burst）且靜音已足：丟掉。
            # 以偵測器的判定為準——它永不會為 too_short 結束 turn，
            # 不丟的話 turn 會卡住，噪音黏到下一個 turn 一起送去轉錄。
            self._clear_turn()
            self.turn_detector.reset()
        return segments

    def flush(self, now: float) -> Segment | None:
        """來源結束／聲道離開：把殘餘送出（語音不足 min_utterance_s 則丟棄）。"""
        if not self._buffer or self._speech_in_turn_s < self.cfg.policy.min_utterance_s:
            return None
        clock = self.vad.clock_s
        end_clock = self._last_speech_clock if self._last_speech_clock is not None else clock
        start_clock = (
            self._speech_start_clock if self._speech_start_clock is not None else end_clock
        )
        segment = Segment(
            bytes(self._buffer[self._partial_mark :]),
            now - max(0.0, clock - start_clock),
            now - max(0.0, clock - end_clock),
            final=True,
            gen=self.gen,
            turn_end_ts=now,
        )
        self._clear_turn()
        self.turn_detector.reset()
        return segment


def merge_parts(parts: list[Any]) -> tuple[str, float, str | None]:
    """把一個 turn 的各段轉錄拼成 ``(text, confidence, language)``。

    confidence 以字數加權；language 取第一個非空值。
    """
    texts = [r.text.strip() for r in parts]
    total_chars = sum(len(t) for t in texts) or 1
    confidence = sum(r.confidence * len(r.text.strip()) for r in parts) / total_chars
    language = next((r.language for r in parts if r.language), None)
    return " ".join(texts), confidence, language


def decode_stats(parts: list[Any]) -> dict[str, float]:
    """跨段落彙整 Whisper 解碼品質，只放有值的鍵（給 Utterance.metadata）。

    temperature 取最大、avg_logprob 取最小、compression_ratio 取最大——都取最差的那段。
    """
    out: dict[str, float] = {}
    for key, pick in (("temperature", max), ("avg_logprob", min), ("compression_ratio", max)):
        values = [v for v in (getattr(r, key, None) for r in parts) if v is not None]
        if values:
            out[key] = float(pick(values))
    return out
