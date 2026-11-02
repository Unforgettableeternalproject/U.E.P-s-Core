"""
tests/test_hypothesis_fixes.py - M5-D 複審修正回歸

涵蓋：Gate 自行重算引用歸屬（繞過審核器直接送 payload）、孤兒昇格邊撤回、
list_hypotheses／工具的 shared_turn 閘、rollback_mutation 的 noop 判定。
"""

from __future__ import annotations

import json

from echo_memory.dream.patch_coordinator import DreamPatchCoordinator
from echo_memory.storage.graph import MemoryGraph
from tests.hypothesis_support import (
    ScriptedLLM,
    build_env,
    claim,
    edge_types,
    llm_reply,
    make_bridge,
    propose,
    rejection,
    two_concepts,
    unit_ids,
    user_ep,
)


def _review_payload(h, token, cited, coverage, result="supported"):
    return {
        "hypothesis_id": h.id, "token": token, "result": result, "pair_digest": h.pair_digest,
        "review": {"status": "supported" if result == "supported" else "contradicted",
                   "cited_evidence_ids": cited},
        "statement": "有關", "claim_kind": "fact", "coverage": coverage,
    }


def _setup(tmp_path, same_speaker=True):
    env = build_env(tmp_path)
    d = two_concepts(env, same_speaker=same_speaker)
    make_bridge(env, d["a"], d["b"])
    hid = propose(env, d["a"], d["b"])
    claim(env, hid, "tok")
    return env, d, hid, env.store.get(hid)


# ─── 1. Gate 重算引用歸屬 ─────────────────────────────────────────────────────


def test_gate_rejects_cited_unit_unrelated_to_both_endpoints(tmp_path):
    """複審重現：A/B 都只有 u1 證據，混入無關 u2 unit 並偽造 coverage -> 不得昇格。"""
    env, d, hid, h = _setup(tmp_path, same_speaker=True)
    ep_x = user_ep(env.engine, "今天天氣很好完全無關", speaker="u2")
    ux = unit_ids(env.engine, ep_x)[0]
    ok, tx = env.gate.process_hypothesis_patch("review", _review_payload(
        h, "tok", [d["ua"], d["ub"], ux], {"a": [d["ua"]], "b": [d["ub"]]},
    ))
    assert not ok and rejection(tx).startswith("cited_not_in_endpoints")
    assert env.store.get(hid).status == "under_review"
    assert edge_types(env, d["a"], d["b"]) == {"LATENT_BRIDGE"}


def test_gate_ignores_payload_coverage(tmp_path):
    """引用只屬於 A 端：就算 payload 宣稱也覆蓋 B，Gate 依 provenance 重算後拒絕。"""
    env, d, hid, h = _setup(tmp_path, same_speaker=False)
    ok, tx = env.gate.process_hypothesis_patch("review", _review_payload(
        h, "tok", [d["ua"]], {"a": [d["ua"]], "b": [d["ua"]]},
    ))
    assert not ok and rejection(tx).startswith("coverage")
    assert edge_types(env, d["a"], d["b"]) == {"LATENT_BRIDGE"}


def test_gate_refuted_also_requires_endpoint_citations(tmp_path):
    env, d, hid, h = _setup(tmp_path)
    ep_x = user_ep(env.engine, "無關的另一句話", speaker="u2")
    ux = unit_ids(env.engine, ep_x)[0]
    ok, tx = env.gate.process_hypothesis_patch("review", _review_payload(
        h, "tok", [ux], {}, result="refuted",
    ))
    assert not ok and rejection(tx).startswith("cited_not_in_endpoints")
    assert edge_types(env, d["a"], d["b"]) == {"LATENT_BRIDGE"}  # bridge 沒被移除
    # 屬於端點的引用則放行
    ok, _ = env.gate.process_hypothesis_patch("review", _review_payload(
        h, "tok", [d["ua"]], {}, result="refuted",
    ))
    assert ok and env.store.get(hid).status == "refuted"


def test_gate_without_provenance_fails_closed(tmp_path):
    env, d, hid, h = _setup(tmp_path, same_speaker=False)
    env.gate.hypothesis_provenance_provider = None
    ok, tx = env.gate.process_hypothesis_patch("review", _review_payload(
        h, "tok", [d["ua"], d["ub"]], {"a": [d["ua"]], "b": [d["ub"]]},
    ))
    assert not ok and rejection(tx).startswith("no_provenance")


# ─── 2. 孤兒昇格邊 ───────────────────────────────────────────────────────────


def test_orphan_promoted_edge_is_unlinked_by_maintain(tmp_path):
    env = build_env(tmp_path)
    d = two_concepts(env, same_speaker=False)
    make_bridge(env, d["a"], d["b"])
    hid = propose(env, d["a"], d["b"])
    # 模擬昇格時「邊已存檔、假說檔尚未寫入」就崩潰：圖上有邊，假說仍是 proposed
    a, b = sorted((d["a"], d["b"]))
    with env.graph.lock:
        for s, t in ((a, b), (b, a)):
            env.graph.graph.remove_edge(s, t)
            env.graph.graph.add_edge(s, t, edge_type="RELATES_TO", weight=0.5,
                                     origin="hypothesis", hypothesis_id=hid)
    env.graph.save()
    assert env.graph.hypothesis_edges() == [(a, b, hid)]
    report = env.phase.maintain()
    assert report.orphans_unlinked == 1
    assert edge_types(env, d["a"], d["b"]) == set()
    assert env.graph.hypothesis_edges() == []


def test_orphan_edge_of_missing_hypothesis_is_unlinked(tmp_path):
    env = build_env(tmp_path)
    d = two_concepts(env)
    a, b = sorted((d["a"], d["b"]))
    with env.graph.lock:
        for s, t in ((a, b), (b, a)):
            env.graph.graph.add_edge(s, t, edge_type="RELATES_TO", weight=0.5,
                                     origin="hypothesis", hypothesis_id="hyp_gone")
    env.graph.save()
    assert env.phase.maintain().orphans_unlinked == 1
    assert edge_types(env, d["a"], d["b"]) == set()


def test_valid_promoted_edge_is_never_unlinked(tmp_path):
    env = build_env(tmp_path)
    d = two_concepts(env, same_speaker=False)
    make_bridge(env, d["a"], d["b"])
    hid = propose(env, d["a"], d["b"])
    env.reviewer._llm_fn = ScriptedLLM(llm_reply("supported", [d["ua"], d["ub"]]))
    assert env.reviewer.review(hid).status == "supported"
    assert env.phase.maintain().orphans_unlinked == 0
    assert edge_types(env, d["a"], d["b"]) == {"RELATES_TO"}
    # Gate 也擋：supported 的邊不可被 unlink
    ok, tx = env.gate.process_hypothesis_patch(
        "unlink", {"hypothesis_id": hid, "concept_a": d["a"], "concept_b": d["b"]},
    )
    assert not ok and rejection(tx).startswith("still_promoted")


# ─── 3. shared_turn 閘 ───────────────────────────────────────────────────────


def test_list_hypotheses_and_tool_respect_shared_turn(tmp_path):
    from llm_integration.tools.executor import ToolExecutor

    env = build_env(tmp_path)
    d = two_concepts(env)
    make_bridge(env, d["a"], d["b"])
    propose(env, d["a"], d["b"])
    assert len(env.engine.list_hypotheses()) == 1
    assert env.engine.list_hypotheses(shared_turn=True) == []
    ex = ToolExecutor(env.engine, "s1")
    assert "UNVERIFIED" in ex.execute("list_hypotheses", {})["result"]
    ex.shared_turn = True
    res = ex.execute("list_hypotheses", {"shared_turn": False})  # LLM 參數無法覆寫脈絡
    assert res["success"] and "No candidate" in res["result"]


# ─── 4. rollback_mutation noop ───────────────────────────────────────────────


def _rollback_records(path):
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [r for r in rows if r.get("mutation") == "rollback"]


def test_rollback_noop_returns_false_and_audits_noop(tmp_path):
    audit = tmp_path / "dream_audit.jsonl"
    coord = DreamPatchCoordinator(audit_log_path=audit)
    graph = MemoryGraph(tmp_path / "g")
    edge_rec = {"mutation": "edge_remove", "stage": "pruning", "mutation_id": "m1",
                "target": {"from": "x", "to": "y", "edge_type": "RELATES_TO"},
                "prior": {"edge_type": "RELATES_TO", "weight": 0.1}}
    assert coord.rollback_mutation(edge_rec, graph=graph) is False  # 端點不存在
    add_rec = {"mutation": "edge_add", "stage": "pruning", "mutation_id": "m2",
               "target": {"from": "x", "to": "y", "edge_type": "RELATES_TO"}, "prior": None}
    assert coord.rollback_mutation(add_rec, graph=graph) is False  # 邊本來就不存在
    arch_rec = {"mutation": "node_archive", "stage": "pruning", "mutation_id": "m3",
                "target": {"node_id": "nope"}, "prior": {"archived": False}}
    assert coord.rollback_mutation(arch_rec, graph=graph) is False  # 節點不存在
    rows = _rollback_records(audit)
    assert len(rows) == 3 and all(r["extra"]["noop"] is True for r in rows)
    assert not graph.graph.has_node("x")  # 沒有被順手建出裸節點


def test_rollback_effective_still_true_and_marked_not_noop(tmp_path):
    audit = tmp_path / "dream_audit.jsonl"
    coord = DreamPatchCoordinator(audit_log_path=audit)
    graph = MemoryGraph(tmp_path / "g")
    graph.graph.add_node("x")
    graph.graph.add_node("y")
    rec = {"mutation": "edge_remove", "stage": "pruning", "mutation_id": "m1",
           "target": {"from": "x", "to": "y", "edge_type": "RELATES_TO"},
           "prior": {"edge_type": "RELATES_TO", "weight": 0.1}}
    assert coord.rollback_mutation(rec, graph=graph) is True
    assert graph.graph.has_edge("x", "y")
    assert _rollback_records(audit)[-1]["extra"]["noop"] is False
