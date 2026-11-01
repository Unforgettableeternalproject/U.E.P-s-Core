"""
M-7：蒸餾依來源重疊建立 Concept↔Concept RELATES_TO。

驗收：
  1. 同批概念來源重疊 → 建 RELATES_TO（雙向，權重 = base + (1-base)·Jaccard）
  2. 與既存（非本批）概念來源重疊 → 同樣建邊
  3. Jaccard 低於下限不建；不重疊不建
  4. 走 DreamPatchCoordinator audit-before-mutate；audit 失敗則不建邊
  5. 已有 CONTRADICTS 的配對不被覆寫；既有 LATENT_BRIDGE 升級為 RELATES_TO
  6. 建出的邊權重能被 ecphory 的 1-hop 擴散門檻展開
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from echo_memory.affect.concept_inhibitor import ConceptInhibitor
from echo_memory.config import get_config
from echo_memory.dream.distillation import KnowledgeDistillation
from echo_memory.dream.patch_coordinator import DreamPatchCoordinator
from echo_memory.retrieval.ecphory import EcphoryRetrieval
from echo_memory.schemas.episode import Episode
from echo_memory.storage.episodic import EpisodicMemory
from echo_memory.storage.semantic import SemanticMemory

DIM = 512


def _embed(text: str) -> list[float]:
    import hashlib
    seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "little")
    vec = np.random.RandomState(seed).randn(DIM).astype(np.float32)
    vec /= float(np.linalg.norm(vec))
    return vec.tolist()


def _one_hot(i: int) -> list[float]:
    v = [0.0] * DIM
    v[i] = 1.0
    return v


@pytest.fixture
def stores(tmp_path: Path):
    (tmp_path / "ep").mkdir()
    (tmp_path / "sem").mkdir()
    episodic = EpisodicMemory(storage_path=tmp_path / "ep")
    semantic = SemanticMemory(storage_path=tmp_path / "sem", embed_fn=_embed)
    return episodic, semantic, episodic.graph


@pytest.fixture
def coordinator(stores, tmp_path: Path):
    _, semantic, _ = stores
    coord = DreamPatchCoordinator(
        inhibitor=ConceptInhibitor(semantic=semantic, llm_fn=None),
        audit_log_path=tmp_path / "audit.jsonl",
    )
    coord.begin_cycle("test")
    return coord


def _episodes(episodic: EpisodicMemory, n: int = 4) -> list[Episode]:
    eps = []
    for i in range(n):
        ep = Episode(
            user_input=f"對話 {i}", agent_response=f"回覆 {i}", session_id="t",
            content_summary=f"對話 {i}", salience_score=0.8,
        )
        episodic.write(ep)
        eps.append(ep)
    return eps


def _llm(concepts: list[dict]):
    return lambda prompt: json.dumps(concepts, ensure_ascii=False)


def _distiller(stores, llm, coordinator=None) -> KnowledgeDistillation:
    episodic, semantic, graph = stores
    return KnowledgeDistillation(
        episodic=episodic, semantic=semantic, graph=graph,
        llm_fn=llm, patch_coordinator=coordinator,
    )


def _two_overlapping_concepts() -> list[dict]:
    # A 來源 {0,1}、B 來源 {1,2}：交集 1 / 聯集 3 = 1/3
    return [
        {"name": "概念A", "description": "A", "concept_type": "pattern", "source_indices": [0, 1]},
        {"name": "概念B", "description": "B", "concept_type": "pattern", "source_indices": [1, 2]},
        {"name": "概念C", "description": "C", "concept_type": "pattern", "source_indices": [3]},
    ]


def _relates(graph, a: str, b: str):
    if not graph.graph.has_edge(a, b):
        return None
    data = graph.graph[a][b]
    return data if data.get("edge_type") == "RELATES_TO" else None


@pytest.mark.parametrize("use_coordinator", [False, True])
def test_same_batch_overlap_creates_relates_to(stores, coordinator, use_coordinator):
    episodic, semantic, graph = stores
    _episodes(episodic)
    report = _distiller(
        stores, _llm(_two_overlapping_concepts()),
        coordinator if use_coordinator else None,
    ).run()

    a, b, c = (semantic.get_by_name(n) for n in ("概念A", "概念B", "概念C"))
    base = get_config().relates_to_weight_base
    expected = base + (1 - base) * (1 / 3)

    for src, dst in ((a.id, b.id), (b.id, a.id)):
        edge = _relates(graph, src, dst)
        assert edge is not None
        assert edge["weight"] == pytest.approx(expected)
        assert edge["jaccard"] == pytest.approx(1 / 3)
    # 與不重疊的 C 不建邊
    assert _relates(graph, a.id, c.id) is None
    assert _relates(graph, b.id, c.id) is None
    assert report.relates_to_edges_created == 1


def test_overlap_with_preexisting_concept(stores):
    episodic, semantic, graph = stores
    eps = _episodes(episodic)
    # 既存概念（非本批）來源 {0,1}
    old_id = semantic.upsert_concept("既存概念", "舊", "pattern", source_episode_id=eps[0].id)
    semantic.upsert_concept("既存概念", "舊", "pattern", source_episode_id=eps[1].id)
    graph.add_concept_node(old_id, "既存概念", "pattern")

    # 本批新概念來源 {1,2} → J = 1/3
    _distiller(stores, _llm([
        {"name": "新概念", "description": "新", "concept_type": "pattern", "source_indices": [1, 2]},
    ])).run()

    new = semantic.get_by_name("新概念")
    assert _relates(graph, new.id, old_id) is not None
    assert _relates(graph, old_id, new.id) is not None


def test_below_min_jaccard_creates_no_edge(stores, monkeypatch):
    episodic, semantic, graph = stores
    _episodes(episodic)
    monkeypatch.setattr(get_config(), "relates_to_min_jaccard", 0.5)
    report = _distiller(stores, _llm(_two_overlapping_concepts())).run()  # J=1/3 < 0.5

    a, b = semantic.get_by_name("概念A"), semantic.get_by_name("概念B")
    assert _relates(graph, a.id, b.id) is None
    assert report.relates_to_edges_created == 0


def test_audit_written_before_mutation(stores, coordinator, tmp_path):
    episodic, _, _ = stores
    _episodes(episodic)
    _distiller(stores, _llm(_two_overlapping_concepts()), coordinator).run()

    entries = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    ops = [e for e in entries if e["operation"] == "add_relates_to_edges"]
    assert len(ops) == 1
    assert ops[0]["count"] == 1
    assert ops[0]["sample_edges"][0]["jaccard"] == pytest.approx(1 / 3, abs=1e-3)


def test_audit_failure_blocks_relates_to(stores, coordinator, monkeypatch):
    episodic, semantic, graph = stores
    _episodes(episodic)
    monkeypatch.setattr(coordinator, "audit_graph_relates_to", lambda *a, **k: False)
    report = _distiller(stores, _llm(_two_overlapping_concepts()), coordinator).run()

    a, b = semantic.get_by_name("概念A"), semantic.get_by_name("概念B")
    assert _relates(graph, a.id, b.id) is None
    assert report.relates_to_edges_created == 0
    # 概念與 DERIVED_FROM 不受影響
    assert report.concepts_created == 3


def test_contradicts_pair_not_overwritten(stores):
    episodic, semantic, graph = stores
    eps = _episodes(episodic)
    old_id = semantic.upsert_concept("既存概念", "舊", "pattern", source_episode_id=eps[1].id)
    graph.add_concept_node(old_id, "既存概念", "pattern")

    # 先讓新概念存在，建立 CONTRADICTS，再重跑一次蒸餾
    llm = _llm([
        {"name": "新概念", "description": "新", "concept_type": "pattern", "source_indices": [1, 2]},
    ])
    _distiller(stores, llm).run()
    new = semantic.get_by_name("新概念")
    # 把原本的 RELATES_TO 換成 CONTRADICTS（模擬矛盾偵測結果）
    graph.graph.remove_edge(new.id, old_id)
    graph.graph.remove_edge(old_id, new.id)
    graph.add_contradicts_edge(new.id, old_id, 0.9)

    assert graph.upsert_overlap_relates_to(new.id, old_id, 0.9, 0.9) is False
    assert graph.graph[new.id][old_id]["edge_type"] == "CONTRADICTS"
    assert graph.graph[old_id][new.id]["edge_type"] == "CONTRADICTS"


def test_latent_bridge_upgraded_to_relates_to(stores):
    _, semantic, graph = stores
    a = semantic.upsert_concept("甲", "甲", "pattern", embedding=_one_hot(0))
    b = semantic.upsert_concept("乙", "乙", "pattern", embedding=_one_hot(0))
    graph.add_concept_node(a, "甲", "pattern")
    graph.add_concept_node(b, "乙", "pattern")
    for s, d in ((a, b), (b, a)):
        graph.graph.add_edge(s, d, edge_type="LATENT_BRIDGE", weight=0.9, created_at=1.0)

    assert graph.upsert_overlap_relates_to(a, b, 0.7, 0.5) is True
    assert graph.graph[a][b]["edge_type"] == "RELATES_TO"
    assert graph.graph[b][a]["edge_type"] == "RELATES_TO"
    # 再次呼叫為更新而非新增
    assert graph.upsert_overlap_relates_to(a, b, 0.8, 0.6) is False
    assert graph.graph[a][b]["weight"] == pytest.approx(0.8)


def test_distilled_relates_to_expands_in_ecphory(stores):
    """蒸餾出的邊權重要能跨過 ecphory 擴散門檻（DA 0.5 → 0.35）。"""
    episodic, semantic, graph = stores
    _episodes(episodic)
    _distiller(stores, _llm(_two_overlapping_concepts())).run()

    a, b = semantic.get_by_name("概念A"), semantic.get_by_name("概念B")
    # A 的 embedding 設成與 B 不同的 one-hot，查詢只命中 A
    for cid, i in ((a.id, 1), (b.id, 2)):
        semantic.get(cid).embedding = _one_hot(i)
    semantic._rebuild_embeddings()

    ecphory = EcphoryRetrieval(episodic, semantic)
    concepts, scores = ecphory._retrieve_concepts(_one_hot(1), top_k=5, dopamine=0.5)
    ids = {c.id for c in concepts}
    assert a.id in ids
    assert b.id in ids  # 經 RELATES_TO 1-hop 展開
