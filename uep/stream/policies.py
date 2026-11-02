"""輸出端預設策略與 spoken 推算純函式（T-07b PR ②）。

策略全部**同步、純計算、不得 I/O**：barge-in 每個 VAD chunk（約 20-100ms）
就在 STT／接收執行緒呼叫一次，任何等待都會變成「叫不停」。

## spoken 推算

兩個純函式，不依賴 coordinator，可單獨做 differential test：

* :func:`partial_spoken`：部分播出算已播（與 ``PipelineRunner._resolve_spoken`` 同義）
* :func:`whole_sentence_spoken`：整句播完才算，**逐字對應 bot 的 VoiceTurn.spoken_text**
  （含 0.05s 容差、dur<=0 break、被打斷時排除最後一個有音訊的句子、無資料回 None）
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING

from .contracts.output import PolicySnapshot, PolicySource, TurnTicket, TurnView, YieldAction
from .contracts.turn import InterruptionDecision, VoiceEvent, VoiceState
from .contracts.types import SentenceSpan, Utterance

if TYPE_CHECKING:
    from .contracts.output import TurnRequest

SPOKEN_TOLERANCE_S = 0.05
"""整句制比對實播時長的容差（bot VO:476）。"""


# ─────────────────────────── spoken 推算（純函式）───────────────────────────


def partial_spoken(
    texts: Mapping[int, str],
    timeline: Sequence[tuple[int, float]],
    emitted_duration: float,
    played_s: float | None,
) -> tuple[str, float]:
    """部分播出算已播：句子的 chunk 起點 < 實播時長即算（寧多算不少算）。

    ``timeline`` 是 ``(句序號, 累積至該 chunk 結尾的音訊秒數)``；``played_s`` 為 sink
    回報的實播秒數（None = 無 sink，送出去就算聽到）。回傳 ``(spoken_text, 實播秒數)``。
    """
    if not timeline:
        return "", 0.0

    played = emitted_duration
    if played_s is not None:
        played = max(0.0, min(emitted_duration, float(played_s)))

    max_index = -1
    chunk_start = 0.0
    for sentence_index, cumulative in timeline:
        if chunk_start + 1e-9 < played or sentence_index <= max_index:
            max_index = max(max_index, sentence_index)
        else:
            break
        chunk_start = cumulative

    if max_index < 0:
        return "", played

    spoken = "".join(texts[i] for i in sorted(texts) if i <= max_index and texts[i])
    return spoken, played


def whole_sentence_spoken(
    sent_text: Mapping[int, str],
    sent_audio_s: Mapping[int, float],
    played_s: float | None,
    interrupted: bool,
) -> str | None:
    """整句播完才算已播（逐字對應 bot ``VoiceTurn.spoken_text``）。

    * ``sent_text``：句序號 → 送進 TTS 的文字（過濾後）
    * ``sent_audio_s``：句序號 → 已交給 sink 的音訊秒數
    * ``played_s``：sink 實播秒數；不是數字回 None
    * ``interrupted``：這輪被截斷（barge-in／被取代／被 abort／出錯）

    沒有任何送出的句子、或沒有實播進度 → None（語意是「未知」，不是空字串）。
    被打斷時最後一個有音訊的句子不算確知（後半可能還沒合成），除非後面已有別句音訊。
    """
    if not sent_text:
        return None
    if not isinstance(played_s, (int, float)):
        return None
    order = sorted(sent_text)
    out: list[str] = []
    cum = 0.0
    for pos, idx in enumerate(order):
        dur = sent_audio_s.get(idx, 0.0)
        if dur <= 0:
            break
        cum += dur
        if cum > played_s + SPOKEN_TOLERANCE_S:
            break
        has_later = any(sent_audio_s.get(j, 0.0) > 0 for j in order[pos + 1:])
        if interrupted and not has_later:
            break
        out.append(sent_text[idx])
    return "".join(out)


def build_sentence_spans(
    timeline: Sequence[tuple[int, float]], texts: Mapping[int, str]
) -> list[SentenceSpan]:
    """由 chunk 時間軸組出各句在實播時間軸上的區間（秒）。"""
    spans: dict[int, list[float]] = {}
    prev_end = 0.0
    for idx, cumulative in timeline:
        if idx in spans:
            spans[idx][1] = cumulative
        else:
            spans[idx] = [prev_end, cumulative]
        prev_end = cumulative
    return [
        SentenceSpan(index=idx, text=texts.get(idx, ""), start_s=lo, end_s=hi)
        for idx, (lo, hi) in sorted(spans.items())
    ]


# ─────────────────────────── 預設策略 ───────────────────────────


class AcceptAllAdmission:
    """預設收件閘門：全部接受。"""

    def admit(self, request: TurnRequest, busy: bool, waiting: int) -> bool:  # noqa: ARG002
        return True


class NeverYield:
    """預設讓位策略：永不讓位。讓位累積器屬宿主（bot 自有 per-user 累積）。"""

    def on_voiced(self, ev: VoiceEvent, live: Sequence[TurnView]) -> list[YieldAction]:  # noqa: ARG002
        return []


class AccumulatingBargeIn:
    """累積式 barge-in（bot VO:420-455 的行為）。

    * **per turn、per 說話者**累積；只累積 ``turn.barge_users`` 內的人
      （``barge_users`` 為空且快照未過期 = 不限說話者；快照過期則沒有人能插話）。
    * 加總的是每個事件的**有聲增量秒數**（``ev.chunk_s``；沒有就用時間戳差，且不超過
      ``gap_reset_s``），**不是** ``duration_s``（那是「狀態已持續多久」的累積值）。
    * 間隔超過 ``gap_reset_s`` 歸零；累積達 ``min_speech_s`` 才算插話。
    * 只在 ``turn.sink_active`` 時判定：排隊中只累積，開播瞬間累積值直接帶入。
    * 觸發後**不歸零**，靠 per-turn flag 防重複觸發。
    * 門檻取自快照（平台 preset）；建構時給 ``min_speech_s`` / ``gap_reset_s`` 可覆寫。

    執行緒：從 STT／接收執行緒呼叫，內部以 :class:`threading.Lock` 保護。
    時間用 ``ev.timestamp``（<=0 時退回 ``clock``），測試可直接注入。
    """

    def __init__(
        self,
        source: PolicySource | None = None,
        *,
        min_speech_s: float | None = None,
        gap_reset_s: float | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._source = source
        self._min_speech_s = min_speech_s
        self._gap_reset_s = gap_reset_s
        self._clock = clock
        self._lock = threading.Lock()
        self._state: dict[str, dict[str | None, list[float]]] = {}
        """turn_id → 說話者 → [累積秒數, 上次有聲時刻]。"""
        self._triggered: set[str] = set()

    def _snapshot(self) -> PolicySnapshot:
        return self._source.current() if self._source is not None else PolicySnapshot()

    def on_voiced(self, turn: TurnView, ev: VoiceEvent) -> InterruptionDecision:
        if not (ev.is_voiced or ev.state is VoiceState.SPEECH):
            return InterruptionDecision(False, reason="not_voiced")
        snap = self._snapshot()
        stale = snap.is_stale()
        min_speech = self._min_speech_s if self._min_speech_s is not None else (
            snap.barge_in_min_speech_s
        )
        gap_reset = self._gap_reset_s if self._gap_reset_s is not None else (
            snap.barge_in_gap_reset_s
        )

        users = turn.barge_users
        if users:
            if ev.speaker_id not in users:
                return InterruptionDecision(False, reason="not_barge_user")
        elif stale:
            return InterruptionDecision(False, reason="stale_snapshot")

        now = ev.timestamp if ev.timestamp > 0 else self._clock()
        with self._lock:
            tid = turn.turn_id
            if tid in self._triggered:
                return InterruptionDecision(False, reason="already_triggered")
            per_turn = self._state.setdefault(tid, {})
            state = per_turn.get(ev.speaker_id)
            if state is None:
                state = per_turn[ev.speaker_id] = [0.0, now]
                prev_at = None
            else:
                prev_at = state[1]
            if now - state[1] > gap_reset:
                state[0] = 0.0
            if ev.chunk_s > 0:
                inc = ev.chunk_s
            elif prev_at is not None:
                inc = max(0.0, min(now - prev_at, gap_reset))
            else:
                inc = 0.0
            state[0] += inc
            state[1] = now
            accumulated = state[0]

            if not turn.sink_active:
                return InterruptionDecision(False, reason="queued_accumulating")
            if accumulated < min_speech:
                return InterruptionDecision(False, reason="below_threshold")
            self._triggered.add(tid)
        return InterruptionDecision(True, reason="sustained_speech")

    def accumulated(self, turn_id: str, speaker_id: str | None = None) -> float:
        """目前累積秒數（診斷與測試用）。"""
        with self._lock:
            state = self._state.get(turn_id, {}).get(speaker_id)
            return 0.0 if state is None else state[0]

    def forget(self, turn_id: str) -> None:
        """輪次結束時由 coordinator 呼叫，清掉該輪狀態。"""
        with self._lock:
            self._state.pop(turn_id, None)
            self._triggered.discard(turn_id)


class StandaloneTicketIssuer:
    """本地簽發 ticket：``turn_id`` 直接沿用 ``Utterance.turn_id``（原 ES ``core.standalone``，N1 併入；
    Core 宿主的 ticket 由 Core 簽發，這裡供單機測試與 fake 驅動）。"""

    def __init__(self, snapshot_version: int = 0) -> None:
        self._snapshot_version = snapshot_version

    def issue(self, utterance: Utterance, *, reply_mode: str | None = None) -> TurnTicket:
        return TurnTicket(
            turn_id=utterance.turn_id,
            source_turn_id=utterance.turn_id,
            owner_id=utterance.speaker_id,
            reply_mode=reply_mode,
            snapshot_version=self._snapshot_version,
        )


class StaticPolicySource:
    """固定快照的 :class:`~uep.stream.contracts.output.PolicySource` 替身（原 ES ``core.standalone``，N1 併入）。"""

    def __init__(self, snapshot: PolicySnapshot | None = None) -> None:
        self._snapshot = snapshot or PolicySnapshot()

    def current(self) -> PolicySnapshot:
        return self._snapshot

    def replace(self, snapshot: PolicySnapshot) -> None:
        """換成新快照（測試用，模擬 Core 推送新版本）。"""
        self._snapshot = snapshot


__all__ = [
    "StandaloneTicketIssuer",
    "StaticPolicySource",
    "SPOKEN_TOLERANCE_S",
    "AcceptAllAdmission",
    "AccumulatingBargeIn",
    "NeverYield",
    "build_sentence_spans",
    "partial_spoken",
    "whole_sentence_spoken",
]
