"""預設決策實作：純規則的 TurnProposer（搬自 bot ``echo_decision.middleware`` 的決策管線，S5）。

不接任何模型。對照 bot 的 trigger，輸出 Core 的 Proposal（詞彙見 P3接入方案 §4.2、§5.2 對照表）：

- 被叫到（called）、延續對話（context／context_passive）、主動模式（active）、被動模式插話（interject_*）
  -> ``respond``，``reason_code`` 帶 trigger；
- 只進 buffer 的旁聽、主動模式下的語氣詞、未確認的疑似叫名 -> ``pass``（不簽 ticket，寫 ``turn.skipped``）。
  ``passive`` 這個 reply_mode 只留給 D80 逾時預設（Orchestrator 的 ``fallback_proposal``）。

頻道模式（active／passive，``ChannelMode``）是 proposer 的內部狀態，變化時透過 ``on_mode_change`` 通知宿主，
宿主據以推送政策快照（讓位只在主動模式生效）。回饋由選用的 ``OutcomeObserver`` 方法接收；
Q-1：插話（``barge_in``）後的冷卻在這裡，不在 ES。

執行緒：``propose`` 在 Orchestrator 的決策執行緒上跑，``on_turn_*``、``tick``、``set_participant_count``
來自其他執行緒，全部以同一把鎖序列化。時間由注入的 ``clock`` 提供。
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from uep.turn.decision_config import DecisionConfig
from uep.turn.engagement import (
    ChannelMode,
    Clock,
    ContextTracker,
    CouplingModel,
    EngagementMonitor,
    ModeController,
    ObservedTurn,
)
from uep.turn.models import Proposal, ReplyMode, TurnCandidate, TurnOutcomeFacts
from uep.turn.proposer import Interjection
from uep.turn.wake import AddressConfidence, WakeDetector

logger = logging.getLogger("uep.turn.rules")

_INTERACTION_LISTENING = "LISTENING"
_PENDING_MAX = 64  # 待結算話題項的上限（沒有回報結果的 turn 不會永遠佔位）


@dataclass(frozen=True)
class _Topic:
    """待結算話題項：主動模式下 turn 先不計分，等 LLM 實際回覆（+）或 PASS（-）才結算。"""


class RuleProposer:
    proposer_id = "rules.default"
    version = "s5-bot-port"

    def __init__(
        self,
        config: DecisionConfig | None = None,
        *,
        clock: Clock = time.monotonic,
        on_mode_change: Callable[[ChannelMode], None] | None = None,
    ) -> None:
        self._cfg = config or DecisionConfig()
        self._clock = clock
        self._lock = threading.RLock()
        self.wake = WakeDetector(self._cfg.gate, self._cfg.bot_identity)
        self._coupling = CouplingModel(self._cfg.coupling, self._cfg.mode.active_max_users, clock)
        self._mode_ctl = ModeController(self._cfg.mode)
        self._context = ContextTracker(self._cfg.gate, clock)
        self._engagement = EngagementMonitor(self._cfg.engagement, clock)
        self._on_mode_change = [on_mode_change] if on_mode_change is not None else []
        self._mode_lock: ChannelMode | None = None
        self._users = 0
        self._responding: set[str] = set()
        self._pre_windows: dict[str, float | None] = {}
        self._pending: dict[str, _Topic] = {}  # candidate_id -> 待結算話題項
        self._barge_cooldown_until: float | None = None

    # ---- 宿主接線 ----

    def add_mode_listener(self, listener: Callable[[ChannelMode], None]) -> None:
        self._on_mode_change.append(listener)

    @property
    def channel_mode(self) -> ChannelMode:
        return self._mode_ctl.mode

    def set_participant_count(self, count: int) -> None:
        """在場人數（含說話者）變動：人數影響耦合度與主動／被動。"""
        with self._lock:
            self._users = max(0, count)
            self._coupling.set_user_count(self._users)
            self._refresh()

    def set_mode_override(self, mode: ChannelMode | None) -> ChannelMode:
        """鎖定 active／passive（None = 解除，回到耦合度與人數的自動判斷）。"""
        with self._lock:
            self._mode_lock = mode
            return self._refresh()

    def reset(self, user_count: int = 0) -> None:
        """加入新頻道：耦合度回到預設、回到被動觀察，清掉所有進行中的狀態。"""
        with self._lock:
            self._users = max(0, user_count)
            self._coupling.reset(self._users)
            self._mode_lock = None
            self._mode_ctl.reset()
            self._engagement.set_active(False)
            self._engagement.consume_buffer()
            self._responding.clear()
            self._pre_windows.clear()
            self._pending.clear()
            self._barge_cooldown_until = None
            self._refresh()

    @property
    def coupling_value(self) -> float:
        with self._lock:
            return self._coupling.value()

    @property
    def topic_term(self) -> float:
        with self._lock:
            return self._coupling.topic_term()

    @property
    def pending_topics(self) -> int:
        with self._lock:
            return len(self._pending)

    def is_listener_continuation(self, text: str) -> bool:
        """主動模式下，這段新文字是否算「對方還在講」（有意義的內容，語氣詞不算）。"""
        with self._lock:
            return self._mode_ctl.mode is ChannelMode.ACTIVE and self._engagement.is_meaningful(text)

    # ---- TurnProposer ----

    def propose(self, candidate: TurnCandidate) -> Proposal:
        with self._lock:
            if candidate.participant_count is not None and candidate.participant_count != self._users:
                self._users = candidate.participant_count
                self._coupling.set_user_count(self._users)
            mode = self._refresh()
            speaker = candidate.speakers[0] if candidate.speakers else ""
            turn = ObservedTurn(candidate.candidate_id, speaker, candidate.text, candidate.utterance_refs)
            return self._decide(candidate, turn, speaker, mode)

    def _decide(self, candidate: TurnCandidate, turn: ObservedTurn, speaker: str, mode: ChannelMode) -> Proposal:
        text = candidate.text
        # Step 1：上下文窗口（含「回覆生成中」）。被動模式下剛被回覆的人接著說的話不一定還是對人設說的：
        # 喚醒詞命中就照一般被叫到走，否則 trigger 標成 context_passive
        in_context = self._context.is_in_context(speaker) or speaker in self._responding
        if in_context:
            passive_ctx = mode is ChannelMode.PASSIVE
            wake = self.wake.detect(text) if passive_ctx else None
            if wake is None or wake.confidence_level is not AddressConfidence.CERTAIN:
                self._record(turn, addressed=True, defer=not passive_ctx)
                if passive_ctx and self._in_barge_cooldown():
                    return self._pass(candidate, "barge_in_cooldown")
                return self._respond(candidate, "context_passive" if passive_ctx else "context", 0.95)
        # Step 2：喚醒詞
        gate = self.wake.detect(text)
        level = gate.confidence_level
        # Step 3：主動模式下每個有意義的 turn 都回應（不必叫名字；疑似叫名一律當成對人設說話）
        if mode is ChannelMode.ACTIVE and level is not AddressConfidence.CERTAIN:
            if self._engagement.is_meaningful(text):
                self._record(turn, addressed=True, defer=True)
                return self._respond(candidate, "active", max(0.85, gate.confidence_score))
            self._record(turn, addressed=False, affect=False)  # 語氣詞：旁聽，主動模式下也不扣耦合度
            return self._pass(candidate, "not_meaningful")
        # Step 4：分流
        if level is AddressConfidence.CERTAIN:
            self._record(turn, addressed=True)
            return self._respond(candidate, "called", gate.confidence_score)
        if level is AddressConfidence.TENTATIVE:
            # 未搬 bot 的 probe（需要「剛剛有叫我嗎？」的內容回覆機制）：當成旁聽，但不計為被叫到
            self._engagement.on_turn(turn, is_addressed=False)
            return self._pass(candidate, "tentative_unconfirmed")
        self._record(turn, addressed=False)  # 被動模式下沒叫到：只進 buffer
        return self._pass(candidate, "observe")

    def _respond(self, candidate: TurnCandidate, reason: str, score: float) -> Proposal:
        # 回覆即派出：取走 buffer 並套用冷卻（bot 的 ``_take_buffer``）；buffer 內的旁聽 turn 不隨 ticket 帶走
        self._engagement.consume_buffer()
        self._engagement.mark_dispatched()
        return Proposal(
            reply_mode=ReplyMode.RESPOND, addressed=True, confidence=min(1.0, score), reason_code=reason,
            proposer_id=self.proposer_id, proposer_version=self.version,
        )

    def _pass(self, candidate: TurnCandidate, reason: str) -> Proposal:
        # 進入旁聽觀察：Interaction 軸從 IDLE 轉 LISTENING（其他狀態不動，避免版本與 from_state 衝突）
        listening = _INTERACTION_LISTENING if candidate.interaction_state == "IDLE" else None
        return Proposal(
            reply_mode=ReplyMode.PASS, addressed=False, confidence=0.0, reason_code=reason,
            interaction_to=listening, proposer_id=self.proposer_id, proposer_version=self.version,
        )

    # ---- tick：被動模式的插話 ----

    def tick(self) -> list[Interjection]:
        with self._lock:
            mode = self._refresh()
            self._engagement.tick()
            self._context.cleanup()
            if mode is not ChannelMode.PASSIVE or self._in_barge_cooldown():
                return []
            decision = self._engagement.evaluate()
            if not decision.should_engage or not decision.context:
                return []
            if self._prefilter():
                return []
            buffered = self._engagement.consume_buffer()
            self._engagement.mark_dispatched()
            last = buffered[-1] if buffered else decision.context[-1]
            reason = "interject_silence" if "silence" in decision.reason else "interject_context"
            proposal = Proposal(
                reply_mode=ReplyMode.RESPOND, addressed=False, confidence=0.0, reason_code=reason,
                proposer_id=self.proposer_id, proposer_version=self.version,
            )
            speakers = tuple(dict.fromkeys(t.speaker_ref for t in (buffered or decision.context)))
            return [Interjection(last.speaker_ref, last.text, last.utterance_refs, proposal, speakers)]

    def _prefilter(self) -> bool:
        """插話前篩選：自上次派出以來新 turn 太少 -> 跳過（模型只會看到同一批舊對話然後 PASS）。跳過不套冷卻。"""
        need = max(1, self._cfg.engagement.group.interject_min_new_turns)
        return self._engagement.new_turns_since_dispatch < need

    # ---- OutcomeObserver ----

    def on_turn_started(self, turn_id: str, candidate_id: str, speakers: tuple[str, ...]) -> None:
        """回覆開始：開 context window 並標記「生成中」（bot 的 ``on_bot_response_started``）。"""
        with self._lock:
            for uid in speakers:
                if uid:
                    self._pre_windows.setdefault(uid, self._context.snapshot(uid))
                    self._context.on_bot_responded(uid)
                    self._responding.add(uid)

    def on_turn_outcome(self, facts: TurnOutcomeFacts) -> None:
        with self._lock:
            topic = self._pending.pop(facts.candidate_id, None) is not None
            if facts.barge_in:
                # Q-1：使用者插話後，非被點名的 passive 類觸發暫停一段時間
                self._barge_cooldown_until = self._clock() + self._cfg.barge_in_cooldown_s
            if facts.status == "dropped_busy":
                self._settle(topic, None)
            elif facts.status == "completed" and facts.generation_ok or facts.barge_in:
                self._responded(facts.speakers)
                self._settle(topic, True)
            elif facts.status == "skipped" and facts.end_reason == "pass":
                self._passed(facts.speakers)
                self._settle(topic, False)
            else:  # 其他跳過、取消、作廢、失敗：沒有回覆也不是 PASS，話題項不增不減
                self._failed(facts.speakers)
                self._settle(topic, None)

    def _responded(self, speakers: tuple[str, ...]) -> None:
        for uid in speakers:
            if uid:
                self._context.on_bot_responded(uid)
                self._responding.discard(uid)
                self._pre_windows.pop(uid, None)
        self._engagement.on_bot_responded()

    def _passed(self, speakers: tuple[str, ...]) -> None:
        """LLM 決定不插話：還原開始時開的 context window，解除「生成中」標記。"""
        for uid in speakers:
            if uid and uid in self._pre_windows:
                self._context.restore(uid, self._pre_windows[uid])
            self._pre_windows.pop(uid, None)
            self._responding.discard(uid)

    def _failed(self, speakers: tuple[str, ...]) -> None:
        for uid in speakers:
            self._responding.discard(uid)
            self._pre_windows.pop(uid, None)

    # ---- 內部 ----

    def _in_barge_cooldown(self) -> bool:
        return self._barge_cooldown_until is not None and self._clock() < self._barge_cooldown_until

    def _record(self, turn: ObservedTurn, addressed: bool, affect: bool = True, defer: bool = False) -> None:
        """每個 turn 恰好記錄一次：engagement buffer 與耦合度話題項。

        ``affect=False``：話題項不增不減。``defer=True``：話題項先不計，等回覆結果出來由 ``_settle`` 結算。
        """
        self._engagement.on_turn(turn, is_addressed=addressed)
        if affect:
            if defer:
                self._pending[turn.turn_key] = _Topic()
                while len(self._pending) > _PENDING_MAX:
                    self._pending.pop(next(iter(self._pending)))
            else:
                self._coupling.on_turn(topic_relevant=addressed)
        self._refresh()

    def _settle(self, holds_topic: bool, relevant: bool | None) -> None:
        """結算一個主動模式 turn 的話題項：True = 實際回覆（+），False = PASS（-），None = 作廢（不計）。"""
        if not holds_topic:
            return
        if relevant is not None:
            self._coupling.on_turn(topic_relevant=relevant)
            self._refresh()

    def _refresh(self) -> ChannelMode:
        """依耦合度與人數重新評估主動／被動，並同步 engagement 的運作模式；模式變化時通知宿主。"""
        before = self._mode_ctl.mode
        if self._mode_lock is not None:
            mode = self._mode_ctl.force(self._mode_lock)
        else:
            mode = self._mode_ctl.update(self._coupling.value(), self._users)
        if mode is not before:
            logger.info("頻道模式 %s -> %s（coupling=%.2f users=%d）", before.value, mode.value,
                        self._coupling.value(), self._users)
            self._engagement.set_active(mode is ChannelMode.ACTIVE)
            for listener in list(self._on_mode_change):
                try:
                    listener(mode)
                except Exception:
                    logger.exception("頻道模式回呼失敗")
        return mode
