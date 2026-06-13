from __future__ import annotations

from types import SimpleNamespace

import Entry
from echo_memory.schemas.episode import Episode
from echo_memory.schemas.memory_context import MemoryContext


def test_cmd_search_uses_similarity_mapping(monkeypatch):
    episode = Episode(
        user_input="query",
        agent_response="answer",
        session_id="session",
        content_summary="query answer",
    )
    context = MemoryContext(episodes=[episode], similarities={episode.id: 0.91})
    engine = SimpleNamespace(retrieve=lambda **kwargs: context)

    answers = iter(["query", "semantic"])
    monkeypatch.setattr(Entry.Prompt, "ask", lambda *args, **kwargs: next(answers))
    monkeypatch.setattr(Entry.console, "print", lambda *args, **kwargs: None)

    Entry._cmd_search(engine, SimpleNamespace())

