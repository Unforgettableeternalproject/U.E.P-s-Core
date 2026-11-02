"""
M-5：LATENT_BRIDGE 的檢索路徑、折扣、衰減與降級。

LATENT_BRIDGE 是探索式關聯，不是事實支持：
  - 檢索 1-hop 可召回 bridge 鄰居（分數 = 來源 × bridge 權重 × 折扣 × 衰減 × 0.7）
  - 折扣可設定；未被重新確認的 bridge 隨時間衰減，過期由 Dream 清除
  - deprecated / archived 的鄰居依生命週期降權；帶 CONTRADICTS 的鄰居再降級
  - 檢索與 bridge 重新確認都不得提高 confidence 或增加來源證據
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from echo_memory.config import get_config
from echo_memory.dream.patch_coordinator import DreamPatchCoordinator
from echo_memory.dream.pruning import GraphPruning
from echo_memory.retrieval.ecphory import EcphoryRetrieval
from echo_memory.storage.episodic import EpisodicMemory
from echo_memory.storage.semantic import SemanticMemory

DIM = 512
DAY = 86400.0


def _one_hot(i: int) -> list[float]:
    v = [0.0] * DIM
    v[i] = 1.0
    return v


@pytest.fixture
def env(tmp_path: Path):
    (tmp_path / "ep").mkdir()
    (tmp_path / "sem").mkdir()
    episodic = EpisodicMemory(storage_path=tmp_path / "ep")
    semantic = SemanticMemory(storage_path=tmp_path / "sem", embed_fn=lambda t: _one_hot(99))
    graph = episodic.graph

    ids = {}
    for name, idx in (("X", 0), ("Y", 1), ("Z", 2)):
        cid = semantic.upsert_concept(
            name, f"概念 {name}", "pattern", source_episode_id=f"ep_{name}",
            embedding=_one_hot(idx),
        )
        graph.add_concept_node(cid, name, "pattern")
        ids[name] = cid
    return episodic, semantic, graph, ids


def _bridge(graph, a, b, weight=0.9, age_days=0.0):
    ts = time.time() - age_days * DAY
    for s, d in ((a, b), (b, a)):
        graph.graph.add_edge(
            s, d, edge_type="LATENT_BRIDGE", weight=weight, similarity=weight,
            created_at=ts, last_confirmed_at=ts,
        )


def _retrieve(episodic, semantic, dopamine=0.5):
    ecphory = EcphoryRetrieval(episodic, semantic)
    concepts, scores = ecphory._retrieve_concepts(_one_hot(0), top_k=5, dopamine=dopamine)
    return {c.id for c in concepts}, scores


def test_no_edge_means_no_recall(env):
    episodic, semantic, _, ids = env
    got, _ = _retrieve(episodic, semantic)
    assert got == {ids["X"]}


def test_bridge_one_hop_recall_with_discount(env):
    episodic, semantic, graph, ids = env
    _bridge(graph, ids["X"], ids["Y"], weight=0.9)
    got, scores = _retrieve(episodic, semantic)
    assert ids["Y"] in got
    cfg = get_config()
    # 未審核概念的排序因子 0.95（support 面向，與 relevance 分開相乘）
    expected = 1.0 * 0.9 * cfg.latent_bridge_discount * 0.7 * 0.95
    assert scores[ids["Y"]] == pytest.approx(expected, rel=1e-3)


def test_bridge_scores_below_relates_to_of_same_weight(env):
    episodic, semantic, graph, ids = env
    _bridge(graph, ids["X"], ids["Y"], weight=0.9)
    graph.upsert_overlap_relates_to(ids["X"], ids["Z"], 0.9, 0.9)
    _, scores = _retrieve(episodic, semantic)
    assert scores[ids["Z"]] > scores[ids["Y"]]


def test_discount_is_configurable(env, monkeypatch):
    episodic, semantic, graph, ids = env
    _bridge(graph, ids["X"], ids["Y"], weight=0.9)
    monkeypatch.setattr(get_config(), "latent_bridge_discount", 0.5)
    got, scores = _retrieve(episodic, semantic)
    assert ids["Y"] in got
    assert scores[ids["Y"]] == pytest.approx(0.9 * 0.5 * 0.7 * 0.95, rel=1e-3)

    # 折扣太重 → 掉到擴散門檻以下，不召回
    monkeypatch.setattr(get_config(), "latent_bridge_discount", 0.3)
    got, _ = _retrieve(episodic, semantic)
    assert ids["Y"] not in got


def test_stale_bridge_decays_out_of_retrieval(env):
    episodic, semantic, graph, ids = env
    _bridge(graph, ids["X"], ids["Y"], weight=0.9, age_days=0.0)
    _bridge(graph, ids["X"], ids["Z"], weight=0.9, age_days=120.0)
    got, _ = _retrieve(episodic, semantic)
    assert ids["Y"] in got
    assert ids["Z"] not in got


def test_decay_factor_is_half_life(env):
    _, _, graph, _ = env
    now = time.time()
    data = {"created_at": now - 21 * DAY, "last_confirmed_at": now - 21 * DAY}
    assert graph.bridge_decay_factor(data, 21.0, now) == pytest.approx(0.5)
    # 重新確認後以 last_confirmed_at 起算
    data["last_confirmed_at"] = now
    assert graph.bridge_decay_factor(data, 21.0, now) == pytest.approx(1.0)


def test_deprecated_neighbor_is_downweighted(env):
    episodic, semantic, graph, ids = env
    _bridge(graph, ids["X"], ids["Y"], weight=0.9)
    _bridge(graph, ids["X"], ids["Z"], weight=0.9)
    semantic.get(ids["Y"]).status = "deprecated"
    got, scores = _retrieve(episodic, semantic)
    assert ids["Y"] not in got  # ×0.15 後低於門檻
    assert ids["Z"] in got


def test_bridge_neighbor_with_contradicts_is_downgraded(env):
    episodic, semantic, graph, ids = env
    _bridge(graph, ids["X"], ids["Y"], weight=0.9)
    _bridge(graph, ids["X"], ids["Z"], weight=0.9)
    # Y 與一個無關概念 W 矛盾；Z 乾淨
    w = semantic.upsert_concept("W", "W", "pattern", embedding=_one_hot(3))
    graph.add_concept_node(w, "W", "pattern")
    graph.add_contradicts_edge(ids["Y"], w, 0.8)
    got, scores = _retrieve(episodic, semantic)
    assert ids["Z"] in got
    # 有矛盾邊的 bridge 鄰居乘 BRIDGE_CONFLICT_FACTOR（0.6）：降權但不再必然被擋掉
    assert scores[ids["Y"]] == pytest.approx(scores[ids["Z"]] * 0.6, rel=1e-3)


def test_retrieval_does_not_change_confidence_or_evidence(env):
    episodic, semantic, graph, ids = env
    _bridge(graph, ids["X"], ids["Y"], weight=0.9)
    before = {
        n: (semantic.get(i).confidence, list(semantic.get(i).source_episode_ids))
        for n, i in ids.items()
    }
    _retrieve(episodic, semantic)
    after = {
        n: (semantic.get(i).confidence, list(semantic.get(i).source_episode_ids))
        for n, i in ids.items()
    }
    assert before == after


# ── Dream 端：確認 / 過期 / 失效清除 ──


@pytest.fixture
def pruning(env, tmp_path):
    episodic, semantic, graph, ids = env
    coord = DreamPatchCoordinator(audit_log_path=tmp_path / "audit.jsonl")
    coord.begin_cycle("test")
    return GraphPruning(graph, semantic=semantic, patch_coordinator=coord), tmp_path / "audit.jsonl"


def test_expire_removes_old_bridge_and_audits(env, pruning):
    _, _, graph, ids = env
    gp, audit = pruning
    _bridge(graph, ids["X"], ids["Y"], age_days=0.0)
    _bridge(graph, ids["X"], ids["Z"], age_days=get_config().latent_bridge_expire_days + 5)
    removed = gp._expire_latent_bridges()
    assert removed == 2  # Z 那對，雙向
    assert graph.graph.has_edge(ids["X"], ids["Y"])
    assert not graph.graph.has_edge(ids["X"], ids["Z"])
    ops = [json.loads(line)["operation"] for line in audit.read_text(encoding="utf-8").splitlines()]
    assert "remove_edges" in ops


def test_expire_removes_bridge_to_deprecated_concept(env, pruning):
    _, semantic, graph, ids = env
    gp, _ = pruning
    _bridge(graph, ids["X"], ids["Y"], age_days=0.0)
    semantic.get(ids["Y"]).status = "deprecated"
    assert gp._expire_latent_bridges() == 2
    assert not graph.graph.has_edge(ids["X"], ids["Y"])


def test_create_sets_timestamp_and_reconfirm_refreshes(env, pruning):
    _, semantic, graph, ids = env
    gp, _ = pruning
    conf_before = semantic.get(ids["X"]).confidence
    # 讓 X、Y 的 embedding 相同 → 相似度 1.0
    semantic.get(ids["Y"]).embedding = _one_hot(0)
    semantic._rebuild_embeddings()

    assert gp._create_latent_bridges() >= 1
    edge = graph.graph[ids["X"]][ids["Y"]]
    assert edge["edge_type"] == "LATENT_BRIDGE"
    assert edge["last_confirmed_at"] == pytest.approx(edge["created_at"])

    # 模擬時間流逝，再跑一次 → 重新確認而非重複建立
    old = time.time() - 30 * DAY
    for s, d in ((ids["X"], ids["Y"]), (ids["Y"], ids["X"])):
        graph.graph[s][d]["last_confirmed_at"] = old
        graph.graph[s][d]["created_at"] = old
    gp._create_latent_bridges()
    assert gp._last_bridges_confirmed >= 1
    assert graph.graph[ids["X"]][ids["Y"]]["last_confirmed_at"] > old + 29 * DAY
    # 重新確認不動證據
    assert semantic.get(ids["X"]).source_episode_ids == ["ep_X"]
    assert semantic.get(ids["X"]).confidence == conf_before
