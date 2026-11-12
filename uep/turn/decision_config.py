"""決策插槽的預設實作設定（P3接入方案 §4、S5）。

數值逐項沿用 bot 現行 ``echo_decision.config``（預設值即 bot 的預設）；欄位名稱也沿用，方便對照。
刻意沒有搬的欄位（tentative probe、session、authority）見方案 §5.2。
全部為不可變 dataclass：改設定請用 ``dataclasses.replace`` 組出新的 ``DecisionConfig``。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("uep.turn.decision_config")
INFERENCE_MARGIN_MS = 80  # 翻譯用滿之後，模型推論至少要留的時間


@dataclass(frozen=True)
class AggregatorConfig:
    merge_gap_ms: int = 1500  # 段落間隔 <= 此值才合併
    max_wait_ms: int = 3000  # 最後段落後等待此時間 -> Turn 完成
    max_duration_s: float = 30.0  # Turn 最大長度（超過即 overflow）
    min_text_length: int = 2  # 去空白後文字 < 此值 -> 丟棄
    active_merge_gap_ms: int = 250  # 主動模式的 merge_gap
    active_max_wait_ms: int = 250  # 主動模式的 max_wait
    # 敘述中保持聆聽（僅主動模式；bot echo_decision/config.py:40-45）：同一人連續發言串（段間停頓 <= narrative_gap_s）
    # 累積語音達 narrative_min_speech_s 或段數達 narrative_min_segments，就整串合併成一個 turn，
    # 等說話者安靜 narrative_end_s 才交出（喚醒詞命中不等）
    narrative_enabled: bool = True  # config.py:40
    narrative_gap_s: float = 2.0  # config.py:41 段間停頓 <= 此值視為同一串（turn_buffer.py:350 的比較為 <=）
    narrative_min_speech_s: float = 6.0  # config.py:42 發言串累積語音 >= 此值 -> 敘述中
    narrative_min_segments: int = 2  # config.py:43 發言串段數 >= 此值 -> 敘述中
    narrative_end_s: float = 2.5  # config.py:44 敘述中：安靜達此秒數才算講完
    narrative_max_duration_s: float = 120.0  # config.py:45 敘述中單一 turn 長度上限（超過先交出）


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
    meaningfulness_min_length_cjk: int = 2  # 含 CJK 字元的文字（中文一個字的資訊量高於英文字母）


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
    # D80：proposer 的決策預算（毫秒），暫定 300；S6 依 D73 的基準校正。
    # <= 0 代表不設預算（呼叫端會一直等 proposer，只建議測試使用；上一個呼叫還沒回來時仍會立即回預設）
    proposer_budget_ms: int = 300
    # 主 proposer 是 laya.primary（組合式，含翻譯與模型推論）時的 turn.reply 預算；只有該情況取代 proposer_budget_ms，
    # turn.interject／presence.absent_intent 與其他 proposer 仍用 proposer_budget_ms。UEP_LAYA_BUDGET_MS 覆寫
    laya_budget_ms: int = 500
    # 哪些頻道模式允許 barge-in（對齊 bot 的 ``barge_in_modes``，預設只有主動頻道）；
    # 只在 proposer 有發布頻道模式時生效（沒有頻道模式的宿主維持 ES 既有的可打斷行為）
    barge_in_channel_modes: tuple[str, ...] = ("active",)
    # Q-1：使用者插話（outcome.barge_in）之後，非被點名的 passive 類觸發（context_passive、插話）暫停多久。
    # bot 沒有對應行為（只有 engage_cooldown_s），此值為 S5 新增的暫定值
    barge_in_cooldown_s: float = 8.0
    # 宿主驅動 ``Orchestrator.tick`` 的間隔（秒）；bot 的 tick 建議 200-500ms
    tick_interval_s: float = 0.25
    # DL-1b：Host 自動組裝的 primary proposer（``proposer_id``，經 registry 建立）；預設 ``rules.default``。
    # 呼叫端明確傳入 ``proposer=`` 時以呼叫端為準，不看此欄位
    proposer_id: str = "rules.default"
    # DL-1b：Host 自動組裝的 shadow proposer（同上經 registry 建立）；預設以 ``respond.minimal`` 記錄舊行為作對照。
    # 與 primary 同 id 者略過，空 tuple 關閉。呼叫端明確傳入 shadow_proposers 時以呼叫端為準，不看此欄位
    shadow_proposer_ids: tuple[str, ...] = ("respond.minimal",)


def decision_config_from_settings(settings: object | None = None, base: DecisionConfig | None = None) -> DecisionConfig:
    """依 ``UEP_DECISION_PROPOSER``／``UEP_DECISION_SHADOW_PROPOSERS`` 疊上 proposer 選擇（其餘欄位沿用 ``base``）。

    未設定的變數不覆寫預設；shadow 清單以逗號分隔，設為空字串代表關閉 shadow。
    未註冊的 id 直接拋 ``ValueError``（啟動時 fail-fast，不靜默退回預設）；shadow 清單去重並剔除與 primary 相同者。
    """
    from dataclasses import replace

    from uep.config import get_settings
    from uep.turn.registry import PROPOSERS

    cfg = base or DecisionConfig()
    st = settings if settings is not None else get_settings()
    primary = (getattr(st, "decision_proposer", None) or "").strip() or cfg.proposer_id
    raw_shadow = getattr(st, "decision_shadow_proposers", None)
    if raw_shadow is None:
        shadows = cfg.shadow_proposer_ids
    else:
        shadows = tuple(p.strip() for p in raw_shadow.split(",") if p.strip())
    known = PROPOSERS.ids()
    unknown = [pid for pid in (primary, *shadows) if pid not in known]
    if unknown:
        raise ValueError(
            f"UEP_DECISION_PROPOSER／UEP_DECISION_SHADOW_PROPOSERS 含未註冊的 proposer_id：{', '.join(unknown)}"
            f"（可用：{', '.join(known)}）"
        )
    shadows = tuple(pid for pid in dict.fromkeys(shadows) if pid != primary)
    cfg = replace(cfg, proposer_id=primary, shadow_proposer_ids=shadows)
    budget = getattr(st, "laya_budget_ms", None)
    if budget is not None:
        if int(budget) <= 0:
            # <= 0 在 ProposerGuard 代表不設預算（無限等待）；laya.primary 不允許靜默變成無限
            raise ValueError(f"UEP_LAYA_BUDGET_MS 必須為正整數（收到 {budget}）")
        cfg = replace(cfg, laya_budget_ms=int(budget))
    _warn_translate_vs_budget(cfg, st)
    return cfg


def _warn_translate_vs_budget(cfg: DecisionConfig, st: object) -> None:
    """主 proposer 是 laya.primary 且翻譯開啟時，翻譯逾時必須小於 laya 預算，否則翻譯逾時前整個決策已先逾時、
    退不回舊規則（只回 D80 預設）。不拋例外（避免擋啟動），只警告。"""
    if cfg.proposer_id != "laya.primary" or not getattr(st, "decision_translate", False):
        return
    from uep.echo.translate_wiring import DEFAULT_TRANSLATE_TIMEOUT_MS

    translate_ms = int(getattr(st, "translate_timeout_ms", None) or DEFAULT_TRANSLATE_TIMEOUT_MS)
    if translate_ms + INFERENCE_MARGIN_MS >= cfg.laya_budget_ms:
        logger.warning(
            "UEP_TRANSLATE_TIMEOUT_MS=%d 加推論餘裕 %dms 不小於 UEP_LAYA_BUDGET_MS=%d：翻譯用滿時間後模型推論來不及，"
            "整個決策會先逾時；請讓翻譯逾時加餘裕小於 laya 預算（例如 400 + 80 < 500）",
            translate_ms, INFERENCE_MARGIN_MS, cfg.laya_budget_ms,
        )
