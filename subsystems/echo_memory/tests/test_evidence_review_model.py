"""
tests/test_evidence_review_model.py — M4 包 A：證據審核資料模型與 Patch Gate

涵蓋：EvidenceReview 序列化、basis_digest 規則、effective_review_status、
review patch 的 apply／rollback／audit-before-mutate／stale／held 分支、confidence 重算，
以及 Golden G6（版本綁定）、G8（舊 JSON 相容）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from echo_memory.affect.concept_inhibitor import ConceptInhibitor, _producer_for_kind
from echo_memory.affect.concept_patch import ConceptPatch, PatchEvidence
from echo_memory.config import get_config
from echo_memory.schemas.concept import Concept
from echo_memory.schemas.evidence_review import (
    EvidenceReview,
    compute_basis_digest,
    compute_review_confidence,
)
from echo_memory.schemas.provenance import PRODUCERS, Derivation
from echo_memory.storage.semantic import SemanticMemory


@pytest.fixture
def semantic(tmp_path: Path) -> SemanticMemory:
    sem = SemanticMemory(storage_path=tmp_path / "semantic")
    for name, desc in [("喜歡咖啡", "使用者喜歡喝咖啡"), ("討厭香菜", "使用者討厭香菜")]:
        c = Concept(
            name=name, description=desc, embedding=[1.0, 0.0],
            source_episode_ids=["ep1", "ep2"],
        )
        sem._concepts[name] = c
        sem._concepts_by_id[c.id] = c
    sem._save()
    return sem


@pytest.fixture
def inhibitor(semantic) -> ConceptInhibitor:
    return ConceptInhibitor(semantic=semantic, llm_fn=None)


def _coffee(semantic) -> Concept:
    return semantic.get_by_name("喜歡咖啡")


def _review(concept: Concept, status="supported", strength=0.95, **kw) -> EvidenceReview:
    base = dict(
        status=status,
        support_strength=strength,
        cited_evidence_ids=["u1"],
        scope={"subject_ref": "user", "context": None},
        independent_count=2,
        user_attested_count=1,
        basis_digest=compute_basis_digest(concept),
        reviewer="llm",
        reason="原文明確",
    )
    base.update(kw)
    return EvidenceReview(**base)


def _audit(semantic) -> list[dict]:
    path = semantic.storage_path / "audit_log.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# ─── EvidenceReview 序列化 ───────────────────────────────────────────────────


class TestEvidenceReviewSchema:
    def test_roundtrip(self):
        now = datetime.now(timezone.utc)
        r = EvidenceReview(
            status="supported", support_strength=0.7, cited_evidence_ids=["a", "b"],
            scope={"subject_ref": "user"}, independent_count=3, user_attested_count=2,
            legacy_unknown_count=1, basis_digest="abc", reviewed_at_version=5,
            reviewed_at=now, reviewer="llm", reason="r", patch_id="patch_x",
        )
        back = EvidenceReview.from_dict(r.to_dict())
        assert back == r

    def test_from_dict_tolerates_empty_unknown_and_bad_values(self):
        assert EvidenceReview.from_dict({}).status == "not_checked"
        assert EvidenceReview.from_dict(None).status == "not_checked"
        r = EvidenceReview.from_dict(
            {"status": "weird", "support_strength": 9, "foo": 1, "independent_count": "x"},
        )
        assert r.status == "not_checked"
        assert r.support_strength == 1.0
        assert r.independent_count == 0

    def test_producers_include_reviewer(self):
        assert "reviewer" in PRODUCERS
        assert Derivation("evidence", "e1", producer="reviewer").producer == "reviewer"
        assert _producer_for_kind("evidence_review") == "reviewer"


# ─── basis_digest ────────────────────────────────────────────────────────────


class TestBasisDigest:
    def test_stable_and_ignores_non_basis_fields(self):
        c = Concept(name="x", description="d", source_episode_ids=["e1"])
        d0 = compute_basis_digest(c)
        c.conflict_count += 2
        c.confidence = 0.1
        c.status = "deprecated"
        c.metadata["support_source"] = "user"
        c.metadata["has_contradiction"] = True
        c.record_mutation()
        assert compute_basis_digest(c) == d0

    def test_changes_on_description_fact_source_derivation(self):
        c = Concept(name="x", description="d", source_episode_ids=["e1"])
        seen = {compute_basis_digest(c)}
        c.description = "d2"
        seen.add(compute_basis_digest(c))
        c.metadata["facts"] = [{"k": "v"}]
        seen.add(compute_basis_digest(c))
        c.add_source_episode("e2")  # 同時加 source_episode_ids 與 derivation
        seen.add(compute_basis_digest(c))
        c.add_derivation("concept", "con_p", "dream", parent_version=3)
        seen.add(compute_basis_digest(c))
        assert len(seen) == 5

    def test_order_independent(self):
        a = Concept(name="x", description="d", source_episode_ids=["e1", "e2"])
        b = Concept(name="x", description="d", source_episode_ids=["e2", "e1"])
        assert compute_basis_digest(a) == compute_basis_digest(b)


# ─── effective_review_status ────────────────────────────────────────────────


class TestEffectiveStatus:
    def test_default_not_checked(self):
        assert Concept(name="x", description="d").effective_review_status() == "not_checked"

    def test_valid_and_invalidated(self):
        c = Concept(name="x", description="d")
        c.evidence_review = _review(c).to_dict()
        assert c.effective_review_status() == "supported"
        c.description = "changed"
        assert c.effective_review_status() == "not_checked"

    def test_missing_digest_is_not_checked(self):
        c = Concept(name="x", description="d")
        c.evidence_review = {"status": "supported", "basis_digest": ""}
        assert c.effective_review_status() == "not_checked"


# ─── process_review_patch ────────────────────────────────────────────────────


class TestProcessReviewPatch:
    def test_supported_applies_and_recomputes(self, semantic, inhibitor):
        c = _coffee(semantic)
        v0 = c.version
        ok, tx = inhibitor.process_review_patch(c.id, _review(c))
        assert ok and tx.status == "committed"
        c = _coffee(semantic)
        assert c.effective_review_status() == "supported"
        assert c.version == v0 + 1
        assert c.last_verified_at is not None
        assert c.source_diversity == 2
        expect = compute_review_confidence("supported", 0.95, 2, None)
        assert c.confidence == pytest.approx(expect)
        # 0.5 + 0.5*0.95 = 0.975；base(2)=0.667
        assert c.confidence == pytest.approx((2 / 3) * 0.975)
        assert c.evidence_review["patch_id"] == tx.patches[0].patch_id
        assert c.evidence_review["reviewed_at_version"] == v0
        # 不新增 derivation
        assert c.derivations == []
        # audit 有 applied 紀錄
        rows = _audit(semantic)
        assert rows[-1]["patches"][0]["action"] == "review"
        assert rows[-1]["patches"][0]["status"] == "applied"
        # 存檔後重載仍有效
        again = SemanticMemory(storage_path=semantic.storage_path)
        assert again.get(c.id).effective_review_status() == "supported"

    def test_insufficient_does_not_stamp_verified(self, semantic, inhibitor):
        c = _coffee(semantic)
        ok, _ = inhibitor.process_review_patch(c.id, _review(c, "insufficient", 1.0))
        assert ok
        c = _coffee(semantic)
        assert c.effective_review_status() == "insufficient"
        assert c.last_verified_at is None
        assert c.confidence <= get_config().review_confidence_cap_insufficient + 1e-9

    def test_confidence_arg_is_capped(self, semantic, inhibitor):
        c = _coffee(semantic)
        ok, _ = inhibitor.process_review_patch(c.id, _review(c, "contradicted"), confidence=0.99)
        assert ok
        assert _coffee(semantic).confidence == pytest.approx(
            get_config().review_confidence_cap_contradicted
        )

    def test_agent_only_confidence_bounded(self):
        # n 很大也受 cap=0.45 與 source_f=0.5 限制
        v = compute_review_confidence("insufficient", 1.0, 100, "agent_only")
        assert v is not None and v <= 0.45
        assert compute_review_confidence("not_checked", 1.0, 3, "user") is None

    def test_rejects_agent_only_supported(self, semantic, inhibitor):
        c = _coffee(semantic)
        c.metadata["support_source"] = "agent_only"
        before = c.to_dict()
        ok, tx = inhibitor.process_review_patch(c.id, _review(c))
        assert not ok and tx.patches[0].status == "rejected"
        assert _coffee(semantic).to_dict() == before
        assert _audit(semantic)[-1]["patches"][0]["status"] == "rejected"

    def test_rejects_invalid_status_and_missing_digest(self, semantic, inhibitor):
        c = _coffee(semantic)
        for bad in (
            _review(c, "not_checked"),
            _review(c, basis_digest=""),
        ):
            ok, tx = inhibitor.process_review_patch(c.id, bad)
            assert not ok
            assert tx.patches[0].status == "rejected"
        assert _coffee(semantic).evidence_review == {}

    def test_rejects_stale_digest(self, semantic, inhibitor):
        c = _coffee(semantic)
        r = _review(c)
        c.description = "使用者其實不喝咖啡"  # 送審後內容變了
        ok, tx = inhibitor.process_review_patch(c.id, r)
        assert not ok
        assert "stale" in tx.patches[0].rejection_reason
        assert _coffee(semantic).evidence_review == {}

    def test_missing_concept(self, inhibitor):
        ok, tx = inhibitor.process_review_patch("con_nope", EvidenceReview(status="supported"))
        assert not ok and tx.patches == [] and tx.status == "failed"

    def test_audit_before_mutate(self, semantic, inhibitor, monkeypatch):
        c = _coffee(semantic)
        before = c.to_dict()
        monkeypatch.setattr(inhibitor, "_try_write_audit", lambda tx: False)
        ok, tx = inhibitor.process_review_patch(c.id, _review(c))
        assert not ok and tx.status == "failed"
        assert _coffee(semantic).to_dict() == before

    def test_no_llm_involved(self, semantic):
        calls: list[str] = []

        def llm(prompt: str) -> str:
            calls.append(prompt)
            raise AssertionError("review patch 不應呼叫 LLM")

        inh = ConceptInhibitor(semantic=semantic, llm_fn=llm)
        c = _coffee(semantic)
        ok, _ = inh.process_review_patch(c.id, _review(c))
        assert ok and calls == []


# ─── rollback ────────────────────────────────────────────────────────────────


class TestReviewRollback:
    def test_rollback_restores_everything_but_version(self, semantic, inhibitor):
        c = _coffee(semantic)
        c.last_verified_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        c.source_diversity = 1
        c.confidence = 0.42
        c.evidence_review = _review(c, "insufficient", 0.3).to_dict()
        semantic._save()
        snap = c.to_dict()

        ok, tx = inhibitor.process_review_patch(
            c.id, _review(c, "supported", 0.9, independent_count=4),
        )
        assert ok
        after = _coffee(semantic)
        assert after.effective_review_status() == "supported"
        assert after.confidence != 0.42

        inhibitor._rollback_patch(tx.patches[0])
        back = _coffee(semantic)
        for key in ("evidence_review", "confidence", "last_verified_at", "source_diversity"):
            assert back.to_dict()[key] == snap[key], key
        assert back.version > snap["version"]  # version 單調，不回退

    def test_rollback_from_empty_review(self, semantic, inhibitor):
        c = _coffee(semantic)
        c0 = c.confidence
        ok, tx = inhibitor.process_review_patch(c.id, _review(c))
        assert ok
        inhibitor._rollback_patch(tx.patches[0])
        back = _coffee(semantic)
        assert back.evidence_review == {}
        assert back.effective_review_status() == "not_checked"
        assert back.confidence == c0
        assert back.last_verified_at is None
        assert back.source_diversity == 0
        # patch_history 的 review 條目也被移除
        assert not [h for h in back.patch_history if h["action"] == "review"]


# ─── held / stale ────────────────────────────────────────────────────────────


class TestHeldStale:
    def _held_patch(self, concept: Concept) -> ConceptPatch:
        return ConceptPatch(
            transaction_id="tx_x", concept_id=concept.id, concept_name=concept.name,
            action="review", target_path="evidence_review",
            old_value={}, new_value=_review(concept).to_dict(),
            evidence=PatchEvidence(
                evidence_kind="evidence_review", evidence_strength=0.7,
                verifier_status="valid", verifier_reason="r",
            ),
            risk_level="low", status="held",
        )

    def test_stale_when_digest_changes(self, semantic, inhibitor):
        c = _coffee(semantic)
        p = self._held_patch(c)
        assert inhibitor._is_stale_patch(p) is False
        c.metadata["facts"] = [{"text": "新事實"}]
        assert inhibitor._is_stale_patch(p) is True

    def test_not_stale_after_conflict_mark(self, semantic, inhibitor):
        a, b = _coffee(semantic), semantic.get_by_name("討厭香菜")
        p = self._held_patch(a)
        ok, _ = inhibitor.process_conflict_mark(a.id, b.id)
        assert ok
        assert inhibitor._is_stale_patch(p) is False

    def test_evaluate_rule_approves_without_llm(self, semantic, inhibitor):
        p = self._held_patch(_coffee(semantic))
        p.status = "pending"
        inhibitor._evaluate_patch(p, "", "")
        assert p.status == "approved"
        p.new_value = {"status": "not_checked", "basis_digest": "x"}
        p.status = "pending"
        inhibitor._evaluate_patch(p, "", "")
        assert p.status == "rejected"

    def test_held_registry_review_retry_applies(self, semantic):
        """held 的 review patch 重審：digest 仍相符時規則層直接 apply，不需 LLM 判斷內容。"""
        inh = ConceptInhibitor(semantic=semantic, llm_fn=lambda p: "{}")
        c = _coffee(semantic)
        p = self._held_patch(c)
        # 手動登記進 held 登記簿
        from echo_memory.affect.concept_patch import PatchTransaction
        tx = PatchTransaction(
            patches=[p], user_input="", llm_response="", session_id="s", turn_id="t",
            triggered_by="dream",
        )
        inh._register_held(p, tx, "", "")
        results = inh.review_held_patches()
        assert results and results[0].patches[0].status == "applied"
        assert _coffee(semantic).effective_review_status() == "supported"

    def test_held_registry_review_stale_rejected(self, semantic):
        inh = ConceptInhibitor(semantic=semantic, llm_fn=lambda p: "{}")
        c = _coffee(semantic)
        p = self._held_patch(c)
        from echo_memory.affect.concept_patch import PatchTransaction
        tx = PatchTransaction(
            patches=[p], user_input="", llm_response="", session_id="s", turn_id="t",
            triggered_by="dream",
        )
        inh._register_held(p, tx, "", "")
        c.description = "改過了"
        results = inh.review_held_patches()
        assert results[0].patches[0].status == "rejected"
        assert _coffee(semantic).evidence_review == {}


# ─── Golden G6：版本綁定 ─────────────────────────────────────────────────────


class TestG6VersionBinding:
    def test_review_survives_mark_conflict_and_review_itself(self, semantic, inhibitor):
        a, b = _coffee(semantic), semantic.get_by_name("討厭香菜")
        ok, _ = inhibitor.process_review_patch(a.id, _review(a))
        assert ok
        v_after_review = _coffee(semantic).version
        ok, _ = inhibitor.process_conflict_mark(a.id, b.id)
        assert ok
        a = _coffee(semantic)
        assert a.version > v_after_review and a.conflict_count == 1
        assert a.effective_review_status() == "supported"

    def test_invalidated_by_expand_and_modify(self, semantic, inhibitor):
        a = _coffee(semantic)
        ok, _ = inhibitor.process_review_patch(a.id, _review(a))
        assert ok
        a = _coffee(semantic)
        a.metadata.setdefault("facts", []).append({"text": "每天早上喝"})  # expand
        assert a.effective_review_status() == "not_checked"
        a.metadata["facts"].clear()
        assert a.effective_review_status() == "supported"  # 回到原基礎又有效
        a.description = "使用者喜歡喝拿鐵"  # modify
        assert a.effective_review_status() == "not_checked"

    def test_support_source_upgrade_keeps_review(self, semantic, inhibitor):
        a = _coffee(semantic)
        ok, _ = inhibitor.process_review_patch(a.id, _review(a))
        assert ok
        a = _coffee(semantic)
        a.metadata["support_source"] = "user"
        assert a.effective_review_status() == "supported"


# ─── Golden G8：舊 JSON 相容 ─────────────────────────────────────────────────


class TestG8Compat:
    def test_old_json_loads_as_not_checked(self, tmp_path: Path):
        sem = SemanticMemory(storage_path=tmp_path / "semantic")
        c = Concept(name="舊概念", description="舊資料", embedding=[1.0, 0.0],
                    source_episode_ids=["ep1"])
        sem._concepts[c.name] = c
        sem._concepts_by_id[c.id] = c
        sem._save()

        # 模擬舊檔：移除 evidence_review 欄位
        path = next((tmp_path / "semantic").glob("*.json"))
        raw = json.loads(path.read_text(encoding="utf-8"))
        stripped = 0

        def strip(obj):
            nonlocal stripped
            if isinstance(obj, dict):
                if "evidence_review" in obj:
                    obj.pop("evidence_review")
                    stripped += 1
                for v in obj.values():
                    strip(v)
            elif isinstance(obj, list):
                for v in obj:
                    strip(v)

        strip(raw)
        assert stripped >= 1
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        again = SemanticMemory(storage_path=tmp_path / "semantic")
        loaded = again.get_by_name("舊概念")
        assert loaded is not None
        assert loaded.evidence_review == {}
        assert loaded.effective_review_status() == "not_checked"
        assert 0.0 <= loaded.health_score <= 1.0  # 既有讀者不 crash

    def test_from_dict_without_field(self):
        d = Concept(name="x", description="d").to_dict()
        d.pop("evidence_review")
        c = Concept.from_dict(d)
        assert c.effective_review_status() == "not_checked"
        assert "evidence_review" in c.to_dict()


# ─── §8 學習程度 ─────────────────────────────────────────────────────────────


def _unit(uid="u1", source_type="user", **attrs):
    from echo_memory.schemas.evidence import EvidenceUnit

    return EvidenceUnit(
        id=uid, episode_id=f"ep_{uid}", source_type=source_type, content="x",
        occurred_at=datetime.now(timezone.utc), attrs=dict(attrs),
    )


class TestLearningWeight:
    @pytest.mark.parametrize("n,w", [(1, 1.0), (2, 0.6), (3, 0.4), (4, 0.25), (5, 0.08), (12, 0.08)])
    def test_base_table(self, n, w):
        from echo_memory.schemas.evidence import learning_weight

        assert learning_weight(_unit(audience_size=n)) == pytest.approx(w)

    @pytest.mark.parametrize(
        "n,expect",
        [(1, 1.0), (2, 0.8), (3, 0.4 + 0.6 * 0.35), (4, 0.25 + 0.75 * 0.2), (5, 0.08 + 0.92 * 0.1)],
    )
    def test_addressed_beta_table(self, n, expect):
        from echo_memory.schemas.evidence import learning_weight

        assert learning_weight(_unit(audience_size=n, addressed_to_agent=True)) == pytest.approx(expect)

    def test_addressed_false_or_missing_no_boost(self):
        from echo_memory.schemas.evidence import learning_weight

        assert learning_weight(_unit(audience_size=2, addressed_to_agent=False)) == pytest.approx(0.6)

    def test_dm_counts_as_one(self):
        from echo_memory.schemas.evidence import learning_weight

        assert learning_weight(_unit(channel_kind="dm", audience_size=6)) == 1.0

    def test_unknown_defaults(self):
        from echo_memory.schemas.evidence import learning_weight

        cfg = get_config()
        assert cfg.learning_weight_unknown == 0.6
        assert learning_weight(_unit()) == pytest.approx(0.6)  # 舊 unit 無 attrs
        assert learning_weight(_unit(audience_size="abc")) == pytest.approx(0.6)
        assert learning_weight(_unit(audience_size=0)) == pytest.approx(0.6)
        # 缺資訊不套 addressed 加權
        assert learning_weight(_unit(addressed_to_agent=True)) == pytest.approx(0.6)

    @pytest.mark.parametrize("st", ["agent", "tool", "observation"])
    def test_non_user_zero(self, st):
        from echo_memory.schemas.evidence import learning_weight

        assert learning_weight(_unit(source_type=st, audience_size=1)) == 0.0


class TestSupportStrength:
    def test_noisy_or(self):
        from echo_memory.schemas.evidence_review import noisy_or

        assert noisy_or([]) == 0.0
        assert noisy_or([0.6, 0.6]) == pytest.approx(1 - 0.4 * 0.4)
        assert noisy_or([1.0, 0.1]) == 1.0
        assert noisy_or([2, -1]) == 1.0  # 截到 [0,1]

    def test_dedup_and_user_only(self):
        from echo_memory.schemas.evidence_review import compute_support_strength

        a = _unit("a", audience_size=2)
        units = [a, a, _unit("b", source_type="agent"), _unit("c", source_type="observation")]
        assert compute_support_strength(units) == pytest.approx(0.6)

    def test_threshold_decision(self):
        from echo_memory.schemas.evidence_review import (
            compute_support_strength,
            decide_review_status,
        )

        assert get_config().support_threshold == 0.9
        one_on_one = compute_support_strength([_unit("a", audience_size=1)])
        two_ppl = compute_support_strength([_unit("a", audience_size=2)])
        two_ppl_addr = compute_support_strength([_unit("a", audience_size=2, addressed_to_agent=True)])
        five_ppl = compute_support_strength([_unit("a", audience_size=5)])
        assert decide_review_status("supported", one_on_one) == "supported"
        assert decide_review_status("supported", two_ppl) == "insufficient"
        assert decide_review_status("supported", two_ppl_addr) == "insufficient"  # 0.8 < 0.9
        # 兩人場合在不同場合累積
        both = compute_support_strength(
            [_unit("a", audience_size=2), _unit("b", audience_size=2, addressed_to_agent=True)]
        )
        assert both == pytest.approx(1 - 0.4 * 0.2)
        assert decide_review_status("supported", both) == "supported"  # 0.92 >= 0.9
        assert decide_review_status("supported", five_ppl) == "insufficient"

    def test_non_supported_pass_through(self):
        from echo_memory.schemas.evidence_review import decide_review_status

        assert decide_review_status("contradicted", 0.1) == "contradicted"
        assert decide_review_status("insufficient", 1.0) == "insufficient"
        assert decide_review_status("garbage", 1.0) == "insufficient"

    def test_intention_needs_corroboration(self):
        from echo_memory.schemas.evidence_review import decide_review_status

        assert decide_review_status("supported", 1.0, "intention", []) == "insufficient"
        assert decide_review_status("supported", 0.3, "intention", ["obs1"]) == "supported"
        assert decide_review_status("supported", 1.0, "preference") == "supported"


class TestClaimKindGate:
    def test_schema_fields_roundtrip_and_defaults(self):
        r = EvidenceReview(claim_kind="intention", corroborated_by=["o1"])
        back = EvidenceReview.from_dict(r.to_dict())
        assert back.claim_kind == "intention" and back.corroborated_by == ["o1"]
        old = EvidenceReview.from_dict({"status": "supported"})
        assert old.claim_kind == "fact" and old.corroborated_by == []
        assert EvidenceReview.from_dict({"claim_kind": "zzz"}).claim_kind == "fact"

    def test_intention_without_corroboration_rejected(self, semantic, inhibitor):
        c = _coffee(semantic)
        ok, tx = inhibitor.process_review_patch(c.id, _review(c, claim_kind="intention"))
        assert not ok
        assert "corroborated_by" in tx.patches[0].rejection_reason
        assert _coffee(semantic).evidence_review == {}

    def test_intention_with_corroboration_ok_and_insufficient_ok(self, semantic, inhibitor):
        c = _coffee(semantic)
        ok, _ = inhibitor.process_review_patch(
            c.id, _review(c, claim_kind="intention", corroborated_by=["obs1"]),
        )
        assert ok
        assert _coffee(semantic).evidence_review["corroborated_by"] == ["obs1"]
        c = _coffee(semantic)
        ok, _ = inhibitor.process_review_patch(c.id, _review(c, "insufficient", claim_kind="intention"))
        assert ok

    def test_invalid_claim_kind_rejected(self, semantic, inhibitor):
        c = _coffee(semantic)
        r = _review(c)
        r.claim_kind = "bogus"
        ok, _ = inhibitor.process_review_patch(c.id, r)
        assert not ok
