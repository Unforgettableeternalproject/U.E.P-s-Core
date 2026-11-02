"""
tests/test_corroboration_classes.py - 證據審核的佐證類別（identity／general／self_fact）

涵蓋：三類分流、contradicted 優先、not_required 的排序與標籤、共享投影傳遞新狀態、
舊資料相容（沒有 corroboration_class 欄位維持原狀態）、未分類／agent_only 的保守降級。
全部使用假 llm_fn，不載入真模型。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from echo_memory.affect.concept_inhibitor import ConceptInhibitor
from echo_memory.config import get_config
from echo_memory.engine import MemoryEngine
from echo_memory.retrieval import standing
from echo_memory.schemas.evidence_review import (
    EvidenceReview,
    compute_basis_digest,
    decide_review_status,
)
from echo_memory.schemas.memory_context import MemoryContext, SharedItem
from echo_memory.storage.share_layer import ScopeShareLayer
from tests.sharing_support import G42, VOICE6, Owner, ShareLLM, make_layer
from tests.test_evidence_reviewer import (
    agent_ep,
    make_concept,
    make_reviewer,
    unit_ids,
    user_ep,
)

# 群聊 6 人：學習權重 0.08，單筆遠低於 SUPPORT_THRESHOLD（self_fact 會是 insufficient）
WEAK = {"channel_kind": "voice", "audience_size": 6}
STRONG = {"channel_kind": "dm", "audience_size": 1}
OMIT = "__omit__"


@pytest.fixture
def engine(tmp_path: Path) -> MemoryEngine:
    return MemoryEngine(storage_path=tmp_path / "data", namespace_id="ns1")


@pytest.fixture
def gate(engine) -> ConceptInhibitor:
    return ConceptInhibitor(semantic=engine.semantic, llm_fn=None)


def reply(status="supported", cited=None, claim_kind="fact", corro=None, cls=OMIT) -> str:
    data = {
        "status": status,
        "claim_kind": claim_kind,
        "cited_unit_ids": list(cited or []),
        "corroborating_unit_ids": list(corro or []),
        "scope": {"subject_ref": "user", "context": None},
        "reason": "測試",
    }
    if cls != OMIT:
        data["corroboration_class"] = cls
    return json.dumps(data, ensure_ascii=False)


def run_review(engine, gate, *, cls, status="supported", claim_kind="fact",
               attrs=WEAK, text="我常聊遊戲"):
    ep = user_ep(engine, text, attrs=attrs)
    cid = make_concept(engine, gate, "概念甲", [ep])
    uid = unit_ids(engine, ep)[0]
    outcome = make_reviewer(
        engine, gate, lambda p: reply(status=status, cited=[uid], claim_kind=claim_kind, cls=cls),
    ).review(cid)
    return cid, outcome, engine.semantic.get(cid)


# ─── 三類分流 ─────────────────────────────────────────────────────────────────


class TestClassRouting:
    @pytest.mark.parametrize("cls", ["identity", "general"])
    def test_not_required_classes_skip_threshold(self, engine, gate, cls):
        _, outcome, concept = run_review(engine, gate, cls=cls)
        assert outcome.outcome == "reviewed" and outcome.status == "not_required"
        assert concept.effective_review_status() == "not_required"
        assert concept.evidence_review["corroboration_class"] == cls
        # 沒有被驗證：不蓋驗證章；confidence 上限與 supported 相同，不被 insufficient 的 0.45 截斷
        assert concept.last_verified_at is None
        assert concept.confidence <= 0.95 + 1e-9

    def test_self_fact_keeps_current_flow_weak_is_insufficient(self, engine, gate):
        _, outcome, concept = run_review(engine, gate, cls="self_fact")
        assert outcome.status == "insufficient"
        assert concept.evidence_review["corroboration_class"] == "self_fact"
        assert concept.confidence <= 0.45 + 1e-9

    def test_self_fact_strong_is_supported(self, engine, gate):
        _, outcome, concept = run_review(engine, gate, cls="self_fact", attrs=STRONG)
        assert outcome.status == "supported"
        assert concept.last_verified_at is not None

    def test_identity_without_any_citation_still_not_required(self, engine, gate):
        ep = user_ep(engine, "之後叫我小明", attrs=WEAK)
        cid = make_concept(engine, gate, "稱呼", [ep])
        outcome = make_reviewer(
            engine, gate, lambda p: reply(cited=[], cls="identity"),
        ).review(cid)
        assert outcome.status == "not_required"

    def test_insufficient_llm_status_also_becomes_not_required(self, engine, gate):
        # LLM 對免佐證類別判 insufficient（證據不足以「驗證」）不再構成降級理由
        _, outcome, _ = run_review(engine, gate, cls="general", status="insufficient")
        assert outcome.status == "not_required"


class TestContradictedWins:
    @pytest.mark.parametrize("cls", ["identity", "general", "self_fact"])
    def test_contradicted_beats_every_class(self, engine, gate, cls):
        _, outcome, concept = run_review(engine, gate, cls=cls, status="contradicted")
        assert outcome.status == "contradicted"
        assert concept.confidence <= 0.30 + 1e-9

    def test_decide_review_status_priority(self):
        assert decide_review_status("contradicted", 0.0, corroboration_class="general") == "contradicted"
        assert decide_review_status("supported", 0.0, corroboration_class="identity") == "not_required"
        assert decide_review_status("supported", 0.0, corroboration_class="self_fact") == "insufficient"
        assert decide_review_status("supported", 0.0, corroboration_class="") == "insufficient"


# ─── intention ───────────────────────────────────────────────────────────────


class TestIntention:
    def test_general_intention_needs_no_corroboration(self, engine, gate):
        _, outcome, concept = run_review(
            engine, gate, cls="general", claim_kind="intention", text="我明天想玩新遊戲",
        )
        assert outcome.status == "not_required"
        assert concept.evidence_review["claim_kind"] == "intention"  # 仍是 intention（顯示「宣稱」）
        assert concept.evidence_review["corroborated_by"] == []

    def test_self_fact_intention_still_needs_corroboration(self, engine, gate):
        _, outcome, _ = run_review(
            engine, gate, cls="self_fact", claim_kind="intention", attrs=STRONG,
        )
        assert outcome.status == "insufficient"

    def test_unclassified_intention_keeps_old_behavior(self, engine, gate):
        _, outcome, _ = run_review(
            engine, gate, cls=OMIT, claim_kind="intention", attrs=STRONG,
        )
        assert outcome.status == "insufficient"


# ─── 降級：未分類、無效值、agent_only、無 LLM ─────────────────────────────────


class TestDegradation:
    @pytest.mark.parametrize("cls", [OMIT, "bogus", None, 3])
    def test_missing_or_invalid_class_treated_as_self_fact(self, engine, gate, cls):
        _, outcome, concept = run_review(engine, gate, cls=cls)
        assert outcome.outcome == "reviewed"  # 不 hold
        assert outcome.status == "insufficient"
        assert concept.evidence_review["corroboration_class"] == ""

    @pytest.mark.parametrize("misplaced", ["general", "identity"])
    def test_class_misplaced_into_claim_kind_not_held(self, engine, gate, misplaced):
        # D88 回填實測：LLM 把佐證類別填進 claim_kind 而省略類別欄 → 採用為類別，claim_kind 退回 fact
        _, outcome, concept = run_review(engine, gate, cls=OMIT, claim_kind=misplaced)
        assert outcome.outcome == "reviewed"
        assert outcome.status == "not_required"
        assert concept.evidence_review["claim_kind"] == "fact"

    def test_misplaced_class_does_not_override_explicit_class(self, engine, gate):
        _, outcome, _ = run_review(engine, gate, cls="self_fact", claim_kind="general")
        assert outcome.outcome == "reviewed"
        assert outcome.status != "not_required"

    def test_agent_only_never_not_required(self, engine, gate):
        ep = agent_ep(engine, "你可能喜歡聊遊戲")
        cid = make_concept(engine, gate, "概念乙", [ep], support_source="agent_only")
        uid = unit_ids(engine, ep)[0]
        outcome = make_reviewer(
            engine, gate, lambda p: reply(cited=[uid], cls="general"),
        ).review(cid)
        assert outcome.status == "insufficient"

    def test_no_llm_stays_not_checked(self, engine, gate):
        ep = user_ep(engine, "我常聊遊戲")
        cid = make_concept(engine, gate, "概念丙", [ep])
        outcome = make_reviewer(engine, gate, None).review(cid)
        assert outcome.outcome == "skipped"
        assert engine.semantic.get(cid).effective_review_status() == "not_checked"

    def test_prompt_declares_class_without_keywords(self, engine, gate):
        seen: list[str] = []
        ep = user_ep(engine, "我常聊遊戲")
        cid = make_concept(engine, gate, "概念丁", [ep])
        uid = unit_ids(engine, ep)[0]

        def llm(prompt: str) -> str:
            seen.append(prompt)
            return reply(cited=[uid], cls="general")

        make_reviewer(engine, gate, llm).review(cid)
        assert "corroboration_class" in seen[0] and "identity|general|self_fact" in seen[0]
        assert "不得依賴特定字詞" in seen[0]


# ─── Gate 結構驗證 ───────────────────────────────────────────────────────────


class TestGateValidation:
    def _patch(self, engine, gate, **kw):
        ep = user_ep(engine, "我常聊遊戲")
        cid = make_concept(engine, gate, "概念戊", [ep])
        concept = engine.semantic.get(cid)
        review = EvidenceReview(
            basis_digest=compute_basis_digest(concept), reviewer="llm", reason="t", **kw,
        )
        return gate.process_review_patch(cid, review)

    def test_not_required_requires_not_required_class(self, engine, gate):
        ok, tx = self._patch(engine, gate, status="not_required", corroboration_class="self_fact")
        assert not ok and "corroboration_class" in tx.patches[0].rejection_reason
        ok, _ = self._patch(engine, gate, status="not_required", corroboration_class="")
        assert not ok

    def test_not_required_with_general_is_accepted(self, engine, gate):
        ok, _ = self._patch(engine, gate, status="not_required", corroboration_class="general")
        assert ok

    def test_invalid_class_rejected_at_gate(self, engine, gate):
        value = EvidenceReview(
            status="insufficient", basis_digest="x", reviewer="llm",
        ).to_dict()
        value["corroboration_class"] = "bogus"
        assert gate._validate_review_value(value) is not None


# ─── 排序與標籤 ──────────────────────────────────────────────────────────────


class TestRankingAndLabels:
    def test_status_factor_not_below_insufficient_and_equals_supported(self):
        table = standing.params_from_config(get_config())["status_table"]
        assert table["not_required"] >= table["insufficient"]
        assert table["not_required"] == pytest.approx(table["supported"])

    def test_concept_not_required_has_no_support_label(self, engine, gate):
        _, _, concept = run_review(engine, gate, cls="general")
        assert MemoryContext._concept_labels(concept).get("support") is None

    def test_concept_insufficient_keeps_label(self, engine, gate):
        _, _, concept = run_review(engine, gate, cls="self_fact")
        assert "證據不足" in MemoryContext._concept_labels(concept)["support"]

    def test_not_required_outranks_insufficient_in_standing(self, engine, gate, tmp_path):
        _, _, nr = run_review(engine, gate, cls="general")
        engine2 = MemoryEngine(storage_path=tmp_path / "d2", namespace_id="ns2")
        gate2 = ConceptInhibitor(semantic=engine2.semantic, llm_fn=None)
        _, _, ins = run_review(engine2, gate2, cls="self_fact")
        assert standing.status_factor(nr) > standing.status_factor(ins)

    def test_shared_item_labels(self):
        nr = SharedItem("p1", "1001", "某成員常聊遊戲", review_status="not_required")
        ins = SharedItem("p2", "1001", "某成員自稱專業", review_status="insufficient")
        intent = SharedItem("p3", "1001", "某成員打算買票", claim_kind="intention",
                            review_status="not_required")
        assert nr.support_label == ""
        assert ins.support_label == "僅本人提過，未經他人佐證"
        assert intent.support_label == "宣稱"  # intention 維持「宣稱」顯示


# ─── 共享投影 ────────────────────────────────────────────────────────────────


class TestSharedProjection:
    def _project(self, tmp_path, *, cls, claim_kind="fact"):
        root = tmp_path / "data"
        layer: ScopeShareLayer = make_layer(root)
        owner = Owner(root, "1001", layer, ShareLLM(corroboration_class=cls, claim_kind=claim_kind))
        owner.concept("聊遊戲", [owner.say("我常聊遊戲", attrs=VOICE6)])
        owner.phase.run()
        return owner, layer.list_projections(G42)

    def test_general_projection_is_not_required(self, tmp_path):
        _, views = self._project(tmp_path, cls="general")
        assert [v.review_status for v in views] == ["not_required"]

    def test_self_fact_projection_stays_insufficient(self, tmp_path):
        _, views = self._project(tmp_path, cls="self_fact")
        assert [v.review_status for v in views] == ["insufficient"]

    def test_unclassified_projection_stays_insufficient(self, tmp_path):
        _, views = self._project(tmp_path, cls=None)
        assert [v.review_status for v in views] == ["insufficient"]

    def test_intention_projection_not_required_but_labeled_claim(self, tmp_path):
        _, views = self._project(tmp_path, cls="general", claim_kind="intention")
        v = views[0]
        assert v.review_status == "not_required" and v.claim_kind == "intention"
        item = SharedItem(v.projection_id, v.owner_uid, v.summary,
                          claim_kind=v.claim_kind, review_status=v.review_status)
        assert item.support_label == "宣稱"

    def test_share_prompt_declares_class(self, tmp_path):
        owner, _ = self._project(tmp_path, cls="general")
        assert "corroboration_class" in owner.llm.share_calls[0]


# ─── 舊資料相容 ──────────────────────────────────────────────────────────────


class TestLegacyCompat:
    def test_from_dict_without_field_is_unclassified(self):
        d = EvidenceReview(status="insufficient", basis_digest="x").to_dict()
        d.pop("corroboration_class")
        assert EvidenceReview.from_dict(d).corroboration_class == ""
        assert EvidenceReview.from_dict({**d, "corroboration_class": "zzz"}).corroboration_class == ""

    def test_roundtrip_keeps_class(self):
        r = EvidenceReview(status="not_required", basis_digest="x", corroboration_class="identity")
        assert EvidenceReview.from_dict(r.to_dict()).corroboration_class == "identity"

    def test_legacy_review_keeps_status(self, engine, gate):
        ep = user_ep(engine, "我常聊遊戲", attrs=WEAK)
        cid = make_concept(engine, gate, "概念庚", [ep])
        concept = engine.semantic.get(cid)
        legacy = EvidenceReview(
            status="insufficient", support_strength=0.08, basis_digest=compute_basis_digest(concept),
            reviewer="llm", reason="舊審核",
        ).to_dict()
        legacy.pop("corroboration_class")  # 舊資料沒有此欄位
        concept.evidence_review = legacy
        assert concept.effective_review_status() == "insufficient"
        assert MemoryContext._concept_labels(concept)["support"]

    def test_legacy_projection_default_is_insufficient(self):
        assert SharedItem("p", "1", "x").review_status == "insufficient"
