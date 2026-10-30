"""Core 端的政策快照與熱路徑純邏輯（P3接入方案 §4.4）。

ES 的 ``PolicySnapshot`` 只放機制欄位（barge-in 門檻、mode_rules、ttl）；讓位判定需要的頻道模式與門檻
屬 Core 政策，放在這裡的 ``PolicySnapshot``，由 ``uep.echo.policy.HostPolicySource`` 與 ES 快照同版推送。
熱路徑（每個 VAD chunk）只讀這個 frozen 參照，不呼叫 Core、DB 或模型。本檔不依賴 echo_stream。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from uep.turn.decision_config import ListenerConfig
from uep.turn.engagement import ChannelMode


@dataclass(frozen=True)
class PolicySnapshot:
    version: int = 0
    channel_mode: ChannelMode = ChannelMode.PASSIVE
    yield_unstarted: bool = True
    yield_min_speech_s: float = 1.5
    yield_min_speech_s_pending: float = 0.7
    speech_gap_reset_s: float = 0.5
    # reply_mode（Proposal 詞彙）-> 機制旗標：不在 speak_modes 的 reply_mode 這一輪不出聲，
    # 不在 barge_in_modes 的不可被插話打斷。預設與 S3 一致：只有 passive（D80 逾時預設）靜音
    silent_modes: frozenset[str] = frozenset({"passive"})
    no_barge_in_modes: frozenset[str] = frozenset({"passive"})
    issued_at: float = 0.0  # time.perf_counter()
    ttl_s: float | None = None  # None = 永不過期

    def is_stale(self, now: float | None = None) -> bool:
        if self.ttl_s is None:
            return False
        return (time.perf_counter() if now is None else now) - self.issued_at > self.ttl_s

    @property
    def yield_active(self) -> bool:
        """讓位只在主動模式、且設定開啟時生效（bot 的 ``reply_mode == ACTIVE`` 與 ``yield_unstarted``）。"""
        return self.yield_unstarted and self.channel_mode is ChannelMode.ACTIVE

    @classmethod
    def from_listener(cls, listener: ListenerConfig, **overrides: object) -> PolicySnapshot:
        base: dict[str, object] = {
            "yield_unstarted": listener.yield_unstarted,
            "yield_min_speech_s": listener.yield_min_speech_s,
            "yield_min_speech_s_pending": listener.yield_min_speech_s_pending,
            "speech_gap_reset_s": listener.speech_gap_reset_s,
        }
        base.update(overrides)
        return cls(**base)  # type: ignore[arg-type]


class YieldAccumulator:
    """聆聽者讓位的有聲累積器（搬自 bot ``BotYield``）：per speaker 累積有聲秒數，間隔超過 gap 歸零。

    門檻：有「尚未開播」的待讓位回覆時用 pending 門檻，否則用一般門檻；達標後歸零重新累積，
    不是每個封包都觸發一次。從 STT 執行緒呼叫，內部以鎖保護。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, list[float]] = {}  # speaker -> [累積有聲秒數, 上次有聲時刻]

    def observe(self, speaker: str, seconds: float, now: float, snapshot: PolicySnapshot, pending: bool) -> bool:
        """回傳這次有聲事件是否使「對方還在講」成立。"""
        threshold = snapshot.yield_min_speech_s_pending if pending else snapshot.yield_min_speech_s
        with self._lock:
            state = self._state.setdefault(speaker, [0.0, now])
            if now - state[1] > snapshot.speech_gap_reset_s:
                state[0] = 0.0
            state[0] += seconds
            state[1] = now
            if state[0] < threshold:
                return False
            state[0] = 0.0
            return True

    def reset(self, speaker: str | None = None) -> None:
        with self._lock:
            if speaker is None:
                self._state.clear()
            else:
                self._state.pop(speaker, None)


def mode_flags(snapshot: PolicySnapshot, reply_mode: str | None) -> tuple[bool, bool]:
    """``reply_mode`` -> (speak, barge_in)；reply_mode 為 None 時視為出聲且可打斷（Web 既有行為）。"""
    if reply_mode is None:
        return True, True
    return reply_mode not in snapshot.silent_modes, reply_mode not in snapshot.no_barge_in_modes


def yieldable(owner: str | None, speaker: str, audio_started: bool, behind_playing: bool) -> bool:
    """這一輪現在能不能因為 ``speaker`` 還在講而讓位：屬於這位說話者、尚未開播（含排隊中）、前一輪沒在播。

    前一輪還在播時，這段聲音可能是在打斷前一輪，交給前一輪的 barge-in，這一輪不讓位（bot ``_ticket_yieldable``；
    bot 另有「文字已送出」的條件，Core 沒有私訊補傳，沒有對應）。
    """
    return owner == speaker and not audio_started and not behind_playing
