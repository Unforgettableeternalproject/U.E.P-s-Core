"""
M4 包 C：檢索側向抑制、prompt 標籤、讀者（standing 與 support 面向）。

涵蓋：
  - get_concept_relations 的 weight 語意（＝矛盾強度，不是乘數）
  - standing 純函式表驅動、側向抑制公式（G7）
  - prompt 標籤快照、get_profile 排除 agent_only／contradicted
  - superseded 預設不召回、include_history 才帶出並標註
  - 舊資料（無 evidence_review／support_source）相容（G8）
  - verifier 的矛盾扣分不再扣已被取代的概念
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from echo_memory.affect.verifier import ResponseVerifier
from echo_memory.engine import MemoryEngine
from echo_memory.retrieval import standing as st
from echo_memory.retrieval.ecphory import EcphoryRetrieval
from echo_memory.schemas.concept import Concept
from echo_memory.schemas.evidence_review import EvidenceReview, compute_basis_digest
from echo_memory.schemas.memory_context import MemoryContext
from echo_memory.storage.episodic import EpisodicMemory
from echo_memory.storage.semantic import SemanticMemory

DIM = 512


def _unit(i: int) -> list[float]:
    """第 i 軸單位向量：不同概念互相正交，cosine 完全可控。"""
    v = np.zeros(DIM, dtype=np.float32)
    v[i] = 1.0
    return v.tolist()


def _mix(*axes: int) -> list[float]:
    """多軸等權混合並正規化：與各軸概念的 cosine 皆為 1/sqrt(n)。"""
    v = np.zeros(DIM, dtype=np.float32)
    for i in axes:
        v[i] = 1.0
    v /= float(np.linalg.norm(v))
    return v.tolist()


@pytest.fixture
def stores(tmp_path: Path):
    ep_dir = tmp_path / "episodic"
    sem_dir = tmp_path / "semantic"
    ep_dir.mkdir()
    sem_dir.mkdir()
    episodic = EpisodicMemory(storage_path=ep_dir)
    semantic = SemanticMemory(storage_path=sem_dir, embed_fn=lambda t: _unit(0))
    retrieval = EcphoryRetrieval(episodic=episodic, semantic=semantic)
    return episodic, semantic, retrieval


def _make(semantic, graph, name: str, axis: int, **attrs) -> Concept:
    """寫入一個以第 axis 軸為向量的概念，並設定指定屬性（metadata 為合併）。"""
    cid = semantic.upsert_concept(name, f"{name}的描述", embedding=_unit(axis))
    graph.add_concept_node(cid, name)
    concept = semantic.get(cid)
    metadata = attrs.pop("metadata", None)
    for key, value in attrs.items():
        setattr(concept, key, value)
    if metadata:
        concept.metadata.update(metadata)
    return concept


def _review(concept: Concept, status: str) -> None:
    """寫入與目前內容綁定的有效審核。"""
    concept.evidence_review = EvidenceReview(
        status=status, basis_digest=compute_basis_digest(concept),
    ).to_dict()


def _payload(ctx: MemoryContext) -> dict:
    lines = ctx.to_text().split("\n")
    return json.loads("\n".join(lines[2:-1]))


# ─── weight 語意（設計稿 §1 額外發現）──────────────────────────────

class TestEdgeWeightSemantics:
    def test_relations_weight_is_strength_not_multiplier(self, stores):
        """get_concept_relations 回傳的 weight 就是 add_contradicts_edge 的矛盾強度，雙向一致。"""
        episodic, semantic, _ = stores
        graph = episodic.graph
        a = _make(semantic, graph, "甲", 1)
        b = _make(semantic, graph, "乙", 2)
        c = _make(semantic, graph, "丙", 3)
        d = _make(semantic, graph, "丁", 4)
        graph.add_contradicts_edge(a.id, b.id, weight=0.9)
        graph.add_contradicts_edge(c.id, d.id, weight=0.3)

        for x, y, w in ((a, b, 0.9), (b, a, 0.9), (c, d, 0.3), (d, c, 0.3)):
            rel = graph.get_concept_relations(x.id, edge_type="CONTRADICTS")
            assert rel == [(y.id, "CONTRADICTS", pytest.approx(w))]

    def test_stronger_contradiction_never_suppresses_less(self):
        """舊式 weight×0.3 當乘數會「越弱壓越狠」；新式乘數隨強度單調下降。"""
        mults = [st.lateral_multiplier(0.3, 0.3, w) for w in (0.1, 0.3, 0.6, 0.9, 1.0)]
        assert mults == sorted(mults, reverse=True)
        # 強度極弱的矛盾幾乎不影響分數（舊式會壓到 ×0.03）
        assert mults[0] > 0.95


# ─── standing 純函式 ────────────────────────────────────────────────

class TestStandingPure:
    @pytest.mark.parametrize(
        "status,source,expected",
        [
            ("supported", "user", 0.8 * 1.0 * 1.0),
            ("not_checked", "user", 0.8 * 0.95 * 1.0),
            ("insufficient", "user", 0.8 * 0.80 * 1.0),
            ("contradicted", "user", 0.8 * 0.45 * 1.0),
            ("supported", "legacy_unknown", 0.8 * 1.0 * 0.8),
            ("supported", "agent_only", 0.8 * 1.0 * 0.6),
            ("contradicted", "agent_only", 0.8 * 0.45 * 0.6),
        ],
    )
    def test_standing_table(self, status, source, expected):
        c = Concept(name="x", description="y", confidence=0.8, metadata={"support_source": source})
        if status != "not_checked":
            _review(c, status)
        assert st.standing(c) == pytest.approx(expected)

    def test_missing_source_is_legacy_unknown(self):
        c = Concept(name="x", description="y", confidence=0.5)
        assert st.support_source_of(c) == "legacy_unknown"
        assert st.standing(c) == pytest.approx(0.5 * 0.95 * 0.8)

    def test_invalidated_review_falls_back_to_not_checked(self):
        c = Concept(name="x", description="y", confidence=0.5)
        _review(c, "supported")
        assert st.review_status(c) == "supported"
        c.description = "改過了"  # 基礎變動 → 審核失效
        assert st.review_status(c) == "not_checked"
        assert st.status_factor(c) == pytest.approx(0.95)

    def test_object_without_review_api_is_not_checked(self):
        """缺 effective_review_status 的物件（舊介面／替身）不崩潰。"""
        stub = SimpleNamespace(confidence=0.5, metadata={})
        assert st.review_status(stub) == "not_checked"

    def test_is_superseded_needs_deprecated_and_replaced_by(self):
        assert st.is_superseded(Concept(name="a", description="b", status="deprecated",
                                        metadata={"replaced_by": "cid"}))
        assert not st.is_superseded(Concept(name="a", description="b", status="deprecated"))
        assert not st.is_superseded(Concept(name="a", description="b",
                                            metadata={"replaced_by": "cid"}))


# ─── G7 側向抑制 ────────────────────────────────────────────────────

class TestLateralInhibition:
    def test_constant_removed(self):
        assert not hasattr(EcphoryRetrieval, "CONTRADICTS_PENALTY")
        assert not hasattr(EcphoryRetrieval, "_apply_contradicts_penalty")

    def test_similar_standing_both_mildly_reduced(self):
        m = st.lateral_multiplier(0.5, 0.5, 0.8)
        assert m == pytest.approx(1 - 0.6 * 0.8 * 0.5)
        assert 0.7 < m < 1.0

    def test_strong_vs_weak_only_weak_suppressed(self):
        strong = st.lateral_multiplier(0.9, 0.1, 1.0)
        weak = st.lateral_multiplier(0.1, 0.9, 1.0)
        assert strong > 0.9
        assert weak < 0.5
        assert strong > weak

    def test_floor_applies(self):
        assert st.lateral_multiplier(0.0, 1.0, 1.0, gamma=1.0, floor=0.4) == pytest.approx(0.4)
        # 預設參數下最重也只到 FLOOR，不會像舊式壓到 0.15
        assert st.lateral_multiplier(0.0, 1.0, 1.0) >= 0.4

    def test_zero_standing_both_uses_even_share(self):
        assert st.lateral_multiplier(0.0, 0.0, 1.0) == pytest.approx(1 - 0.6 * 0.5)

    def test_multiple_edges_take_minimum(self):
        a = Concept(name="a", description="a", confidence=0.2, id="a")
        b = Concept(name="b", description="b", confidence=0.9, id="b")
        c = Concept(name="c", description="c", confidence=0.4, id="c")
        edges = {"a": [("b", 1.0), ("c", 1.0)], "b": [("a", 1.0)], "c": [("a", 1.0)]}
        m = st.inhibition_multipliers({"a": a, "b": b, "c": c}, lambda cid: edges[cid])
        only_b = st.lateral_multiplier(st.standing(a), st.standing(b), 1.0)
        assert m["a"] == pytest.approx(only_b)  # b 比 c 更強 → 取較小的乘數
        assert m["a"] < m["c"] < m["b"]

    def test_edge_to_outside_result_set_is_ignored(self):
        a = Concept(name="a", description="a", id="a")
        m = st.inhibition_multipliers({"a": a}, lambda cid: [("zzz", 1.0)])
        assert m == {}

    def test_retrieval_only_weak_side_pressed(self, stores):
        """端到端：兩概念與查詢同等相關，standing 高者幾乎不動，低者被壓。"""
        episodic, semantic, retrieval = stores
        graph = episodic.graph
        strong = _make(semantic, graph, "強", 1, confidence=0.9)
        weak = _make(semantic, graph, "弱", 2, confidence=0.2)
        _review(strong, "supported")
        graph.add_contradicts_edge(strong.id, weak.id, weight=1.0)

        concepts, scores = retrieval._retrieve_concepts(_mix(1, 2), top_k=5)
        sim = 1 / np.sqrt(2)
        assert [c.id for c in concepts] == [strong.id, weak.id]
        # 強者：status_factor=1.0、只被輕度抑制；弱者：0.95 且被重壓
        s_strong, s_weak = scores[strong.id], scores[weak.id]
        assert s_strong > 0.8 * sim
        assert s_weak < 0.6 * sim
        # 與 tie-break 無關：沒有矛盾邊時兩者本來只差 status_factor
        graph.graph.remove_edge(strong.id, weak.id)
        graph.graph.remove_edge(weak.id, strong.id)
        _, scores_free = retrieval._retrieve_concepts(_mix(1, 2), top_k=5)
        assert scores_free[strong.id] == pytest.approx(sim, abs=1e-4)
        assert scores_free[weak.id] == pytest.approx(sim * 0.95, abs=1e-4)


# ─── 排序：relevance 與 support 分開相乘，confidence 不計入 ─────────

class TestRankingFactors:
    def test_status_factor_multiplies_relevance_only(self, stores):
        episodic, semantic, retrieval = stores
        graph = episodic.graph
        a = _make(semantic, graph, "已查證", 1, confidence=0.3)
        b = _make(semantic, graph, "不足", 2, confidence=0.99)
        c = _make(semantic, graph, "矛盾", 3, confidence=0.99)
        _review(a, "supported")
        _review(b, "insufficient")
        _review(c, "contradicted")
        _, scores = retrieval._retrieve_concepts(_mix(1, 2, 3), top_k=5, min_similarity=0.1)
        sim = 1 / np.sqrt(3)
        # confidence 不進排序：低 confidence 的 supported 仍是第一
        assert scores[a.id] == pytest.approx(sim * 1.0, abs=1e-4)
        assert scores[b.id] == pytest.approx(sim * 0.80, abs=1e-4)
        assert scores[c.id] == pytest.approx(sim * 0.45, abs=1e-4)

    def test_confidence_breaks_ties(self, stores):
        episodic, semantic, retrieval = stores
        graph = episodic.graph
        lo = _make(semantic, graph, "低", 1, confidence=0.2)
        hi = _make(semantic, graph, "高", 2, confidence=0.8)
        concepts, _ = retrieval._retrieve_concepts(_mix(1, 2), top_k=5)
        assert [c.id for c in concepts] == [hi.id, lo.id]

    def test_bridge_uses_bridge_conflict_factor(self, stores, monkeypatch):
        """橋接展開遇矛盾邊鄰居乘 BRIDGE_CONFLICT_FACTOR（0.6），不再沿用舊常數。"""
        from echo_memory.config import get_config

        episodic, semantic, retrieval = stores
        graph = episodic.graph
        cfg = get_config()
        monkeypatch.setattr(cfg, "latent_bridge_discount", 1.0)
        seed = _make(semantic, graph, "種子", 1)
        plain = _make(semantic, graph, "無矛盾鄰居", 5)
        disputed = _make(semantic, graph, "有矛盾鄰居", 6)
        other = _make(semantic, graph, "對立方", 7)
        now = time.time()
        for target in (plain, disputed):
            for s, d in ((seed.id, target.id), (target.id, seed.id)):
                graph.graph.add_edge(
                    s, d, edge_type="LATENT_BRIDGE", weight=1.0, similarity=1.0,
                    created_at=now, last_confirmed_at=now,
                )
        graph.add_contradicts_edge(disputed.id, other.id, weight=0.5)

        _, scores = retrieval._retrieve_concepts(_unit(1), top_k=5, min_similarity=0.05)
        assert scores[disputed.id] == pytest.approx(scores[plain.id] * 0.6, rel=1e-3)


# ─── prompt 標籤快照 ─────────────────────────────────────────────────

class TestPromptLabels:
    def _entry(self, concept: Concept) -> dict:
        ctx = MemoryContext(concepts=[concept], concept_similarities={concept.id: 0.9})
        return _payload(ctx)["相關語義概念"][0]

    def test_supported_user_has_no_labels(self):
        c = Concept(name="咖啡", description="喜歡手沖", id="c1",
                    metadata={"support_source": "user"})
        _review(c, "supported")
        assert self._entry(c) == {"match": "匹配度:90%", "name": "咖啡", "description": "喜歡手沖"}

    @pytest.mark.parametrize(
        "status,source,extra",
        [
            ("not_checked", "user", {}),
            ("not_checked", "legacy_unknown", {}),
            ("supported", "legacy_unknown", {}),
            ("insufficient", "legacy_unknown", {"support": "（證據不足，勿當確定事實）"}),
            ("insufficient", "user", {"support": "（證據不足，勿當確定事實）"}),
            ("contradicted", "user", {"support": "（證據相互矛盾）"}),
            ("supported", "agent_only", {"source": "（助手推測，非使用者陳述）"}),
            ("insufficient", "agent_only", {
                "support": "（證據不足，勿當確定事實）",
                "source": "（助手推測，非使用者陳述）",
            }),
        ],
    )
    def test_label_snapshot(self, status, source, extra):
        c = Concept(name="咖啡", description="喜歡手沖", id="c1",
                    metadata={"support_source": source})
        if status != "not_checked":
            _review(c, status)
        assert self._entry(c) == {
            "match": "匹配度:90%", "name": "咖啡", "description": "喜歡手沖", **extra,
        }

    def test_superseded_history_label(self):
        c = Concept(name="咖啡", description="喜歡 A", id="c1", status="deprecated",
                    metadata={"support_source": "user", "replaced_by": "c2",
                              "supersession": {"by": "c2", "at": "2026-10-27T08:00:00+00:00"}})
        entry = self._entry(c)
        assert entry["history"] == "（歷史資料，已被取代於 2026-10-27）"

    def test_superseded_without_date_falls_back_to_updated_at(self):
        c = Concept(name="咖啡", description="喜歡 A", id="c1", status="deprecated",
                    metadata={"support_source": "user", "replaced_by": "c2"})
        assert self._entry(c)["history"].startswith("（歷史資料，已被取代於 ")


# ─── get_profile ─────────────────────────────────────────────────────

class TestGetProfile:
    def _engine(self, semantic):
        return SimpleNamespace(semantic=semantic)

    def test_excludes_agent_only_and_contradicted(self, stores):
        episodic, semantic, _ = stores
        graph = episodic.graph
        _make(semantic, graph, "使用者偏好", 1, subject="user",
              metadata={"support_source": "user"})
        _make(semantic, graph, "助手推測", 2, subject="user",
              metadata={"support_source": "agent_only"})
        bad = _make(semantic, graph, "被判矛盾", 3, subject="user",
                    metadata={"support_source": "user"})
        _review(bad, "contradicted")

        text = MemoryEngine.get_profile(self._engine(semantic))
        assert "使用者偏好" in text
        assert "助手推測" not in text
        assert "被判矛盾" not in text

    def test_sorted_by_standing_not_raw_confidence(self, stores):
        episodic, semantic, _ = stores
        graph = episodic.graph
        # 原始 confidence 較高但來源為舊資料且未審核；standing 較低
        _make(semantic, graph, "舊資料", 1, subject="user", confidence=0.8,
              metadata={"support_source": "legacy_unknown"})
        sure = _make(semantic, graph, "已確認", 2, subject="user", confidence=0.7,
                     metadata={"support_source": "user"})
        _review(sure, "supported")
        lines = MemoryEngine.get_profile(self._engine(semantic)).split("\n")
        assert lines[0].startswith("- 已確認")
        assert lines[1].startswith("- 舊資料")

    def test_limit_applies_after_filtering(self, stores):
        episodic, semantic, _ = stores
        graph = episodic.graph
        for i in range(3):  # 高 confidence 的 agent_only 不得佔掉名額
            _make(semantic, graph, f"推測{i}", 10 + i, subject="user", confidence=0.9,
                  metadata={"support_source": "agent_only"})
        _make(semantic, graph, "真偏好", 1, subject="user", confidence=0.3,
              metadata={"support_source": "user"})
        assert "真偏好" in MemoryEngine.get_profile(self._engine(semantic), limit=2)


# ─── superseded 預設不召回 ──────────────────────────────────────────

class TestSupersededRetrieval:
    def _setup(self, stores):
        episodic, semantic, retrieval = stores
        graph = episodic.graph
        old = _make(semantic, graph, "喜歡A", 1, status="deprecated",
                    metadata={"replaced_by": "pending", "support_source": "user"})
        new = _make(semantic, graph, "改喜歡B", 2, metadata={"support_source": "user"})
        old.metadata["replaced_by"] = new.id
        return retrieval, old, new

    def test_default_excludes_superseded(self, stores):
        retrieval, old, new = self._setup(stores)
        # 門檻放低到歷史權重（×0.15）也過得了，才能證明是「明確排除」而非算術巧合
        concepts, _ = retrieval._retrieve_concepts(_mix(1, 2), top_k=5, min_similarity=0.05)
        assert [c.id for c in concepts] == [new.id]

    def test_include_history_brings_it_back_ranked_below(self, stores):
        retrieval, old, new = self._setup(stores)
        concepts, scores = retrieval._retrieve_concepts(
            _mix(1, 2), top_k=5, min_similarity=0.05, include_history=True,
        )
        assert [c.id for c in concepts] == [new.id, old.id]
        assert scores[old.id] < scores[new.id] * 0.5
        ctx = MemoryContext(concepts=concepts, concept_similarities=scores)
        entries = _payload(ctx)["相關語義概念"]
        assert "history" not in entries[0]
        assert entries[1]["history"].startswith("（歷史資料，已被取代")

    def test_history_mode_does_not_loosen_threshold_for_active(self, stores):
        episodic, semantic, retrieval = stores
        weak = _make(semantic, episodic.graph, "不相關", 3)
        concepts, _ = retrieval._retrieve_concepts(
            _mix(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12),  # sim≈0.29 < 0.3
            top_k=5, min_similarity=0.3, include_history=True,
        )
        assert weak.id not in [c.id for c in concepts]

    def test_expansion_skips_superseded_by_default(self, stores):
        episodic, semantic, retrieval = stores
        graph = episodic.graph
        retrieval_, old, new = self._setup(stores)
        graph.add_relates_to_edge(new.id, old.id, weight=0.9)
        concepts, _ = retrieval_._retrieve_concepts(_unit(2), top_k=5, min_similarity=0.05)
        assert old.id not in [c.id for c in concepts]
        concepts, _ = retrieval_._retrieve_concepts(
            _unit(2), top_k=5, min_similarity=0.05, include_history=True,
        )
        assert old.id in [c.id for c in concepts]


# ─── G8 舊資料相容 ──────────────────────────────────────────────────

class TestLegacyCompat:
    def _legacy(self) -> Concept:
        fresh = Concept(name="舊概念", description="舊描述", id="old1", subject="user",
                        confidence=0.6)
        data = fresh.to_dict()
        data.pop("evidence_review")
        data["metadata"].pop("support_source", None)
        return Concept.from_dict(data)

    def test_loads_as_not_checked(self):
        c = self._legacy()
        assert c.evidence_review == {}
        assert c.effective_review_status() == "not_checked"

    def test_retrieval_applies_not_checked_factor(self, stores):
        episodic, semantic, retrieval = stores
        _make(semantic, episodic.graph, "舊概念", 1)  # 沒有任何審核／support_source
        concepts, scores = retrieval._retrieve_concepts(_unit(1), top_k=5)
        assert len(concepts) == 1
        assert scores[concepts[0].id] == pytest.approx(0.95, abs=1e-4)

    def test_profile_and_labels_do_not_crash(self, stores):
        episodic, semantic, _ = stores
        _make(semantic, episodic.graph, "舊概念", 1, subject="user")
        text = MemoryEngine.get_profile(SimpleNamespace(semantic=semantic))
        assert "舊概念" in text
        entry = _payload(MemoryContext(concepts=[self._legacy()]))["相關語義概念"][0]
        assert "support" not in entry and "source" not in entry and "history" not in entry


# ─── verifier 矛盾扣分 ──────────────────────────────────────────────

class TestVerifierContradictionPenalty:
    def _penalty(self, *concepts: Concept) -> float:
        return ResponseVerifier._contradiction_penalty(
            None, MemoryContext(concepts=list(concepts)),
        )

    def test_superseded_concept_not_penalised(self):
        old = Concept(name="a", description="b", status="deprecated",
                      metadata={"has_contradiction": True, "replaced_by": "x"})
        old.conflict_count = 1
        assert self._penalty(old) == 0.0

    def test_active_conflict_still_penalised(self):
        c = Concept(name="a", description="b")
        c.conflict_count = 1
        assert self._penalty(c) == pytest.approx(0.1)

    def test_valid_contradicted_review_penalised(self):
        c = Concept(name="a", description="b")
        _review(c, "contradicted")
        assert self._penalty(c) == pytest.approx(0.1)

    def test_stale_contradicted_review_not_penalised(self):
        c = Concept(name="a", description="b")
        _review(c, "contradicted")
        c.description = "變了"
        assert self._penalty(c) == 0.0


# ─── review_concept 入口 ────────────────────────────────────────────

class TestReviewConceptEntry:
    def test_skipped_without_llm_and_never_raises(self):
        """審核器未落地或無 LLM 時回 skipped，不寫資料也不拋例外。"""
        stub = SimpleNamespace(
            ensure_initialized=lambda: None, _llm_fn=None, _concept_inhibitor=None,
        )
        result = MemoryEngine.review_concept(stub, "con_x")
        assert result["outcome"] == "skipped"
        assert result["concept_id"] == "con_x"
        assert result["reason"] in ("reviewer_unavailable", "no_llm")
