"""
StorageJudge — 記憶存儲決策裁判 + 工具過濾器（Hybrid 版）

前身為 JudgeAgent（工具過濾），重構為雙職責：
  1. 工具過濾（前處理）— filter_tools / recommend_tool
  2. 存儲決策（後處理）— storage_decision

存儲決策架構：
  - SalienceEvaluator 每輪執行，提供 heuristic baseline（salience/da/ht）
  - JudgeGate 根據結構化數值信號判斷是否需要 LLM
  - LLM 只處理灰區：概念變更、矛盾、長期偏好、高風險情境
  - 低信號回合直接使用 baseline
  - Episode 永遠存儲，不是 Judge 的決策項

對應 OpenSoul soul/gating/judge.py（工具過濾部分）。
存儲決策為 Echo Memory 原創，無 OpenSoul 對應。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from echo_memory.affect.signals import SubconsciousAssessment
from echo_memory.schemas.memory_context import MemoryContext

logger = logging.getLogger("echo_memory.affect.judge")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 資料結構
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class ConceptSeed:
    """
    Judge 提議的新概念候選。

    由 StorageJudge 產生，不直接寫入 SemanticMemory。
    後續由 ConceptExtractor 接收為優先提取建議。
    """

    name: str
    description: str
    evidence: str  # 為什麼認為這是一個概念
    source_episode_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "evidence": self.evidence,
            "source_episode_id": self.source_episode_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConceptSeed:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class JudgeGateResult:
    """
    JudgeGate 的決策結果。

    決定是否需要呼叫 LLM 做存儲決策。
    gate 使用結構化數值信號，不做語意分析。
    """

    should_invoke_llm: bool
    reason: str
    triggered_signals: list[str] = field(default_factory=list)
    signal_score: float = 0.0  # 觸發信號的加權總分

    def to_dict(self) -> dict[str, Any]:
        return {
            "should_invoke_llm": self.should_invoke_llm,
            "reason": self.reason,
            "triggered_signals": self.triggered_signals,
            "signal_score": round(self.signal_score, 4),
        }


@dataclass
class StorageDecision:
    """
    StorageJudge 的存儲決策結果。

    與 SalienceEvaluator 的 baseline 合併後決定 episode 如何被存儲。

    注意：episode 永遠被存儲。StorageDecision 決定「怎麼被記住」，
    不是「要不要記住」。

    Attributes:
        salience_delta: 對 baseline 的調整（-0.3 ~ +0.3）
        concept_seeds: 建議提取的新概念（由 ConceptExtractor 審核）
        entity_triggers: 是否應觸發實體提取
        reflection_trigger: 是否應強制觸發反思
        force_remember: 使用者明確表達「記住這個」
        metadata_tags: 語意標記（preference/correction/creative/...）
        reasoning: 決策理由
        source: 決策來源（"baseline" | "llm" | "gate_bypass"）
        gate_result: JudgeGate 的結果
    """

    # 核心決策
    salience_delta: float = 0.0
    concept_seeds: list[ConceptSeed] = field(default_factory=list)
    entity_triggers: bool = False
    reflection_trigger: bool = False
    force_remember: bool = False
    metadata_tags: list[str] = field(default_factory=list)

    # 來源追蹤
    reasoning: str = ""
    source: str = "baseline"
    gate_result: JudgeGateResult | None = None

    # 唯一識別
    decision_id: str = field(default="")

    def __post_init__(self) -> None:
        if not self.decision_id:
            self.decision_id = f"jd_{uuid.uuid4().hex[:12]}"
        # clamp salience_delta
        self.salience_delta = max(-0.3, min(0.3, self.salience_delta))

    @property
    def has_semantic_enrichment(self) -> bool:
        """是否有語意層面的增補（concept seeds、tags 等）。"""
        return bool(
            self.concept_seeds or self.metadata_tags or self.force_remember
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "salience_delta": round(self.salience_delta, 4),
            "concept_seeds": [s.to_dict() for s in self.concept_seeds],
            "entity_triggers": self.entity_triggers,
            "reflection_trigger": self.reflection_trigger,
            "force_remember": self.force_remember,
            "metadata_tags": self.metadata_tags,
            "reasoning": self.reasoning,
            "source": self.source,
            "gate_result": (
                self.gate_result.to_dict() if self.gate_result else None
            ),
        }


@dataclass
class JudgeRecommendation:
    """LLM 工具推薦結果（保留原有）。"""

    recommended_tool: str  # 工具名稱或 "none"
    reasoning: str
    confidence: float
    source: str  # "llm" 或 "rules"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LLM Prompts
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 工具推薦 prompt（保留原有）
_RECOMMEND_PROMPT = (
    "你是記憶系統的工具決策裁判。根據使用者的意圖，從可用工具中推薦最適合的。\n\n"
    "使用者輸入：{user_input}\n\n"
    "可用工具：\n{tool_descriptions}\n\n"
    "記憶脈絡摘要：{context_summary}\n\n"
    '請以純 JSON 格式回覆（不要包含 markdown 標記）：\n'
    '{{"recommended_tool": "工具名稱或 none", "reasoning": "一句話理由", '
    '"confidence": 0.0到1.0}}\n\n'
    "規則：\n"
    "- 若沒有工具適合，recommended_tool 設為 \"none\"\n"
    "- confidence 低於 0.5 時應設為 \"none\"\n"
    "- 優先考慮使用者的實際意圖，而非文字表面"
)

# 存儲決策 prompt（新增）
_STORAGE_DECISION_PROMPT = (
    "你是記憶系統的存儲決策裁判。根據這輪對話，決定記憶如何被存儲和標記。\n"
    "注意：每輪對話都會被存儲為 episode，你不決定「要不要存」，而是決定"
    "「怎麼被記住」。\n\n"
    "用戶輸入：{user_input}\n"
    "LLM 回覆：{agent_response}\n"
    "已知概念：{existing_concepts}\n"
    "概念驗證結果：{concept_verification}\n"
    "基線 salience：{baseline_salience:.3f}\n\n"
    "以純 JSON 格式回覆（不要包含 markdown 標記）：\n"
    '{{"salience_delta": 0.0, "concept_seeds": [], "force_remember": false, '
    '"entity_triggers": false, "reflection_trigger": false, '
    '"metadata_tags": [], "reasoning": "一句話說明"}}\n\n'
    'concept_seeds 格式（若有）：'
    '[{{"name": "概念名稱", "description": "概念描述", "evidence": "為何提議"}}]\n\n'
    "規則：\n"
    "- salience_delta 是對基線的微調（-0.3 ~ +0.3），不是絕對值\n"
    "- concept_seeds 只提議不直接寫入（由 Inhibitor 審核）\n"
    "- force_remember 用於使用者明確表達「記住這個」的情況\n"
    "- entity_triggers 在出現人名、地點、組織等具名實體時啟用\n"
    "- reflection_trigger 在發現矛盾、重大修正、或需要反思的情境時啟用\n"
    "- metadata_tags 可選：preference, correction, creative, relationship, "
    "factual, emotional, technical\n"
    "- 不要過度標記——閒聊不需要 concept_seeds 也不需要 entity_triggers"
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 結構化輸出 Schema（供 structured_llm_fn 使用）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 工具推薦的結構化回應 schema
TOOL_RECOMMEND_SCHEMA = {
    "type": "object",
    "properties": {
        "recommended_tool": {"type": "string"},
        "reasoning": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    "required": ["recommended_tool", "reasoning", "confidence"],
}

# 存儲決策的結構化回應 schema
STORAGE_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "salience_delta": {"type": "number", "minimum": -0.3, "maximum": 0.3},
        "concept_seeds": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["name"],
            },
        },
        "force_remember": {"type": "boolean"},
        "entity_triggers": {"type": "boolean"},
        "reflection_trigger": {"type": "boolean"},
        "metadata_tags": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
    },
    "required": ["salience_delta", "reasoning"],
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Gate 閾值配置
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 加權信號總分超過此值才呼叫 LLM
_GATE_THRESHOLD = 0.55

# 各信號的觸發閾值與 gate 權重
# 這些是結構化數值信號，不是 keyword matching
_GATE_SIGNALS: dict[str, dict[str, float]] = {
    "novelty": {"threshold": 0.65, "weight": 0.20},
    "emotional_intensity": {"threshold": 0.55, "weight": 0.20},
    "uncertainty": {"threshold": 0.55, "weight": 0.15},
    "complexity": {"threshold": 0.70, "weight": 0.10},
    "concept_changes": {"threshold": 0.5, "weight": 0.30},
    "high_baseline_salience": {"threshold": 0.70, "weight": 0.15},
    "dream_resonance": {"threshold": 0.50, "weight": 0.10},
}

# 合法的 metadata_tags（LLM 輸出驗證用）
_ALLOWED_TAGS = frozenset({
    "preference", "correction", "creative", "relationship",
    "factual", "emotional", "technical",
})

# LLM 推薦最低信心門檻
_MIN_RECOMMEND_CONFIDENCE = 0.5


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# StorageJudge
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class StorageJudge:
    """
    記憶存儲決策裁判 + 工具過濾器（Hybrid 版）。

    職責：
    1. 工具過濾（前處理）— filter_tools / recommend_tool（保留原有功能）
    2. 存儲決策（後處理）— storage_decision（新增）

    存儲決策架構：
    - SalienceEvaluator 每輪執行，提供 heuristic baseline（salience/da/ht）
    - JudgeGate 根據結構化數值信號判斷是否需要 LLM
    - LLM 只處理灰區：概念變更、矛盾、長期偏好、高風險
    - 低信號回合直接使用 baseline（StorageDecision 保持預設值）
    - Episode 永遠存儲——StorageJudge 決定「怎麼記住」，不是「要不要記住」
    """

    def __init__(
        self,
        llm_fn: Callable[[str], str] | None = None,
        structured_llm_fn: Callable[[str], dict] | None = None,
    ):
        """
        Args:
            llm_fn: LLM 呼叫函式。為 None 時所有功能降級為規則/啟發式。
            structured_llm_fn: 結構化 LLM 呼叫函式，直接回傳 dict（省去 JSON parse）。
                               存在時優先於 llm_fn 使用；不存在時 fallback 至 llm_fn + JSON parse。
                               對工具推薦和存儲決策兩個呼叫點均適用。
        """
        self._llm_fn = llm_fn
        self._structured_llm_fn = structured_llm_fn

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 工具過濾（前處理，保留原有功能）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def filter_tools(
        self,
        context: MemoryContext | None,
        all_tools: list[dict[str, Any]],
        total_episodes: int = 0,
    ) -> list[dict[str, Any]]:
        """
        根據上下文過濾工具集（確定性規則層）。

        過濾規則（不可繞過）：
        1. 無記憶脈絡 → 移除 adjust_importance
        2. 記憶太少 → 移除 trigger_dream
        3. 記憶太少 → 移除 deep_recall

        Args:
            context: 記憶脈絡（可為 None）
            all_tools: 完整工具定義清單
            total_episodes: 總 episode 數

        Returns:
            過濾後的工具清單
        """
        if not all_tools:
            return []

        filtered = []
        removed = []

        for tool in all_tools:
            name = tool.get("name", "")

            # 規則 1：無脈絡時不能調整重要性
            if name == "adjust_importance":
                if not context or not context.episodes:
                    removed.append(name)
                    continue

            # 規則 2：記憶太少時 Dream 沒意義
            if name == "trigger_dream":
                if total_episodes < 5:
                    removed.append(name)
                    continue

            # 規則 3：記憶太少時深度搜尋無意義
            if name == "deep_recall":
                if total_episodes < 3:
                    removed.append(name)
                    continue

            filtered.append(tool)

        if removed:
            logger.debug("[StorageJudge] 規則過濾工具: %s", removed)

        return filtered

    def recommend_tool(
        self,
        user_input: str,
        context: MemoryContext | None,
        available_tools: list[dict[str, Any]],
    ) -> JudgeRecommendation:
        """
        對過濾後的工具集做語意推薦。

        流程：
        1. llm_fn 可用時，送 LLM 推薦
        2. LLM 失敗或信心不足時，回傳 "none"

        Args:
            user_input: 使用者輸入
            context: 記憶脈絡
            available_tools: 已過濾的工具清單

        Returns:
            推薦結果
        """
        # 沒有工具或沒有任何 LLM → 規則降級
        if not available_tools or (
            self._llm_fn is None and self._structured_llm_fn is None
        ):
            return JudgeRecommendation(
                recommended_tool="none",
                reasoning="無可用工具或 LLM 未配置",
                confidence=0.0,
                source="rules",
            )

        # 建立工具描述
        tool_descs = []
        for t in available_tools:
            name = t.get("name", "unknown")
            desc = t.get("description", "無描述")
            tool_descs.append(f"- {name}: {desc}")
        tool_text = "\n".join(tool_descs)

        # 建立脈絡摘要
        ctx_summary = "無記憶脈絡"
        if context and context.episodes:
            ctx_summary = f"已召回 {len(context.episodes)} 個相關記憶"
            if context.concepts:
                concept_names = [c.name for c in context.concepts[:5]]
                ctx_summary += f"，相關概念：{', '.join(concept_names)}"

        prompt = _RECOMMEND_PROMPT.format(
            user_input=user_input[:300],
            tool_descriptions=tool_text,
            context_summary=ctx_summary,
        )

        # 合法候選集（驗證 LLM 輸出用）
        valid_names = {t.get("name", "") for t in available_tools}

        try:
            # ── 結構化路徑：直接拿到 dict，省去 JSON parse ──
            if self._structured_llm_fn is not None:
                data = self._structured_llm_fn(prompt)
                return self._parse_recommendation_dict(data, valid_names)

            # ── Fallback 路徑：llm_fn + 手動 JSON parse ──
            assert self._llm_fn is not None
            raw = self._llm_fn(prompt)
            return self._parse_recommendation(raw, valid_names)
        except Exception as e:
            logger.warning(
                "[StorageJudge] LLM 推薦失敗，降級為規則結果: %s", e
            )
            return JudgeRecommendation(
                recommended_tool="none",
                reasoning=f"LLM 呼叫失敗: {e}",
                confidence=0.0,
                source="rules",
            )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 存儲決策（後處理，新增）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def storage_decision(
        self,
        user_input: str,
        agent_response: str,
        baseline_salience: float,
        assessment: SubconsciousAssessment,
        context: MemoryContext | None = None,
        concept_verification: Any | None = None,
    ) -> StorageDecision:
        """
        決定本輪 episode 如何被存儲（怎麼記住，不是要不要記住）。

        流程：
        1. JudgeGate — 用結構化數值信號判斷是否需要 LLM
        2. Gate 不開 → 直接回傳 baseline StorageDecision（預設值）
        3. Gate 開但無 LLM → 啟發式決策（根據信號做有限 metadata 添加）
        4. Gate 開且有 LLM → LLM 存儲決策（語意分析灰區）

        Args:
            user_input: 使用者輸入
            agent_response: LLM 回覆
            baseline_salience: SalienceEvaluator 計算的 baseline salience
            assessment: SubconsciousAssessment 五維度評估
            context: 記憶脈絡（可為 None）
            concept_verification: ConceptVerification 結果（可為 None）

        Returns:
            StorageDecision — 與 baseline 合併後控制 episode 存儲行為
        """
        # Step 1: JudgeGate — 結構化信號判斷
        gate = self._judge_gate(
            assessment=assessment,
            baseline_salience=baseline_salience,
            concept_verification=concept_verification,
        )

        # Step 2: Gate 不開 → baseline 直接返回
        if not gate.should_invoke_llm:
            return StorageDecision(
                reasoning=gate.reason,
                source="baseline",
                gate_result=gate,
            )

        # Step 3: Gate 開但無任何 LLM → 啟發式決策
        if self._llm_fn is None and self._structured_llm_fn is None:
            return self._heuristic_decision(
                assessment=assessment,
                concept_verification=concept_verification,
                gate=gate,
            )

        # Step 4: LLM 存儲決策
        try:
            return self._llm_storage_decision(
                user_input=user_input,
                agent_response=agent_response,
                baseline_salience=baseline_salience,
                context=context,
                concept_verification=concept_verification,
                gate=gate,
            )
        except Exception as e:
            logger.warning(
                "[StorageJudge] LLM 存儲決策失敗，降級為啟發式: %s", e
            )
            return self._heuristic_decision(
                assessment=assessment,
                concept_verification=concept_verification,
                gate=gate,
            )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # JudgeGate — 純結構化數值信號判斷
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _judge_gate(
        self,
        assessment: SubconsciousAssessment,
        baseline_salience: float,
        concept_verification: Any | None,
    ) -> JudgeGateResult:
        """
        JudgeGate — 純結構化數值信號判斷。

        使用 SubconsciousAssessment 五維度 + baseline_salience
        + concept_verification.has_changes 做加權信號累計。
        每個超閾值的信號貢獻其權重到總分。
        總分超過 _GATE_THRESHOLD 則呼叫 LLM。

        不做語意分析，不使用 keyword matching。
        """
        triggered = []
        score = 0.0

        # novelty
        if assessment.novelty >= _GATE_SIGNALS["novelty"]["threshold"]:
            triggered.append(f"novelty={assessment.novelty:.2f}")
            score += _GATE_SIGNALS["novelty"]["weight"]

        # emotional_intensity — 使用 |emotional_tone| 作為情感強度代理
        emotional_intensity = abs(assessment.emotional_tone)
        if emotional_intensity >= _GATE_SIGNALS["emotional_intensity"]["threshold"]:
            triggered.append(f"emotional={emotional_intensity:.2f}")
            score += _GATE_SIGNALS["emotional_intensity"]["weight"]

        # uncertainty
        if assessment.uncertainty >= _GATE_SIGNALS["uncertainty"]["threshold"]:
            triggered.append(f"uncertainty={assessment.uncertainty:.2f}")
            score += _GATE_SIGNALS["uncertainty"]["weight"]

        # complexity
        if assessment.complexity >= _GATE_SIGNALS["complexity"]["threshold"]:
            triggered.append(f"complexity={assessment.complexity:.2f}")
            score += _GATE_SIGNALS["complexity"]["weight"]

        # concept_changes — 布林信號，觸發時直接加權重（最高權重）
        has_changes = (
            hasattr(concept_verification, "has_changes")
            and concept_verification.has_changes
        ) if concept_verification is not None else False
        if has_changes:
            triggered.append("concept_changes")
            score += _GATE_SIGNALS["concept_changes"]["weight"]

        # high_baseline_salience
        if baseline_salience >= _GATE_SIGNALS["high_baseline_salience"]["threshold"]:
            triggered.append(f"high_salience={baseline_salience:.2f}")
            score += _GATE_SIGNALS["high_baseline_salience"]["weight"]

        # dream_resonance — 與夢境記憶的共鳴（bonus）
        if assessment.dream_resonance >= _GATE_SIGNALS["dream_resonance"]["threshold"]:
            triggered.append(f"dream={assessment.dream_resonance:.2f}")
            score += _GATE_SIGNALS["dream_resonance"]["weight"]

        should_invoke = score >= _GATE_THRESHOLD

        if should_invoke:
            reason = (
                f"Gate 開啟（score={score:.2f} >= {_GATE_THRESHOLD}）: "
                f"{', '.join(triggered)}"
            )
        else:
            reason = (
                f"Gate 關閉（score={score:.2f} < {_GATE_THRESHOLD}）— "
                "使用 baseline"
            )

        logger.debug("[StorageJudge] %s", reason)

        return JudgeGateResult(
            should_invoke_llm=should_invoke,
            reason=reason,
            triggered_signals=triggered,
            signal_score=score,
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 啟發式存儲決策（無 LLM fallback）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _heuristic_decision(
        self,
        assessment: SubconsciousAssessment,
        concept_verification: Any | None,
        gate: JudgeGateResult,
    ) -> StorageDecision:
        """
        無 LLM 時的啟發式存儲決策。

        根據 gate 觸發的信號做有限的 metadata 添加和 salience 微調。
        不做語意分析——僅基於數值信號的簡單映射。
        """
        tags: list[str] = []
        salience_delta = 0.0

        # 情感強度高 → 加標記 + 微調
        if abs(assessment.emotional_tone) >= 0.6:
            tags.append("emotional")
            salience_delta += 0.05

        # 概念有變更 → 需要更多關注
        has_changes = (
            hasattr(concept_verification, "has_changes")
            and concept_verification.has_changes
        ) if concept_verification is not None else False
        if has_changes:
            salience_delta += 0.10
            # 嘗試從 concept_verification 推斷標記
            if hasattr(concept_verification, "changes"):
                for change in concept_verification.changes:
                    if hasattr(change, "status"):
                        if change.status in ("modify", "deprecate"):
                            if "correction" not in tags:
                                tags.append("correction")

        # 高新奇度 → 可能有新實體
        entity_triggers = assessment.novelty >= 0.7

        # Dream resonance → 增強 salience
        if assessment.dream_resonance >= 0.5:
            salience_delta += 0.05

        # 高複雜度 → technical 標記
        if assessment.complexity >= 0.7:
            tags.append("technical")

        return StorageDecision(
            salience_delta=salience_delta,
            entity_triggers=entity_triggers,
            metadata_tags=tags,
            reasoning="啟發式決策（LLM 未配置或呼叫失敗）",
            source="gate_bypass",
            gate_result=gate,
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # LLM 存儲決策
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _llm_storage_decision(
        self,
        user_input: str,
        agent_response: str,
        baseline_salience: float,
        context: MemoryContext | None,
        concept_verification: Any | None,
        gate: JudgeGateResult,
    ) -> StorageDecision:
        """使用 LLM 做存儲決策（只在 gate 開啟時呼叫）。"""
        # 建構上下文
        concepts_text = "無已知概念"
        if context and context.concepts:
            concept_lines = [
                f"- {c.name}: {c.description[:80]}"
                for c in context.concepts[:10]
            ]
            concepts_text = "\n".join(concept_lines)

        cv_text = "無變更"
        if concept_verification is not None and hasattr(
            concept_verification, "has_changes"
        ):
            if concept_verification.has_changes and hasattr(
                concept_verification, "summary"
            ):
                cv_text = concept_verification.summary()

        prompt = _STORAGE_DECISION_PROMPT.format(
            user_input=user_input[:500],
            agent_response=agent_response[:500],
            existing_concepts=concepts_text,
            concept_verification=cv_text,
            baseline_salience=baseline_salience,
        )

        # ── 結構化路徑：直接拿到 dict，省去 JSON parse ──
        if self._structured_llm_fn is not None:
            data = self._structured_llm_fn(prompt)
            return self._parse_storage_decision_dict(data, gate)

        # ── Fallback 路徑：llm_fn + 手動 JSON parse ──
        raw = self._llm_fn(prompt)  # type: ignore[misc]
        return self._parse_storage_decision(raw, gate)

    def _parse_storage_decision(
        self,
        raw: str,
        gate: JudgeGateResult,
    ) -> StorageDecision:
        """解析 LLM 存儲決策結果。每個欄位獨立解析，任一欄位失敗使用預設值。"""
        parsed = self._extract_json(raw)

        if not parsed:
            logger.warning(
                "[StorageJudge] LLM 存儲決策無法解析: %s", raw[:200]
            )
            return StorageDecision(
                reasoning="LLM 回應格式無效，使用預設值",
                source="baseline",
                gate_result=gate,
            )

        # 逐欄位安全解析
        salience_delta = 0.0
        try:
            salience_delta = float(parsed.get("salience_delta", 0.0))
        except (TypeError, ValueError):
            pass

        seeds = self._parse_concept_seeds(parsed.get("concept_seeds", []))

        raw_tags = parsed.get("metadata_tags", [])
        tags = (
            [str(t) for t in raw_tags if str(t) in _ALLOWED_TAGS]
            if isinstance(raw_tags, list)
            else []
        )

        return StorageDecision(
            salience_delta=salience_delta,
            concept_seeds=seeds,
            entity_triggers=bool(parsed.get("entity_triggers", False)),
            reflection_trigger=bool(parsed.get("reflection_trigger", False)),
            force_remember=bool(parsed.get("force_remember", False)),
            metadata_tags=tags,
            reasoning=str(parsed.get("reasoning", "")),
            source="llm",
            gate_result=gate,
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 內部工具方法
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _parse_recommendation_dict(
        self,
        data: dict[str, Any],
        valid_names: set[str] | None = None,
    ) -> JudgeRecommendation:
        """
        從結構化 dict 組裝工具推薦結果（structured_llm_fn 路徑使用）。

        邏輯與 _parse_recommendation 一致，省去 JSON parse 步驟。
        """
        tool = str(data.get("recommended_tool", "none"))
        reasoning = str(data.get("reasoning", ""))
        confidence = float(data.get("confidence", 0.0))

        # 信心不足時回退
        if confidence < _MIN_RECOMMEND_CONFIDENCE:
            tool = "none"

        # 驗證推薦工具是否在合法候選集內
        if tool != "none" and valid_names and tool not in valid_names:
            logger.warning(
                "[StorageJudge] structured LLM 推薦了不存在的工具 '%s'，回退為 none",
                tool,
            )
            tool = "none"
            reasoning = "LLM 推薦的工具不在合法候選集中"

        return JudgeRecommendation(
            recommended_tool=tool,
            reasoning=reasoning,
            confidence=confidence,
            source="llm",
        )

    def _parse_storage_decision_dict(
        self,
        data: dict[str, Any],
        gate: JudgeGateResult,
    ) -> StorageDecision:
        """
        從結構化 dict 組裝存儲決策結果（structured_llm_fn 路徑使用）。

        邏輯與 _parse_storage_decision 一致，省去 JSON parse 步驟。
        每個欄位獨立解析，任一欄位異常使用預設值。
        """
        salience_delta = 0.0
        try:
            salience_delta = float(data.get("salience_delta", 0.0))
        except (TypeError, ValueError):
            pass

        seeds = self._parse_concept_seeds(data.get("concept_seeds", []))

        raw_tags = data.get("metadata_tags", [])
        tags = (
            [str(t) for t in raw_tags if str(t) in _ALLOWED_TAGS]
            if isinstance(raw_tags, list)
            else []
        )

        return StorageDecision(
            salience_delta=salience_delta,
            concept_seeds=seeds,
            entity_triggers=bool(data.get("entity_triggers", False)),
            reflection_trigger=bool(data.get("reflection_trigger", False)),
            force_remember=bool(data.get("force_remember", False)),
            metadata_tags=tags,
            reasoning=str(data.get("reasoning", "")),
            source="llm",
            gate_result=gate,
        )

    def _parse_recommendation(
        self,
        raw: str,
        valid_names: set[str] | None = None,
    ) -> JudgeRecommendation:
        """解析 LLM 工具推薦結果。"""
        parsed = self._extract_json(raw)

        if not parsed:
            logger.warning(
                "[StorageJudge] LLM 推薦回應無法解析: %s", raw[:200]
            )
            return JudgeRecommendation(
                recommended_tool="none",
                reasoning="LLM 回應格式無效",
                confidence=0.0,
                source="rules",
            )

        tool = str(parsed.get("recommended_tool", "none"))
        reasoning = str(parsed.get("reasoning", ""))
        confidence = float(parsed.get("confidence", 0.0))

        # 信心不足時回退
        if confidence < _MIN_RECOMMEND_CONFIDENCE:
            tool = "none"

        # 驗證推薦工具是否在合法候選集內
        if tool != "none" and valid_names and tool not in valid_names:
            logger.warning(
                "[StorageJudge] LLM 推薦了不存在的工具 '%s'，回退為 none",
                tool,
            )
            tool = "none"
            reasoning = "LLM 推薦的工具不在合法候選集中"

        return JudgeRecommendation(
            recommended_tool=tool,
            reasoning=reasoning,
            confidence=confidence,
            source="llm",
        )

    @staticmethod
    def _extract_json(raw: str) -> dict[str, Any] | None:
        """從 LLM 回應中提取 JSON。支援直接 JSON 和 markdown code block。"""
        if not raw or not raw.strip():
            return None

        # 嘗試直接解析
        try:
            parsed = json.loads(raw.strip())
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        # 嘗試從 markdown code block 或內嵌 JSON 提取
        match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", raw, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        return None

    @staticmethod
    def _parse_concept_seeds(
        raw_seeds: Any,
    ) -> list[ConceptSeed]:
        """安全解析 concept_seeds 陣列。"""
        if not isinstance(raw_seeds, list):
            return []

        seeds = []
        for seed_data in raw_seeds:
            if not isinstance(seed_data, dict) or "name" not in seed_data:
                continue
            seeds.append(
                ConceptSeed(
                    name=str(seed_data["name"]),
                    description=str(seed_data.get("description", "")),
                    evidence=str(seed_data.get("evidence", "")),
                )
            )

        return seeds


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Backward-compatible alias
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 保留舊名稱作為別名，避免其他模組 import 出錯
JudgeAgent = StorageJudge
