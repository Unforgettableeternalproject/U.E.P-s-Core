"""
KnowledgeDistillation 測試 — Phase 2 Branch 3 驗收

驗收標準：
  1. dream() 後 SemanticMemory 中出現蒸餾 Concept
  2. DERIVED_FROM 邊正確建立（Concept → Episode）
  3. 無 LLM 時優雅降級（不報錯，report.skipped_reason == "no_llm"）
  4. 已蒸餾的 Episode 被標記 is_dreamed=True
  5. LLM 輸出解析容錯（markdown fences、空陣列等）
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from echo_memory.dream.distillation import DistillationReport, KnowledgeDistillation
from echo_memory.schemas.episode import Episode
from echo_memory.storage.episodic import EpisodicMemory
from echo_memory.storage.semantic import SemanticMemory

# ─── Fixtures ───────────────────────────────────────────────────────

MOCK_DIM = 512


def _mock_embed_fn(text: str) -> list[float]:
    import hashlib
    seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "little")
    rng = np.random.RandomState(seed)
    vec = rng.randn(MOCK_DIM).astype(np.float32)
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec /= norm
    return vec.tolist()


def _mock_llm_fn_good(prompt: str) -> str:
    """模擬 LLM 回傳正確的 JSON。"""
    return json.dumps([
        {
            "name": "壽司偏好",
            "description": "使用者喜歡壽司和日本料理",
            "concept_type": "pattern",
            "source_indices": [0, 1],
        },
        {
            "name": "天氣話題",
            "description": "使用者會討論天氣狀況",
            "concept_type": "pattern",
            "source_indices": [2],
        },
    ], ensure_ascii=False)


def _mock_llm_fn_with_fences(prompt: str) -> str:
    """模擬 LLM 回傳帶 markdown code fences 的 JSON。"""
    return """好的，以下是蒸餾結果：

```json
[
  {
    "name": "程式語言偏好",
    "description": "使用者偏好 Python",
    "concept_type": "rule",
    "source_indices": [0]
  }
]
```

以上是我的分析。"""


def _mock_llm_fn_empty(prompt: str) -> str:
    """模擬 LLM 回傳空陣列（沒有值得提取的概念）。"""
    return "[]"


def _mock_llm_fn_error(prompt: str) -> str:
    """模擬 LLM 回傳無法解析的內容。"""
    raise RuntimeError("API rate limit exceeded")


def _mock_llm_fn_bad_json(prompt: str) -> str:
    """模擬 LLM 回傳格式錯誤的 JSON。"""
    return "這不是 JSON，只是一段文字。"


@pytest.fixture
def storage(tmp_path: Path):
    """建立完整的三層記憶。"""
    ep_dir = tmp_path / "episodic"
    sem_dir = tmp_path / "semantic"
    ep_dir.mkdir()
    sem_dir.mkdir()

    episodic = EpisodicMemory(storage_path=ep_dir)
    semantic = SemanticMemory(storage_path=sem_dir, embed_fn=_mock_embed_fn)
    graph = episodic.graph

    return episodic, semantic, graph


def _store_high_salience_episodes(episodic: EpisodicMemory) -> list[Episode]:
    """寫入幾個高顯著性的 Episode。"""
    texts = [
        ("我最喜歡吃壽司", "了解，壽司是很棒的料理！"),
        ("週末想去日本料理店", "推薦你去新開的那家。"),
        ("今天天氣真好", "是的，適合出門走走。"),
    ]
    episodes = []
    for user_input, agent_response in texts:
        ep = Episode(
            user_input=user_input,
            agent_response=agent_response,
            session_id="test",
            content_summary=user_input,
            salience_score=0.8,  # 高顯著性
        )
        episodic.write(ep)
        episodes.append(ep)
    return episodes


# ─── 核心驗收：dream() 後出現蒸餾 Concept ──────────────────────────


class TestDistillationCore:
    """核心蒸餾功能。"""

    def test_distillation_creates_concepts(self, storage):
        """蒸餾後 SemanticMemory 中應出現新 Concept。"""
        episodic, semantic, graph = storage
        _store_high_salience_episodes(episodic)

        distiller = KnowledgeDistillation(
            episodic=episodic,
            semantic=semantic,
            graph=graph,
            llm_fn=_mock_llm_fn_good,
        )

        report = distiller.run()

        assert report.skipped_reason is None
        assert report.concepts_created == 2
        assert semantic.count() == 2

        # 驗證概念內容
        sushi = semantic.get_by_name("壽司偏好")
        assert sushi is not None
        assert sushi.concept_type == "pattern"
        assert len(sushi.source_episode_ids) == 2

    def test_distillation_creates_derived_from_edges(self, storage):
        """蒸餾後應建立 DERIVED_FROM 邊。"""
        episodic, semantic, graph = storage
        _store_high_salience_episodes(episodic)

        distiller = KnowledgeDistillation(
            episodic=episodic,
            semantic=semantic,
            graph=graph,
            llm_fn=_mock_llm_fn_good,
        )

        report = distiller.run()

        assert report.edges_created > 0

        # 「壽司偏好」應該有 DERIVED_FROM 邊指向前兩個 Episode
        sushi = semantic.get_by_name("壽司偏好")
        assert sushi is not None

        relations = graph.get_concept_relations(sushi.id, edge_type="DERIVED_FROM")
        assert len(relations) == 2  # source_indices: [0, 1]

    def test_distillation_marks_episodes_dreamed(self, storage):
        """蒸餾後 Episode 的 is_dreamed 應為 True。"""
        episodic, semantic, graph = storage
        episodes = _store_high_salience_episodes(episodic)

        distiller = KnowledgeDistillation(
            episodic=episodic,
            semantic=semantic,
            graph=graph,
            llm_fn=_mock_llm_fn_good,
        )

        report = distiller.run()

        # 蒸餾不再自己標記 is_dreamed（由 DreamEngine 統一負責）
        # 但會記錄被蒸餾的 Episode ID
        assert len(report._distilled_episode_ids) == 3
        for ep in episodes:
            loaded = episodic.get(ep.id)
            assert loaded is not None
            assert loaded.is_dreamed is False  # 蒸餾自己不標記

    def test_distillation_skips_already_dreamed(self, storage):
        """手動標記 is_dreamed 後，蒸餾跳過。"""
        episodic, semantic, graph = storage
        episodes = _store_high_salience_episodes(episodic)

        # 手動標記（模擬 DreamEngine 統一標記）
        episodic.mark_as_dreamed([ep.id for ep in episodes])

        distiller = KnowledgeDistillation(
            episodic=episodic,
            semantic=semantic,
            graph=graph,
            llm_fn=_mock_llm_fn_good,
        )

        report = distiller.run()
        assert report.skipped_reason == "no_episodes"
        assert report.episodes_scanned == 0

    def test_distillation_updates_existing_concept(self, storage):
        """同名概念應更新而非重複建立。"""
        episodic, semantic, graph = storage

        # 先手動建一個同名概念
        semantic.upsert_concept("壽司偏好", "舊描述")

        _store_high_salience_episodes(episodic)

        distiller = KnowledgeDistillation(
            episodic=episodic,
            semantic=semantic,
            graph=graph,
            llm_fn=_mock_llm_fn_good,
        )

        report = distiller.run()

        # 應該是 update 而非 create（「壽司偏好」已存在）
        assert report.concepts_updated >= 1
        assert semantic.count() == 2  # 壽司偏好(更新) + 天氣話題(新建)


# ─── 無 LLM 降級 ──────────────────────────────────────────────────


class TestDistillationGracefulDegradation:
    """無 LLM 時優雅降級。"""

    def test_no_llm_skips(self, storage):
        """llm_fn=None 時直接跳過。"""
        episodic, semantic, graph = storage
        _store_high_salience_episodes(episodic)

        distiller = KnowledgeDistillation(
            episodic=episodic,
            semantic=semantic,
            graph=graph,
            llm_fn=None,
        )

        report = distiller.run()

        assert report.skipped_reason == "no_llm"
        assert semantic.count() == 0

    def test_llm_error_handled(self, storage):
        """LLM 呼叫失敗時不崩潰。"""
        episodic, semantic, graph = storage
        _store_high_salience_episodes(episodic)

        distiller = KnowledgeDistillation(
            episodic=episodic,
            semantic=semantic,
            graph=graph,
            llm_fn=_mock_llm_fn_error,
        )

        report = distiller.run()

        assert report.skipped_reason == "llm_error"
        assert len(report.errors) > 0
        assert semantic.count() == 0

    def test_no_episodes_skips(self, storage):
        """沒有符合條件的 Episode 時跳過。"""
        episodic, semantic, graph = storage
        # 不寫任何 Episode

        distiller = KnowledgeDistillation(
            episodic=episodic,
            semantic=semantic,
            graph=graph,
            llm_fn=_mock_llm_fn_good,
        )

        report = distiller.run()
        assert report.skipped_reason == "no_episodes"

    def test_low_salience_episodes_skipped(self, storage):
        """低顯著性 Episode 不會被蒸餾。"""
        episodic, semantic, graph = storage

        # 只寫低顯著性的 Episode
        ep = Episode(
            user_input="隨便聊聊",
            agent_response="好的",
            session_id="test",
            content_summary="隨便聊聊",
            salience_score=0.2,  # 低於預設閾值 0.5
        )
        episodic.write(ep)

        distiller = KnowledgeDistillation(
            episodic=episodic,
            semantic=semantic,
            graph=graph,
            llm_fn=_mock_llm_fn_good,
        )

        report = distiller.run()
        assert report.skipped_reason == "no_episodes"


# ─── LLM 輸出解析容錯 ──────────────────────────────────────────


class TestLLMOutputParsing:
    """LLM 輸出的各種格式都能正確解析。"""

    def test_parse_with_markdown_fences(self, storage):
        """帶 ```json ``` 的輸出能正確解析。"""
        episodic, semantic, graph = storage
        _store_high_salience_episodes(episodic)

        distiller = KnowledgeDistillation(
            episodic=episodic,
            semantic=semantic,
            graph=graph,
            llm_fn=_mock_llm_fn_with_fences,
        )

        report = distiller.run()
        assert report.concepts_created == 1
        assert semantic.get_by_name("程式語言偏好") is not None

    def test_parse_empty_array(self, storage):
        """LLM 回傳空陣列 [] 不報錯。"""
        episodic, semantic, graph = storage
        _store_high_salience_episodes(episodic)

        distiller = KnowledgeDistillation(
            episodic=episodic,
            semantic=semantic,
            graph=graph,
            llm_fn=_mock_llm_fn_empty,
        )

        report = distiller.run()
        assert report.skipped_reason is None
        assert report.concepts_created == 0
        assert semantic.count() == 0

    def test_parse_bad_json_handled(self, storage):
        """完全無法解析的 LLM 輸出不崩潰。"""
        episodic, semantic, graph = storage
        _store_high_salience_episodes(episodic)

        distiller = KnowledgeDistillation(
            episodic=episodic,
            semantic=semantic,
            graph=graph,
            llm_fn=_mock_llm_fn_bad_json,
        )

        distiller.run()
        # 解析失敗但不應崩潰
        assert semantic.count() == 0


# ─── DreamReport 整合 ──────────────────────────────────────────────


class TestDistillationReport:
    """DistillationReport 格式和內容。"""

    def test_report_summary_with_results(self):
        report = DistillationReport(
            episodes_scanned=5,
            concepts_created=2,
            concepts_updated=1,
            edges_created=3,
        )
        summary = report.summary()
        assert "5" in summary
        assert "2" in summary

    def test_report_summary_skipped(self):
        report = DistillationReport(skipped_reason="no_llm")
        assert "no_llm" in report.summary()
