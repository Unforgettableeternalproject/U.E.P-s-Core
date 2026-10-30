"""耦合度、主動／被動模式、上下文窗口與插話判斷（搬自 bot ``echo_decision.engagement`` 與 ``gate.context_tracker``）。

純同步邏輯，不依賴 Discord 與任何子系統；時間一律由注入的 ``clock``（預設 ``time.monotonic``）提供，
測試可用假時鐘逐步推進。非執行緒安全：由預設 proposer 的單一決策執行緒序列化呼叫。
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from uep.turn.decision_config import (
    CouplingModelConfig,
    EngagementConfig,
    GateConfig,
    ModeConfig,
)

Clock = Callable[[], float]

_EPS = 1e-9  # 浮點誤差容忍（0.5 + 0.05 要能剛好等於 0.55）
_BUFFER_MAX = 20  # 滾動對話 buffer 上限

# 純語氣詞與感嘆詞：主動模式不查詢 LLM
_FILLER_WORDS = frozenset({"嗯", "啊", "哦", "呃", "唔", "嘿", "哈", "喔", "欸", "誒", "唉", "哼", "嗚", "咦"})
_EXCLAMATION_WORDS = frozenset({"哇", "天啊", "靠", "幹", "臥槽", "我的天", "好棒", "不會吧", "真的假的", "太誇張了"})


class ChannelMode(StrEnum):
    """頻道層級的回應模式（bot 的 active／passive）；與 Proposal.reply_mode 不是同一個軸。"""

    ACTIVE = "active"  # 每個完整 Turn 都交給決策
    PASSIVE = "passive"  # 只在被叫到、延續對話、插話條件成立時回應


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class CouplingModel:
    """耦合度 = 加入預設 + 人數項 + 話題項（clamp 到 0~1）。人數項是絕對值，話題項以半衰期衰減回 0。"""

    def __init__(
        self, config: CouplingModelConfig | None = None, active_max_users: int | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        self._cfg = config or CouplingModelConfig()
        self._active_max_users = ModeConfig().active_max_users if active_max_users is None else active_max_users
        self._clock = clock
        self._user_count = 0
        self._topic = 0.0
        self._topic_ts = clock()

    def reset(self, user_count: int = 0) -> None:
        self._user_count = max(0, user_count)
        self._topic = 0.0
        self._topic_ts = self._clock()

    def set_user_count(self, count: int) -> None:
        self._user_count = max(0, count)

    def on_turn(self, topic_relevant: bool) -> None:
        """話題相關 +topic_up，無關 -topic_down。"""
        self._decay()
        delta = self._cfg.topic_up if topic_relevant else -self._cfg.topic_down
        cap = self._cfg.topic_cap
        self._topic = _clamp(self._topic + delta, -cap, cap)

    @property
    def user_count(self) -> int:
        return self._user_count

    def headcount_term(self) -> float:
        if self._user_count <= 0:
            return 0.0
        raw = (self._active_max_users + 1 - self._user_count) * self._cfg.headcount_coefficient
        return max(raw, self._cfg.headcount_floor)

    def topic_term(self) -> float:
        self._decay()
        return self._topic

    def value(self) -> float:
        return _clamp(self._cfg.join_default + self.headcount_term() + self.topic_term(), 0.0, 1.0)

    def _decay(self) -> None:
        now = self._clock()
        elapsed = now - self._topic_ts
        self._topic_ts = now
        if elapsed <= 0 or self._topic == 0.0:
            return
        half = self._cfg.topic_halflife_s
        self._topic = 0.0 if half <= 0 else self._topic * math.pow(0.5, elapsed / half)


class ModeController:
    """依耦合度與人數決定主動／被動，進入與離開用不同門檻（遲滯）。"""

    def __init__(self, config: ModeConfig | None = None) -> None:
        self._cfg = config or ModeConfig()
        self._mode = ChannelMode.PASSIVE

    @property
    def mode(self) -> ChannelMode:
        return self._mode

    def reset(self) -> None:
        self._mode = ChannelMode.PASSIVE

    def force(self, mode: ChannelMode) -> ChannelMode:
        self._mode = mode
        return mode

    def update(self, coupling: float, user_count: int) -> ChannelMode:
        cfg = self._cfg
        if user_count < 1 or user_count > cfg.active_max_users:
            self._mode = ChannelMode.PASSIVE
        elif self._mode is ChannelMode.PASSIVE:
            if coupling + _EPS >= cfg.active_enter:
                self._mode = ChannelMode.ACTIVE
        elif coupling + _EPS < cfg.active_exit:
            self._mode = ChannelMode.PASSIVE
        return self._mode


class ContextTracker:
    """回覆後的上下文延續窗口：被回覆的說話者在 ``context_window_s`` 內不必再叫名字。"""

    _ALL_USERS_KEY = "__all__"

    def __init__(self, config: GateConfig | None = None, clock: Clock = time.monotonic) -> None:
        self._config = config or GateConfig()
        self._clock = clock
        self._windows: dict[str, float] = {}

    def _key(self, speaker: str) -> str:
        return speaker if self._config.context_user_sticky else self._ALL_USERS_KEY

    def on_bot_responded(self, speaker: str) -> None:
        self._windows[self._key(speaker)] = self._clock() + self._config.context_window_s

    def snapshot(self, speaker: str) -> float | None:
        return self._windows.get(self._key(speaker))

    def restore(self, speaker: str, expire_at: float | None) -> None:
        """還原 snapshot 的窗口：None = 原本沒有窗口，直接關掉。"""
        if expire_at is None:
            self._windows.pop(self._key(speaker), None)
        else:
            self._windows[self._key(speaker)] = expire_at

    def is_in_context(self, speaker: str) -> bool:
        expire_at = self._windows.get(self._key(speaker))
        return expire_at is not None and self._clock() < expire_at

    def cleanup(self) -> None:
        now = self._clock()
        for key in [k for k, t in self._windows.items() if now >= t]:
            del self._windows[key]


@dataclass(frozen=True)
class ObservedTurn:
    """進入 engagement buffer 的一個完整 Turn（插話時的素材）。"""

    turn_key: str
    speaker_ref: str
    text: str
    utterance_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class EngagementDecision:
    should_engage: bool
    reason: str  # context_ready／silence_trigger／low_confidence
    context: tuple[ObservedTurn, ...] = ()


class EngagementMonitor:
    """被動模式的插話狀態機：累積 context_confidence，達門檻或沉默超時才插話。

    外部呼叫順序：``on_turn``（每個 Turn）、``mark_dispatched`` / ``on_bot_responded``（派出或回覆後）、
    ``tick``（定期，信心衰減）、``evaluate``（要決策時）。預設為被動（GROUP）；``set_active`` 切換。
    """

    def __init__(self, config: EngagementConfig | None = None, clock: Clock = time.monotonic) -> None:
        self._cfg = config or EngagementConfig()
        self._clock = clock
        self._active = False
        self._buffer: list[ObservedTurn] = []
        self._confidence = 0.0
        self._last_engage: float | None = None
        self._last_turn: float | None = None
        self._last_decay = clock()
        self._new_turns = 0

    def set_active(self, active: bool) -> None:
        """切換主動／被動；切到被動時重置信心，避免殘留主動模式的高信心。"""
        if active == self._active:
            return
        self._active = active
        if not active:
            self._confidence = 0.0

    def on_turn(self, turn: ObservedTurn, is_addressed: bool) -> None:
        self._last_turn = self._clock()
        self._new_turns += 1
        self._buffer.append(turn)
        if len(self._buffer) > _BUFFER_MAX:
            self._buffer.pop(0)
        if not self._active:
            cc = self._cfg.context_confidence
            self._confidence = min(
                1.0, self._confidence + (cc.topic_relevant_increment if is_addressed else cc.per_turn_increment)
            )

    def on_bot_responded(self) -> None:
        now = self._clock()
        self._last_engage = now
        self._last_turn = now  # 沉默基線重新起算，避免 silence_trigger 重複觸發
        if not self._active:
            self._confidence = 0.0

    def mark_dispatched(self) -> None:
        """決策已派出（回覆還在生成）時立刻呼叫：套用冷卻並回到觀察狀態。"""
        now = self._clock()
        self._last_engage = now
        self._last_turn = now
        self._new_turns = 0
        if not self._active:
            self._confidence = 0.0

    def consume_buffer(self) -> list[ObservedTurn]:
        taken = list(self._buffer)
        self._buffer.clear()
        return taken

    def discard_turns(self, turn_keys: Sequence[str]) -> None:
        drop = set(turn_keys)
        self._buffer[:] = [t for t in self._buffer if t.turn_key not in drop]

    def discard_speaker(self, speaker_ref: str) -> None:
        """成員離場：清掉他留在 buffer 的文字。"""
        self._buffer[:] = [t for t in self._buffer if t.speaker_ref != speaker_ref]

    def is_meaningful(self, text: str) -> bool:
        """主動模式的意義性過濾：過短、純語氣詞、純感嘆詞不查詢。"""
        text = text.strip()
        if len(text) < self._cfg.solo.meaningfulness_min_length:
            return False
        return text not in _FILLER_WORDS and text not in _EXCLAMATION_WORDS

    def tick(self) -> None:
        """被動模式：context_confidence 的時間衰減。"""
        if self._active:
            return
        now = self._clock()
        cc = self._cfg.context_confidence
        elapsed = now - self._last_decay
        if elapsed >= cc.decay_interval_s:
            self._confidence *= cc.decay_factor ** int(elapsed / cc.decay_interval_s)
            self._last_decay = now

    def evaluate(self) -> EngagementDecision:
        """被動模式的插話條件：信心達門檻且冷卻結束且累積足夠 Turn，或沉默超時且冷卻結束。"""
        group = self._cfg.group
        context = tuple(self._buffer)
        now = self._clock()
        cooldown_ok = self._last_engage is None or (now - self._last_engage) >= group.engage_cooldown_s
        if (
            self._confidence >= group.context_confidence_threshold
            and cooldown_ok
            and len(context) >= group.min_turns_before_engage
        ):
            return EngagementDecision(True, "context_ready", context)
        if (
            self._last_turn is not None
            and (now - self._last_turn) >= group.silence_trigger_s
            and cooldown_ok
        ):
            return EngagementDecision(True, "silence_trigger", context)
        return EngagementDecision(False, "low_confidence", context)

    @property
    def new_turns_since_dispatch(self) -> int:
        return self._new_turns

    @property
    def context_confidence(self) -> float:
        return 0.0 if self._active else self._confidence

    def peek_buffer(self) -> list[ObservedTurn]:
        return list(self._buffer)
