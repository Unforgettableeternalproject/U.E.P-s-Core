from __future__ import annotations

import pytest

from echo_memory.engine import MemoryEngine
from echo_memory import namespace as namespace_module


def test_namespace_rejects_path_traversal(tmp_path):
    with pytest.raises(ValueError):
        MemoryEngine(storage_path=tmp_path, namespace_id="../escape")


def test_namespaces_use_isolated_storage_and_retrieval(tmp_path):
    alpha = MemoryEngine(storage_path=tmp_path, namespace_id="alpha")
    beta = MemoryEngine(storage_path=tmp_path, namespace_id="beta")

    alpha.store_episode(
        user_input="alpha secret",
        agent_response="alpha response",
        session_id="alpha-session",
    )

    assert alpha.episodic.count() == 1
    assert beta.episodic.count() == 0
    assert alpha.storage_path != beta.storage_path


def test_local_default_keeps_legacy_root_path(tmp_path):
    engine = MemoryEngine(storage_path=tmp_path, namespace_id="local-default")

    assert engine.storage_path == tmp_path


def test_registry_caches_one_engine_per_namespace(tmp_path):
    registry = namespace_module.NamespaceEngineRegistry(
        lambda namespace_id: MemoryEngine(
            storage_path=tmp_path,
            namespace_id=namespace_id,
            embedding_model="custom",
        )
    )

    alpha = registry.get("alpha")
    alpha_again = registry.get("alpha")
    beta = registry.get("beta")

    assert alpha is alpha_again
    assert alpha is not beta
    assert alpha.namespace_id == "alpha"
    assert beta.namespace_id == "beta"
