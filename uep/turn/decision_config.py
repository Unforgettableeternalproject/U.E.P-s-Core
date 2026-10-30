"""決策插槽的預設實作設定（P3接入方案 §4、S5）。

數值逐項沿用 bot 現行 ``echo_decision.config``（預設值即 bot 的預設）；欄位名稱也沿用，方便對照。
刻意沒有搬的欄位（narrative、tentative probe、session、authority）見方案 §5.2。
全部為不可變 dataclass：改設定請用 ``dataclasses.replace`` 組出新的 ``DecisionConfig``。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AggregatorConfig:
    merge_gap_ms: int = 1500  # 段落間隔 <= 此值才合併
    max_wait_ms: int = 3000  # 最後段落後等待此時間 -> Turn 完成
    max_duration_s: float = 30.0  # Turn 最大長度（超過即 overflow）
    min_text_length: int = 2  # 去空白後文字 < 此值 -> 丟棄
    active_merge_gap_ms: int = 250  # 主動模式的 merge_gap
    active_max_wait_ms: int = 250  # 主動模式的 max_wait


@dataclass(frozen=True)
class BotIdentityConfig:
    """喚醒詞：人設自己的名字與別名（不是平台成員名稱）。"""

    primary_name: str = "U.E.P"
    aliases: tuple[str, ...] = ("小U", "UEP", "uep")

    @property
    def all_names(self) -> tuple[str, ...]:
        return (self.primary_name, *self.aliases)


@dataclass(frozen=True)
class GateConfig:
    certain_threshold: float = 0.8  # >= 此值 -> CERTAIN
    tentative_threshold: float = 0.3  # 0.3~0.8 -> TENTATIVE
    context_window_s: float = 8.0  # 回覆後的上下文延續窗口
    context_user_sticky: bool = True  # 窗口只屬於被回覆的那位說話者
    fuzzy_min_name_length: int = 4  # 名字字元數 < 此值 -> 不做模糊匹配
    fuzzy_chars_per_edit: int = 3  # 容許編輯距離 = min(2, 字元數 // 此值)


@dataclass(frozen=True)
class ContextConfidenceConfig:
    per_turn_increment: float = 0.15
    topic_relevant_increment: float = 0.25
    decay_interval_s: float = 10.0
    decay_factor: float = 0.9


@dataclass(frozen=True)
class SoloEngagementConfig:
    meaningfulness_min_length: int = 3


@dataclass(frozen=True)
class GroupEngagementConfig:
    min_turns_before_engage: int = 3
    context_confidence_threshold: float = 0.6
    silence_trigger_s: float = 10.0
    engage_cooldown_s: float = 40.0  # 被動模式兩次插話的最短間隔
    interject_min_new_turns: int = 3  # 自上次派出以來新完成的 Turn 數 < 此值就不插話


@dataclass(frozen=True)
class EngagementConfig:
    solo: SoloEngagementConfig = field(default_factory=SoloEngagementConfig)
    group: GroupEngagementConfig = field(default_factory=GroupEngagementConfig)
    context_confidence: ContextConfidenceConfig = field(default_factory=ContextConfidenceConfig)


@dataclass(frozen=True)
class CouplingModelConfig:
    join_default: float = 0.5
    headcount_coefficient: float = 0.2
    headcount_floor: float = -0.15
    topic_up: float = 0.05
    topic_down: float = 0.03
    topic_cap: float = 0.3
    topic_halflife_s: float = 120.0


@dataclass(frozen=True)
class ModeConfig:
    active_enter: float = 0.55
    active_exit: float = 0.45
    active_max_users: int = 2  # 人數 > 此值一律被動


@dataclass(frozen=True)
class ListenerConfig:
    """聆聽者讓位：主動模式下對方還在講，尚未開播的回覆讓位。"""

    yield_unstarted: bool = True
    yield_min_speech_s: float = 1.5  # 沒有待讓位的回覆時的門檻（擋嗯、咳嗽）
    yield_min_speech_s_pending: float = 0.7  # 有尚未開播（含排隊中）的回覆時的較短門檻
    speech_gap_reset_s: float = 0.5  # 有聲事件間隔超過此值，累積歸零


@dataclass(frozen=True)
class DecisionConfig:
    bot_identity: BotIdentityConfig = field(default_factory=BotIdentityConfig)
    aggregator: AggregatorConfig = field(default_factory=AggregatorConfig)
    gate: GateConfig = field(default_factory=GateConfig)
    engagement: EngagementConfig = field(default_factory=EngagementConfig)
    coupling: CouplingModelConfig = field(default_factory=CouplingModelConfig)
    mode: ModeConfig = field(default_factory=ModeConfig)
    listener: ListenerConfig = field(default_factory=ListenerConfig)
    # D80：proposer 的決策預算（毫秒），暫定 300；S6 依 D73 的基準校正
    proposer_budget_ms: int = 300
    # Q-1：使用者插話（outcome.barge_in）之後，非被點名的 passive 類觸發（context_passive、插話）暫停多久。
    # bot 沒有對應行為（只有 engage_cooldown_s），此值為 S5 新增的暫定值
    barge_in_cooldown_s: float = 8.0
    # 宿主驅動 ``Orchestrator.tick`` 的間隔（秒）；bot 的 tick 建議 200-500ms
    tick_interval_s: float = 0.25
