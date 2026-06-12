from __future__ import annotations

from echo_memory.schemas.episode import Episode
from echo_memory.storage.episodic import EpisodicMemory


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

