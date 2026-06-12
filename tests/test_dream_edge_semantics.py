from __future__ import annotations

import time

from echo_memory.dream.pruning import GraphPruning
from echo_memory.storage.graph import MemoryGraph


def test_time_decay_only_updates_precedes_edges(tmp_path):
    graph = MemoryGraph(tmp_path)
    graph.graph.add_edge(
        "ep-a", "ep-b", edge_type="PRECEDES", weight=0.9,
        recency_score=1.0, frequency=1, avg_salience=0.5,
        created_at=time.time() - 86400,
    )
    graph.graph.add_edge(
        "concept-a", "concept-b", edge_type="CONTRADICTS", weight=0.83,
        created_at=time.time() - 86400,
    )
    graph.graph.add_edge(
        "concept-a", "concept-c", edge_type="RELATES_TO", weight=0.74,
        created_at=time.time() - 86400,
    )
    graph.graph.add_edge(
        "ep-a", "entity-a", edge_type="MENTIONS", weight=1.0,
        created_at=time.time() - 86400,
    )

    updated = GraphPruning(graph).update_edge_weights()

    assert updated == 1
    assert graph.graph["ep-a"]["ep-b"]["weight"] != 0.9
    assert graph.graph["concept-a"]["concept-b"]["weight"] == 0.83
    assert graph.graph["concept-a"]["concept-c"]["weight"] == 0.74
    assert graph.graph["ep-a"]["entity-a"]["weight"] == 1.0

