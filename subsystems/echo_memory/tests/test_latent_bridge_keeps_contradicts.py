"""LATENT_BRIDGE 不得覆寫既有的 CONTRADICTS 邊（高相似度矛盾配對）。"""

from pathlib import Path

from echo_memory.dream.patch_coordinator import DreamPatchCoordinator
from echo_memory.dream.pruning import GraphPruning
from echo_memory.storage.graph import MemoryGraph
from echo_memory.storage.semantic import SemanticMemory


def test_latent_bridge_does_not_overwrite_contradicts(tmp_path: Path):
    semantic = SemanticMemory(storage_path=tmp_path / "semantic")
    a = semantic.upsert_concept(
        name="喜歡咖啡", description="A", concept_type="abstract", embedding=[1.0, 0.0],
    )
    b = semantic.upsert_concept(
        name="討厭咖啡", description="B", concept_type="abstract", embedding=[0.99, 0.01],
    )
    graph = MemoryGraph(tmp_path / "graph")
    graph.add_concept_node(a, "喜歡咖啡", "abstract")
    graph.add_concept_node(b, "討厭咖啡", "abstract")
    graph.add_contradicts_edge(a, b, 0.8)

    coord = DreamPatchCoordinator(inhibitor=None, audit_log_path=tmp_path / "audit.jsonl")
    coord.begin_cycle("bridge_vs_contradicts")
    report = GraphPruning(graph, semantic=semantic, patch_coordinator=coord).run()

    assert report.latent_bridges_created == 0
    for src, dst in ((a, b), (b, a)):
        if graph.graph.has_edge(src, dst):
            assert graph.graph[src][dst].get("edge_type") == "CONTRADICTS"
