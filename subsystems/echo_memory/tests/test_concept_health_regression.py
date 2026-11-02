"""
tests/test_concept_health_regression.py

Regression 測試：Concept Health 的 mutation_count 在持久化後能正確保留。

背景：
  ConceptInhibitor._apply_patch()（行 776-779）曾有 Bug：_save() 在
  record_mutation() 之前被呼叫，導致 mutation_count 增量寫入磁碟前就先
  被舊版資料蓋掉。修正後順序應為 record_mutation() → _save()。
  _apply_expand_source() 有同樣問題，亦同步修正。

本測試確保：
  1. create 路徑：新建概念後重新載入，mutation_count 仍為 0（create 本身
     不呼叫 record_mutation，但 upsert_concept 呼叫 _save，確保資料落磁碟）。
  2. expand 路徑（_apply_patch via process_extractor_patch synonym_merge）：
     重新載入後 mutation_count > 0。
  3. expand_source 路徑（_apply_expand_source）：
     重新載入後 mutation_count > 0，且 last_verified_at 可正確保留（若有設定）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from echo_memory.affect.concept_inhibitor import ConceptInhibitor
from echo_memory.schemas.concept import Concept
from echo_memory.storage.semantic import SemanticMemory


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 輔助函式
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _reload_semantic(storage_path: Path) -> SemanticMemory:
    """模擬程式重啟：建立一個全新的 SemanticMemory 實例（重讀磁碟）。"""
    return SemanticMemory(storage_path=storage_path)


def _insert_concept_directly(
    semantic: SemanticMemory, name: str, desc: str
) -> Concept:
    """
    直接插入概念到 SemanticMemory 並持久化。
    不經過 ConceptInhibitor，模擬「原始概念已存在」的前提狀態。
    """
    concept = Concept(name=name, description=desc, concept_type="entity")
    semantic._concepts[name] = concept
    semantic._concepts_by_id[concept.id] = concept
    semantic._save()
    return concept


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture
def storage_path(tmp_path: Path) -> Path:
    """測試專用的暫存儲存目錄。"""
    p = tmp_path / "semantic_regression"
    p.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def semantic(storage_path: Path) -> SemanticMemory:
    """乾淨的 SemanticMemory 實例。"""
    return SemanticMemory(storage_path=storage_path)


@pytest.fixture
def inhibitor(semantic: SemanticMemory) -> ConceptInhibitor:
    """無 LLM 的 ConceptInhibitor（所有 patch 走規則層直接 approve）。"""
    return ConceptInhibitor(semantic=semantic, llm_fn=None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 測試群組 1：create 路徑
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCreatePathPersistence:
    """新建概念路徑的持久化正確性。"""

    def test_create_concept_persists_to_disk(
        self,
        semantic: SemanticMemory,
        inhibitor: ConceptInhibitor,
        storage_path: Path,
    ):
        """
        透過 process_extractor_patch（create 路徑）建立概念後，
        重新載入 SemanticMemory 能找到同一個概念。
        """
        concept_id, tx = inhibitor.process_extractor_patch(
            noun="Python",
            desc="一種高階動態程式語言",
            episode_id="ep_001",
            evidence_kind="extractor_new_concept",
            concept_type="entity",
        )

        assert tx.status == "committed", f"Transaction 應為 committed，但得到 {tx.status}"
        assert concept_id is not None, "新建概念應回傳 concept_id"

        # 模擬重啟
        reloaded = _reload_semantic(storage_path)
        reloaded_concept = reloaded.get(concept_id)

        assert reloaded_concept is not None, (
            f"重啟後應能根據 concept_id={concept_id} 找到概念"
        )
        assert reloaded_concept.name == "Python"

    def test_create_mutation_count_is_zero_after_reload(
        self,
        semantic: SemanticMemory,
        inhibitor: ConceptInhibitor,
        storage_path: Path,
    ):
        """
        create 路徑本身不呼叫 record_mutation()，
        重啟後 mutation_count 應為 0（不因序列化/反序列化而意外被改動）。
        """
        concept_id, _ = inhibitor.process_extractor_patch(
            noun="TypeScript",
            desc="JavaScript 的強型別超集",
            episode_id="ep_002",
            evidence_kind="extractor_new_concept",
        )
        assert concept_id is not None

        reloaded = _reload_semantic(storage_path)
        reloaded_concept = reloaded.get(concept_id)
        assert reloaded_concept is not None

        assert reloaded_concept.mutation_count == 0, (
            f"create 路徑後 mutation_count 應為 0，得到 {reloaded_concept.mutation_count}"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 測試群組 2：_apply_expand_source 路徑（核心 Regression）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestExpandSourceMutationCountRegression:
    """
    Regression 測試核心：_apply_expand_source() 的 record_mutation → _save 順序。

    修正前：_save() 先於 record_mutation() 呼叫 → mutation_count 不被存入磁碟。
    修正後：record_mutation() → _save() → mutation_count 正確落磁碟。
    """

    def test_expand_source_mutation_count_persists(
        self,
        semantic: SemanticMemory,
        inhibitor: ConceptInhibitor,
        storage_path: Path,
    ):
        """
        透過 synonym_merge 路徑呼叫 _apply_expand_source()，
        重啟後 mutation_count 必須 > 0。

        這是 Bug 修正的核心驗證點。
        """
        # 先建立一個原始概念（直接插入，不走 inhibitor）
        original = _insert_concept_directly(semantic, "機器學習", "統計學習方法的子領域")
        original_id = original.id

        # 以 synonym_merge 路徑呼叫 process_extractor_patch
        # 這會走 _apply_expand_source → record_mutation → _save
        concept_id, tx = inhibitor.process_extractor_patch(
            noun="ML",  # 同義詞
            desc="機器學習的縮寫",
            episode_id="ep_003",
            evidence_kind="extractor_synonym_merge",
            existing_concept_id=original_id,
        )

        assert tx.status == "committed", f"Transaction 應 committed，得到 {tx.status}"
        assert concept_id == original_id, "synonym_merge 應回傳原始概念的 ID"

        # 確認記憶體內已更新
        in_memory_concept = semantic.get(original_id)
        assert in_memory_concept is not None
        assert in_memory_concept.mutation_count > 0, (
            "apply expand_source 後，記憶體內 mutation_count 應 > 0，"
            f"但得到 {in_memory_concept.mutation_count}"
        )

        # 模擬重啟（核心驗證）
        reloaded = _reload_semantic(storage_path)
        reloaded_concept = reloaded.get(original_id)

        assert reloaded_concept is not None, "重啟後應能找到原始概念"
        assert reloaded_concept.mutation_count > 0, (
            "Regression: 重啟後 mutation_count 不應歸零！"
            f"得到 {reloaded_concept.mutation_count}。"
            "這代表 _apply_expand_source 的 record_mutation → _save 順序仍有問題。"
        )

    def test_expand_source_mutation_count_exact_value(
        self,
        semantic: SemanticMemory,
        inhibitor: ConceptInhibitor,
        storage_path: Path,
    ):
        """
        連續做 N 次 expand_source，重啟後 mutation_count 應等於 N。
        確保每次累積都被正確持久化，不會只保留「最後一次」的值。
        """
        original = _insert_concept_directly(semantic, "深度學習", "多層神經網路的機器學習方法")
        original_id = original.id

        # 第一次 expand_source
        _, tx1 = inhibitor.process_extractor_patch(
            noun="DL",
            desc="深度學習的縮寫",
            episode_id="ep_100",
            evidence_kind="extractor_synonym_merge",
            existing_concept_id=original_id,
        )
        assert tx1.status == "committed"

        # 第二次 expand_source（不同 episode_id）
        _, tx2 = inhibitor.process_extractor_patch(
            noun="DeepLearning",
            desc="深度學習的英文",
            episode_id="ep_101",
            evidence_kind="extractor_synonym_merge",
            existing_concept_id=original_id,
        )
        assert tx2.status == "committed"

        # 模擬重啟
        reloaded = _reload_semantic(storage_path)
        reloaded_concept = reloaded.get(original_id)

        assert reloaded_concept is not None
        assert reloaded_concept.mutation_count == 2, (
            f"兩次 expand_source 後，重啟 mutation_count 應為 2，"
            f"得到 {reloaded_concept.mutation_count}"
        )

    def test_expand_source_episode_id_also_persists(
        self,
        semantic: SemanticMemory,
        inhibitor: ConceptInhibitor,
        storage_path: Path,
    ):
        """
        expand_source 不只影響 mutation_count，
        新的 episode_id 也應該被持久化到 source_episode_ids。
        """
        original = _insert_concept_directly(semantic, "強化學習", "透過獎勵機制學習決策的方法")
        original_id = original.id
        new_episode_id = "ep_200"

        inhibitor.process_extractor_patch(
            noun="RL",
            desc="強化學習的縮寫",
            episode_id=new_episode_id,
            evidence_kind="extractor_synonym_merge",
            existing_concept_id=original_id,
        )

        reloaded = _reload_semantic(storage_path)
        reloaded_concept = reloaded.get(original_id)

        assert reloaded_concept is not None
        assert new_episode_id in reloaded_concept.source_episode_ids, (
            f"重啟後 source_episode_ids 應包含 {new_episode_id}，"
            f"實際為 {reloaded_concept.source_episode_ids}"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 測試群組 3：_apply_patch expand 路徑（透過 process() 的 expand action）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestApplyPatchExpandRegression:
    """
    _apply_patch() 的 record_mutation → _save 順序驗證。

    _apply_patch 處理 expand/modify/deprecate，
    修正前同樣有 _save() 先於 record_mutation() 的問題。
    """

    def test_apply_patch_expand_mutation_persists(
        self,
        semantic: SemanticMemory,
        storage_path: Path,
    ):
        """
        直接手動呼叫 _apply_patch（expand action），
        重啟後 mutation_count 應 > 0。
        """
        from echo_memory.affect.concept_patch import (
            ConceptPatch,
            ExpandMode,
            PatchEvidence,
        )

        # 建立原始概念
        original = _insert_concept_directly(semantic, "自然語言處理", "讓電腦理解人類語言的技術")
        original_id = original.id

        inhibitor = ConceptInhibitor(semantic=semantic, llm_fn=None)

        # 手動建立 expand patch
        evidence = PatchEvidence(
            evidence_kind="manual_test",
            evidence_strength=0.8,
            verifier_status="valid",
            verifier_reason="直接測試 _apply_patch",
        )
        patch = ConceptPatch(
            transaction_id="tx_test_expand_001",
            concept_id=original_id,
            concept_name="自然語言處理",
            action="expand",
            expand_mode=ExpandMode.ADD_FACT,
            target_path="facts",
            new_value={"content": "NLP 包含語音識別、文本分類等子任務"},
            patch_confidence=0.8,
            evidence=evidence,
            risk_level="low",
        )

        # 呼叫 _apply_patch（修正後的版本）
        success = inhibitor._apply_patch(patch)
        assert success, "_apply_patch expand 應成功"

        # 確認記憶體內已更新
        in_memory = semantic.get(original_id)
        assert in_memory is not None
        assert in_memory.mutation_count > 0, (
            f"apply_patch 後記憶體 mutation_count 應 > 0，得到 {in_memory.mutation_count}"
        )

        # 模擬重啟（核心驗證）
        reloaded = _reload_semantic(storage_path)
        reloaded_concept = reloaded.get(original_id)

        assert reloaded_concept is not None
        assert reloaded_concept.mutation_count > 0, (
            "Regression: 重啟後 _apply_patch expand 的 mutation_count 不應歸零！"
            f"得到 {reloaded_concept.mutation_count}"
        )

    def test_apply_patch_modify_mutation_persists(
        self,
        semantic: SemanticMemory,
        storage_path: Path,
    ):
        """
        _apply_patch modify action 後，重啟 mutation_count 應 > 0。
        """
        from echo_memory.affect.concept_patch import ConceptPatch, PatchEvidence

        original = _insert_concept_directly(semantic, "遷移學習", "將預訓練模型應用到新任務")
        original_id = original.id

        inhibitor = ConceptInhibitor(semantic=semantic, llm_fn=None)

        evidence = PatchEvidence(
            evidence_kind="manual_test",
            evidence_strength=0.9,
            verifier_status="valid",
            verifier_reason="直接測試 modify",
        )
        patch = ConceptPatch(
            transaction_id="tx_test_modify_001",
            concept_id=original_id,
            concept_name="遷移學習",
            action="modify",
            target_path="description",
            old_value="將預訓練模型應用到新任務",
            new_value="利用在大型資料集上訓練的模型，遷移至相似但資料較少的新任務",
            old_confidence=original.confidence,
            new_confidence=0.8,
            patch_confidence=0.9,
            evidence=evidence,
            risk_level="medium",
        )

        success = inhibitor._apply_patch(patch)
        assert success, "_apply_patch modify 應成功"

        # 模擬重啟
        reloaded = _reload_semantic(storage_path)
        reloaded_concept = reloaded.get(original_id)

        assert reloaded_concept is not None
        assert reloaded_concept.mutation_count > 0, (
            f"Regression: modify 後重啟 mutation_count 不應歸零，"
            f"得到 {reloaded_concept.mutation_count}"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 測試群組 4：last_verified_at 持久化
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestLastVerifiedAtPersistence:
    """
    last_verified_at 欄位的持久化正確性。

    record_verification() 寫入 last_verified_at 後，
    若 _save() 沒有被呼叫（或順序錯誤），重啟後該欄位會是 None。
    """

    def test_last_verified_at_persists_after_manual_save(
        self,
        semantic: SemanticMemory,
        storage_path: Path,
    ):
        """
        手動呼叫 record_verification() + _save()，
        重啟後 last_verified_at 不應為 None。
        """
        original = _insert_concept_directly(semantic, "聯邦學習", "在本地裝置上訓練模型的分散式學習")
        original_id = original.id

        # 模擬 Verifier 驗證動作
        concept = semantic.get(original_id)
        assert concept is not None
        concept.record_verification()
        verified_at = concept.last_verified_at
        assert verified_at is not None

        # 手動持久化（模擬 Verifier 呼叫 _save 的情境）
        semantic._save()

        # 模擬重啟
        reloaded = _reload_semantic(storage_path)
        reloaded_concept = reloaded.get(original_id)

        assert reloaded_concept is not None
        assert reloaded_concept.last_verified_at is not None, (
            "重啟後 last_verified_at 不應為 None"
        )
        # 比較 isoformat 字串避免 tzinfo 差異導致誤判
        assert reloaded_concept.last_verified_at.isoformat() == verified_at.isoformat(), (
            f"重啟後 last_verified_at 應與寫入時相同，"
            f"預期 {verified_at.isoformat()}，"
            f"得到 {reloaded_concept.last_verified_at.isoformat()}"
        )

    def test_last_verified_at_is_none_when_never_verified(
        self,
        semantic: SemanticMemory,
        storage_path: Path,
    ):
        """
        從未呼叫 record_verification() 的概念，
        重啟後 last_verified_at 應仍為 None（不應被意外賦值）。
        """
        _insert_concept_directly(semantic, "知識蒸餾", "以小模型模仿大模型輸出的訓練方式")

        reloaded = _reload_semantic(storage_path)
        reloaded_concept = reloaded.get_by_name("知識蒸餾")

        assert reloaded_concept is not None
        assert reloaded_concept.last_verified_at is None, (
            "從未驗證的概念，重啟後 last_verified_at 應為 None，"
            f"但得到 {reloaded_concept.last_verified_at}"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 測試群組 5：mutation_count 向後相容性
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestMutationCountBackwardCompatibility:
    """
    mutation_count 欄位的向後相容性。

    Concept.from_dict 有向後相容邏輯（舊資料可能沒有 health 欄位），
    確認從舊格式 JSON 反序列化時，mutation_count 預設為 0 而非崩潰。
    """

    def test_concept_from_dict_without_mutation_count(self):
        """
        若 JSON 資料沒有 mutation_count（舊版資料），
        from_dict 應正常反序列化，mutation_count 預設為 0。
        """
        old_format_data = {
            "id": "con_abc123",
            "name": "測試概念",
            "description": "這是一個舊格式的概念",
            "concept_type": "entity",
            "embedding": [],
            "source_episode_ids": [],
            "confidence": 0.5,
            "status": "active",
            "access_count": 0,
            "created_at": "2025-01-01T00:00:00+00:00",
            "updated_at": "2025-01-01T00:00:00+00:00",
            "metadata": {},
            # 故意省略 mutation_count / conflict_count / last_verified_at / source_diversity
        }

        concept = Concept.from_dict(old_format_data)

        assert concept.mutation_count == 0, (
            f"舊格式資料反序列化後 mutation_count 應預設為 0，得到 {concept.mutation_count}"
        )
        assert concept.conflict_count == 0
        assert concept.last_verified_at is None
        assert concept.source_diversity == 0

    def test_record_mutation_increments_correctly(self):
        """
        record_mutation() 應每次遞增 mutation_count 一次。
        """
        concept = Concept(name="遞增測試", description="測試 record_mutation")

        assert concept.mutation_count == 0

        concept.record_mutation()
        assert concept.mutation_count == 1

        concept.record_mutation()
        assert concept.mutation_count == 2

        concept.record_mutation()
        assert concept.mutation_count == 3

    def test_record_mutation_updates_updated_at(self):
        """
        record_mutation() 應同時更新 updated_at 時間戳。
        """
        import time

        concept = Concept(name="時間戳測試", description="測試 updated_at 更新")
        before = concept.updated_at

        # 稍微等一下確保時間戳不同
        time.sleep(0.01)
        concept.record_mutation()

        assert concept.updated_at > before, (
            "record_mutation() 應更新 updated_at"
        )
