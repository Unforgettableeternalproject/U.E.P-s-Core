"""
StorageJudge 測試 — 存儲決策裁判 + 工具過濾

測試覆蓋：
  1. JudgeGate 信號閾值判斷（各信號觸發/不觸發）
  2. StorageDecision 資料結構正確性
  3. storage_decision 完整流程（baseline / heuristic / LLM）
  4. LLM 解析容錯（壞 JSON、部分欄位缺失、超範圍 delta）
  5. 工具過濾（保留原有 JudgeAgent 行為）
  6. 向後相容（JudgeAgent alias）
"""

import json

import pytest

from echo_memory.affect.judge import (
    ConceptSeed,
    JudgeAgent,
    JudgeGateResult,
    JudgeRecommendation,
    StorageDecision,
    StorageJudge,
    _GATE_THRESHOLD,
)
from echo_memory.affect.signals import SubconsciousAssessment
from echo_memory.schemas.memory_context import MemoryContext


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 工具方法
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _low_signal_assessment() -> SubconsciousAssessment:
    """所有維度都低的評估——閒聊場景。"""
    return SubconsciousAssessment(
        emotional_tone=0.1,
        novelty=0.2,
        complexity=0.3,
        uncertainty=0.1,
        dream_resonance=0.0,
    )


def _high_signal_assessment() -> SubconsciousAssessment:
    """多個維度超閾值的評估——重要對話場景。"""
    return SubconsciousAssessment(
        emotional_tone=0.8,
        novelty=0.9,
        complexity=0.8,
        uncertainty=0.7,
        dream_resonance=0.6,
    )


class _MockConceptVerification:
    """模擬 ConceptVerification，僅提供 has_changes 和 changes。"""

    def __init__(self, has_changes: bool = False, changes: list | None = None):
        self._has_changes = has_changes
        self._changes = changes or []

    @property
    def has_changes(self) -> bool:
        return self._has_changes

    @property
    def changes(self) -> list:
        return self._changes

    def summary(self) -> str:
        return f"Mock: {len(self._changes)} changes"


class _MockChange:
    """模擬 ConceptAssessment。"""

    def __init__(self, status: str = "valid"):
        self.status = status


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 資料結構測試
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestStorageDecision:
    def test_default_values(self):
        """預設值正確。"""
        d = StorageDecision()
        assert d.salience_delta == 0.0
        assert d.concept_seeds == []
        assert d.entity_triggers is False
        assert d.reflection_trigger is False
        assert d.force_remember is False
        assert d.metadata_tags == []
        assert d.source == "baseline"
        assert d.decision_id.startswith("jd_")

    def test_salience_delta_clamp(self):
        """salience_delta 超出 ±0.3 時被 clamp。"""
        d1 = StorageDecision(salience_delta=0.5)
        assert d1.salience_delta == 0.3

        d2 = StorageDecision(salience_delta=-0.8)
        assert d2.salience_delta == -0.3

    def test_has_semantic_enrichment(self):
        """語意增補屬性正確。"""
        empty = StorageDecision()
        assert empty.has_semantic_enrichment is False

        with_seeds = StorageDecision(
            concept_seeds=[ConceptSeed(name="x", description="y", evidence="z")]
        )
        assert with_seeds.has_semantic_enrichment is True

        with_tags = StorageDecision(metadata_tags=["preference"])
        assert with_tags.has_semantic_enrichment is True

        with_force = StorageDecision(force_remember=True)
        assert with_force.has_semantic_enrichment is True

    def test_to_dict_roundtrip(self):
        """to_dict 序列化正確。"""
        d = StorageDecision(
            salience_delta=0.15,
            concept_seeds=[ConceptSeed(name="test", description="d", evidence="e")],
            metadata_tags=["preference", "emotional"],
            reasoning="test reasoning",
            source="llm",
        )
        data = d.to_dict()
        assert data["salience_delta"] == 0.15
        assert len(data["concept_seeds"]) == 1
        assert data["concept_seeds"][0]["name"] == "test"
        assert data["metadata_tags"] == ["preference", "emotional"]
        assert data["source"] == "llm"


class TestConceptSeed:
    def test_basic(self):
        seed = ConceptSeed(name="Python", description="程式語言", evidence="使用者提到")
        assert seed.name == "Python"
        d = seed.to_dict()
        assert d["name"] == "Python"

    def test_from_dict(self):
        seed = ConceptSeed.from_dict({
            "name": "A", "description": "B", "evidence": "C",
        })
        assert seed.name == "A"


class TestJudgeGateResult:
    def test_basic(self):
        g = JudgeGateResult(
            should_invoke_llm=True,
            reason="test",
            triggered_signals=["novelty=0.80"],
            signal_score=0.65,
        )
        assert g.should_invoke_llm is True
        d = g.to_dict()
        assert d["signal_score"] == 0.65


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# JudgeGate 測試
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestJudgeGate:
    """測試 StorageJudge._judge_gate() 的信號判斷邏輯。"""

    def test_all_low_signals_gate_closed(self):
        """所有信號低於閾值 → gate 關閉。"""
        judge = StorageJudge()
        gate = judge._judge_gate(
            assessment=_low_signal_assessment(),
            baseline_salience=0.3,
            concept_verification=None,
        )
        assert gate.should_invoke_llm is False
        assert gate.triggered_signals == []
        assert gate.signal_score == 0.0

    def test_high_novelty_triggers_gate(self):
        """高新奇度觸發 gate。"""
        judge = StorageJudge()
        assessment = SubconsciousAssessment(novelty=0.8)
        gate = judge._judge_gate(
            assessment=assessment,
            baseline_salience=0.3,
            concept_verification=None,
        )
        assert any("novelty" in s for s in gate.triggered_signals)
        assert gate.signal_score > 0

    def test_concept_changes_triggers_gate(self):
        """概念變更觸發 gate（權重最高）。"""
        judge = StorageJudge()
        cv = _MockConceptVerification(has_changes=True)
        gate = judge._judge_gate(
            assessment=_low_signal_assessment(),
            baseline_salience=0.3,
            concept_verification=cv,
        )
        assert "concept_changes" in gate.triggered_signals
        assert gate.signal_score >= 0.30  # concept_changes 權重 0.30

    def test_high_emotional_intensity_triggers(self):
        """高情感強度觸發 gate。"""
        judge = StorageJudge()
        # 負面情感也計入強度
        assessment = SubconsciousAssessment(emotional_tone=-0.7)
        gate = judge._judge_gate(
            assessment=assessment,
            baseline_salience=0.3,
            concept_verification=None,
        )
        assert any("emotional" in s for s in gate.triggered_signals)

    def test_high_baseline_salience_triggers(self):
        """高 baseline salience 觸發 gate。"""
        judge = StorageJudge()
        gate = judge._judge_gate(
            assessment=_low_signal_assessment(),
            baseline_salience=0.85,
            concept_verification=None,
        )
        assert any("high_salience" in s for s in gate.triggered_signals)

    def test_multiple_signals_accumulate(self):
        """多個信號的權重累加。"""
        judge = StorageJudge()
        gate = judge._judge_gate(
            assessment=_high_signal_assessment(),
            baseline_salience=0.85,
            concept_verification=_MockConceptVerification(has_changes=True),
        )
        assert gate.should_invoke_llm is True
        assert len(gate.triggered_signals) >= 4
        assert gate.signal_score >= _GATE_THRESHOLD

    def test_dream_resonance_triggers(self):
        """夢境共鳴觸發 gate。"""
        judge = StorageJudge()
        assessment = SubconsciousAssessment(dream_resonance=0.7)
        gate = judge._judge_gate(
            assessment=assessment,
            baseline_salience=0.3,
            concept_verification=None,
        )
        assert any("dream" in s for s in gate.triggered_signals)

    def test_uncertainty_triggers(self):
        """高不確定性觸發 gate。"""
        judge = StorageJudge()
        assessment = SubconsciousAssessment(uncertainty=0.7)
        gate = judge._judge_gate(
            assessment=assessment,
            baseline_salience=0.3,
            concept_verification=None,
        )
        assert any("uncertainty" in s for s in gate.triggered_signals)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# storage_decision 完整流程測試
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestStorageDecisionFlow:
    def test_low_signals_returns_baseline(self):
        """低信號回合 → gate 關閉 → 返回 baseline StorageDecision。"""
        judge = StorageJudge(llm_fn=lambda p: "不應被呼叫")
        decision = judge.storage_decision(
            user_input="你好",
            agent_response="嗨",
            baseline_salience=0.3,
            assessment=_low_signal_assessment(),
        )
        assert decision.source == "baseline"
        assert decision.salience_delta == 0.0
        assert decision.concept_seeds == []

    def test_high_signals_no_llm_heuristic(self):
        """高信號但無 LLM → 啟發式決策。"""
        judge = StorageJudge(llm_fn=None)
        decision = judge.storage_decision(
            user_input="我覺得這個架構設計很重要，記住它",
            agent_response="好的，已記錄",
            baseline_salience=0.5,
            assessment=_high_signal_assessment(),
            concept_verification=_MockConceptVerification(
                has_changes=True,
                changes=[_MockChange("modify")],
            ),
        )
        assert decision.source == "gate_bypass"
        assert decision.salience_delta > 0  # 概念變更 + 情感 → 正 delta
        assert "correction" in decision.metadata_tags  # modify → correction
        assert "emotional" in decision.metadata_tags  # 高情感
        assert decision.entity_triggers is True  # 高 novelty

    def test_high_signals_with_llm(self):
        """高信號 + LLM → LLM 決策。"""
        llm_response = json.dumps({
            "salience_delta": 0.2,
            "concept_seeds": [
                {"name": "架構設計", "description": "系統架構", "evidence": "使用者提及"},
            ],
            "force_remember": True,
            "entity_triggers": False,
            "reflection_trigger": True,
            "metadata_tags": ["technical", "preference"],
            "reasoning": "使用者明確表達重要性",
        })

        judge = StorageJudge(llm_fn=lambda p: llm_response)
        decision = judge.storage_decision(
            user_input="記住這個架構設計",
            agent_response="好的",
            baseline_salience=0.5,
            assessment=_high_signal_assessment(),
        )
        assert decision.source == "llm"
        assert decision.salience_delta == 0.2
        assert len(decision.concept_seeds) == 1
        assert decision.concept_seeds[0].name == "架構設計"
        assert decision.force_remember is True
        assert decision.reflection_trigger is True
        assert "technical" in decision.metadata_tags
        assert "preference" in decision.metadata_tags

    def test_llm_failure_degrades_to_heuristic(self):
        """LLM 拋異常 → 降級為啟發式。"""
        judge = StorageJudge(llm_fn=lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
        decision = judge.storage_decision(
            user_input="重要的東西",
            agent_response="好",
            baseline_salience=0.5,
            assessment=_high_signal_assessment(),
        )
        assert decision.source == "gate_bypass"

    def test_llm_bad_json_returns_baseline(self):
        """LLM 回傳無法解析的 JSON → 使用預設值。"""
        judge = StorageJudge(llm_fn=lambda p: "這不是 JSON")
        decision = judge.storage_decision(
            user_input="重要",
            agent_response="好",
            baseline_salience=0.5,
            assessment=_high_signal_assessment(),
        )
        assert decision.source == "baseline"  # 解析失敗回退
        assert decision.salience_delta == 0.0

    def test_llm_partial_json(self):
        """LLM 回傳部分欄位的 JSON → 缺失欄位使用預設值。"""
        partial = json.dumps({
            "salience_delta": 0.1,
            "reasoning": "部分資料",
        })
        judge = StorageJudge(llm_fn=lambda p: partial)
        decision = judge.storage_decision(
            user_input="重要",
            agent_response="好",
            baseline_salience=0.5,
            assessment=_high_signal_assessment(),
        )
        assert decision.source == "llm"
        assert decision.salience_delta == 0.1
        assert decision.concept_seeds == []
        assert decision.metadata_tags == []
        assert decision.force_remember is False

    def test_llm_excessive_delta_clamped(self):
        """LLM 回傳超範圍的 salience_delta → 被 clamp。"""
        excessive = json.dumps({
            "salience_delta": 0.9,
            "reasoning": "太高了",
        })
        judge = StorageJudge(llm_fn=lambda p: excessive)
        decision = judge.storage_decision(
            user_input="重要",
            agent_response="好",
            baseline_salience=0.5,
            assessment=_high_signal_assessment(),
        )
        assert decision.salience_delta == 0.3  # clamp 到上限

    def test_llm_invalid_tags_filtered(self):
        """LLM 回傳不合法的 tags → 過濾掉。"""
        with_bad_tags = json.dumps({
            "salience_delta": 0.0,
            "metadata_tags": ["preference", "invalid_tag", "emotional", "haha"],
            "reasoning": "test",
        })
        judge = StorageJudge(llm_fn=lambda p: with_bad_tags)
        decision = judge.storage_decision(
            user_input="重要",
            agent_response="好",
            baseline_salience=0.5,
            assessment=_high_signal_assessment(),
        )
        assert "preference" in decision.metadata_tags
        assert "emotional" in decision.metadata_tags
        assert "invalid_tag" not in decision.metadata_tags
        assert "haha" not in decision.metadata_tags

    def test_llm_markdown_wrapped_json(self):
        """LLM 回傳 markdown code block 包裹的 JSON → 正確提取。"""
        wrapped = '```json\n{"salience_delta": 0.15, "reasoning": "test"}\n```'
        judge = StorageJudge(llm_fn=lambda p: wrapped)
        decision = judge.storage_decision(
            user_input="重要",
            agent_response="好",
            baseline_salience=0.5,
            assessment=_high_signal_assessment(),
        )
        assert decision.source == "llm"
        assert decision.salience_delta == 0.15

    def test_episode_always_stored(self):
        """確認 StorageDecision 永遠不會阻止 episode 存儲。"""
        # StorageDecision 沒有 "should_store" 或類似的 boolean
        d = StorageDecision()
        # 確認沒有任何欄位可以阻止存儲
        assert not hasattr(d, "should_store")
        assert not hasattr(d, "store_episode")
        assert not hasattr(d, "skip")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 工具過濾測試（保留原有行為）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestToolFiltering:
    """確認 StorageJudge 保留 JudgeAgent 的工具過濾功能。"""

    def _make_tools(self) -> list[dict]:
        return [
            {"name": "force_remember", "description": "強制記憶"},
            {"name": "deep_recall", "description": "深度搜尋"},
            {"name": "adjust_importance", "description": "調整重要性"},
            {"name": "trigger_dream", "description": "觸發夢境"},
        ]

    def test_all_tools_pass_with_context(self):
        """有足夠脈絡時，所有工具都通過。"""
        judge = StorageJudge()
        from echo_memory.schemas.episode import Episode

        ctx = MemoryContext(
            episodes=[Episode(
                user_input="test", agent_response="test",
                session_id="s1", content_summary="test",
            )],
        )
        result = judge.filter_tools(ctx, self._make_tools(), total_episodes=10)
        assert len(result) == 4

    def test_no_context_removes_adjust(self):
        """無脈絡時移除 adjust_importance。"""
        judge = StorageJudge()
        result = judge.filter_tools(None, self._make_tools(), total_episodes=10)
        names = [t["name"] for t in result]
        assert "adjust_importance" not in names

    def test_few_episodes_removes_dream(self):
        """記憶太少時移除 trigger_dream。"""
        judge = StorageJudge()
        result = judge.filter_tools(None, self._make_tools(), total_episodes=3)
        names = [t["name"] for t in result]
        assert "trigger_dream" not in names

    def test_few_episodes_removes_deep_recall(self):
        """記憶太少時移除 deep_recall。"""
        judge = StorageJudge()
        result = judge.filter_tools(None, self._make_tools(), total_episodes=1)
        names = [t["name"] for t in result]
        assert "deep_recall" not in names

    def test_empty_tools_returns_empty(self):
        judge = StorageJudge()
        result = judge.filter_tools(None, [], total_episodes=10)
        assert result == []

    def test_force_remember_always_available(self):
        """force_remember 不受限制。"""
        judge = StorageJudge()
        result = judge.filter_tools(None, self._make_tools(), total_episodes=0)
        names = [t["name"] for t in result]
        assert "force_remember" in names


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 工具推薦測試（保留原有行為）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestToolRecommendation:
    def _make_tools(self) -> list[dict]:
        return [
            {"name": "force_remember", "description": "強制記憶"},
            {"name": "deep_recall", "description": "深度搜尋"},
        ]

    def test_no_llm_returns_rules(self):
        judge = StorageJudge(llm_fn=None)
        rec = judge.recommend_tool("測試", None, self._make_tools())
        assert rec.source == "rules"
        assert rec.recommended_tool == "none"

    def test_llm_success(self):
        def mock_llm(prompt):
            return '{"recommended_tool": "deep_recall", "reasoning": "搜尋", "confidence": 0.8}'

        judge = StorageJudge(llm_fn=mock_llm)
        rec = judge.recommend_tool("找之前的", None, self._make_tools())
        assert rec.source == "llm"
        assert rec.recommended_tool == "deep_recall"

    def test_llm_low_confidence(self):
        def mock_llm(prompt):
            return '{"recommended_tool": "deep_recall", "reasoning": "不確定", "confidence": 0.3}'

        judge = StorageJudge(llm_fn=mock_llm)
        rec = judge.recommend_tool("隨便", None, self._make_tools())
        assert rec.recommended_tool == "none"

    def test_llm_failure_degrades(self):
        judge = StorageJudge(llm_fn=lambda p: (_ for _ in ()).throw(RuntimeError()))
        rec = judge.recommend_tool("測試", None, self._make_tools())
        assert rec.source == "rules"

    def test_rejects_invalid_tool(self):
        def mock_llm(prompt):
            return '{"recommended_tool": "delete_all", "reasoning": "刪", "confidence": 0.99}'

        judge = StorageJudge(llm_fn=mock_llm)
        rec = judge.recommend_tool("刪掉", None, self._make_tools())
        assert rec.recommended_tool == "none"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 向後相容測試
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBackwardCompatibility:
    def test_judge_agent_alias(self):
        """JudgeAgent 是 StorageJudge 的 alias。"""
        assert JudgeAgent is StorageJudge

    def test_import_from_affect(self):
        """可以從 affect 模組 import。"""
        from echo_memory.affect import StorageJudge as SJ, JudgeAgent as JA
        assert SJ is JA

    def test_old_judge_agent_works(self):
        """用舊名稱建立的實例具有新功能。"""
        judge = JudgeAgent()
        assert hasattr(judge, "storage_decision")
        assert hasattr(judge, "filter_tools")
        assert hasattr(judge, "recommend_tool")
