"""
test_benchmark_quality.py - EchoMemory MVP 品質基準 pytest 測試

測試策略：
  - 不依賴 sentence-transformers（monkeypatch _generate_embedding）
  - 使用 4 維固定向量，確保跨類別隔離性可預測
  - 每個 scenario 使用獨立 tmp_path 隔離
  - 時間戳基準：T0 = datetime(2026, 1, 1, tzinfo=UTC)

執行方式：
    pytest tests/test_benchmark_quality.py -v
    pytest tests/test_benchmark_quality.py -v -k SC-01
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from echo_memory.engine import MemoryEngine
from echo_memory.schemas.episode import Episode
from tests.benchmark.run_quality import (
    compute_mrr,
    compute_precision_at_k,
    compute_recall_at_k,
)

# ─── 常數 ────────────────────────────────────────────────────────────────────

# 基準時間點（所有注入的 episode timestamp 均在此之前）
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

# benchmark_dataset.json 路徑
_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_DATASET_PATH = _FIXTURES_DIR / "benchmark_dataset.json"


# ─── Fixtures ────────────────────────────────────────────────────────────────
# benchmark_dataset 與 episodes_by_id 定義在 tests/conftest.py，此處直接引用。


def _build_episode(ep_dict: dict[str, Any]) -> Episode:
    """
    從 JSON dict 建構 Episode 物件。

    timestamp = T0 - timedelta(seconds=t_offset_seconds)
    embedding 直接採用 JSON 中的值（write() 會自動正規化）。
    """
    t_offset = ep_dict.get("t_offset_seconds", 0)
    timestamp = T0 - timedelta(seconds=t_offset)

    return Episode(
        id=ep_dict["id"],
        user_input=ep_dict["user_input"],
        agent_response=ep_dict["agent_response"],
        session_id=ep_dict["session_id"],
        content_summary=ep_dict["content_summary"],
        embedding=list(ep_dict["embedding"]),  # 預設非空，write() 會跳過生成
        salience_score=ep_dict.get("salience_score", 0.5),
        da_weight=ep_dict.get("da_weight", 0.5),
        ht_weight=ep_dict.get("ht_weight", 0.5),
        is_dreamed=ep_dict.get("is_dreamed", False),
        timestamp=timestamp,
        metadata=ep_dict.get("metadata", {}),
    )


def _make_engine(tmp_path: Path) -> MemoryEngine:
    """建立使用 tmp_path 的隔離 MemoryEngine"""
    return MemoryEngine(
        storage_path=tmp_path,
        embedding_model="all-MiniLM-L6-v2",
    )


def _inject_episodes(
    engine: MemoryEngine,
    episode_ids: list[str],
    episodes_by_id: dict[str, dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    注入指定的 episodes 到 engine，並 monkeypatch _generate_embedding。

    monkeypatch 策略：
    - episode 已帶有 embedding（非空），所以 write() 中的
      `if not episode.embedding:` 分支不會進入 _generate_embedding。
    - 仍需 patch retrieve 時 EcphoryRetrieval 呼叫的 _generate_embedding，
      此時回傳 query 固定向量（由呼叫端設定）。
    - 使用閉包（closure）讓 patch 在 retrieve 時返回正確的 query 向量。
    """
    # 先把所有 episode 寫入（embedding 已填好，不會觸發 _generate_embedding）
    for ep_id in episode_ids:
        ep_dict = episodes_by_id[ep_id]
        ep = _build_episode(ep_dict)
        engine.episodic.write(ep)


def _run_scenario(
    scenario: dict[str, Any],
    episodes_by_id: dict[str, dict],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """
    執行單一 scenario 並返回指標結果。

    Args:
        scenario: scenario 定義字典
        episodes_by_id: 所有 episode 的查詢表
        tmp_path: 隔離的臨時目錄
        monkeypatch: pytest monkeypatch fixture

    Returns:
        包含指標的結果字典
    """
    setup = scenario["setup"]
    query = scenario["query"]
    expected = scenario["expected"]

    episode_ids: list[str] = setup["episode_ids"]
    retrieval_params: dict[str, Any] = setup["retrieval_params"]
    top_k: int = retrieval_params.get("top_k", 10)
    min_similarity: float = retrieval_params.get("min_similarity", 0.3)

    query_embedding: list[float] = query["embedding"]

    # 建立隔離 engine
    engine = _make_engine(tmp_path)

    # 注入 episodes
    _inject_episodes(engine, episode_ids, episodes_by_id, monkeypatch)

    # Monkeypatch _generate_embedding：retrieve 時回傳固定的 query 向量
    # EcphoryRetrieval.retrieve() 呼叫 self.episodic._generate_embedding(query_text)
    def _fake_generate_embedding(text: str) -> list[float]:
        return list(query_embedding)

    monkeypatch.setattr(
        engine.episodic,
        "_generate_embedding",
        _fake_generate_embedding,
    )

    # 執行 retrieve
    context = engine.retrieve(
        query=query["text"],
        top_k=top_k,
        min_similarity=min_similarity,
    )

    # 提取結果 ID（依相似度排序，已由 EcphoryRetrieval 完成）
    retrieved_ids = [ep.id for ep in context.episodes]

    # 計算指標
    relevant_ids: list[str] = expected.get("relevant_ids", [])
    k = 3

    recall_at_3 = compute_recall_at_k(retrieved_ids, relevant_ids, k)
    precision_at_3 = compute_precision_at_k(retrieved_ids, relevant_ids, k)
    mrr = compute_mrr(retrieved_ids, relevant_ids)
    is_empty = context.is_empty()
    empty_expected = expected.get("empty_result_expected", False)

    # 判斷是否通過 MVP 門檻
    min_recall = expected.get("min_recall_at_3", 0.0)
    min_precision = expected.get("min_precision_at_3", 0.0)

    if empty_expected:
        # 應空的 scenario：通過條件是 is_empty == True
        passed = is_empty
    else:
        passed = (recall_at_3 >= min_recall) and (precision_at_3 >= min_precision)

    return {
        "scenario_id": scenario["id"],
        "name": scenario["name"],
        "retrieved_ids": retrieved_ids,
        "relevant_ids": relevant_ids,
        "recall_at_3": recall_at_3,
        "precision_at_3": precision_at_3,
        "mrr": mrr,
        "is_empty": is_empty,
        "empty_expected": empty_expected,
        "passed": passed,
        "context": context,
        "engine": engine,
    }


# ─── 參數化測試 ───────────────────────────────────────────────────────────────

def _load_scenarios() -> list[tuple[str, dict]]:
    """載入所有 scenarios（供 parametrize 使用）"""
    if not _DATASET_PATH.exists():
        return []
    with open(_DATASET_PATH, "r", encoding="utf-8") as f:
        dataset = json.load(f)
    return [(sc["id"], sc) for sc in dataset.get("scenarios", [])]


@pytest.mark.parametrize(
    "scenario_id,scenario",
    _load_scenarios(),
    ids=[sc_id for sc_id, _ in _load_scenarios()],
)
def test_scenario_quality(
    scenario_id: str,
    scenario: dict[str, Any],
    episodes_by_id: dict[str, dict],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    參數化品質測試：每個 scenario 獨立執行並驗證指標。

    測試流程：
    1. 依 scenario.setup.episode_ids 建立隔離 MemoryEngine
    2. 注入 episodes（帶預設 embedding，跳過 _generate_embedding）
    3. Monkeypatch _generate_embedding 回傳固定 query 向量
    4. 呼叫 engine.retrieve()
    5. 計算 Recall@3、Precision@3、MRR
    6. 驗證結果符合 expected 門檻
    """
    result = _run_scenario(scenario, episodes_by_id, tmp_path, monkeypatch)

    expected = scenario["expected"]
    empty_expected = expected.get("empty_result_expected", False)

    if empty_expected:
        # 應空 scenario：嚴格要求返回空結果
        assert result["is_empty"], (
            f"[{scenario_id}] 預期空結果，但實際返回 {result['retrieved_ids']}"
        )
    else:
        min_recall = expected.get("min_recall_at_3", 0.0)
        min_precision = expected.get("min_precision_at_3", 0.0)

        assert result["recall_at_3"] >= min_recall, (
            f"[{scenario_id}] Recall@3 不足：{result['recall_at_3']:.3f} < {min_recall}"
            f"\n  retrieved: {result['retrieved_ids']}"
            f"\n  relevant:  {result['relevant_ids']}"
        )
        assert result["precision_at_3"] >= min_precision, (
            f"[{scenario_id}] Precision@3 不足：{result['precision_at_3']:.3f} < {min_precision}"
            f"\n  retrieved: {result['retrieved_ids']}"
            f"\n  relevant:  {result['relevant_ids']}"
        )


# ─── 特化斷言：時間戳基準 (SC-08) ────────────────────────────────────────────

def test_sc08_timestamps_before_t0(
    episodes_by_id: dict[str, dict],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    SC-08：驗證所有注入的 episodes 的 timestamp 均在 T0 之前。

    t_offset_seconds > 0 → timestamp = T0 - timedelta(seconds=offset) < T0。
    """
    episode_ids = ["ep_food_001", "ep_food_002", "ep_food_003"]

    engine = _make_engine(tmp_path)

    for ep_id in episode_ids:
        ep_dict = episodes_by_id[ep_id]
        ep = _build_episode(ep_dict)
        engine.episodic.write(ep)

    for ep_id in episode_ids:
        ep = engine.episodic.get(ep_id)
        assert ep is not None, f"Episode {ep_id} 未找到"

        ts = ep.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        assert ts < T0, (
            f"Episode {ep_id} 的 timestamp {ts} 應早於 T0 {T0}"
        )


# ─── 特化斷言：圖譜不跨 session (SC-09) ─────────────────────────────────────

def test_sc09_no_cross_session_graph_edges(
    episodes_by_id: dict[str, dict],
    tmp_path: Path,
) -> None:
    """
    SC-09：驗證 EpisodicMemory 不在不同 session 的 episodes 之間建立 PRECEDES 邊。

    ep_graph_A 和 ep_graph_B 屬於 session_graph_test，
    ep_diff_session 屬於 session_other。
    注入順序：A → B → diff_session，驗證圖中 B→diff_session 之間沒有邊。
    """
    episode_ids = ["ep_graph_A", "ep_graph_B", "ep_diff_session"]

    engine = _make_engine(tmp_path)

    for ep_id in episode_ids:
        ep_dict = episodes_by_id[ep_id]
        ep = _build_episode(ep_dict)
        engine.episodic.write(ep)

    graph = engine.episodic.graph.graph

    # 驗證同 session 之間有邊（ep_graph_A → ep_graph_B 或反向）
    has_intra_session_edge = (
        graph.has_edge("ep_graph_A", "ep_graph_B")
        or graph.has_edge("ep_graph_B", "ep_graph_A")
    )
    assert has_intra_session_edge, (
        "同 session 的 ep_graph_A 和 ep_graph_B 之間應有 PRECEDES 邊"
    )

    # 驗證不同 session 之間沒有邊（ep_graph_B 和 ep_diff_session）
    has_cross_session_edge = (
        graph.has_edge("ep_graph_B", "ep_diff_session")
        or graph.has_edge("ep_diff_session", "ep_graph_B")
        or graph.has_edge("ep_graph_A", "ep_diff_session")
        or graph.has_edge("ep_diff_session", "ep_graph_A")
    )
    assert not has_cross_session_edge, (
        "不同 session 的 episodes 之間不應有 PRECEDES 邊"
    )


# ─── 空庫邊界測試 (SC-06) ────────────────────────────────────────────────────

def test_sc06_empty_db_no_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    SC-06：空庫邊界——對空的 MemoryEngine 執行 retrieve 不應拋出例外。
    """
    engine = _make_engine(tmp_path)

    query_embedding = [0.7071, 0.7071, 0.0, 0.0]

    def _fake_generate_embedding(text: str) -> list[float]:
        return list(query_embedding)

    monkeypatch.setattr(
        engine.episodic,
        "_generate_embedding",
        _fake_generate_embedding,
    )

    # 不應拋出任何例外
    context = engine.retrieve(query="任何查詢", top_k=5, min_similarity=0.3)

    assert context.is_empty(), "空庫 retrieve 應返回空的 MemoryContext"
    assert context.episodes == [], "空庫 retrieve 不應有任何 episodes"


# ─── 指標函式單元測試 ─────────────────────────────────────────────────────────

class TestQualityMetrics:
    """品質指標計算函式的單元測試"""

    def test_recall_at_k_full_hit(self) -> None:
        """全部命中應返回 1.0"""
        assert compute_recall_at_k(["a", "b", "c"], ["a", "b", "c"], 3) == 1.0

    def test_recall_at_k_partial_hit(self) -> None:
        """部分命中"""
        result = compute_recall_at_k(["a", "x", "y"], ["a", "b", "c"], 3)
        assert abs(result - 1 / 3) < 1e-9

    def test_recall_at_k_no_hit(self) -> None:
        """完全未命中應返回 0.0"""
        assert compute_recall_at_k(["x", "y", "z"], ["a", "b", "c"], 3) == 0.0

    def test_recall_at_k_empty_relevant(self) -> None:
        """relevant_ids 為空應返回 1.0（無應召回的目標）"""
        assert compute_recall_at_k(["a", "b"], [], 3) == 1.0

    def test_precision_at_k_full_hit(self) -> None:
        """全部命中應返回 1.0"""
        assert compute_precision_at_k(["a", "b", "c"], ["a", "b", "c"], 3) == 1.0

    def test_precision_at_k_partial_hit(self) -> None:
        """部分命中"""
        result = compute_precision_at_k(["a", "x", "y"], ["a", "b"], 3)
        assert abs(result - 1 / 3) < 1e-9

    def test_precision_at_k_empty_retrieved(self) -> None:
        """retrieved_ids 為空應返回 0.0"""
        assert compute_precision_at_k([], ["a", "b"], 3) == 0.0

    def test_mrr_first_hit(self) -> None:
        """第一個就命中應返回 1.0"""
        assert compute_mrr(["a", "b", "c"], ["a"]) == 1.0

    def test_mrr_second_hit(self) -> None:
        """第二個命中應返回 0.5"""
        assert compute_mrr(["x", "a", "b"], ["a"]) == 0.5

    def test_mrr_no_hit(self) -> None:
        """完全未命中應返回 0.0"""
        assert compute_mrr(["x", "y", "z"], ["a"]) == 0.0

    def test_recall_k_boundary(self) -> None:
        """k 超過列表長度時只計算實際有的結果"""
        result = compute_recall_at_k(["a"], ["a", "b", "c"], 10)
        assert abs(result - 1 / 3) < 1e-9
