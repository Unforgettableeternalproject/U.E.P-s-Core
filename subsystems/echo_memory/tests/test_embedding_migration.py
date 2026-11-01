from __future__ import annotations

import json

import numpy as np

from echo_memory.schemas.episode import Episode
from echo_memory.storage.episodic import EpisodicMemory


def _episode(label: str) -> Episode:
    return Episode(
        user_input=label,
        agent_response=label,
        session_id=label,
        content_summary=label,
    )


def test_model_change_reindexes_all_episodes_in_order(tmp_path):
    old = EpisodicMemory(tmp_path, embedding_model="old-model")
    old_vectors = {"old-a": [1.0, 0.0], "old-b": [0.0, 1.0]}
    old._generate_embedding = lambda text: old_vectors[text]
    old.write(_episode("old-a"))
    old.write(_episode("old-b"))

    new = EpisodicMemory(tmp_path, embedding_model="new-model")
    new_vectors = {
        "old-a": [1.0, 0.0, 0.0],
        "old-b": [0.0, 1.0, 0.0],
        "new-c": [0.0, 0.0, 1.0],
    }
    new._generate_embedding = lambda text: new_vectors[text]
    new.write(_episode("new-c"))

    reloaded = EpisodicMemory(tmp_path, embedding_model="new-model")
    hits = reloaded.search([0.0, 0.0, 1.0], top_k=3, min_similarity=0.0)

    assert reloaded.count() == 3
    assert reloaded._embeddings.shape == (3, 3)
    assert hits[0][0].session_id == "new-c"
    assert set(reloaded._id_to_index) == set(reloaded._episodes)
    manifest = json.loads((tmp_path / "embedding_index.json").read_text(encoding="utf-8"))
    assert manifest["model"] == "new-model"
    assert manifest["dimension"] == 3
    assert manifest["ordered_ids"] == list(reloaded._episodes)
    assert np.allclose(reloaded._embeddings[2], [0.0, 0.0, 1.0])

