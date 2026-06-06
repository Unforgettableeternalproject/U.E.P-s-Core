"""Dream Engine integration tests."""

from echo_memory import MemoryEngine


def test_dream_engine_uses_isolated_storage(tmp_path, monkeypatch):
    """A Dream cycle updates an isolated graph and returns a valid report."""
    engine = MemoryEngine(storage_path=tmp_path)
    monkeypatch.setattr(
        engine.episodic,
        "_generate_embedding",
        lambda text: [1.0, 0.0],
    )

    engine.store_episode("first", "response", "session")
    engine.store_episode("second", "response", "session")
    graph_before = engine.episodic.graph.get_stats()

    report = engine.dream(triggered_by="test")
    graph_after = engine.episodic.graph.get_stats()

    assert report["triggered_by"] == "test"
    assert report["error"] is None
    assert report["edges_updated"] >= 0
    assert report["edges_pruned"] >= 0
    assert graph_after["nodes"] == graph_before["nodes"]
