"""
M3 審查修正（二）：已合併概念（status != active / metadata.merged_into）不得被當 active 使用。

涵蓋：矛盾偵測不配對、LATENT_BRIDGE 不建邊且不生裸節點、過期清除裸節點、
ecphory 鄰居展開直接略過、蒸餾不補回圖譜節點、蒸餾無 coordinator 時改走降級 Gate。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from echo_memory.dream.contradiction import ContradictionDetector
from echo_memory.dream.distillation import KnowledgeDistillation
from echo_memory.dream.pruning import GraphPruning
from echo_memory.retrieval.ecphory import EcphoryRetrieval
from echo_memory.schemas.episode import Episode
from echo_memory.storage.episodic import EpisodicMemory
from echo_memory.storage.semantic import SemanticMemory

DIM = 512


def _one_hot(i: int) -> list[float]:
    v = [0.0] * DIM
    v[i] = 1.0
    return v


@pytest.fixture
def env(tmp_path: Path):
    """A（active）、B（active）、M（已合併進 A，圖譜節點已摘除）；三者向量相同"""
    (tmp_path / "ep").mkdir()
    (tmp_path / "sem").mkdir()
    episodic = EpisodicMemory(storage_path=tmp_path / "ep")
    semantic = SemanticMemory(storage_path=tmp_path / "sem", embed_fn=lambda t: _one_hot(99))
    graph = episodic.graph
    ids = {}
    for name in ("A", "B", "M"):
        cid = semantic.upsert_concept(
            name, f"概念 {name}", "pattern", source_episode_id=f"ep_{name}",
            embedding=_one_hot(0),
        )
        ids[name] = cid
        if name != "M":
            graph.add_concept_node(cid, name, "pattern")
    merged = semantic.get(ids["M"])
    merged.status = "deprecated"
    merged.metadata["merged_into"] = ids["A"]
    return episodic, semantic, graph, ids


def test_contradiction_does_not_pair_merged(env):
    _, semantic, graph, ids = env
    det = ContradictionDetector(semantic=semantic, graph=graph, llm_fn=lambda p: "{}")
    a, m = semantic.get(ids["A"]), semantic.get(ids["M"])
    assert ids["M"] not in {c.id for c in det._get_all_concepts()}
    assert det._filter_candidates(a, [m]) == []
    assert det.detect_and_register(m) == []


def test_contradiction_does_not_restore_merged_node(env):
    _, semantic, graph, ids = env
    det = ContradictionDetector(semantic=semantic, graph=graph, llm_fn=lambda p: "{}")
    det._ensure_concept_node(ids["M"])
    assert not graph.has_node(ids["M"])


def test_bridge_skips_merged_and_makes_no_bare_node(env):
    episodic, semantic, graph, ids = env
    GraphPruning(graph, semantic=semantic)._create_latent_bridges()
    assert not graph.graph.has_node(ids["M"])
    assert graph.graph.has_edge(ids["A"], ids["B"])  # active 之間仍正常建橋
    assert all(graph.graph.nodes[n] for n in graph.graph.nodes)  # 無缺屬性裸節點


def test_bridge_requires_existing_nodes(env):
    _, semantic, graph, ids = env
    graph.graph.remove_node(ids["B"])  # B 沒有圖譜節點
    GraphPruning(graph, semantic=semantic)._create_latent_bridges()
    assert not graph.graph.has_node(ids["B"])
    assert graph.graph.number_of_edges() == 0


def test_expire_removes_bare_bridge_residue_only(env):
    _, semantic, graph, ids = env
    now = time.time()
    # 既有的 bridge 殘留：M 端是 add_edge 生出的裸節點
    graph.graph.add_edge(ids["A"], ids["M"], edge_type="LATENT_BRIDGE", weight=0.9,
                         created_at=now, last_confirmed_at=now)
    graph.graph.add_edge(ids["M"], ids["A"], edge_type="LATENT_BRIDGE", weight=0.9,
                         created_at=now, last_confirmed_at=now)
    assert graph.graph.has_node(ids["M"]) and not graph.graph.nodes[ids["M"]]
    GraphPruning(graph, semantic=semantic)._expire_latent_bridges()
    assert not graph.graph.has_node(ids["M"])
    assert graph.graph.has_node(ids["A"]) and graph.graph.nodes[ids["A"]]  # 正常節點保留


def test_ecphory_skips_merged_neighbor(tmp_path: Path):
    (tmp_path / "ep").mkdir()
    (tmp_path / "sem").mkdir()
    episodic = EpisodicMemory(storage_path=tmp_path / "ep")
    semantic = SemanticMemory(storage_path=tmp_path / "sem", embed_fn=lambda t: _one_hot(99))
    graph = episodic.graph
    ids = {}
    for name, idx in (("A", 0), ("M", 1)):
        ids[name] = semantic.upsert_concept(
            name, f"概念 {name}", "pattern", source_episode_id=f"ep_{name}",
            embedding=_one_hot(idx),
        )
        graph.add_concept_node(ids[name], name, "pattern")
    # A 以強邊連到 M；M 已合併（最壞情況：圖譜節點仍在）
    graph.graph.add_edge(ids["A"], ids["M"], edge_type="RELATES_TO", weight=1.0)
    graph.graph.add_edge(ids["M"], ids["A"], edge_type="RELATES_TO", weight=1.0)
    m = semantic.get(ids["M"])
    m.status = "deprecated"
    m.metadata["merged_into"] = ids["A"]
    concepts, _ = EcphoryRetrieval(episodic, semantic)._retrieve_concepts(
        _one_hot(0), top_k=5, min_similarity=0.01, dopamine=0.5,
    )
    got = {c.id for c in concepts}
    assert ids["A"] in got
    assert ids["M"] not in got


class _StubCoordinator:
    def __init__(self, concept_id):
        self._id = concept_id

    def submit_distillation_concept(self, **kwargs):
        return self._id, object()

    def audit_graph_derived_from(self, *a, **k):
        return True

    def audit_graph_relates_to(self, *a, **k):
        return True


def test_distillation_does_not_restore_merged_node(env):
    episodic, semantic, graph, ids = env
    ep = Episode(user_input="x", agent_response="y", session_id="t",
                 content_summary="x", salience_score=0.8)
    episodic.write(ep)
    distiller = KnowledgeDistillation(
        episodic=episodic, semantic=semantic, graph=graph, llm_fn=None,
        patch_coordinator=_StubCoordinator(ids["M"]),
    )
    out = distiller._write_concept_locked(
        {"name": "M", "description": "d", "source_indices": [0]}, [ep], None,
    )
    assert out is not None
    assert not graph.has_node(ids["M"])


def test_distillation_without_coordinator_uses_degraded_gate(env):
    episodic, semantic, graph, _ = env
    distiller = KnowledgeDistillation(
        episodic=episodic, semantic=semantic, graph=graph, llm_fn=None,
    )
    assert distiller._patch_coordinator is not None
    assert distiller._patch_coordinator._inhibitor is not None
