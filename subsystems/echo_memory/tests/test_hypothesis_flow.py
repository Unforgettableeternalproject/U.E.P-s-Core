"""
tests/test_hypothesis_flow.py - M5-D：Hypothesis 產生、狀態機、審核、昇格／回滾

涵蓋：phase 前置與條件（結構、不用 keyword／salience 門檻）；每條狀態轉移（含租約過期）；
審核護欄（Golden：缺來源不自動 verified、同一使用者自述不升格）；Gate 昇格與完整回滾；
refuted 抑制 bridge；端點失效／證據消失 fail-closed；llm_fn=None 不崩潰；共享 namespace 不運作。
全部使用假 llm_fn，不載入真模型。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from echo_memory.config import get_config
from echo_memory.schemas.hypothesis import make_pair_key
from tests.hypothesis_support import (
    ScriptedLLM,
    agent_ep,
    build_env,
    claim,
    edge_types,
    llm_reply,
    make_bridge,
    make_concept,
    propose,
    rejection,
    supported_env,
    two_concepts,
    unit_ids,
    user_ep,
)


@pytest.fixture
def cfg(monkeypatch):
    """可就地覆寫的全域 config（測試結束自動還原）。"""
    c = get_config()

    def _set(**kw):
        for k, v in kw.items():
            monkeypatch.setattr(c, k, v)

    _set.cfg = c  # type: ignore[attr-defined]
    return _set


def make_pair(env, tag: str, *, confirm: int = 2, speaker_b: str = "u1") -> dict:
    ep_a = user_ep(env.engine, f"{tag} 甲方的經驗", speaker="u1")
    ep_b = user_ep(env.engine, f"{tag} 乙方的經驗", speaker=speaker_b)
    a = make_concept(env, f"{tag}甲", [ep_a])
    b = make_concept(env, f"{tag}乙", [ep_b])
    make_bridge(env, a, b, confirm=confirm)
    return {"a": a, "b": b, "ua": unit_ids(env.engine, ep_a)[0], "ub": unit_ids(env.engine, ep_b)[0]}


def audit_lines(env) -> list[dict]:
    path = env.engine.semantic.storage_path / "audit_log.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def hyp_patches(env) -> list[dict]:
    """每個 transaction 取最後一筆 audit（audit-before-mutate 會先後各寫一行）。"""
    last: dict[str, dict] = {}
    for tx in audit_lines(env):
        last[tx["transaction_id"]] = tx
    return [p for tx in last.values() for p in tx["patches"] if p["action"] == "hypothesis"]


# ━━━ phase：前置與條件 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_phase_proposes_from_confirmed_bridge(tmp_path):
    env = build_env(tmp_path)
    d = two_concepts(env)
    key = make_bridge(env, d["a"], d["b"], confirm=2)
    report = env.phase.run()
    assert report.proposed == 1 and report.rejected == 0
    h = env.store.get_by_pair(d["a"], d["b"])
    assert h.status == "proposed" and h.pair_key == key
    assert h.statement is None  # proposed 不存斷言文字
    assert h.origin.type == "latent_bridge" and h.origin.confirm_count == 2
    assert h.origin.bridge_id == key and h.origin.similarity == pytest.approx(0.9)
    assert h.pair_digest and h.expires_at is not None
    # bridge 仍在（提案不動圖譜）
    assert edge_types(env, d["a"], d["b"]) == {"LATENT_BRIDGE"}
    # 經 Gate：留有 hypothesis patch audit
    ops = [p["new_value"]["op"] for p in hyp_patches(env)]
    assert ops == ["propose"]


def test_phase_requires_cross_cycle_confirmation(tmp_path):
    env = build_env(tmp_path)
    d = two_concepts(env)
    make_bridge(env, d["a"], d["b"], confirm=1)
    assert env.phase.run().proposed == 0 and env.store.count() == 0


def test_phase_requires_active_bridge(tmp_path):
    env = build_env(tmp_path)
    d = two_concepts(env)
    make_bridge(env, d["a"], d["b"], state="degraded")
    assert env.phase.run().proposed == 0


def test_phase_requires_user_evidence_on_both_ends(tmp_path):
    env = build_env(tmp_path)
    ep_a = user_ep(env.engine, "使用者說的話")
    ep_b = agent_ep(env.engine, "助手自己的推測")
    a = make_concept(env, "甲", [ep_a])
    b = make_concept(env, "乙", [ep_b], support_source="agent_only")
    make_bridge(env, a, b)
    report = env.phase.run()
    assert report.proposed == 0 and report.candidates == 0


def test_phase_ignores_salience_and_keywords(tmp_path):
    """salience 不是門檻：salience=0 的來源 episode 照樣可提案。"""
    env = build_env(tmp_path)
    ep_a = user_ep(env.engine, "完全沒有任何關鍵字的內容一", salience=0.0)
    ep_b = user_ep(env.engine, "完全沒有任何關鍵字的內容二", salience=0.0)
    a = make_concept(env, "甲", [ep_a])
    b = make_concept(env, "乙", [ep_b])
    make_bridge(env, a, b)
    assert env.phase.run().proposed == 1


def test_phase_skips_pairs_that_already_have_a_relation(tmp_path):
    env = build_env(tmp_path)
    d = two_concepts(env)
    env.graph.add_relates_to_edge(d["a"], d["b"], weight=0.6)
    env.graph.save()
    assert env.phase.run().candidates == 0
    # Gate 自己也擋（縱深防禦）：直接送提案被拒
    ca, cb = env.engine.semantic.get(d["a"]), env.engine.semantic.get(d["b"])
    from echo_memory.storage.conflict_ledger import compute_pair_digest

    ok, tx = env.gate.process_hypothesis_patch(
        "propose", {"concept_a": d["a"], "concept_b": d["b"],
                    "pair_digest": compute_pair_digest(ca, cb)},
    )
    assert not ok and rejection(tx).startswith("pair_has_relation")


def test_phase_skips_contradicting_pair(tmp_path):
    env = build_env(tmp_path)
    d = two_concepts(env)
    make_bridge(env, d["a"], d["b"])
    env.graph.add_contradicts_edge(d["a"], d["b"], weight=0.7)
    env.graph.save()
    assert env.phase.run().candidates == 0 and env.store.count() == 0


def test_phase_caps_new_per_cycle_and_open_total(tmp_path, cfg):
    env = build_env(tmp_path)
    for i in range(5):
        make_pair(env, f"組{i}")
    cfg(hypothesis_max_new_per_cycle=3)
    assert env.phase.run().proposed == 3
    cfg(hypothesis_max_open=4)
    assert env.phase.run().proposed == 1  # 開放總量上限 4
    assert env.store.open_count() == 4
    assert env.phase.run().proposed == 0


def test_phase_does_not_duplicate_open_hypothesis(tmp_path):
    env = build_env(tmp_path)
    make_pair(env, "唯一")
    assert env.phase.run().proposed == 1
    assert env.phase.run().proposed == 0 and env.store.count() == 1


def test_phase_ranks_unconnected_pairs_first(tmp_path, cfg):
    env = build_env(tmp_path)
    p1 = make_pair(env, "有共同鄰居")
    p2 = make_pair(env, "無共同鄰居")
    ep_c = user_ep(env.engine, "共同鄰居的來源")
    c = make_concept(env, "鄰居", [ep_c])
    env.graph.add_relates_to_edge(p1["a"], c, weight=0.6)
    env.graph.add_relates_to_edge(p1["b"], c, weight=0.6)
    env.graph.save()
    cfg(hypothesis_max_new_per_cycle=1)
    report = env.phase.run()
    assert report.proposed == 1
    assert env.store.get_by_pair(p2["a"], p2["b"]) is not None
    assert env.store.get_by_pair(p1["a"], p1["b"]) is None


def test_phase_without_provenance_never_proposes(tmp_path):
    env = build_env(tmp_path)
    make_pair(env, "無來源解析")
    env.phase._provenance = None
    report = env.phase.run()
    assert report.proposed == 0 and env.store.count() == 0


def test_phase_disabled_by_config(tmp_path, cfg):
    env = build_env(tmp_path)
    make_pair(env, "停用")
    cfg(hypothesis_enabled=False)
    report = env.phase.run()
    assert report.skipped_reason == "disabled" and env.store.count() == 0


def test_scope_namespace_never_generates_or_reviews(tmp_path):
    env = build_env(tmp_path, llm=ScriptedLLM(llm_reply()), namespace_id="scope-g42")
    d = two_concepts(env)
    make_bridge(env, d["a"], d["b"])
    report = env.phase.run()
    assert report.skipped_reason == "shared_namespace" and env.store.count() == 0
    # Gate 也拒絕直接提案
    from echo_memory.storage.conflict_ledger import compute_pair_digest

    digest = compute_pair_digest(env.engine.semantic.get(d["a"]), env.engine.semantic.get(d["b"]))
    ok, tx = env.gate.process_hypothesis_patch(
        "propose", {"concept_a": d["a"], "concept_b": d["b"], "pair_digest": digest},
    )
    assert not ok and rejection(tx).startswith("scope_namespace")
    assert env.reviewer.review("hyp_whatever").reason == "shared_namespace"
    assert env.engine.review_hypothesis("hyp_x")["reason"] == "disabled_or_shared_namespace"
    assert env.engine.list_hypotheses() == []


# ━━━ 狀態機 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_claim_lease_and_expiry(tmp_path):
    env = build_env(tmp_path)
    p = make_pair(env, "租約")
    env.phase.propose()
    hid = env.store.get_by_pair(p["a"], p["b"]).id

    ok, _ = claim(env, hid, "tok1")
    h = env.store.get(hid)
    assert ok and h.status == "under_review" and h.claim_token == "tok1" and h.claimed_until

    ok, tx = claim(env, hid, "tok2")
    assert not ok and rejection(tx).startswith("leased")

    # 租約過期（lease=0）：他人可重新認領；舊 token 的審核結果是 stale
    env.gate.process_hypothesis_patch("release", {"hypothesis_id": hid, "token": "tok1"})
    ok, _ = claim(env, hid, "tok1", lease_seconds=0)
    assert ok
    ok, _ = claim(env, hid, "tok2")
    assert ok and env.store.get(hid).claim_token == "tok2"
    ok, tx = env.gate.process_hypothesis_patch(
        "review", {"hypothesis_id": hid, "token": "tok1", "result": "llm_failed"},
    )
    assert not ok and rejection(tx).startswith("stale")


def test_review_with_expired_lease_is_stale(tmp_path):
    env = build_env(tmp_path)
    p = make_pair(env, "過期")
    env.phase.propose()
    hid = env.store.get_by_pair(p["a"], p["b"]).id
    claim(env, hid, "tok", lease_seconds=0)
    ok, tx = env.gate.process_hypothesis_patch(
        "review", {"hypothesis_id": hid, "token": "tok", "result": "llm_failed"},
    )
    assert not ok and "租約已過期" in rejection(tx)


def test_release_requires_owner_unless_lease_expired(tmp_path):
    env = build_env(tmp_path)
    p = make_pair(env, "釋放")
    env.phase.propose()
    hid = env.store.get_by_pair(p["a"], p["b"]).id
    claim(env, hid, "tok")
    ok, tx = env.gate.process_hypothesis_patch("release", {"hypothesis_id": hid, "token": "other"})
    assert not ok and rejection(tx).startswith("leased")
    ok, _ = env.gate.process_hypothesis_patch("release", {"hypothesis_id": hid, "token": "tok"})
    h = env.store.get(hid)
    assert ok and h.status == "proposed" and h.claim_token == "" and h.claimed_until is None


def test_maintain_releases_expired_lease_back_to_proposed(tmp_path):
    env = build_env(tmp_path)
    p = make_pair(env, "回到提案")
    env.phase.propose()
    hid = env.store.get_by_pair(p["a"], p["b"]).id
    claim(env, hid, "tok", lease_seconds=0)
    report = env.phase.maintain()
    assert report.released == 1 and env.store.get(hid).status == "proposed"


def test_llm_failure_keeps_proposed_and_backs_off(tmp_path):
    def boom():
        raise RuntimeError("LLM 掛了")

    env = build_env(tmp_path)
    p = make_pair(env, "失敗")
    env.phase.propose()
    hid = env.store.get_by_pair(p["a"], p["b"]).id
    env.reviewer._llm_fn = ScriptedLLM(boom)
    out = env.reviewer.review(hid)
    h = env.store.get(hid)
    assert out.outcome == "held" and "llm_error" in out.reason
    assert h.status == "proposed" and h.fail_count == 1 and h.attempts == 0
    assert h.next_review_at > datetime.now(timezone.utc)
    assert h.review.status == "not_checked"  # 不寫假審核
    # 退避期內不會被批次挑中，也不能自動認領；手動審核可忽略退避
    assert env.reviewer.reviewable_ids() == []
    ok, tx = claim(env, hid, "t")
    assert not ok and rejection(tx).startswith("backoff")
    env.reviewer._llm_fn = ScriptedLLM(boom)
    env.reviewer.review(hid, manual=True)
    h2 = env.store.get(hid)
    assert h2.fail_count == 2 and h2.next_review_at > h.next_review_at


@pytest.mark.parametrize("raw", [
    "不是 JSON",
    json.dumps({"status": "maybe", "claim_kind": "fact", "cited_unit_ids": []}),
    json.dumps({"status": "supported", "claim_kind": "??", "cited_unit_ids": []}),
    json.dumps({"status": "supported", "claim_kind": "fact", "cited_unit_ids": "ev_x"}),
    json.dumps({"status": "supported", "claim_kind": "fact", "cited_unit_ids": ["ev_不存在"],
                "statement": "有關"}),
])
def test_invalid_llm_output_backs_off_without_fake_review(tmp_path, raw):
    env = build_env(tmp_path)
    p = make_pair(env, "無效輸出")
    env.phase.propose()
    hid = env.store.get_by_pair(p["a"], p["b"]).id
    env.reviewer._llm_fn = ScriptedLLM(raw)
    out = env.reviewer.review(hid)
    h = env.store.get(hid)
    assert out.outcome == "held"
    assert h.status == "proposed" and h.fail_count == 1 and h.review.status == "not_checked"
    assert "RELATES_TO" not in edge_types(env, p["a"], p["b"])


def test_insufficient_exhausts_after_max_attempts_and_blocks_same_digest(tmp_path, cfg):
    env = build_env(tmp_path)
    p = make_pair(env, "耗盡")
    env.phase.propose()
    hid = env.store.get_by_pair(p["a"], p["b"]).id
    env.reviewer._llm_fn = ScriptedLLM(llm_reply("insufficient", [p["ua"], p["ub"]], statement=None))
    for n in (1, 2):
        out = env.reviewer.review(hid, manual=True)
        h = env.store.get(hid)
        assert out.outcome == "reviewed" and h.status == "proposed" and h.attempts == n
        assert h.next_review_at is not None and h.statement is None
    env.reviewer.review(hid, manual=True)
    h = env.store.get(hid)
    assert h.status == "expired" and h.outcome.expire_reason == "insufficient_exhausted"
    # 同 digest 不再提案
    assert env.phase.run().proposed == 0
    # 內容變了（pair_digest 不同）可再提案，取代舊紀錄
    c = env.engine.semantic.get(p["a"])
    c.description += "（補充）"
    env.engine.semantic._save()
    report = env.phase.run()
    assert report.proposed == 1
    new = env.store.get_by_pair(p["a"], p["b"])
    assert new.id != hid and new.status == "proposed" and env.store.count() == 1


def test_ttl_expiry(tmp_path, cfg):
    env = build_env(tmp_path)
    p = make_pair(env, "逾期")
    cfg(hypothesis_proposed_ttl_days=-1.0)
    env.phase.propose()
    hid = env.store.get_by_pair(p["a"], p["b"]).id
    report = env.phase.maintain()
    h = env.store.get(hid)
    assert report.expired == 1 and h.status == "expired" and h.outcome.expire_reason == "ttl"
    # 未逾期者 Gate 不放行
    p2 = make_pair(env, "未逾期")
    cfg(hypothesis_proposed_ttl_days=30.0)
    env.phase.propose()
    h2 = env.store.get_by_pair(p2["a"], p2["b"])
    ok, tx = env.gate.process_hypothesis_patch("expire", {"hypothesis_id": h2.id, "reason": "ttl"})
    assert not ok and rejection(tx).startswith("ttl_not_elapsed")


def test_endpoint_gone_expires_open_hypothesis(tmp_path):
    env = build_env(tmp_path)
    p = make_pair(env, "端點消失")
    env.phase.propose()
    hid = env.store.get_by_pair(p["a"], p["b"]).id
    # 端點仍 active 時 Gate 不放行
    ok, tx = env.gate.process_hypothesis_patch("expire", {"hypothesis_id": hid, "reason": "endpoint_gone"})
    assert not ok and rejection(tx).startswith("endpoint_alive")
    c = env.engine.semantic.get(p["b"])
    c.status = "deprecated"
    env.engine.semantic._save()
    report = env.phase.maintain()
    h = env.store.get(hid)
    assert report.expired == 1 and h.status == "expired" and h.outcome.expire_reason == "endpoint_gone"
    # 端點失效後審核與認領都不會進行
    ok, _ = claim(env, hid)
    assert not ok


def test_purge_after_retention(tmp_path, cfg):
    env = build_env(tmp_path)
    p = make_pair(env, "清除")
    cfg(hypothesis_proposed_ttl_days=-1.0, hypothesis_refuted_retention_days=-1.0)
    env.phase.propose()
    hid = env.store.get_by_pair(p["a"], p["b"]).id
    env.phase.maintain()  # ttl -> expired（保留期已滿）
    assert env.store.get(hid).status == "expired"
    report = env.phase.maintain()
    assert report.purged == 1 and env.store.get(hid) is None
    # 保留期內 Gate 不放行
    cfg(hypothesis_refuted_retention_days=180.0)
    p2 = make_pair(env, "保留中")
    cfg(hypothesis_proposed_ttl_days=-1.0)
    env.phase.propose()
    h2 = env.store.get_by_pair(p2["a"], p2["b"])
    env.phase.maintain()
    ok, tx = env.gate.process_hypothesis_patch("purge", {"hypothesis_ids": [h2.id]})
    assert not ok and rejection(tx).startswith("retention_not_elapsed")


def test_gate_rejects_invalid_ops_and_leaves_audit(tmp_path):
    env = build_env(tmp_path)
    ok, tx = env.gate.process_hypothesis_patch("nonsense", {})
    assert not ok and tx.status == "failed"
    ok, tx = env.gate.process_hypothesis_patch("claim", {"hypothesis_id": "hyp_nope", "token": "t"})
    assert not ok and rejection(tx).startswith("hypothesis_missing")
    assert len(hyp_patches(env)) == 2  # 拒絕也留 audit


# ━━━ 審核：昇格與護欄 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_supported_with_other_speaker_promotes_relates_to(tmp_path):
    env, d, hid = supported_env(tmp_path)
    h = env.store.get(hid)
    assert h.status == "supported" and h.statement
    assert h.review.status == "supported" and set(h.review.cited_evidence_ids) == {d["ua"], d["ub"]}
    assert h.review.support_strength >= 0.9 and h.expires_at is None
    # 昇格：LATENT_BRIDGE 換成帶 hypothesis_id 與 cited ids 的 RELATES_TO（雙向）
    assert edge_types(env, d["a"], d["b"]) == {"RELATES_TO"}
    for src, dst in ((d["a"], d["b"]), (d["b"], d["a"])):
        data = env.graph.graph[src][dst]
        assert data["origin"] == "hypothesis" and data["hypothesis_id"] == hid
        assert set(data["cited_unit_ids"]) == {d["ua"], d["ub"]}
    assert h.outcome.promoted_edge == {"a": min(d["a"], d["b"]), "b": max(d["a"], d["b"]), "hypothesis_id": hid}
    assert h.outcome.promote_patch_id == h.review.patch_id
    # 不自動建新 Concept、不建 CONTRADICTS
    assert env.engine.semantic.count() == 2
    assert not env.graph.has_contradicts_edge(d["a"], d["b"])
    # 圖譜已存檔
    from echo_memory.storage.graph import MemoryGraph

    reloaded = MemoryGraph(env.graph.storage_path)
    assert reloaded.edge_types_between(d["a"], d["b"]) == {"RELATES_TO"}
    # 查詢介面：statement 只在證據仍成立時帶出
    item = env.engine.list_hypotheses(status="supported")[0]
    assert item["statement"] == h.statement and item["stale"] is None and item["promoted"] is True


def test_supported_with_observation_corroboration(tmp_path):
    from datetime import timedelta

    from tests.hypothesis_support import T0

    env = build_env(tmp_path)
    d = two_concepts(env, same_speaker=True)  # 同一使用者自述
    obs = env.engine.record_observation(
        kind="game_activity", subject="咖啡館模擬器", started_at=T0, ended_at=T0 + timedelta(hours=1),
    )
    concept = env.engine.semantic.get(d["a"])
    concept.add_derivation("evidence", obs, producer="unknown")
    env.engine.semantic._save()
    make_bridge(env, d["a"], d["b"])
    hid = propose(env, d["a"], d["b"])
    env.reviewer._llm_fn = ScriptedLLM(llm_reply("supported", [d["ua"], d["ub"], obs]))
    out = env.reviewer.review(hid)
    h = env.store.get(hid)
    assert out.status == "supported" and h.status == "supported"
    assert obs in h.review.corroborated_by


def test_same_user_self_report_is_not_promoted(tmp_path):
    """同一使用者的陳述只是主觀事實：沒有獨立佐證最多 insufficient，不昇格。"""
    env = build_env(tmp_path)
    d = two_concepts(env, same_speaker=True)
    make_bridge(env, d["a"], d["b"])
    hid = propose(env, d["a"], d["b"])
    env.reviewer._llm_fn = ScriptedLLM(llm_reply("supported", [d["ua"], d["ub"]]))
    out = env.reviewer.review(hid)
    h = env.store.get(hid)
    assert out.outcome == "reviewed" and out.status == "insufficient"
    assert h.status == "proposed" and h.attempts == 1 and h.statement is None
    assert "subjective_only_no_independent_corroboration" in h.review.reason
    assert edge_types(env, d["a"], d["b"]) == {"LATENT_BRIDGE"}


def test_gate_rechecks_independence_even_if_reviewer_lies(tmp_path):
    env = build_env(tmp_path)
    d = two_concepts(env, same_speaker=True)
    make_bridge(env, d["a"], d["b"])
    hid = propose(env, d["a"], d["b"])
    claim(env, hid, "tok")
    h = env.store.get(hid)
    review = {"status": "supported", "cited_evidence_ids": [d["ua"], d["ub"]],
              "support_strength": 1.0, "corroborated_by": [d["ua"]]}
    ok, tx = env.gate.process_hypothesis_patch("review", {
        "hypothesis_id": hid, "token": "tok", "result": "supported", "pair_digest": h.pair_digest,
        "review": review, "statement": "有關", "claim_kind": "fact",
        "coverage": {"a": [d["ua"]], "b": [d["ub"]]},
    })
    assert not ok and rejection(tx).startswith("not_supported")
    assert env.store.get(hid).status == "under_review"
    assert edge_types(env, d["a"], d["b"]) == {"LATENT_BRIDGE"}


# ─── Golden：Dream 缺來源不自動 verified ─────────────────────────────────────────


def _bridge_pair_with_agent_evidence(env):
    """A、B 都有使用者來源，並各自多一筆 agent 衍生證據。"""
    d = two_concepts(env, same_speaker=False)
    ep_g = agent_ep(env.engine, "助手推測這兩件事有關")
    for cid in (d["a"], d["b"]):
        c = env.engine.semantic.get(cid)
        c.source_episode_ids.append(ep_g)
    env.engine.semantic._save()
    d["ug"] = unit_ids(env.engine, ep_g)[0]
    make_bridge(env, d["a"], d["b"])
    return d


def test_golden_llm_supported_without_cited_is_not_supported(tmp_path):
    env = build_env(tmp_path)
    d = two_concepts(env, same_speaker=False)
    make_bridge(env, d["a"], d["b"])
    hid = propose(env, d["a"], d["b"])
    env.reviewer._llm_fn = ScriptedLLM(llm_reply("supported", [], statement="有關"))
    out = env.reviewer.review(hid)
    h = env.store.get(hid)
    assert out.status == "insufficient" and h.status == "proposed" and h.statement is None
    assert "no_cited_evidence" in h.review.reason
    assert "RELATES_TO" not in edge_types(env, d["a"], d["b"])


def test_golden_agent_units_are_not_evidence(tmp_path):
    env = build_env(tmp_path)
    d = _bridge_pair_with_agent_evidence(env)
    hid = propose(env, d["a"], d["b"])
    # LLM 只引用助手衍生的 unit：該 unit 根本沒有送審，等同引用不存在 -> 不寫審核
    env.reviewer._llm_fn = ScriptedLLM(llm_reply("supported", [d["ug"]], statement="有關"))
    prompt_holder = env.reviewer._llm_fn
    out = env.reviewer.review(hid)
    h = env.store.get(hid)
    assert out.outcome == "held" and "unknown_evidence_id" in out.reason
    assert h.status == "proposed" and h.review.status == "not_checked"
    assert d["ug"] not in prompt_holder.prompts[0]
    assert "RELATES_TO" not in edge_types(env, d["a"], d["b"])


def test_golden_only_agent_evidence_never_supported(tmp_path):
    """兩端除使用者來源外都沒有可引用證據時，引用單端證據的 supported 也不成立（需覆蓋兩端）。"""
    env = build_env(tmp_path)
    d = two_concepts(env, same_speaker=False)
    make_bridge(env, d["a"], d["b"])
    hid = propose(env, d["a"], d["b"])
    env.reviewer._llm_fn = ScriptedLLM(llm_reply("supported", [d["ua"]], statement="有關"))
    out = env.reviewer.review(hid)
    h = env.store.get(hid)
    assert out.status == "insufficient" and h.status == "proposed"
    assert "citation_not_covering_both_endpoints" in h.review.reason


def test_golden_refuted_without_citation_is_insufficient(tmp_path):
    env = build_env(tmp_path)
    d = two_concepts(env)
    make_bridge(env, d["a"], d["b"])
    hid = propose(env, d["a"], d["b"])
    env.reviewer._llm_fn = ScriptedLLM(llm_reply("refuted", [], statement=None))
    out = env.reviewer.review(hid)
    assert out.status == "insufficient" and env.store.get(hid).status == "proposed"
    assert edge_types(env, d["a"], d["b"]) == {"LATENT_BRIDGE"}  # 缺引用不得移除 bridge


def test_gate_rejects_ineligible_or_missing_citations(tmp_path):
    env = build_env(tmp_path)
    d = _bridge_pair_with_agent_evidence(env)
    hid = propose(env, d["a"], d["b"])
    h = env.store.get(hid)

    def submit(cited, token):
        claim(env, hid, token)
        return env.gate.process_hypothesis_patch("review", {
            "hypothesis_id": hid, "token": token, "result": "supported", "pair_digest": h.pair_digest,
            "review": {"status": "supported", "cited_evidence_ids": cited}, "statement": "有關",
            "claim_kind": "fact", "coverage": {"a": cited, "b": cited},
        })

    ok, tx = submit([d["ua"], d["ug"]], "t1")  # 含助手衍生 unit
    assert not ok and rejection(tx).startswith("cited_unit_ineligible")
    env.gate.process_hypothesis_patch("release", {"hypothesis_id": hid, "token": "t1"})
    ok, tx = submit([d["ua"], "ev_不存在"], "t2")
    assert not ok and rejection(tx).startswith("cited_unit_missing")
    env.gate.process_hypothesis_patch("release", {"hypothesis_id": hid, "token": "t2"})
    ok, tx = submit([], "t3")
    assert not ok and rejection(tx).startswith("no_cited_evidence")
    assert env.store.get(hid).status == "under_review"


def test_no_llm_is_a_safe_noop(tmp_path):
    env = build_env(tmp_path, llm=None)
    d = two_concepts(env)
    make_bridge(env, d["a"], d["b"])
    hid = propose(env, d["a"], d["b"])
    before = env.store.get(hid)
    out = env.reviewer.review(hid)
    assert out.outcome == "skipped" and out.reason == "no_llm"
    report = env.reviewer.review_batch()
    assert report.skipped == 1 and report.reviewed == 0
    after = env.store.get(hid)
    assert after.to_dict() == before.to_dict()  # 不認領、不寫任何東西
    assert env.engine.review_hypothesis(hid)["reason"] == "no_llm"


def test_engine_manual_review_entry(tmp_path):
    env = build_env(tmp_path)
    d = two_concepts(env, same_speaker=False)
    make_bridge(env, d["a"], d["b"])
    hid = propose(env, d["a"], d["b"])
    env.engine._llm_fn = ScriptedLLM(llm_reply("supported", [d["ua"], d["ub"]]))
    result = env.engine.review_hypothesis(hid)
    assert result["outcome"] == "reviewed" and result["status"] == "supported"
    assert env.store.get(hid).status == "supported"
    assert env.engine.review_hypothesis("hyp_missing")["outcome"] == "skipped"


def test_digest_change_during_llm_is_stale(tmp_path):
    env = build_env(tmp_path)
    d = two_concepts(env, same_speaker=False)
    make_bridge(env, d["a"], d["b"])
    hid = propose(env, d["a"], d["b"])

    def mutate_during_llm():
        c = env.engine.semantic.get(d["a"])
        c.description += "（LLM 期間被改動）"
        env.engine.semantic._save()
        return llm_reply("supported", [d["ua"], d["ub"]])

    env.reviewer._llm_fn = ScriptedLLM(mutate_during_llm)
    out = env.reviewer.review(hid)
    h = env.store.get(hid)
    assert out.outcome == "stale"
    assert h.status == "proposed" and h.claim_token == ""  # 租約已釋放，可立刻重審
    assert edge_types(env, d["a"], d["b"]) == {"LATENT_BRIDGE"}


def test_llm_runs_outside_storage_lock(tmp_path):
    from tests.sharing_support import lock_is_free

    env = build_env(tmp_path)
    d = two_concepts(env, same_speaker=False)
    make_bridge(env, d["a"], d["b"])
    hid = propose(env, d["a"], d["b"])
    seen = {}

    def probe():
        seen["free"] = lock_is_free(env.root)
        return llm_reply("insufficient", [d["ua"], d["ub"]], statement=None)

    env.reviewer._llm_fn = ScriptedLLM(probe)
    env.reviewer.review(hid)
    assert seen["free"] is True


# ━━━ refuted ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_refuted_removes_bridge_without_contradicts_and_suppresses(tmp_path):
    env = build_env(tmp_path)
    d = two_concepts(env, same_speaker=False)
    make_bridge(env, d["a"], d["b"])
    hid = propose(env, d["a"], d["b"])
    env.reviewer._llm_fn = ScriptedLLM(llm_reply("refuted", [d["ua"], d["ub"]], statement=None))
    out = env.reviewer.review(hid)
    h = env.store.get(hid)
    assert out.status == "refuted" and h.status == "refuted" and h.statement is None
    assert h.review.status == "contradicted" and h.expires_at is not None
    assert edge_types(env, d["a"], d["b"]) == set()  # bridge 逐條移除
    assert not env.graph.has_contradicts_edge(d["a"], d["b"])  # 不建 CONTRADICTS
    # 抑制：GraphPruning 的 bridge_suppressed 回呼經 Dream 接到唯讀 store
    dream = env.engine._ensure_dream_engine()
    assert dream._pruning._is_suppressed(d["a"], d["b"]) is True
    assert dream._pruning._is_suppressed(d["b"], d["a"]) is True
    # 同 digest 不再提案（即使 bridge 又出現）
    make_bridge(env, d["a"], d["b"])
    assert env.phase.run().proposed == 0
    assert env.store.get(hid).status == "refuted"


def test_refuted_pair_is_not_recreated_by_pruning(tmp_path, monkeypatch):
    env = build_env(tmp_path)
    d = two_concepts(env, same_speaker=False)
    make_bridge(env, d["a"], d["b"])
    hid = propose(env, d["a"], d["b"])
    env.reviewer._llm_fn = ScriptedLLM(llm_reply("refuted", [d["ua"], d["ub"]], statement=None))
    env.reviewer.review(hid)
    dream = env.engine._ensure_dream_engine()
    # 讓兩概念的 embedding 完全相同 -> 相似度 1.0，正常會被建橋
    for cid in (d["a"], d["b"]):
        c = env.engine.semantic.get(cid)
        c.embedding = [1.0] + [0.0] * 511
    env.engine.semantic._save()
    dream._pruning._create_latent_bridges()
    assert "LATENT_BRIDGE" not in edge_types(env, d["a"], d["b"])


# ━━━ 昇格後：漂移、證據消失、重審、回滾 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_drift_reopens_then_non_supported_rereview_unlinks_edge(tmp_path):
    env, d, hid = supported_env(tmp_path)
    c = env.engine.semantic.get(d["a"])
    c.description += "（內容改了）"
    env.engine.semantic._save()
    report = env.phase.maintain()
    h = env.store.get(hid)
    assert report.reopened == 1 and h.status == "proposed"
    assert h.statement is None  # proposed 不存斷言文字
    assert h.outcome.promoted_edge is not None  # 邊還在，等重審決定
    assert edge_types(env, d["a"], d["b"]) == {"RELATES_TO"}
    # 重審非 supported -> 經 Gate unlink 撤回昇格邊
    env.reviewer._llm_fn = ScriptedLLM(llm_reply("insufficient", [d["ua"], d["ub"]], statement=None))
    out = env.reviewer.review(hid)
    h = env.store.get(hid)
    assert out.status == "insufficient" and h.status == "proposed"
    assert h.outcome.promoted_edge is None
    assert edge_types(env, d["a"], d["b"]) == set()


def test_drift_rereview_supported_again_keeps_and_refreshes_edge(tmp_path):
    env, d, hid = supported_env(tmp_path)
    c = env.engine.semantic.get(d["a"])
    c.description += "（內容改了）"
    env.engine.semantic._save()
    env.phase.maintain()
    env.reviewer._llm_fn = ScriptedLLM(llm_reply("supported", [d["ua"], d["ub"]]))
    out = env.reviewer.review(hid)
    h = env.store.get(hid)
    assert out.status == "supported" and h.status == "supported" and h.statement
    assert edge_types(env, d["a"], d["b"]) == {"RELATES_TO"}
    data = env.graph.graph[d["a"]][d["b"]]
    assert data["patch_id"] == h.review.patch_id
    assert env.phase.maintain().reopened == 0  # digest 已更新，不再漂移


def test_rereview_refuted_removes_promoted_edge(tmp_path):
    env, d, hid = supported_env(tmp_path)
    c = env.engine.semantic.get(d["b"])
    c.description += "（內容改了）"
    env.engine.semantic._save()
    env.phase.maintain()
    env.reviewer._llm_fn = ScriptedLLM(llm_reply("refuted", [d["ua"], d["ub"]], statement=None))
    env.reviewer.review(hid)
    h = env.store.get(hid)
    assert h.status == "refuted" and h.outcome.promoted_edge is None
    assert edge_types(env, d["a"], d["b"]) == set()


def test_cited_evidence_redacted_fails_closed(tmp_path):
    env, d, hid = supported_env(tmp_path)
    assert env.engine.list_hypotheses(status="supported")[0]["statement"]
    env.engine.evidence.redact([d["ua"]])  # forget／M-8 遮蔽
    # 讀取時重解析：statement 不再帶出
    item = env.engine.list_hypotheses(status="supported")[0]
    assert item["statement"] is None and item["stale"] == "evidence_gone"
    # 維護階段把它退回 proposed 重審
    report = env.phase.maintain()
    assert report.reopened == 1 and env.store.get(hid).status == "proposed"


def test_endpoint_gone_expires_supported_hypothesis(tmp_path):
    env, d, hid = supported_env(tmp_path)
    c = env.engine.semantic.get(d["b"])
    c.status = "archived"
    env.engine.semantic._save()
    report = env.phase.maintain()
    h = env.store.get(hid)
    assert report.expired == 1 and h.status == "expired" and h.outcome.expire_reason == "endpoint_gone"


def test_rollback_review_restores_edges_and_record(tmp_path):
    env = build_env(tmp_path)
    d = two_concepts(env, same_speaker=False)
    make_bridge(env, d["a"], d["b"])
    hid = propose(env, d["a"], d["b"])
    bridge_before = {
        (s, t): dict(env.graph.graph[s][t]) for s, t in ((d["a"], d["b"]), (d["b"], d["a"]))
    }
    claim(env, hid, "tok")
    before = env.store.get(hid).to_dict()
    h = env.store.get(hid)
    ok, tx = env.gate.process_hypothesis_patch("review", {
        "hypothesis_id": hid, "token": "tok", "result": "supported", "pair_digest": h.pair_digest,
        "review": {"status": "supported", "cited_evidence_ids": [d["ua"], d["ub"]]},
        "statement": "兩者有關", "claim_kind": "fact",
        "coverage": {"a": [d["ua"]], "b": [d["ub"]]},
    })
    assert ok and edge_types(env, d["a"], d["b"]) == {"RELATES_TO"}
    env.gate.rollback_hypothesis_patch(tx)
    # 完整還原：bridge 邊（含原屬性）、無昇格邊、紀錄回到 under_review
    assert edge_types(env, d["a"], d["b"]) == {"LATENT_BRIDGE"}
    for (s, t), attrs in bridge_before.items():
        assert dict(env.graph.graph[s][t]) == attrs
    restored = env.store.get(hid).to_dict()
    for key in ("status", "statement", "pair_digest", "claim_token", "outcome", "review"):
        assert restored[key] == before[key]
    assert tx.status == "rolled_back"


def test_failed_apply_rolls_back_edges_and_record(tmp_path, monkeypatch):
    env = build_env(tmp_path)
    d = two_concepts(env, same_speaker=False)
    make_bridge(env, d["a"], d["b"])
    hid = propose(env, d["a"], d["b"])
    claim(env, hid, "tok")
    h = env.store.get(hid)
    bridge_before = dict(env.graph.graph[d["a"]][d["b"]])
    writer = env.gate._hypothesis_writer()

    def boom(*a, **k):
        raise OSError("磁碟寫入失敗")

    monkeypatch.setattr(writer, "put", boom)
    ok, tx = env.gate.process_hypothesis_patch("review", {
        "hypothesis_id": hid, "token": "tok", "result": "supported", "pair_digest": h.pair_digest,
        "review": {"status": "supported", "cited_evidence_ids": [d["ua"], d["ub"]]},
        "statement": "兩者有關", "claim_kind": "fact",
        "coverage": {"a": [d["ua"]], "b": [d["ub"]]},
    })
    assert not ok and tx.status == "failed"
    assert edge_types(env, d["a"], d["b"]) == {"LATENT_BRIDGE"}
    assert dict(env.graph.graph[d["a"]][d["b"]]) == bridge_before
    assert env.store.get(hid).status == "under_review"


def test_audit_failure_blocks_all_mutation(tmp_path, monkeypatch):
    env = build_env(tmp_path)
    d = two_concepts(env)
    make_bridge(env, d["a"], d["b"])
    monkeypatch.setattr(env.gate, "_try_write_audit", lambda tx: False)
    report = env.phase.propose()
    assert report.proposed == 0 and report.rejected == 1
    assert env.store.count() == 0


def test_promotion_blocked_when_contradicts_appears(tmp_path):
    env = build_env(tmp_path)
    d = two_concepts(env, same_speaker=False)
    make_bridge(env, d["a"], d["b"])
    hid = propose(env, d["a"], d["b"])
    env.reviewer._llm_fn = ScriptedLLM(
        lambda: (env.graph.add_contradicts_edge(d["a"], d["b"], weight=0.7), env.graph.save(),
                 llm_reply("supported", [d["ua"], d["ub"]]))[2],
    )
    out = env.reviewer.review(hid)
    h = env.store.get(hid)
    assert out.outcome == "held" and "pair_contradicts" in out.reason
    assert h.status == "proposed" and env.graph.has_contradicts_edge(d["a"], d["b"])
    assert "RELATES_TO" not in edge_types(env, d["a"], d["b"])


# ━━━ 檢索：預設不進召回 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _stub_retrieval(env, concept_ids):
    from echo_memory.schemas.memory_context import MemoryContext

    concepts = [env.engine.semantic.get(c) for c in concept_ids]

    def fake_retrieve(**kwargs):
        return MemoryContext(concepts=list(concepts))

    env.engine.retrieval.retrieve = fake_retrieve  # type: ignore[method-assign]


def test_retrieve_excludes_hypotheses_by_default(tmp_path):
    env, d, hid = supported_env(tmp_path)
    _stub_retrieval(env, [d["a"]])
    ctx = env.engine.retrieve("咖啡")
    assert ctx.hypotheses == []
    assert "候選關聯" not in ctx.to_text()


def test_retrieve_include_hypotheses_is_separate_and_labeled(tmp_path):
    env, d, hid = supported_env(tmp_path)
    _stub_retrieval(env, [d["a"]])
    ctx = env.engine.retrieve("咖啡", include_hypotheses=True)
    assert len(ctx.hypotheses) == 1
    item = ctx.hypotheses[0]
    assert item.status == "supported" and item.statement and {item.name_a, item.name_b} == {"咖啡拉花", "早晨咖啡店"}
    text = ctx.to_text()
    assert "候選關聯（未驗證，不可當事實）" in text
    # 獨立欄位：不混進概念清單
    assert all(c.id in (d["a"],) for c in ctx.concepts)


def test_retrieve_proposed_lists_endpoints_only(tmp_path):
    env = build_env(tmp_path)
    d = two_concepts(env)
    make_bridge(env, d["a"], d["b"])
    propose(env, d["a"], d["b"])
    _stub_retrieval(env, [d["b"]])
    ctx = env.engine.retrieve("x", include_hypotheses=True)
    assert len(ctx.hypotheses) == 1
    assert ctx.hypotheses[0].status == "proposed" and ctx.hypotheses[0].statement is None


def test_retrieve_requires_endpoint_in_recall_seeds(tmp_path):
    env = build_env(tmp_path)
    d = two_concepts(env)
    make_bridge(env, d["a"], d["b"])
    propose(env, d["a"], d["b"])
    ep_c = user_ep(env.engine, "無關的來源")
    c = make_concept(env, "無關概念", [ep_c])
    _stub_retrieval(env, [c])
    assert env.engine.retrieve("x", include_hypotheses=True).hypotheses == []


def test_retrieve_excludes_refuted_expired_and_stale_supported(tmp_path, cfg):
    env = build_env(tmp_path)
    cfg(hypothesis_max_new_per_cycle=5)
    pairs = [make_pair(env, f"r{i}", speaker_b="u2") for i in range(3)]
    env.phase.propose()
    refuted_id = env.store.get_by_pair(pairs[0]["a"], pairs[0]["b"]).id
    env.reviewer._llm_fn = ScriptedLLM(
        llm_reply("refuted", [pairs[0]["ua"], pairs[0]["ub"]], statement=None),
    )
    env.reviewer.review(refuted_id)
    assert env.store.get(refuted_id).status == "refuted"
    cfg(hypothesis_proposed_ttl_days=-1.0)  # 讓其餘維持 proposed，但把第 2 筆直接過期
    h2 = env.store.get_by_pair(pairs[1]["a"], pairs[1]["b"])
    c = env.engine.semantic.get(pairs[1]["b"])
    c.status = "deprecated"
    env.engine.semantic._save()
    env.phase.maintain()
    assert env.store.get(h2.id).status == "expired"
    _stub_retrieval(env, [pairs[0]["a"], pairs[1]["a"]])
    assert env.engine.retrieve("x", include_hypotheses=True).hypotheses == []


def test_retrieve_stale_supported_is_not_brought_up(tmp_path):
    env, d, hid = supported_env(tmp_path)
    env.engine.evidence.redact([d["ub"]])
    _stub_retrieval(env, [d["a"]])
    assert env.engine.retrieve("x", include_hypotheses=True).hypotheses == []


def test_retrieve_caps_at_two_and_never_in_shared_turn(tmp_path, cfg):
    env = build_env(tmp_path)
    cfg(hypothesis_max_new_per_cycle=5)
    pairs = [make_pair(env, f"m{i}") for i in range(3)]
    env.phase.propose()
    seeds = [p["a"] for p in pairs]
    _stub_retrieval(env, seeds)
    assert len(env.engine.retrieve("x", include_hypotheses=True).hypotheses) == 2
    assert env.engine.retrieve("x", include_hypotheses=True, shared_turn=True).hypotheses == []


def test_retrieve_never_includes_hypotheses_for_shared_namespace(tmp_path):
    env = build_env(tmp_path, namespace_id="scope-g42")
    d = two_concepts(env)
    _stub_retrieval(env, [d["a"]])
    assert env.engine.retrieve("x", include_hypotheses=True).hypotheses == []


# ━━━ Dream 整合 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_dream_runs_hypothesis_after_evidence_review_and_before_sharing(tmp_path):
    env = build_env(tmp_path, llm=lambda p: "{}")
    dream = env.engine._ensure_dream_engine()
    order: list[str] = []

    def spy(owner, attr, tag):
        fn = getattr(owner, attr)

        def wrapped(*a, **k):
            order.append(tag)
            return fn(*a, **k)

        setattr(owner, attr, wrapped)

    spy(dream._evidence_reviewer, "review_batch", "evidence_review")
    spy(dream._hypothesis_phase, "run", "hypothesis")
    spy(dream._hypothesis_reviewer, "review_batch", "hypothesis_review")
    if dream._sharing_phase is not None:
        spy(dream._sharing_phase, "run", "sharing")
    dream.dream_now(triggered_by="manual")
    assert order[:3] == ["evidence_review", "hypothesis", "hypothesis_review"]
    if "sharing" in order:
        assert order.index("sharing") > order.index("hypothesis_review")


def test_dream_without_llm_proposes_but_does_not_review(tmp_path):
    env = build_env(tmp_path, llm=None)
    d = two_concepts(env)
    make_bridge(env, d["a"], d["b"])
    dream = env.engine._ensure_dream_engine()
    report = dream.dream_now(triggered_by="manual")
    assert report.error is None
    assert report.hypothesis.proposed == 1
    assert report.hypothesis_review.skipped == 1 and report.hypothesis_review.reviewed == 0
    assert env.store.get_by_pair(d["a"], d["b"]).status == "proposed"
    assert "假說" in report.summary()


def test_dream_end_to_end_supported(tmp_path):
    def llm(prompt: str) -> str:
        if "假說審核員" in prompt:
            return reply_holder["reply"]
        return "{}"

    reply_holder = {"reply": ""}
    env = build_env(tmp_path, llm=llm)
    d = two_concepts(env, same_speaker=False)
    make_bridge(env, d["a"], d["b"])
    reply_holder["reply"] = llm_reply("supported", [d["ua"], d["ub"]])
    dream = env.engine._ensure_dream_engine()
    first = dream.dream_now(triggered_by="manual")
    assert first.error is None
    h = env.store.get_by_pair(d["a"], d["b"])
    assert h is not None
    # 同一週期內：提案後立刻審核
    assert h.status == "supported", (first.hypothesis_review.outcomes if first.hypothesis_review else None)
    assert edge_types(env, d["a"], d["b"]) == {"RELATES_TO"}


def test_dream_shared_namespace_skips_hypothesis_phase(tmp_path):
    env = build_env(tmp_path, llm=lambda p: "{}", namespace_id="scope-g42")
    dream = env.engine._ensure_dream_engine()
    assert dream._hypothesis_reviewer is None
    report = dream.dream_now(triggered_by="manual")
    assert report.hypothesis.skipped_reason in ("disabled", "shared_namespace")
    assert report.hypothesis_review is None


# ━━━ 工具入口：只讀查詢 + 請求審核 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_llm_tools_list_and_request_review(tmp_path):
    from llm_integration.tools.executor import ToolExecutor

    env = build_env(tmp_path)
    d = two_concepts(env, same_speaker=False)
    make_bridge(env, d["a"], d["b"])
    hid = propose(env, d["a"], d["b"])
    ex = ToolExecutor(env.engine, "s1")
    listed = ex.execute("list_hypotheses", {})
    assert listed["success"] and hid in listed["result"] and "UNVERIFIED" in listed["result"]
    env.engine._llm_fn = ScriptedLLM(llm_reply("supported", [d["ua"], d["ub"]]))
    res = ex.execute("request_hypothesis_review", {"hypothesis_id": hid})
    assert res["success"] and "reviewed" in res["result"]
    assert env.store.get(hid).status == "supported"
    assert not ex.execute("request_hypothesis_review", {})["success"]


def test_llm_tool_declarations_have_no_action_tools():
    from llm_integration.tools.definitions import TOOL_DECLARATIONS

    names = {t["name"] for t in TOOL_DECLARATIONS}
    assert {"list_hypotheses", "request_hypothesis_review"} <= names
    # 沒有任何可直接改變假說狀態／圖譜的入口
    assert not names & {"promote_hypothesis", "approve_hypothesis", "refute_hypothesis", "delete_hypothesis"}


def test_mcp_handlers_route_hypothesis_tools_through_policy():
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "mcp_server" / "handlers.py").read_text(encoding="utf-8")
    assert "_handle_list_hypotheses" in text and "_handle_request_hypothesis_review" in text
    review = text.split("async def _handle_request_hypothesis_review")[1]
    assert "write=True" in review  # 共享 namespace 一律拒絕寫入類
    listing = text.split("async def _handle_list_hypotheses")[1].split("async def _handle_request")[0]
    assert "write=True" not in listing and "_get_engine(" in listing
    # 沒有其他外部行動入口
    assert "promote_hypothesis" not in text and "approve_hypothesis" not in text


def test_make_pair_key_matches_bridge_id(tmp_path):
    env = build_env(tmp_path)
    d = two_concepts(env)
    key = make_bridge(env, d["a"], d["b"])
    assert key == make_pair_key(d["b"], d["a"])
    assert env.graph.iter_latent_bridges()[0].bridge_id == key
