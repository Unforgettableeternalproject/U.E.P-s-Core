"""M-13 記憶遺留補強的回歸測試。

涵蓋：Entity／Concept 來源（provenance、support_source）、Reflection 來源感知摘要、
health_score 獨立來源數、review_concept 的 MCP 入口、graph dirty 重試、
intention 佐證學習權重（Golden）。全部使用假 llm_fn，不載入真模型。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from echo_memory.affect.concept_extractor import ConceptExtractor
from echo_memory.affect.concept_inhibitor import ConceptInhibitor
from echo_memory.affect.reflection_executor import ReflectionExecutor
from echo_memory.engine import MemoryEngine
from echo_memory.schemas.concept import Concept
from echo_memory.schemas.entity import Entity
from echo_memory.schemas.evidence import EvidenceInput, EvidenceUnit, compute_content_hash
from echo_memory.schemas.evidence_review import EvidenceReview, compute_basis_digest
from echo_memory.schemas.hypothesis import evaluate_hypothesis_support
from echo_memory.storage.graph import MemoryGraph
from echo_memory.utils.source_view import recent_user_summaries
from tests.test_evidence_reviewer import (
    DM,
    T0,
    agent_ep,
    make_concept,
    make_reviewer,
    reply,
    unit_ids,
    user_ep,
)


@pytest.fixture
def engine(tmp_path: Path) -> MemoryEngine:
    return MemoryEngine(storage_path=tmp_path / "data", namespace_id="ns1")


@pytest.fixture
def gate(engine) -> ConceptInhibitor:
    g = ConceptInhibitor(semantic=engine.semantic, llm_fn=None)
    engine.set_concept_inhibitor(g)
    return g


def _audit_records(engine, mutation: str) -> list[dict]:
    path = engine.semantic.storage_path / "dream_audit_log.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [r for r in rows if r.get("mutation") == mutation]


# ─── 1. Entity provenance ────────────────────────────────────────────────────


class TestEntityProvenance:
    def test_apply_entities_records_audit_and_provenance(self, engine):
        ep_id = user_ep(engine, "我在台北認識了小明")
        uid = unit_ids(engine, ep_id)[0]
        engine._apply_entities(ep_id, [Entity(name="小明", entity_type="person")])

        (rec,) = _audit_records(engine, "entity_upsert")
        assert rec["producer"] == "extractor"
        assert rec["source_episode_ids"] == [ep_id]
        assert rec["extra"]["evidence_ids"] == [uid]
        assert rec["prior"] is None and rec["new"]["is_new"] is True

        ent = engine.entity_store.get_by_name("小明")
        assert ent is not None and ent.source_episode_ids == [ep_id]
        prov = ent.metadata["provenance"]
        assert prov["producer"] == "extractor" and prov["evidence_ids"] == [uid]
        assert prov["mutation_id"] == rec["mutation_id"]

        graph = engine.episodic.graph
        assert graph.graph.nodes[ent.id]["provenance"]["mutation_id"] == rec["mutation_id"]
        assert graph.graph[ep_id][ent.id]["provenance"]["evidence_ids"] == [uid]

    def test_second_mention_keeps_prior_in_audit_and_accumulates_evidence(self, engine):
        ep1 = user_ep(engine, "小明來了")
        ep2 = user_ep(engine, "小明又來了")
        engine._apply_entities(ep1, [Entity(name="小明", entity_type="person")])
        engine._apply_entities(ep2, [Entity(name="小明", entity_type="person")])

        recs = _audit_records(engine, "entity_upsert")
        assert len(recs) == 2
        assert recs[1]["prior"]["name"] == "小明" and recs[1]["new"]["is_new"] is False
        ent = engine.entity_store.get_by_name("小明")
        assert ent.mention_count == 2
        assert set(ent.metadata["provenance"]["evidence_ids"]) == {
            unit_ids(engine, ep1)[0], unit_ids(engine, ep2)[0],
        }
        # source_episode_ids 合併既有值，不覆蓋
        assert ent.metadata["provenance"]["source_episode_ids"] == [ep1, ep2]

    def test_audit_failure_blocks_entity_write(self, engine, monkeypatch):
        ep_id = user_ep(engine, "小明來了")
        coord = engine._entity_audit()
        monkeypatch.setattr(coord, "record_mutations", lambda stage, entries: False)
        engine._apply_entities(ep_id, [Entity(name="小明", entity_type="person")])
        assert engine.entity_store.get_by_name("小明") is None
        # 丟棄可觀測：累計計數出現在 stats，log 帶完整 episode id
        assert engine.get_stats()["entity_audit_dropped"] == 1
        engine._apply_entities(ep_id, [Entity(name="小華"), Entity(name="小美")])
        assert engine.get_stats()["entity_audit_dropped"] == 3


class TestSupportSourceFromEpisodes:
    def _extractor(self, engine, gate, noun: str) -> ConceptExtractor:
        return ConceptExtractor(
            llm_fn=lambda p: json.dumps([{"noun": noun, "desc": f"{noun} 的描述"}], ensure_ascii=False),
            semantic=engine.semantic, graph=engine.episodic.graph, embed_fn=None,
            inhibitor=gate, evidence_store=engine.evidence,
        )

    def test_extractor_user_episode_is_user_support(self, engine, gate):
        ep_id = user_ep(engine, "我喜歡手沖咖啡")
        ex = self._extractor(engine, gate, "手沖咖啡")
        ex._extract_concepts_bg(engine.episodic.get(ep_id), 0.5)
        concept = engine.semantic.get_by_name("手沖咖啡")
        assert concept is not None and concept.metadata["support_source"] == "user"

    def test_extractor_agent_only_episode_is_agent_only(self, engine, gate):
        ep_id = agent_ep(engine, "也許你會喜歡花茶")
        ex = self._extractor(engine, gate, "花茶")
        ex._extract_concepts_bg(engine.episodic.get(ep_id), 0.5)
        concept = engine.semantic.get_by_name("花茶")
        assert concept is not None and concept.metadata["support_source"] == "agent_only"

    def test_reflection_create_derives_support_from_parent_episodes(self, engine, gate):
        ep_user = user_ep(engine, "我常去爬山")
        ep_agent = agent_ep(engine, "也許你適合露營")
        executor = ReflectionExecutor(engine, concept_inhibitor=gate)
        executor._exec_create_concept(
            "登山習慣", {"description": "使用者常爬山", "parent_episode_ids": [ep_user]},
        )
        executor._exec_create_concept(
            "露營推測", {"description": "助手推測", "parent_episode_ids": [ep_agent]},
        )
        assert engine.semantic.get_by_name("登山習慣").metadata["support_source"] == "user"
        assert engine.semantic.get_by_name("露營推測").metadata["support_source"] == "agent_only"

    def test_reflection_create_without_episodes_leaves_support_unset(self, engine, gate):
        executor = ReflectionExecutor(engine, concept_inhibitor=gate)
        executor._exec_create_concept("抽象想法", {"description": "沒有來源 episode"})
        assert "support_source" not in engine.semantic.get_by_name("抽象想法").metadata


# ─── 2. Reflection 來源感知摘要 ──────────────────────────────────────────────


class TestReflectionSummaries:
    def test_summaries_use_only_user_text(self, engine):
        ep_id = engine.store_episode(
            "我喜歡咖啡", "助手的推測：你也許愛茶", "s1",
            evidence=[
                EvidenceInput(source_type="user", content="我喜歡咖啡", speaker_id="u1",
                              occurred_at=T0, attrs=dict(DM)),
                EvidenceInput(source_type="agent", content="助手的推測：你也許愛茶",
                              speaker_id="agent", occurred_at=T0),
            ],
        )
        # 舊的 content_summary 混有 agent 原文
        assert "助手" in engine.episodic.get(ep_id).content_summary
        summaries = recent_user_summaries(engine.evidence, [engine.episodic.get(ep_id)])
        assert summaries == ["我喜歡咖啡"]

    def test_agent_only_episode_yields_no_summary(self, engine):
        ep_id = agent_ep(engine, "助手自言自語")
        assert recent_user_summaries(engine.evidence, [engine.episodic.get(ep_id)]) == []

    def test_entry_and_probe_use_source_aware_summaries(self):
        root = Path(__file__).resolve().parent.parent
        for rel in ("Entry.py", "scripts/probe_pipeline.py"):
            text = (root / rel).read_text(encoding="utf-8")
            assert "recent_user_summaries(engine.evidence, recent)" in text, rel
            assert "ep.content_summary for ep in recent" not in text, rel


# ─── 3. health_score 與 review_concept ───────────────────────────────────────


def _aged_concept(independent: int | None) -> Concept:
    c = Concept(
        name="x", description="d", confidence=0.9,
        source_episode_ids=["a", "b", "c"],
        updated_at=datetime.now(timezone.utc) - timedelta(days=30),
    )
    if independent is not None:
        c.evidence_review = {"status": "supported", "independent_count": independent}
    return c


class TestHealthScore:
    def test_unreviewed_falls_back_to_raw_count(self):
        assert _aged_concept(None).health_score == pytest.approx(0.9, abs=0.01)

    def test_reviewed_uses_independent_count_not_raw(self):
        raw = _aged_concept(None).health_score
        one = _aged_concept(1).health_score
        three = _aged_concept(3).health_score
        assert one < raw
        assert one == pytest.approx(0.9 * (1 / 3), abs=0.01)
        assert three == pytest.approx(raw, abs=0.01)

    def test_reviewed_zero_independent_uses_floor(self):
        assert _aged_concept(0).health_score == pytest.approx(0.9 * 0.3, abs=0.01)


class TestReviewConceptEntry:
    def test_engine_review_concept_reviews_with_llm(self, tmp_path):
        eng = MemoryEngine(
            storage_path=tmp_path / "d", namespace_id="ns1", llm_fn=lambda p: "",
        )
        gate = ConceptInhibitor(semantic=eng.semantic, llm_fn=None)
        eng.set_concept_inhibitor(gate)
        ep = user_ep(eng, "我喜歡咖啡")
        cid = make_concept(eng, gate, "咖啡", [ep])
        uid = unit_ids(eng, ep)[0]
        eng.set_llm_fn(lambda p: reply(cited=[uid]))
        result = eng.review_concept(cid)
        assert result["outcome"] == "reviewed"
        assert eng.semantic.get(cid).evidence_review["status"] == "supported"

    def test_mcp_handler_routes_to_engine(self, mcp_handlers):
        calls = []

        class FakeEngine:
            def review_concept(self, concept_id):
                calls.append(concept_id)
                return {"concept_id": concept_id, "outcome": "reviewed", "reason": ""}

        text = asyncio.run(
            mcp_handlers._handle_review_concept(lambda ns: FakeEngine(), {"concept_id": "c1"})
        )
        assert calls == ["c1"] and "reviewed" in text

    def test_mcp_handler_rejects_shared_namespace_write(self, mcp_handlers):
        class FakeEngine:
            def review_concept(self, concept_id):  # pragma: no cover - 不應被呼叫
                raise AssertionError("共享 namespace 不得執行寫入")

        # fixture 載入後會清掉 mcp_server.*，重新 import 會得到不同的例外類別，故比對類別名稱
        with pytest.raises(Exception) as exc_info:
            asyncio.run(mcp_handlers._handle_review_concept(
                lambda ns: FakeEngine(), {"concept_id": "c1", "namespace_id": "scope-g42"},
            ))
        assert type(exc_info.value).__name__ == "NamespaceAccessDenied"

    def test_cli_command_reports_unknown_and_reviews(self, engine, gate):
        pytest.importorskip("rich")
        import Entry

        messages: list[str] = []

        class Con:
            def print(self, msg, *a, **k):
                messages.append(str(msg))

        Entry._cmd_review_concept(engine, "/review-concept", Con())
        # 指令名稱精確比對：/review-conceptx 不是此指令
        src = Path(Entry.__file__).read_text(encoding="utf-8")
        assert 'cmd.split(maxsplit=1)[0] == "/review-concept"' in src
        assert "用法" in messages[-1]
        Entry._cmd_review_concept(engine, "/review-concept 不存在", Con())
        assert "找不到" in messages[-1]
        ep = user_ep(engine, "我喜歡咖啡")
        make_concept(engine, gate, "咖啡", [ep])
        Entry._cmd_review_concept(engine, "/review-concept 咖啡", Con())
        assert "概念審核" in messages[-1] and "skipped" in messages[-1]  # 無 LLM -> 不寫資料


class TestMcpFixtureIsolation:
    """mcp_handlers／srv 的假模組只存在於單一測試期間（順序相依：先用、再檢查）。"""

    def test_a_uses_handlers(self, mcp_handlers):
        assert hasattr(mcp_handlers, "_handle_review_concept")

    def test_b_nothing_leaks_into_next_test(self):
        import sys

        assert "mcp_server.handlers" not in sys.modules
        leaked = [
            n for n, m in sys.modules.items()
            if (n == "mcp" or n.startswith("mcp.")) and type(m).__name__ == "_AnyAttrModule"
        ]
        assert leaked == []
        for name in ("mcp", "mcp.server", "mcp.server.stdio", "mcp.types"):
            mod = sys.modules.get(name)
            assert mod is None or getattr(mod, "__file__", None), name  # 假模組沒有 __file__


# ─── 5. graph dirty ──────────────────────────────────────────────────────────


class TestGraphDirty:
    def test_save_failure_marks_dirty_and_retry_heals(self, tmp_path, monkeypatch):
        graph = MemoryGraph(storage_path=tmp_path / "g")
        graph.add_entity_node("ent_1", name="x")
        import echo_memory.storage.graph as graph_mod

        real = graph_mod.replace_with_retry
        monkeypatch.setattr(
            graph_mod, "replace_with_retry",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk")),
        )
        with pytest.raises(OSError):
            graph.save()
        assert graph.is_dirty
        assert graph.retry_save_if_dirty() is False and graph.is_dirty  # 仍失敗：維持 dirty、不拋

        monkeypatch.setattr(graph_mod, "replace_with_retry", real)
        assert graph.retry_save_if_dirty() is True
        assert not graph.is_dirty
        assert MemoryGraph(storage_path=tmp_path / "g").has_node("ent_1")

    def test_clean_graph_retry_is_noop(self, tmp_path):
        graph = MemoryGraph(storage_path=tmp_path / "g")
        assert graph.retry_save_if_dirty() is False

    def test_next_successful_save_clears_dirty(self, tmp_path, monkeypatch):
        graph = MemoryGraph(storage_path=tmp_path / "g")
        import echo_memory.storage.graph as graph_mod

        real = graph_mod.replace_with_retry
        monkeypatch.setattr(
            graph_mod, "replace_with_retry",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk")),
        )
        with pytest.raises(OSError):
            graph.save()
        monkeypatch.setattr(graph_mod, "replace_with_retry", real)
        graph.save()
        assert not graph.is_dirty

    def test_dream_cycle_retries_dirty_graph(self, engine, gate, monkeypatch):
        engine.ensure_initialized()
        graph = engine.episodic.graph
        graph._dirty = True
        calls = []
        real = graph.save
        monkeypatch.setattr(graph, "save", lambda: (calls.append(1), real())[1])
        engine.dream("manual")
        assert calls and not graph.is_dirty


# ─── 6. intention 佐證學習權重（Golden）──────────────────────────────────────

GROUP = {"channel_kind": "group", "audience_size": 8}


class TestIntentionCorroborationWeight:
    def _claim(self, engine, gate, extra_eps):
        ep_claim = user_ep(engine, "我明天會去健身房", at=T0)
        cid = make_concept(engine, gate, "明天健身", [ep_claim, *extra_eps])
        return cid, unit_ids(engine, ep_claim)[0]

    def test_low_weight_group_corroboration_is_insufficient(self, engine, gate):
        ep_later = user_ep(engine, "我今天去了健身房", at=T0 + timedelta(days=1), attrs=GROUP)
        cid, claim = self._claim(engine, gate, [ep_later])
        later = unit_ids(engine, ep_later)[0]
        outcome = make_reviewer(
            engine, gate, lambda p: reply(cited=[claim], claim_kind="intention", corro=[later]),
        ).review(cid)
        assert outcome.status == "insufficient"
        review = engine.semantic.get(cid).evidence_review
        assert review["corroborated_by"] == [later]
        assert review["corroboration_strength"] == pytest.approx(0.08)
        assert "corroboration_below_threshold" in review["reason"]

    def test_full_weight_dm_corroboration_is_supported(self, engine, gate):
        ep_later = user_ep(engine, "我今天去了健身房", at=T0 + timedelta(days=1))
        cid, claim = self._claim(engine, gate, [ep_later])
        later = unit_ids(engine, ep_later)[0]
        outcome = make_reviewer(
            engine, gate, lambda p: reply(cited=[claim], claim_kind="intention", corro=[later]),
        ).review(cid)
        assert outcome.status == "supported"
        assert engine.semantic.get(cid).evidence_review["corroboration_strength"] == pytest.approx(1.0)

    def test_several_group_corroborators_can_reach_threshold(self, engine, gate):
        eps = [
            user_ep(
                engine, f"我也看到他去了健身房{i}", at=T0 + timedelta(days=1, minutes=i),
                attrs={"channel_kind": "group", "audience_size": 2, "addressed_to_agent": True},
                speaker=f"v{i}",
            )
            for i in range(2)
        ]
        cid, claim = self._claim(engine, gate, eps)
        ids = [unit_ids(engine, e)[0] for e in eps]
        outcome = make_reviewer(
            engine, gate, lambda p: reply(cited=[claim], claim_kind="intention", corro=ids),
        ).review(cid)
        assert outcome.status == "supported"  # 1 - (1-0.8)^2 = 0.96 >= 0.9

    def test_observation_corroborates_at_full_weight(self, engine, gate):
        cid, claim = self._claim(engine, gate, [])
        obs = EvidenceUnit(
            id="obs1", episode_id=None, source_type="observation", content="簽到紀錄",
            occurred_at=T0 + timedelta(days=1), content_hash=compute_content_hash("簽到紀錄"),
        )
        engine.evidence.append([obs])
        engine.semantic.add_derivation(cid, "evidence", "obs1", "dream")
        outcome = make_reviewer(
            engine, gate, lambda p: reply(cited=[claim], claim_kind="intention", corro=["obs1"]),
        ).review(cid)
        assert outcome.status == "supported"

    def test_gate_rejects_supported_intention_with_weak_corroboration(self, engine, gate):
        ep = user_ep(engine, "我明天會去健身房")
        cid = make_concept(engine, gate, "明天健身", [ep])
        concept = engine.semantic.get(cid)
        weak = EvidenceReview(
            status="supported", support_strength=0.5, claim_kind="intention",
            corroborated_by=["u1"], corroboration_strength=0.08,
            basis_digest=compute_basis_digest(concept), reviewer="llm",
        )
        ok, tx = gate.process_review_patch(cid, weak)
        assert not ok and "佐證強度" in tx.patches[0].rejection_reason
        # 舊資料（未計算佐證強度）沿用有佐證即可
        legacy = EvidenceReview(
            status="supported", support_strength=0.5, claim_kind="intention",
            corroborated_by=["u1"], basis_digest=compute_basis_digest(concept), reviewer="llm",
        )
        ok, _tx = gate.process_review_patch(cid, legacy)
        assert ok


# ─── Hypothesis：主張者自述不計入佐證強度 ────────────────────────────────────


def _hunit(uid, speaker, attrs, ep, source_type="user"):
    return EvidenceUnit(
        id=uid, episode_id=ep, source_type=source_type, speaker_id=speaker,
        content=uid, content_hash=compute_content_hash(uid), occurred_at=T0,
        provenance_status="known", attrs=dict(attrs),
    )


class TestHypothesisSelfReportExcluded:
    def test_claimant_dm_plus_low_weight_other_is_insufficient(self):
        # 重現：u1 自述 DM（w=1.0）＋u2 群聊低權重，過去因計入自述而 supported、強度 1.0
        out = evaluate_hypothesis_support(
            [_hunit("a", "u1", DM, "e1"), _hunit("b", "u2", GROUP, "e2")],
        )
        assert out.status == "insufficient"
        assert out.strength == pytest.approx(0.08)
        assert out.independent_ids == ["b"]

    def test_claimant_dm_plus_enough_weight_other_is_supported(self):
        dm = evaluate_hypothesis_support(
            [_hunit("a", "u1", DM, "e1"), _hunit("b", "u2", DM, "e2")],
        )
        assert dm.status == "supported" and dm.independent_ids == ["b"]
        addressed = {"channel_kind": "group", "audience_size": 2, "addressed_to_agent": True}
        out = evaluate_hypothesis_support(
            [_hunit("a", "u1", DM, "e1"), _hunit("b", "u2", addressed, "e2"),
             _hunit("c", "u2", addressed, "e3")],
        )
        assert out.status == "supported"  # 1 - 0.2 * 0.2 = 0.96
        assert out.independent_ids == ["b", "c"] and "a" not in out.independent_ids

    def test_many_claimant_units_do_not_lift_low_weight_other(self):
        units = [_hunit(f"a{i}", "u1", DM, f"e{i}") for i in range(4)]
        units.append(_hunit("b", "u2", GROUP, "eb"))
        out = evaluate_hypothesis_support(units)
        assert out.status == "insufficient" and out.independent_ids == ["b"]

    def test_observation_corroboration_is_supported_and_claim_excluded(self):
        out = evaluate_hypothesis_support(
            [_hunit("a", "u1", GROUP, "e1"), _hunit("o", None, {}, None, "observation")],
        )
        assert out.status == "supported" and out.independent_ids == ["o"]

    def test_observation_alone_has_no_claim(self):
        out = evaluate_hypothesis_support([_hunit("o", None, {}, None, "observation")])
        assert out.status == "insufficient" and "no_user_claim" in out.notes

    def test_primary_speaker_is_stable_and_overridable(self):
        units = [_hunit("a", "u1", DM, "e1"), _hunit("b", "u2", DM, "e2")]
        # 同分同數：取 speaker id 排序最小者（u1）為主要說話者，佐證是 u2
        assert evaluate_hypothesis_support(units).independent_ids == ["b"]
        assert evaluate_hypothesis_support(list(reversed(units))).independent_ids == ["b"]
        # 明確主張者優先
        assert evaluate_hypothesis_support(units, claimant_speaker="u2").independent_ids == ["a"]

    def test_intention_uses_independent_strength_only(self):
        out = evaluate_hypothesis_support(
            [_hunit("a", "u1", DM, "e1"), _hunit("b", "u2", GROUP, "e2")], "intention",
        )
        assert out.status == "insufficient"
        assert out.corroboration_strength == pytest.approx(0.08)

    def test_gate_recomputes_same_rule(self, tmp_path):
        from tests.hypothesis_support import (
            ScriptedLLM, build_env, llm_reply, make_bridge, make_concept, propose, user_ep,
        )

        env = build_env(tmp_path)
        ep_a = user_ep(env.engine, "我最近很迷咖啡拉花", speaker="u1")
        ep_b = user_ep(env.engine, "他每天早上都去那間咖啡店", speaker="u2", attrs=GROUP)
        a = make_concept(env, "咖啡拉花", [ep_a])
        b = make_concept(env, "早晨咖啡店", [ep_b])
        make_bridge(env, a, b)
        hid = propose(env, a, b)
        cited = [unit_ids(env.engine, ep_a)[0], unit_ids(env.engine, ep_b)[0]]
        env.reviewer._llm_fn = ScriptedLLM(llm_reply("supported", cited))
        env.reviewer.review(hid)
        # LLM 說 supported，Gate 依同一規則重算：低權重他人佐證不足 -> 不得 supported
        assert env.store.get(hid).status != "supported"
