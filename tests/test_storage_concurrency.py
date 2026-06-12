from __future__ import annotations

from echo_memory.schemas.episode import Episode
from echo_memory.storage.episodic import EpisodicMemory
from echo_memory.storage.locking import storage_lock


def _episode(label: str) -> Episode:
    return Episode(
        user_input=label,
        agent_response=f"response-{label}",
        session_id=label,
        content_summary=label,
    )


def test_independent_instances_do_not_lose_updates(tmp_path):
    first = EpisodicMemory(tmp_path, embedding_model="custom")
    second = EpisodicMemory(tmp_path, embedding_model="custom")

    first._generate_embedding = lambda text: [1.0, 0.0]
    second._generate_embedding = lambda text: [0.0, 1.0]

    first.write(_episode("first"))
    second.write(_episode("second"))

    reloaded = EpisodicMemory(tmp_path, embedding_model="custom")
    assert {ep.session_id for ep in reloaded.list_episodes()} == {"first", "second"}


def test_stale_mutation_does_not_resurrect_deleted_episode(tmp_path):
    writer = EpisodicMemory(tmp_path, embedding_model="custom")
    writer._generate_embedding = lambda text: [1.0, 0.0]
    deleted_id = writer.write(_episode("deleted"))
    kept_id = writer.write(_episode("kept"))

    stale = EpisodicMemory(tmp_path, embedding_model="custom")
    assert writer.delete_episode(deleted_id)
    assert stale.update_salience(kept_id, 0.9)

    reloaded = EpisodicMemory(tmp_path, embedding_model="custom")
    assert reloaded.get(deleted_id) is None
    assert reloaded.get(kept_id).salience_score == 0.9


def test_namespace_lock_is_reentrant_in_same_thread(tmp_path):
    with storage_lock(tmp_path, timeout=0.2):
        with storage_lock(tmp_path, timeout=0.2):
            pass
