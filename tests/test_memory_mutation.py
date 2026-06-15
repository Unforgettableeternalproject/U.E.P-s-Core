"""
Memory Mutation Tests — 記憶變更行為測試（GPT 建議 #9）

這些測試驗證的不是 API 能不能跑，而是「跑起來後記憶會不會越記越歪」。

測試場景：
  1. 明確補充：expand 不覆蓋 description
  2. 明確修正：modify 保留舊版本
  3. 時間演化：temporal transition 而非粗暴 deprecate
  4. 假矛盾：共存偏好不是 contradiction
  5. LLM 幻覺污染：無 user evidence 不得寫入 concept
  6. Concept Health：健康度計算正確
"""

import json
from datetime import datetime, timezone, timedelta

import pytest

from echo_memory.schemas.concept import Concept
from echo_memory.schemas.episode import Episode
from echo_memory.storage.conflict_ledger import (
    ConflictLedger,
    ConflictRecord,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 場景 1：明確補充 — expand 不覆蓋 description
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestExplicitExpansion:
    """使用者補充資訊時，原有 description 不應被覆蓋。"""

    def test_expand_adds_fact_not_replaces_description(self):
        """expand 應新增 fact，不改 canonical description。"""
        concept = Concept(
            name="鋼琴",
            description="使用者喜歡的樂器",
        )
        original_desc = concept.description

        # 模擬 expand：新增 fact 到 metadata
        new_fact = {"content": "也用鋼琴作曲", "source": "ep_001"}
        facts = concept.metadata.get("facts", [])
        facts.append(new_fact)
        concept.metadata["facts"] = facts

        # description 不應被改變
        assert concept.description == original_desc
        # fact 應被記錄
        assert len(concept.facts) == 1
        assert concept.facts[0]["content"] == "也用鋼琴作曲"

    def test_expand_increases_evidence_count(self):
        """expand 後 evidence_count 應增加。"""
        concept = Concept(
            name="Python",
            description="程式語言",
            source_episode_ids=["ep_001"],
        )
        initial = concept.evidence_count

        concept.add_source_episode("ep_002")
        concept.metadata.setdefault("facts", []).append(
            {"content": "用於機器學習", "source": "ep_002"}
        )

        assert concept.evidence_count > initial


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 場景 2：明確修正 — modify 應保留舊值
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestExplicitCorrection:
    """使用者修正概念時，舊值應保留在 patch_history。"""

    def test_modify_preserves_old_value(self):
        """修正後 patch_history 應記錄舊值。"""
        concept = Concept(
            name="工作地點",
            description="在佶昕接案",
        )

        # 模擬 modify：更新 description + 記錄 patch_history
        old_desc = concept.description
        concept.description = "在佶昕實習"
        concept.record_mutation()

        history_entry = {
            "action": "modify",
            "old_value": old_desc,
            "new_value": concept.description,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        concept.metadata.setdefault("patch_history", []).append(history_entry)

        assert concept.description == "在佶昕實習"
        assert len(concept.patch_history) == 1
        assert concept.patch_history[0]["old_value"] == "在佶昕接案"
        assert concept.mutation_count == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 場景 3：時間演化 — 不粗暴 deprecate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTemporalEvolution:
    """「以前是 A，現在改成 B」應建立 temporal transition，不是 deprecate A。"""

    def test_temporal_evolution_preserves_both(self):
        """時間演化：兩個概念都應存在。"""
        old_concept = Concept(
            name="MEM 模組",
            description="U.E.P 的記憶模組",
        )
        new_concept = Concept(
            name="Echo Memory",
            description="從 U.E.P MEM 獨立遷出的新系統",
        )

        # 舊概念不應被 deprecate
        assert old_concept.is_active

        # 兩個概念都可以 active
        assert new_concept.is_active

        # 可透過 metadata 記錄演化關係
        old_concept.metadata["evolved_into"] = new_concept.id
        new_concept.metadata["evolved_from"] = old_concept.id

        assert old_concept.metadata["evolved_into"] == new_concept.id

    def test_conflict_ledger_temporal_evolution(self, tmp_path):
        """Conflict Ledger 可以將矛盾標記為 temporal_evolution。"""
        ledger = ConflictLedger(tmp_path)
        record = ledger.register(
            concept_a_id="con_old",
            concept_b_id="con_new",
            strength=0.7,
            concept_a_name="MEM 模組",
            concept_b_name="Echo Memory",
        )

        assert record.status == "unresolved"

        ledger.resolve(
            conflict_id=record.conflict_id,
            status="temporal_evolution",
            reason="舊版本 → 新架構，歷史演化而非矛盾",
        )

        resolved = ledger.find_between("con_old", "con_new")
        assert resolved is not None
        assert resolved.status == "temporal_evolution"
        assert resolved.is_resolved


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 場景 4：假矛盾 — 共存偏好不是 contradiction
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestFalseContradiction:
    """「我喜歡安靜，但我也喜歡舞台演出」不是矛盾，是 dual truth。"""

    def test_dual_truth_resolution(self, tmp_path):
        """可以共存的偏好不應被視為矛盾。"""
        ledger = ConflictLedger(tmp_path)
        record = ledger.register(
            concept_a_id="con_quiet",
            concept_b_id="con_stage",
            strength=0.5,
            concept_a_name="喜歡安靜",
            concept_b_name="喜歡舞台演出",
        )

        # 應可解決為 accepted_dual_truth
        ledger.resolve(
            conflict_id=record.conflict_id,
            status="accepted_dual_truth",
            reason="不同語境的偏好可以共存",
        )

        resolved = ledger.find_between("con_quiet", "con_stage")
        assert resolved.status == "accepted_dual_truth"

    def test_resolved_conflict_not_counted(self, tmp_path):
        """已解決的矛盾不應計入 unresolved_count。"""
        ledger = ConflictLedger(tmp_path)
        r1 = ledger.register("a", "b", 0.5)
        r2 = ledger.register("c", "d", 0.5)

        assert ledger.unresolved_count == 2

        ledger.resolve(r1.conflict_id, "resolved", "已修正")

        assert ledger.unresolved_count == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 場景 5：LLM 幻覺防護
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestLLMHallucinationGuard:
    """LLM 自行推論的內容（無 user evidence）不應被存為 concept。"""

    def test_concept_requires_source_episode(self):
        """有 source_episode 的概念透過 add_source_episode 提升 confidence。"""
        # 無來源的概念
        hallucinated = Concept(
            name="爵士樂",
            description="LLM 推論使用者喜歡爵士樂",
        )

        # 有來源的概念（透過 add_source_episode 正確更新 confidence）
        grounded = Concept(
            name="古典鋼琴",
            description="使用者明確表示喜歡古典鋼琴",
        )
        grounded.add_source_episode("ep_001")
        grounded.add_source_episode("ep_002")

        # 有來源的 confidence 應遠高於無來源的
        assert grounded.confidence > hallucinated.confidence
        assert len(grounded.source_episode_ids) == 2

    def test_no_source_health_is_low(self):
        """無 evidence 的概念 health_score 應很低。"""
        concept = Concept(
            name="幻覺概念",
            description="沒有任何 episode 支撐",
        )
        # health 應很低（evidence_factor 低）
        assert concept.health_score < 0.3


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 場景 6：Concept Health 計算
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestConceptHealth:
    """Concept Health 指標計算正確性。"""

    def test_health_increases_with_evidence(self):
        """多 evidence → 高 health。"""
        poor = Concept(name="A", description="x")
        rich = Concept(
            name="B",
            description="y",
            source_episode_ids=["ep1", "ep2", "ep3"],
        )
        rich.metadata["facts"] = [
            {"content": "fact1"},
            {"content": "fact2"},
        ]

        assert rich.health_score > poor.health_score

    def test_health_decreases_with_conflicts(self):
        """未解矛盾 → 低 health。"""
        healthy = Concept(
            name="A",
            description="x",
            source_episode_ids=["ep1", "ep2", "ep3"],
            confidence=0.8,
        )
        conflicted = Concept(
            name="B",
            description="y",
            source_episode_ids=["ep1", "ep2", "ep3"],
            confidence=0.8,
            conflict_count=3,
        )

        assert conflicted.health_score < healthy.health_score

    def test_health_stable_concept_higher(self):
        """長期未修改的概念更穩定。"""
        recent = Concept(
            name="A",
            description="x",
            source_episode_ids=["ep1", "ep2"],
            confidence=0.7,
            updated_at=datetime.now(timezone.utc),
        )
        stable = Concept(
            name="B",
            description="y",
            source_episode_ids=["ep1", "ep2"],
            confidence=0.7,
            updated_at=datetime.now(timezone.utc) - timedelta(days=7),
        )

        assert stable.health_score >= recent.health_score

    def test_mutation_count_tracked(self):
        """每次 record_mutation 正確累計。"""
        c = Concept(name="A", description="x")
        assert c.mutation_count == 0

        c.record_mutation()
        c.record_mutation()
        c.record_mutation()

        assert c.mutation_count == 3

    def test_verification_timestamp_updated(self):
        """record_verification 更新 last_verified_at。"""
        c = Concept(name="A", description="x")
        assert c.last_verified_at is None

        c.record_verification()
        assert c.last_verified_at is not None
        assert (
            datetime.now(timezone.utc) - c.last_verified_at
        ).total_seconds() < 5


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Conflict Ledger 持久化
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestConflictLedgerPersistence:
    """Conflict Ledger 的持久化和查詢正確性。"""

    def test_register_and_find(self, tmp_path):
        """登記後可查詢。"""
        ledger = ConflictLedger(tmp_path)
        record = ledger.register("con_a", "con_b", 0.8, reason="測試矛盾")

        found = ledger.find_between("con_a", "con_b")
        assert found is not None
        assert found.conflict_id == record.conflict_id

        # 反向查找也可以
        found2 = ledger.find_between("con_b", "con_a")
        assert found2 is not None

    def test_duplicate_register_updates_strength(self, tmp_path):
        """重複登記同組概念更新 strength 而非建新記錄。"""
        ledger = ConflictLedger(tmp_path)
        r1 = ledger.register("a", "b", 0.5)
        r2 = ledger.register("a", "b", 0.9)

        assert r1.conflict_id == r2.conflict_id
        assert r2.contradiction_strength == 0.9
        assert ledger.total_count == 1

    def test_persistence_roundtrip(self, tmp_path):
        """持久化後重新載入資料完整。"""
        ledger1 = ConflictLedger(tmp_path)
        ledger1.register("a", "b", 0.7, concept_a_name="概念A")
        ledger1.register("c", "d", 0.5)

        # 重新載入
        ledger2 = ConflictLedger(tmp_path)
        assert ledger2.total_count == 2
        found = ledger2.find_between("a", "b")
        assert found is not None
        assert found.concept_a_name == "概念A"

    def test_get_by_concept(self, tmp_path):
        """查詢涉及某概念的所有矛盾。"""
        ledger = ConflictLedger(tmp_path)
        ledger.register("a", "b", 0.5)
        ledger.register("a", "c", 0.6)
        ledger.register("d", "e", 0.7)

        a_conflicts = ledger.get_by_concept("a")
        assert len(a_conflicts) == 2

    def test_stats(self, tmp_path):
        """統計摘要正確。"""
        ledger = ConflictLedger(tmp_path)
        r1 = ledger.register("a", "b", 0.5)
        ledger.register("c", "d", 0.6)
        ledger.resolve(r1.conflict_id, "resolved", "已修正")

        stats = ledger.get_stats()
        assert stats["total"] == 2
        assert stats["by_status"]["resolved"] == 1
        assert stats["by_status"]["unresolved"] == 1

    def test_resolve_returns_none_for_unknown(self, tmp_path):
        """解決不存在的矛盾返回 None。"""
        ledger = ConflictLedger(tmp_path)
        result = ledger.resolve("cf_nonexist", "resolved", "test")
        assert result is None
