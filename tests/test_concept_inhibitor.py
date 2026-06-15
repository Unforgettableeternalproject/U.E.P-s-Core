"""
tests/test_concept_inhibitor.py — ConceptInhibitor 單元測試

驗證 patch 建立、hybrid 決策、apply、audit log。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from echo_memory.affect.concept_inhibitor import ConceptInhibitor
from echo_memory.affect.concept_patch import (
    ConceptPatch,
    ConceptState,
    ExpandMode,
    PatchEvidence,
    PatchHistoryEntry,
    PatchTransaction,
)
from echo_memory.affect.concept_verifier import (
    ConceptAssessment,
    ConceptVerification,
)
from echo_memory.schemas.concept import Concept
from echo_memory.storage.semantic import SemanticMemory


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture
def semantic(tmp_path: Path) -> SemanticMemory:
    """建立帶有測試概念的 SemanticMemory"""
    sem = SemanticMemory(storage_path=tmp_path / "semantic")

    # 插入測試概念（手動，不需 embed_fn）
    for name, desc in [
        ("貓", "一種常見的家養寵物"),
        ("狗", "犬科動物，常見的家養寵物"),
        ("魚", "水生動物，用戶養了一缸熱帶魚"),
    ]:
        c = Concept(name=name, description=desc)
        sem._concepts[name] = c
        sem._concepts_by_id[c.id] = c
    sem._save()
    return sem


@pytest.fixture
def inhibitor(semantic: SemanticMemory) -> ConceptInhibitor:
    """無 LLM 的 Inhibitor"""
    return ConceptInhibitor(semantic=semantic, llm_fn=None)


def _make_verification(
    assessments: list[ConceptAssessment],
) -> ConceptVerification:
    return ConceptVerification(assessments=assessments, used_llm=True)


def _get_concept_id(semantic: SemanticMemory, name: str) -> str:
    c = semantic.get_by_name(name)
    assert c is not None, f"概念 {name} 不存在"
    return c.id


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 資料結構測試
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDataStructures:
    """ConceptPatch / PatchTransaction 資料結構"""

    def test_patch_auto_id(self):
        """patch_id 應自動生成"""
        evidence = PatchEvidence(
            evidence_kind="explicit_user_claim",
            evidence_strength=0.8,
            verifier_status="expand",
            verifier_reason="test",
        )
        p = ConceptPatch(
            transaction_id="tx_test",
            concept_name="test",
            action="expand",
            evidence=evidence,
        )
        assert p.patch_id.startswith("patch_")

    def test_patch_serialization(self):
        """ConceptPatch 序列化/反序列化"""
        evidence = PatchEvidence(
            evidence_kind="user_correction",
            evidence_strength=0.7,
            verifier_status="modify",
            verifier_reason="用戶修正",
        )
        p = ConceptPatch(
            transaction_id="tx_001",
            concept_name="貓",
            action="modify",
            evidence=evidence,
            old_value="舊描述",
            new_value="新描述",
        )
        d = p.to_dict()
        p2 = ConceptPatch.from_dict(d)
        assert p2.concept_name == "貓"
        assert p2.action == "modify"
        assert p2.evidence.evidence_kind == "user_correction"

    def test_transaction_auto_id(self):
        """transaction_id 應自動生成"""
        tx = PatchTransaction(
            patches=[],
            user_input="test",
            llm_response="test",
            session_id="s1",
            turn_id="t1",
        )
        assert tx.transaction_id.startswith("tx_")

    def test_transaction_serialization(self):
        """PatchTransaction 序列化/反序列化"""
        evidence = PatchEvidence(
            evidence_kind="explicit_user_claim",
            evidence_strength=0.8,
            verifier_status="expand",
            verifier_reason="test",
        )
        tx = PatchTransaction(
            patches=[
                ConceptPatch(
                    transaction_id="tx_001",
                    concept_name="A",
                    action="expand",
                    evidence=evidence,
                ),
            ],
            user_input="hello",
            llm_response="world",
            session_id="s1",
            turn_id="t1",
        )
        d = tx.to_dict()
        tx2 = PatchTransaction.from_dict(d)
        assert tx2.patch_count == 1
        assert tx2.patches[0].concept_name == "A"

    def test_concept_state_serialization(self):
        """ConceptState 序列化/反序列化"""
        from datetime import datetime, timezone
        state = ConceptState(
            status="deprecated",
            deprecated_at=datetime(2026, 6, 15, tzinfo=timezone.utc),
            deprecated_reason="已被取代",
            replaced_by="con_new123",
        )
        d = state.to_dict()
        s2 = ConceptState.from_dict(d)
        assert s2.status == "deprecated"
        assert s2.replaced_by == "con_new123"

    def test_expand_mode_enum(self):
        """ExpandMode enum 值"""
        assert ExpandMode.ADD_FACT.value == "add_fact"
        assert ExpandMode.REFINE_SUMMARY.value == "refine_summary"
        assert ExpandMode.ADD_RELATION.value == "add_relation"

    def test_patch_history_entry(self):
        """PatchHistoryEntry 序列化"""
        from datetime import datetime, timezone
        entry = PatchHistoryEntry(
            patch_id="patch_001",
            transaction_id="tx_001",
            timestamp=datetime.now(timezone.utc),
            action="expand",
            old_confidence=0.5,
            new_confidence=0.55,
        )
        d = entry.to_dict()
        e2 = PatchHistoryEntry.from_dict(d)
        assert e2.patch_id == "patch_001"
        assert e2.action == "expand"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 空 / 無變更
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestNoChanges:
    """無變更場景"""

    def test_empty_verification(self, inhibitor: ConceptInhibitor):
        """無變更 → committed 空 transaction"""
        v = _make_verification([])
        tx = inhibitor.process(v, "hello", "world", "s1", "t1")
        assert tx.status == "committed"
        assert tx.patch_count == 0

    def test_all_valid(self, inhibitor: ConceptInhibitor, semantic: SemanticMemory):
        """全部 valid → committed 空 transaction"""
        cat_id = _get_concept_id(semantic, "貓")
        v = _make_verification([
            ConceptAssessment(
                concept_id=cat_id,
                concept_name="貓",
                status="valid",
                relevance=0.8,
            ),
        ])
        tx = inhibitor.process(v, "hello", "world", "s1", "t1")
        assert tx.status == "committed"
        assert tx.patch_count == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Expand — 規則層批准
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestExpandApproval:
    """expand/ADD_FACT + 強證據 → 規則直接批准"""

    def test_expand_strong_evidence_approved(
        self, inhibitor: ConceptInhibitor, semantic: SemanticMemory
    ):
        """高相關度 expand → applied"""
        cat_id = _get_concept_id(semantic, "貓")
        v = _make_verification([
            ConceptAssessment(
                concept_id=cat_id,
                concept_name="貓",
                status="expand",
                reason="用戶提到貓的品種是英國短毛",
                suggested_update="英國短毛貓",
                relevance=0.85,
            ),
        ])

        tx = inhibitor.process(v, "我的貓是英國短毛", "好的", "s1", "t1")

        assert tx.patch_count == 1
        patch = tx.patches[0]
        assert patch.status == "applied"
        assert patch.action == "expand"
        assert patch.expand_mode == ExpandMode.ADD_FACT

        # 檢查 SemanticMemory 已更新
        cat = semantic.get_by_name("貓")
        assert cat is not None
        assert len(cat.facts) == 1
        assert "英國短毛" in cat.facts[0]["text"]

    def test_expand_updates_patch_history(
        self, inhibitor: ConceptInhibitor, semantic: SemanticMemory
    ):
        """expand 應寫入 patch_history"""
        cat_id = _get_concept_id(semantic, "貓")
        v = _make_verification([
            ConceptAssessment(
                concept_id=cat_id,
                concept_name="貓",
                status="expand",
                reason="新事實",
                relevance=0.9,
            ),
        ])

        tx = inhibitor.process(v, "test", "ok", "s1", "t1")

        cat = semantic.get_by_name("貓")
        assert len(cat.patch_history) == 1
        assert cat.patch_history[0]["action"] == "expand"

    def test_expand_confidence_increase(
        self, inhibitor: ConceptInhibitor, semantic: SemanticMemory
    ):
        """強證據 expand → confidence 微增"""
        cat = semantic.get_by_name("貓")
        old_conf = cat.confidence
        cat_id = cat.id

        v = _make_verification([
            ConceptAssessment(
                concept_id=cat_id,
                concept_name="貓",
                status="expand",
                reason="新事實",
                suggested_update="用戶很喜歡貓",
                relevance=0.9,
            ),
        ])

        inhibitor.process(v, "我很喜歡貓", "好的", "s1", "t1")

        cat = semantic.get_by_name("貓")
        assert cat.confidence >= old_conf  # 不應降低


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Modify / Deprecate — 需要 LLM 或 held
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestModifyDeprecate:
    """modify/deprecate 的高風險處理"""

    def test_modify_without_llm_held(
        self, inhibitor: ConceptInhibitor, semantic: SemanticMemory
    ):
        """modify 無 LLM → held（不 commit 也不 reject）"""
        cat_id = _get_concept_id(semantic, "貓")
        v = _make_verification([
            ConceptAssessment(
                concept_id=cat_id,
                concept_name="貓",
                status="modify",
                reason="品種修正",
                suggested_update="緬因貓",
                relevance=0.7,
            ),
        ])

        tx = inhibitor.process(v, "其實是緬因貓", "好的", "s1", "t1")

        assert tx.patches[0].status == "held"
        # description 不應被修改
        cat = semantic.get_by_name("貓")
        assert cat.description == "一種常見的家養寵物"

    def test_deprecate_without_llm_held(
        self, inhibitor: ConceptInhibitor, semantic: SemanticMemory
    ):
        """deprecate 無 LLM → held"""
        fish_id = _get_concept_id(semantic, "魚")
        v = _make_verification([
            ConceptAssessment(
                concept_id=fish_id,
                concept_name="魚",
                status="deprecate",
                reason="用戶不再養魚",
                relevance=0.8,
            ),
        ])

        tx = inhibitor.process(v, "我不養魚了", "好的", "s1", "t1")
        assert tx.patches[0].status == "held"

    def test_modify_with_llm_approve(self, semantic: SemanticMemory):
        """modify + LLM 佐證 → approved + applied"""
        def approve_llm(_: str) -> str:
            return json.dumps({
                "approve": True,
                "confidence": 0.8,
                "reason": "用戶明確修正品種",
            })

        inhibitor = ConceptInhibitor(semantic=semantic, llm_fn=approve_llm)
        cat_id = _get_concept_id(semantic, "貓")

        v = _make_verification([
            ConceptAssessment(
                concept_id=cat_id,
                concept_name="貓",
                status="modify",
                reason="品種修正",
                suggested_update="緬因貓，大型家貓品種",
                relevance=0.8,
            ),
        ])

        tx = inhibitor.process(v, "其實我養的是緬因貓", "好的", "s1", "t1")
        assert tx.patches[0].status == "applied"

        cat = semantic.get_by_name("貓")
        assert "緬因貓" in cat.description

    def test_modify_with_llm_reject(self, semantic: SemanticMemory):
        """modify + LLM 不佐證 → rejected"""
        def reject_llm(_: str) -> str:
            return json.dumps({
                "approve": False,
                "confidence": 0.3,
                "reason": "用戶沒有明確修正",
            })

        inhibitor = ConceptInhibitor(semantic=semantic, llm_fn=reject_llm)
        cat_id = _get_concept_id(semantic, "貓")

        v = _make_verification([
            ConceptAssessment(
                concept_id=cat_id,
                concept_name="貓",
                status="modify",
                reason="可能修正",
                suggested_update="新描述",
                relevance=0.6,
            ),
        ])

        tx = inhibitor.process(v, "test", "test", "s1", "t1")
        assert tx.patches[0].status == "rejected"
        assert tx.patches[0].rejection_reason is not None

    def test_deprecate_with_llm_apply(self, semantic: SemanticMemory):
        """deprecate + LLM 佐證 → applied + status=deprecated"""
        def approve_llm(_: str) -> str:
            return json.dumps({
                "approve": True,
                "confidence": 0.9,
                "reason": "用戶明確表示不再養魚",
            })

        inhibitor = ConceptInhibitor(semantic=semantic, llm_fn=approve_llm)
        fish_id = _get_concept_id(semantic, "魚")

        v = _make_verification([
            ConceptAssessment(
                concept_id=fish_id,
                concept_name="魚",
                status="deprecate",
                reason="用戶不再養魚",
                relevance=0.85,
            ),
        ])

        tx = inhibitor.process(v, "我不養魚了", "好的", "s1", "t1")
        assert tx.patches[0].status == "applied"

        fish = semantic.get_by_name("魚")
        assert fish.status == "deprecated"
        assert fish.metadata.get("deprecated_reason") == "用戶不再養魚"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 規則拒絕
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRuleRejection:
    """規則層拒絕"""

    def test_weak_evidence_rejected(
        self, inhibitor: ConceptInhibitor, semantic: SemanticMemory
    ):
        """極弱證據 → rejected"""
        cat_id = _get_concept_id(semantic, "貓")
        v = _make_verification([
            ConceptAssessment(
                concept_id=cat_id,
                concept_name="貓",
                status="expand",
                reason="x",
                relevance=0.05,  # 極低
            ),
        ])

        tx = inhibitor.process(v, "test", "ok", "s1", "t1")
        assert tx.patches[0].status == "rejected"
        assert "證據強度" in tx.patches[0].rejection_reason

    def test_deprecate_no_reason_rejected(
        self, inhibitor: ConceptInhibitor, semantic: SemanticMemory
    ):
        """deprecate 無理由 → rejected"""
        cat_id = _get_concept_id(semantic, "貓")
        v = _make_verification([
            ConceptAssessment(
                concept_id=cat_id,
                concept_name="貓",
                status="deprecate",
                reason="",  # 空
                relevance=0.5,
            ),
        ])

        tx = inhibitor.process(v, "test", "ok", "s1", "t1")
        assert tx.patches[0].status == "rejected"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LLM 失敗降級
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestLLMFailure:
    """LLM 呼叫失敗 → held"""

    def test_llm_error_held(self, semantic: SemanticMemory):
        """LLM 報錯 → held"""
        def failing_llm(_: str) -> str:
            raise RuntimeError("API 錯誤")

        inhibitor = ConceptInhibitor(semantic=semantic, llm_fn=failing_llm)
        cat_id = _get_concept_id(semantic, "貓")

        v = _make_verification([
            ConceptAssessment(
                concept_id=cat_id,
                concept_name="貓",
                status="modify",
                reason="修正",
                suggested_update="新描述",
                relevance=0.7,
            ),
        ])

        tx = inhibitor.process(v, "test", "ok", "s1", "t1")
        assert tx.patches[0].status == "held"

    def test_llm_bad_json_held(self, semantic: SemanticMemory):
        """LLM 回傳非 JSON → held"""
        inhibitor = ConceptInhibitor(
            semantic=semantic, llm_fn=lambda _: "不是 JSON"
        )
        cat_id = _get_concept_id(semantic, "貓")

        v = _make_verification([
            ConceptAssessment(
                concept_id=cat_id,
                concept_name="貓",
                status="modify",
                reason="修正",
                relevance=0.7,
            ),
        ])

        tx = inhibitor.process(v, "test", "ok", "s1", "t1")
        assert tx.patches[0].status == "held"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Audit Log
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAuditLog:
    """Audit log 寫入"""

    def test_audit_log_written(
        self, inhibitor: ConceptInhibitor, semantic: SemanticMemory
    ):
        """處理後應寫入 audit log"""
        cat_id = _get_concept_id(semantic, "貓")
        v = _make_verification([
            ConceptAssessment(
                concept_id=cat_id,
                concept_name="貓",
                status="expand",
                reason="新事實",
                relevance=0.9,
            ),
        ])

        tx = inhibitor.process(v, "test", "ok", "s1", "t1")

        log_path = semantic.storage_path / "audit_log.jsonl"
        assert log_path.exists()

        with open(log_path, encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) >= 1

        record = json.loads(lines[-1])
        assert record["transaction_id"] == tx.transaction_id


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Concept Schema 新增屬性
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestConceptSchemaExtensions:
    """Concept 新增的 status / facts / retrieval_weight"""

    def test_default_status_active(self):
        c = Concept(name="test", description="test")
        assert c.status == "active"
        assert c.is_active
        assert not c.is_deprecated

    def test_retrieval_weight(self):
        c = Concept(name="test", description="test")
        assert c.retrieval_weight == 1.0
        c.status = "deprecated"
        assert c.retrieval_weight == 0.15
        c.status = "archived"
        assert c.retrieval_weight == 0.03

    def test_facts_property(self):
        c = Concept(name="test", description="test")
        assert c.facts == []
        c.metadata["facts"] = [{"text": "fact1"}]
        assert len(c.facts) == 1

    def test_patch_history_property(self):
        c = Concept(name="test", description="test")
        assert c.patch_history == []
        c.metadata["patch_history"] = [{"patch_id": "p1"}]
        assert len(c.patch_history) == 1

    def test_status_serialization(self):
        c = Concept(name="test", description="test", status="deprecated")
        d = c.to_dict()
        assert d["status"] == "deprecated"
        c2 = Concept.from_dict(d)
        assert c2.status == "deprecated"
