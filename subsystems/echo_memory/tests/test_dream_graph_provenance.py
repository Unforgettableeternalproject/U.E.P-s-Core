"""
M5-B：Dream 圖譜／episode／procedure mutation 逐條 provenance 紀錄。

驗收：
  1. pruning（弱邊、節點歸檔、max_frequency）、bridge（建立／確認／降級／撤銷）、
     replay（salience、邊）、procedure（寫入、APPLIES_TO）、distillation（DERIVED_FROM、
     RELATES_TO）每筆 mutation 都有逐條紀錄（類型、目標、producer、來源、前值、cycle id）
  2. 逐條 audit 失敗時不 mutate（比照 test_m9b_bypass）
  3. LATENT_BRIDGE 帶 provenance、confirm_count（不同週期才累計）、endpoint_digests、
     state；CONTRADICTS 降級（檢索排除）、端點失效撤銷；抑制回呼；舊邊容錯
  4. provenance producer 擴充後舊值仍有效；回滾可還原前值
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from echo_memory.dream.distillation import KnowledgeDistillation
from echo_memory.dream.patch_coordinator import DreamPatchCoordinator
from echo_memory.dream.procedure_extractor import ProcedureExtractor
from echo_memory.dream.pruning import GraphPruning
from echo_memory.dream.replay import LiDERReplay
from echo_memory.schemas.episode import Episode
from echo_memory.schemas.provenance import PRODUCERS, Derivation, dream_producer
from echo_memory.storage.episodic import EpisodicMemory
from echo_memory.storage.graph import MemoryGraph
from echo_memory.storage.procedural import ProceduralMemory
from echo_memory.storage.semantic import SemanticMemory

DIM = 512
DAY = 86400.0


def _one_hot(i: int) -> list[float]:
    v = [0.0] * DIM
    v[i] = 1.0
    return v


class _FailingCoordinator(DreamPatchCoordinator):
    """逐條紀錄一律失敗（摘要級 audit 仍可寫），用來驗證「逐條 audit 失敗不 mutate」。"""

    def record_mutations(self, stage, entries):
        return False if entries else True


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


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


@pytest.fixture
def coord(audit_path):
    c = DreamPatchCoordinator(audit_log_path=audit_path)
    c.begin_cycle("c1")
    return c


@pytest.fixture
def bad_coord(audit_path):
    c = _FailingCoordinator(audit_log_path=audit_path)
    c.begin_cycle("c1")
    return c


def _mutations(path: Path, kind: str | None = None) -> list[dict]:
    if not path.exists():
        return []
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    rows = [r for r in rows if r.get("operation") == "mutation"]
    return [r for r in rows if kind is None or r["mutation"] == kind]


def _check_common(rec: dict, stage: str):
    assert rec["stage"] == stage
    assert rec["producer"] == dream_producer(stage)
    assert rec["mutation_id"]
    assert rec["cycle_id"].startswith("dream_c1_")
    assert "prior" in rec and "target" in rec


# ─── pruning ─────────────────────────────────────────────────────────────────


def test_weak_edge_removal_has_per_edge_record_with_prior(env, coord, audit_path):
    _, semantic, graph, ids = env
    graph.graph.add_edge(ids["X"], ids["Y"], edge_type="RELATES_TO", weight=0.01)
    graph.graph.add_edge(ids["Y"], ids["Z"], edge_type="RELATES_TO", weight=0.02)
    gp = GraphPruning(graph, patch_coordinator=coord)
    assert gp._remove_edges(gp._find_weak_edges()) == 2

    recs = _mutations(audit_path, "edge_remove")
    assert len(recs) == 2
    for r in recs:
        _check_common(r, "pruning")
        assert r["prior"]["edge_type"] == "RELATES_TO"
        assert set(r["source_concept_ids"]) <= set(ids.values())
    assert {(r["target"]["from"], r["target"]["to"]) for r in recs} == {
        (ids["X"], ids["Y"]), (ids["Y"], ids["Z"]),
    }


def test_weak_edge_removal_blocked_when_record_fails(env, bad_coord):
    _, _, graph, ids = env
    graph.graph.add_edge(ids["X"], ids["Y"], edge_type="RELATES_TO", weight=0.01)
    gp = GraphPruning(graph, patch_coordinator=bad_coord)
    assert gp._remove_edges([(ids["X"], ids["Y"])]) == 0
    assert graph.graph.has_edge(ids["X"], ids["Y"])


def _stale_episode_node(graph, node_id="ep_old"):
    old = time.time() - 90 * DAY
    iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(old))
    graph.graph.add_node(node_id, node_type="Episode", timestamp=iso, salience_score=0.05)
    return node_id


def test_node_archive_has_per_node_record(env, coord, audit_path):
    _, _, graph, _ = env
    nid = _stale_episode_node(graph)
    assert GraphPruning(graph, patch_coordinator=coord)._archive_stale_nodes() == 1
    assert graph.graph.nodes[nid]["archived"] is True
    recs = _mutations(audit_path, "node_archive")
    assert len(recs) == 1
    _check_common(recs[0], "pruning")
    assert recs[0]["target"] == {"node_id": nid}
    assert recs[0]["source_episode_ids"] == [nid]
    assert recs[0]["prior"]["archived"] is False


def test_node_archive_blocked_when_record_fails(env, bad_coord):
    _, _, graph, _ = env
    nid = _stale_episode_node(graph)
    assert GraphPruning(graph, patch_coordinator=bad_coord)._archive_stale_nodes() == 0
    assert not graph.graph.nodes[nid].get("archived", False)


def test_max_frequency_record_and_block(env, coord, bad_coord, audit_path):
    _, _, graph, _ = env
    graph._max_frequency = 3
    graph.graph.add_edge("a", "b", edge_type="PRECEDES", frequency=9, weight=0.5)
    assert GraphPruning(graph, patch_coordinator=bad_coord)._recalc_max_frequency() == 3
    assert graph._max_frequency == 3
    assert GraphPruning(graph, patch_coordinator=coord)._recalc_max_frequency() == 9
    rec = _mutations(audit_path, "max_frequency")[0]
    assert rec["prior"] == {"max_frequency": 3}


# ─── LATENT_BRIDGE ───────────────────────────────────────────────────────────


def _make_similar(semantic, ids):
    semantic.get(ids["Y"]).embedding = _one_hot(0)
    semantic._rebuild_embeddings()


def test_bridge_create_records_provenance_and_edge_attrs(env, coord, audit_path):
    _, semantic, graph, ids = env
    _make_similar(semantic, ids)
    conf = {n: semantic.get(i).confidence for n, i in ids.items()}
    gp = GraphPruning(graph, semantic=semantic, patch_coordinator=coord)
    assert gp._create_latent_bridges() == 1

    recs = _mutations(audit_path, "bridge_create")
    assert len(recs) == 2  # 雙向各一筆
    for r in recs:
        _check_common(r, "bridge")
        assert r["prior"] is None
        assert set(r["source_concept_ids"]) == {ids["X"], ids["Y"]}
        assert set(r["source_episode_ids"]) == {"ep_X", "ep_Y"}
        prov = r["extra"]["bridge"]["provenance"]
        assert {e["concept_id"] for e in prov["endpoints"]} == {ids["X"], ids["Y"]}

    edge = graph.graph[ids["X"]][ids["Y"]]
    assert edge["bridge_id"] == "|".join(sorted((ids["X"], ids["Y"])))
    assert edge["state"] == "active"
    assert edge["confirm_count"] == 1
    assert edge["created_cycle_id"] == edge["last_confirmed_cycle_id"] == coord.cycle_id
    assert set(edge["endpoint_digests"]) == {ids["X"], ids["Y"]}
    assert all(edge["endpoint_digests"].values())
    assert edge["provenance"]["producer"] == "dream_bridge"
    assert edge["provenance"]["similarity"] == pytest.approx(1.0)
    # bridge 是探索式關聯，不影響 Concept confidence
    assert {n: semantic.get(i).confidence for n, i in ids.items()} == conf


def test_bridge_create_blocked_when_record_fails(env, bad_coord):
    _, semantic, graph, ids = env
    _make_similar(semantic, ids)
    gp = GraphPruning(graph, semantic=semantic, patch_coordinator=bad_coord)
    assert gp._create_latent_bridges() == 0
    assert not graph.graph.has_edge(ids["X"], ids["Y"])


def test_bridge_suppressed_callback_blocks_creation_and_default_allows(env, coord):
    _, semantic, graph, ids = env
    _make_similar(semantic, ids)
    seen = []

    def suppress(a, b):
        seen.append((a, b))
        return True

    gp = GraphPruning(graph, semantic=semantic, patch_coordinator=coord, bridge_suppressed=suppress)
    assert gp._create_latent_bridges() == 0
    assert seen and not graph.graph.has_edge(ids["X"], ids["Y"])
    # 回呼丟例外視為不抑制
    gp2 = GraphPruning(
        graph, semantic=semantic, patch_coordinator=coord,
        bridge_suppressed=lambda a, b: 1 / 0,
    )
    assert gp2._create_latent_bridges() == 1


def test_confirm_count_accumulates_per_distinct_cycle(env, coord, audit_path):
    _, semantic, graph, ids = env
    _make_similar(semantic, ids)
    gp = GraphPruning(graph, semantic=semantic, patch_coordinator=coord)
    gp._create_latent_bridges()
    first_cycle = coord.cycle_id
    edge = graph.graph[ids["X"]][ids["Y"]]

    # 同週期重複確認不計
    gp._create_latent_bridges()
    assert edge["confirm_count"] == 1

    # 不同週期才累計
    coord._cycle_id = "dream_c1_next"
    gp._create_latent_bridges()
    assert edge["confirm_count"] == 2
    assert edge["created_cycle_id"] == first_cycle
    assert edge["last_confirmed_cycle_id"] == "dream_c1_next"
    assert graph.graph[ids["Y"]][ids["X"]]["confirm_count"] == 2
    confirms = _mutations(audit_path, "bridge_confirm")
    assert confirms and confirms[0]["prior"]["confirm_count"] == 1


def _legacy_bridge(graph, a, b):
    ts = time.time()
    for s, d in ((a, b), (b, a)):
        graph.graph.add_edge(
            s, d, edge_type="LATENT_BRIDGE", weight=0.9, similarity=0.9,
            created_at=ts, last_confirmed_at=ts,
        )


def test_iter_latent_bridges_tolerates_legacy_edges(env):
    _, _, graph, ids = env
    _legacy_bridge(graph, ids["X"], ids["Y"])
    infos = graph.iter_latent_bridges()
    assert len(infos) == 1
    info = infos[0]
    assert info.state == "active"
    assert info.confirm_count == 0
    assert info.bridge_id == "|".join(sorted((ids["X"], ids["Y"])))
    assert {info.concept_a, info.concept_b} == {ids["X"], ids["Y"]}
    assert graph.iter_latent_bridges(states=("degraded",)) == []


def test_expire_no_longer_degrades_bridge_on_contradicts(env, coord, audit_path):
    """DiGraph 同向只能一條邊：降級分支已移除，CONTRADICTS 取代由 graph 記 bridge_revoke。"""
    _, semantic, graph, ids = env
    _legacy_bridge(graph, ids["X"], ids["Y"])
    graph.graph.add_edge(ids["Y"], ids["X"], edge_type="CONTRADICTS", weight=0.8)
    gp = GraphPruning(graph, semantic=semantic, patch_coordinator=coord)
    assert gp._expire_latent_bridges() == 0
    assert graph.graph[ids["X"]][ids["Y"]].get("state", "active") == "active"
    assert _mutations(audit_path, "bridge_degrade") == []


def test_degraded_bridge_excluded_from_retrieval(env, coord):
    from echo_memory.retrieval.ecphory import EcphoryRetrieval

    episodic, semantic, graph, ids = env
    _legacy_bridge(graph, ids["X"], ids["Y"])
    ecphory = EcphoryRetrieval(episodic, semantic)

    def recalled():
        concepts, _ = ecphory._retrieve_concepts(_one_hot(0), top_k=5, dopamine=0.5)
        return {c.id for c in concepts}

    assert ids["Y"] in recalled()
    for s, d in ((ids["X"], ids["Y"]), (ids["Y"], ids["X"])):
        graph.graph[s][d]["state"] = "degraded"
    assert ids["Y"] not in recalled()


def test_bridge_revoked_on_deprecated_endpoint_has_record_and_blocked_on_failure(
    env, coord, bad_coord, audit_path,
):
    _, semantic, graph, ids = env
    _legacy_bridge(graph, ids["X"], ids["Y"])
    semantic.get(ids["Y"]).status = "deprecated"

    assert GraphPruning(
        graph, semantic=semantic, patch_coordinator=bad_coord,
    )._expire_latent_bridges() == 0
    assert graph.graph.has_edge(ids["X"], ids["Y"])

    assert GraphPruning(
        graph, semantic=semantic, patch_coordinator=coord,
    )._expire_latent_bridges() == 2
    recs = _mutations(audit_path, "bridge_revoke")
    assert len(recs) == 2
    for r in recs:
        _check_common(r, "bridge")
        assert r["reason"] == "endpoint_inactive"
        assert r["prior"]["edge_type"] == "LATENT_BRIDGE"


# ─── replay ──────────────────────────────────────────────────────────────────


def _replay_setup(tmp_path, coordinator):
    (tmp_path / "rep").mkdir(exist_ok=True)
    episodic = EpisodicMemory(storage_path=tmp_path / "rep")
    eps = []
    for i in range(2):
        ep = Episode(
            user_input=f"對話 {i}", agent_response=f"回覆 {i}", session_id=f"s{i}",
            content_summary=f"對話 {i}", salience_score=0.7,
        )
        episodic.write(ep)
        eps.append(ep)
    llm = lambda prompt: json.dumps({"reflections": [  # noqa: E731
        {"index": 0, "reflection": "r0", "importance": 0.9, "related_indices": [1]},
        {"index": 1, "reflection": "r1", "importance": 0.5, "related_indices": [0]},
    ]})
    replay = LiDERReplay(
        episodic=episodic, graph=episodic.graph, llm_fn=llm, patch_coordinator=coordinator,
    )
    return episodic, eps, replay


def test_replay_mutations_have_per_item_records(tmp_path, coord, audit_path):
    episodic, eps, replay = _replay_setup(tmp_path, coord)
    report = replay.run()
    assert report.edges_created >= 1

    sal = _mutations(audit_path, "episode_salience")
    assert {r["target"]["episode_id"] for r in sal} == {eps[0].id, eps[1].id}
    for r in sal:
        _check_common(r, "replay")
        assert r["prior"]["salience_score"] == pytest.approx(0.7)
        assert r["new"]["salience_score"] != pytest.approx(0.7)
    assert episodic.get(eps[0].id).salience_score == pytest.approx((0.7 + 0.9) / 2)

    adds = _mutations(audit_path, "edge_add")
    assert adds
    for r in adds:
        _check_common(r, "replay")
        assert r["prior"] is None
        assert set(r["source_episode_ids"]) == {eps[0].id, eps[1].id}
        assert r["target"]["edge_type"] == "REPLAY_RELATED"
    # 第二筆 reflection 反向關聯 → 強化既有邊，前值可回滾
    updates = _mutations(audit_path, "edge_update")
    assert updates and updates[0]["prior"]["edge_type"] == "REPLAY_RELATED"


def test_replay_does_not_mutate_when_record_fails(tmp_path, bad_coord):
    episodic, eps, replay = _replay_setup(tmp_path, bad_coord)
    report = replay.run()
    assert report.edges_created == 0 and report.edges_strengthened == 0
    assert episodic.get(eps[0].id).salience_score == pytest.approx(0.7)
    assert not replay.graph.graph.has_edge(eps[0].id, eps[1].id)
    assert not replay.graph.graph.has_edge(eps[1].id, eps[0].id)


# ─── procedure ───────────────────────────────────────────────────────────────

_PROC_LLM = json.dumps({
    "found": True, "name": "部署流程", "description": "標準部署", "domain": "coding",
    "steps": ["build", "test", "deploy"], "trigger_patterns": ["部署"], "source_indices": [0],
})


def _proc_setup(tmp_path, coordinator):
    (tmp_path / "proc").mkdir(exist_ok=True)
    procedural = ProceduralMemory(storage_path=tmp_path / "proc")
    graph = MemoryGraph(tmp_path / "pgraph")
    ep = Episode(
        user_input="部署", agent_response="做 build、test、deploy", session_id="t",
        content_summary="部署", salience_score=0.9,
    )
    extractor = ProcedureExtractor(
        procedural=procedural, graph=graph, llm_fn=lambda p: _PROC_LLM,
        patch_coordinator=coordinator,
    )
    return procedural, extractor, ep


def test_procedure_write_goes_through_record_with_sources(tmp_path, coord, audit_path):
    procedural, extractor, ep = _proc_setup(tmp_path, coord)
    report = extractor.run([ep])
    assert report.procedures_created == 1
    proc = procedural.get_by_name("部署流程")
    assert proc is not None

    recs = _mutations(audit_path, "procedure_write")
    assert len(recs) == 1
    _check_common(recs[0], "procedure")
    assert recs[0]["source_episode_ids"] == [ep.id]
    assert recs[0]["prior"] is None
    assert recs[0]["new"]["is_new"] is True

    # 再寫一次 → 前值為舊版內容（可回滾）
    extractor.run([ep])
    second = _mutations(audit_path, "procedure_write")[-1]
    assert second["prior"]["name"] == "部署流程"
    assert second["prior"]["steps"] == ["build", "test", "deploy"]
    assert "embedding" not in second["prior"]


def test_procedure_not_written_when_record_fails(tmp_path, bad_coord):
    procedural, extractor, ep = _proc_setup(tmp_path, bad_coord)
    report = extractor.run([ep])
    assert report.procedures_created == 0
    assert procedural.get_by_name("部署流程") is None


# ─── distillation 邊 ─────────────────────────────────────────────────────────


@pytest.fixture
def dstores(tmp_path: Path):
    (tmp_path / "dep").mkdir()
    (tmp_path / "dsem").mkdir()
    episodic = EpisodicMemory(storage_path=tmp_path / "dep")
    semantic = SemanticMemory(storage_path=tmp_path / "dsem", embed_fn=lambda t: _one_hot(5))
    return episodic, semantic, episodic.graph


def _distill(dstores, coordinator):
    episodic, semantic, graph = dstores
    for i in range(3):
        episodic.write(Episode(
            user_input=f"對話 {i}", agent_response=f"回覆 {i}", session_id="t",
            content_summary=f"對話 {i}", salience_score=0.8,
        ))
    llm = lambda p: json.dumps([  # noqa: E731
        {"name": "概念A", "description": "A", "concept_type": "pattern", "source_indices": [0, 1]},
        {"name": "概念B", "description": "B", "concept_type": "pattern", "source_indices": [1, 2]},
    ], ensure_ascii=False)
    KnowledgeDistillation(
        episodic=episodic, semantic=semantic, graph=graph, llm_fn=llm,
        patch_coordinator=coordinator,
    ).run()
    return semantic, graph


def test_distillation_edges_have_per_edge_records(dstores, audit_path):
    from echo_memory.affect.concept_inhibitor import ConceptInhibitor

    _, semantic, _ = dstores
    coord = DreamPatchCoordinator(
        inhibitor=ConceptInhibitor(semantic=semantic, llm_fn=None), audit_log_path=audit_path,
    )
    coord.begin_cycle("c1")
    semantic, graph = _distill(dstores, coord)

    derived = [r for r in _mutations(audit_path, "edge_add") if r["target"]["edge_type"] == "DERIVED_FROM"]
    assert len(derived) == 4  # A 兩個來源 + B 兩個來源
    for r in derived:
        _check_common(r, "distillation")
        assert len(r["source_episode_ids"]) == 1 and len(r["source_concept_ids"]) == 1
    relates = [r for r in _mutations(audit_path) if r["target"].get("edge_type") == "RELATES_TO"]
    assert len(relates) == 2  # 雙向
    a, b = semantic.get_by_name("概念A"), semantic.get_by_name("概念B")
    assert {(r["target"]["from"], r["target"]["to"]) for r in relates} == {(a.id, b.id), (b.id, a.id)}
    for r in relates:
        assert set(r["source_concept_ids"]) == {a.id, b.id}
        assert r["source_episode_ids"]


def test_distillation_edges_blocked_when_record_fails(dstores, audit_path):
    from echo_memory.affect.concept_inhibitor import ConceptInhibitor

    _, semantic, _ = dstores
    coord = _FailingCoordinator(
        inhibitor=ConceptInhibitor(semantic=semantic, llm_fn=None), audit_log_path=audit_path,
    )
    coord.begin_cycle("c1")
    semantic, graph = _distill(dstores, coord)
    types = {d.get("edge_type") for _, _, d in graph.graph.edges(data=True)}
    assert "DERIVED_FROM" not in types and "RELATES_TO" not in types


# ─── provenance 列舉與回滾 ───────────────────────────────────────────────────


def test_producers_extended_and_legacy_values_still_valid():
    for legacy in ("dream", "reflection", "extractor", "conversation", "reviewer", "unknown"):
        assert legacy in PRODUCERS
        Derivation(kind="episode", id="e", producer=legacy)
    for stage in ("replay", "pruning", "bridge", "distillation", "procedure", "entity"):
        assert dream_producer(stage) in PRODUCERS
        Derivation(kind="episode", id="e", producer=dream_producer(stage))
    # 缺 producer 的舊資料仍可讀
    assert Derivation.from_dict({"kind": "episode", "id": "e"}).producer == "unknown"


def test_rollback_restores_removed_edge_and_archive(env, coord, audit_path):
    _, _, graph, ids = env
    graph.graph.add_edge(ids["X"], ids["Y"], edge_type="RELATES_TO", weight=0.01)
    gp = GraphPruning(graph, patch_coordinator=coord)
    gp._remove_edges([(ids["X"], ids["Y"])])
    assert not graph.graph.has_edge(ids["X"], ids["Y"])
    rec = _mutations(audit_path, "edge_remove")[0]
    assert coord.rollback_mutation(rec, graph=graph) is True
    assert graph.graph[ids["X"]][ids["Y"]]["weight"] == pytest.approx(0.01)

    nid = _stale_episode_node(graph)
    gp._archive_stale_nodes()
    arch = _mutations(audit_path, "node_archive")[0]
    assert coord.rollback_mutation(arch, graph=graph) is True
    assert graph.graph.nodes[nid]["archived"] is False
    assert _mutations(audit_path, "rollback")


# ─── CONTRADICTS 取代 bridge 前先留逐條紀錄 ──────────────────────────────────


def test_contradicts_replacing_bridge_records_revoke_first(env, coord, audit_path):
    _, _, graph, ids = env
    _legacy_bridge(graph, ids["X"], ids["Y"])
    assert graph.add_contradicts_edge(
        ids["X"], ids["Y"], 0.8, on_bridge_displaced=coord.record_bridge_displaced,
    ) is True
    assert graph.graph[ids["X"]][ids["Y"]]["edge_type"] == "CONTRADICTS"
    assert "similarity" not in graph.graph[ids["X"]][ids["Y"]]  # 不混入 bridge 屬性
    recs = _mutations(audit_path, "bridge_revoke")
    assert len(recs) == 2
    for r in recs:
        _check_common(r, "bridge")
        assert r["prior"]["edge_type"] == "LATENT_BRIDGE"
        assert r["new"]["degraded_reason"] == "endpoint_contradicts"
        assert r["new"]["state"] == "expired"
        assert r["reason"] == "endpoint_contradicts"


def test_contradicts_blocked_when_bridge_record_fails(env, bad_coord):
    _, _, graph, ids = env
    _legacy_bridge(graph, ids["X"], ids["Y"])
    assert graph.add_contradicts_edge(
        ids["X"], ids["Y"], 0.8, on_bridge_displaced=bad_coord.record_bridge_displaced,
    ) is False
    assert graph.graph[ids["X"]][ids["Y"]]["edge_type"] == "LATENT_BRIDGE"
    assert not graph.has_contradicts_edge(ids["X"], ids["Y"])


def test_contradicts_without_bridge_needs_no_record(env, bad_coord):
    _, _, graph, ids = env
    assert graph.add_contradicts_edge(
        ids["X"], ids["Z"], 0.5, on_bridge_displaced=bad_coord.record_bridge_displaced,
    ) is True
    assert graph.has_contradicts_edge(ids["X"], ids["Z"])
