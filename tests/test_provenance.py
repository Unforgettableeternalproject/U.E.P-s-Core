"""EM-006 / EM-007：Concept 多階 Provenance、版本引用、來源去重／循環／缺失。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from echo_memory.affect.concept_inhibitor import ConceptInhibitor
from echo_memory.engine import MemoryEngine
from echo_memory.schemas.concept import Concept
from echo_memory.schemas.evidence import EvidenceInput, EvidenceUnit, compute_content_hash
from echo_memory.schemas.provenance import Derivation
from tests.baseline._support import SNAPSHOT_DIR, restore_snapshot


@pytest.fixture
def engine(tmp_path):
    return MemoryEngine(storage_path=tmp_path / "data", namespace_id="ns1")


@pytest.fixture
def inhibitor(engine):
    return ConceptInhibitor(semantic=engine.semantic, llm_fn=None)


def _known_episode(engine, text: str) -> str:
    """寫入一個只有 user unit 的 known episode，回傳 episode_id。"""
    return engine.store_episode(
        text, "", "s1",
        evidence=[EvidenceInput(source_type="user", content=text, speaker_id="u1")],
    )


def _evidence_ids(engine, ep_id: str) -> list[str]:
    return list(engine.episodic.get(ep_id).evidence_ids)


def _dream_concept(engine, inhibitor, name: str, ep_ids: list[str]) -> str:
    """模擬 Dream 蒸餾：走 extractor/distillation patch pipeline。"""
    cid, tx = inhibitor.process_extractor_patch(
        noun=name, desc=f"{name} 的描述", episode_id=ep_ids[0],
        evidence_kind="distillation", triggered_by="dream",
        source_episode_ids=ep_ids,
    )
    assert cid is not None and tx.status == "committed"
    return cid


def _reflect_from(engine, inhibitor, name: str, parents: list[str]) -> str:
    """模擬 Reflection：從既有概念（以名稱）產生新概念。"""
    ok, result = inhibitor.process_reflection_patch(
        operation="create", target_name=name,
        params={"description": f"{name} 的描述", "parent_concepts": parents},
    )
    assert ok, result
    return result["concept_id"]


# ─── 資料結構 ─────────────────────────────────────────────────────────


class TestConceptFields:
    def test_old_concept_dict_loads_with_defaults(self):
        """舊 concepts.json 沒有 version／derivations 也能載入。"""
        old = Concept(name="x", description="d").to_dict()
        del old["version"]
        del old["derivations"]
        c = Concept.from_dict(old)
        assert c.version == 1
        assert c.derivations == []

    def test_round_trip_keeps_derivations(self):
        c = Concept(name="x", description="d")
        c.add_derivation("concept", "con_p", "reflection", parent_version=3)
        c2 = Concept.from_dict(c.to_dict())
        assert Derivation.from_dict(c2.derivations[0]).parent_version == 3

    def test_version_monotonic_and_derivation_dedup(self):
        c = Concept(name="x", description="d")
        c.record_mutation()
        c.record_mutation()
        assert c.version == 3
        assert c.add_derivation("episode", "ep1", "dream") is True
        assert c.add_derivation("episode", "ep1", "dream") is False
        assert c.derivations[0]["at_version"] == 3

    def test_invalid_kind_or_producer_rejected(self):
        c = Concept(name="x", description="d")
        with pytest.raises(ValueError):
            c.add_derivation("tweet", "a", "dream")
        with pytest.raises(ValueError):
            c.add_derivation("episode", "a", "gossip")

    def test_upsert_update_bumps_version_and_records_producer(self, engine):
        sem = engine.semantic
        cid = sem.upsert_concept("貓", "d1", source_episode_id="ep1", producer="dream")
        assert sem.get(cid).version == 1
        sem.upsert_concept("貓", "d2", source_episode_id="ep2", producer="extractor")
        c = sem.get(cid)
        assert c.version == 2
        by_id = {d["id"]: d for d in c.derivations}
        assert by_id["ep1"]["producer"] == "dream" and by_id["ep1"]["at_version"] == 1
        assert by_id["ep2"]["producer"] == "extractor" and by_id["ep2"]["at_version"] == 2


# ─── 多階回溯 ─────────────────────────────────────────────────────────


class TestMultiHop:
    def test_dream_then_reflection_then_derived_again(self, engine, inhibitor):
        ep = _known_episode(engine, "我喜歡黑咖啡")
        ev_ids = _evidence_ids(engine, ep)

        b = _dream_concept(engine, inhibitor, "B", [ep])
        c = _reflect_from(engine, inhibitor, "C", ["B"])
        d = _reflect_from(engine, inhibitor, "D", ["C"])

        res = engine.resolve_provenance(d)
        assert res.complete
        assert res.evidence_ids == ev_ids
        steps = res.paths[0].steps
        assert [(s.kind, s.id) for s in steps[:3]] == [
            ("concept", d), ("concept", c), ("concept", b),
        ]
        assert [s.kind for s in steps[3:]] == ["episode", "evidence"]
        assert steps[1].producer == "reflection"
        assert steps[3].producer == "dream"

    def test_extractor_synonym_merge_recorded_as_extractor(self, engine, inhibitor):
        ep1 = _known_episode(engine, "a")
        ep2 = _known_episode(engine, "b")
        b = _dream_concept(engine, inhibitor, "B", [ep1])
        inhibitor.process_extractor_patch(
            noun="B2", desc="x", episode_id=ep2,
            evidence_kind="extractor_synonym_merge", existing_concept_id=b,
        )
        derivs = {d["id"]: d for d in engine.semantic.get(b).derivations}
        assert derivs[ep1]["producer"] == "dream"
        assert derivs[ep2]["producer"] == "extractor"
        assert set(engine.resolve_provenance(b).evidence_ids) == set(
            _evidence_ids(engine, ep1) + _evidence_ids(engine, ep2)
        )

    def test_reflection_create_without_parents_has_no_evidence(self, engine, inhibitor):
        ok, res = inhibitor.process_reflection_patch(
            operation="create", target_name="孤兒", params={"description": "d"},
        )
        assert ok
        out = engine.resolve_provenance(res["concept_id"])
        assert out.found and out.evidence == []

    def test_unknown_reflection_parent_name_is_skipped(self, engine, inhibitor):
        cid = _reflect_from(engine, inhibitor, "C", ["不存在的概念"])
        assert engine.semantic.get(cid).derivations == []

    def test_reflection_merge_copies_secondary_sources(self, engine, inhibitor):
        ep1 = _known_episode(engine, "a")
        ep2 = _known_episode(engine, "b")
        _dream_concept(engine, inhibitor, "P", [ep1])
        s = _dream_concept(engine, inhibitor, "S", [ep2])
        child = _reflect_from(engine, inhibitor, "Child", ["S"])
        p = engine.semantic.get_by_name("P").id

        ok, _ = inhibitor.process_reflection_patch(
            operation="merge", target_name="P", params={"merge_with": "S"},
        )
        assert ok
        assert set(engine.resolve_provenance(p).evidence_ids) == set(
            _evidence_ids(engine, ep1) + _evidence_ids(engine, ep2)
        )
        # secondary 已被移除：子概念回報 missing，不崩潰
        res = engine.resolve_provenance(child)
        assert [(m.kind, m.id) for m in res.missing] == [("concept", s)]

    def test_extractor_rollback_removes_derivation(self, engine, inhibitor):
        ep1 = _known_episode(engine, "a")
        ep2 = _known_episode(engine, "b")
        b = _dream_concept(engine, inhibitor, "B", [ep1])
        _, tx = inhibitor.process_extractor_patch(
            noun="B2", desc="x", episode_id=ep2,
            evidence_kind="extractor_synonym_merge", existing_concept_id=b,
        )
        inhibitor._rollback_patch(tx.patches[0])
        concept = engine.semantic.get(b)
        assert ep2 not in concept.source_episode_ids
        assert ep2 not in {d["id"] for d in concept.derivations}
        assert engine.resolve_provenance(b).evidence_ids == _evidence_ids(engine, ep1)

    def test_evidence_derived_from_resolves_to_original(self, engine):
        ep = _known_episode(engine, "原話")
        original_id = _evidence_ids(engine, ep)[0]
        now = datetime.now(timezone.utc)
        derived = EvidenceUnit(
            id="ev_derived", episode_id=None, source_type="reflection",
            content="整理後", occurred_at=now, content_hash=compute_content_hash("整理後"),
            derived_from=(original_id,),
        )
        engine.evidence.append([derived])
        cid = engine.semantic.upsert_concept("E", "d")
        engine.semantic.add_derivation(cid, "evidence", "ev_derived", "reflection")
        res = engine.resolve_provenance(cid)
        assert res.evidence_ids == [original_id]
        assert [s.id for s in res.paths[0].steps][-2:] == ["ev_derived", original_id]


# ─── 去重／獨立性 ─────────────────────────────────────────────────────


class TestIndependentSupport:
    def test_shared_origin_counts_once(self, engine, inhibitor):
        ep = _known_episode(engine, "同一句話")
        _dream_concept(engine, inhibitor, "B", [ep])
        c = _reflect_from(engine, inhibitor, "C", ["B"])
        d = _reflect_from(engine, inhibitor, "D", ["B"])

        sup = engine.independent_support([c, d])
        assert sup.independent_count == 1
        assert sup.independent_ids == _evidence_ids(engine, ep)
        assert sup.shared_ids == sup.independent_ids
        assert sup.per_concept[c] == sup.per_concept[d]

    def test_distinct_origins_add_up(self, engine, inhibitor):
        ep1 = _known_episode(engine, "一")
        ep2 = _known_episode(engine, "二")
        _dream_concept(engine, inhibitor, "B1", [ep1])
        _dream_concept(engine, inhibitor, "B2", [ep2])
        c = _reflect_from(engine, inhibitor, "C", ["B1"])
        d = _reflect_from(engine, inhibitor, "D", ["B2"])
        e = _reflect_from(engine, inhibitor, "E", ["B1", "B2"])

        assert engine.independent_support([c, d]).independent_count == 2
        assert engine.independent_support([c, d, e]).independent_count == 2
        assert engine.independent_support([e]).independent_count == 2
        assert engine.independent_support([c]).independent_count == 1

    def test_user_and_agent_units_of_one_episode(self, engine, inhibitor):
        ep = engine.store_episode(
            "問", "答", "s1",
            evidence=[
                EvidenceInput(source_type="user", content="問", speaker_id="u1"),
                EvidenceInput(source_type="agent", content="答", speaker_id="agent"),
            ],
        )
        b = _dream_concept(engine, inhibitor, "B", [ep])
        sup = engine.independent_support([b])
        assert sup.independent_count == 2          # 兩個原始 Evidence
        assert sup.independent_episode_count == 1  # 但只來自同一個 Episode
        assert sup.user_attested_count == 1        # 只有 user unit 能證明使用者說過

    def test_legacy_unknown_counted_separately_never_attests(self, engine, inhibitor):
        legacy_ep = engine.store_episode("舊的問", "舊的答", "s1")  # 舊介面→legacy_unknown
        known_ep = _known_episode(engine, "新的話")
        b = _dream_concept(engine, inhibitor, "B", [legacy_ep])
        sup_legacy = engine.independent_support([b])
        assert sup_legacy.independent_count == 0
        assert sup_legacy.user_attested_count == 0
        assert sup_legacy.legacy_unknown_count == 2

        c = _dream_concept(engine, inhibitor, "C", [known_ep])
        sup_mix = engine.independent_support([b, c])
        assert sup_mix.independent_count == 1
        assert sup_mix.user_attested_count == 1
        assert sup_mix.legacy_unknown_count == 2

    def test_unmigrated_legacy_episode_resolves_to_synthetic_unknown(self, engine, inhibitor):
        ep_id = engine.store_episode("舊的問", "舊的答", "s1")
        episode = engine.episodic.get(ep_id)
        episode.evidence_ids = []  # 模擬尚未遷移的舊 Episode
        b = _dream_concept(engine, inhibitor, "B", [ep_id])
        res = engine.resolve_provenance(b)
        assert res.evidence and all(u.provenance_status == "legacy_unknown" for u in res.evidence)
        assert engine.independent_support([b]).independent_count == 0

    def test_unknown_concept_reported_missing(self, engine):
        sup = engine.independent_support(["con_nope"])
        assert sup.independent_count == 0
        assert [(m.kind, m.id) for m in sup.missing] == [("concept", "con_nope")]


# ─── 循環／缺失 ──────────────────────────────────────────────────────


class TestCyclesAndMissing:
    def test_cycle_detected_and_aborted(self, engine):
        ep = _known_episode(engine, "根")
        sem = engine.semantic
        a = sem.upsert_concept("A", "d", source_episode_id=ep)
        b = sem.upsert_concept("B", "d")
        sem.add_derivation(a, "concept", b, "reflection")
        sem.add_derivation(b, "concept", a, "reflection")

        res = engine.resolve_provenance(a)
        assert [c.cycle for c in res.cycles] == [(a, b, a)]
        assert not res.complete
        # 循環不吞掉合法來源
        assert res.evidence_ids == _evidence_ids(engine, ep)

    def test_self_reference_is_cycle(self, engine):
        sem = engine.semantic
        a = sem.upsert_concept("A", "d")
        sem.add_derivation(a, "concept", a, "reflection")
        res = engine.resolve_provenance(a)
        assert [c.cycle for c in res.cycles] == [(a, a)]

    def test_cycle_does_not_break_independent_support(self, engine):
        sem = engine.semantic
        a = sem.upsert_concept("A", "d")
        b = sem.upsert_concept("B", "d")
        sem.add_derivation(a, "concept", b, "reflection")
        sem.add_derivation(b, "concept", a, "reflection")
        sup = engine.independent_support([a, b])
        assert sup.independent_count == 0
        assert sup.cycles

    def test_diamond_is_not_a_cycle(self, engine, inhibitor):
        ep = _known_episode(engine, "根")
        _dream_concept(engine, inhibitor, "B", [ep])
        _reflect_from(engine, inhibitor, "C", ["B"])
        _reflect_from(engine, inhibitor, "D", ["B"])
        top = _reflect_from(engine, inhibitor, "Top", ["C", "D"])
        res = engine.resolve_provenance(top)
        assert res.complete and len(res.evidence) == 1

    def test_missing_episode_concept_and_evidence(self, engine):
        sem = engine.semantic
        a = sem.upsert_concept("A", "d", source_episode_id="ep_gone")
        sem.add_derivation(a, "concept", "con_gone", "reflection")
        sem.add_derivation(a, "evidence", "ev_gone", "reflection")
        res = engine.resolve_provenance(a)
        assert {(m.kind, m.id) for m in res.missing} == {
            ("episode", "ep_gone"), ("concept", "con_gone"), ("evidence", "ev_gone"),
        }
        assert res.evidence == [] and not res.complete

    def test_missing_branch_does_not_hide_good_branch(self, engine):
        ep = _known_episode(engine, "好的來源")
        sem = engine.semantic
        a = sem.upsert_concept("A", "d", source_episode_id=ep)
        sem.add_derivation(a, "episode", "ep_gone", "dream")
        res = engine.resolve_provenance(a)
        assert res.evidence_ids == _evidence_ids(engine, ep)
        assert [(m.kind, m.id) for m in res.missing] == [("episode", "ep_gone")]

    def test_episode_with_dangling_evidence_id(self, engine):
        ep = _known_episode(engine, "x")
        engine.episodic.get(ep).evidence_ids.append("ev_dangling")
        a = engine.semantic.upsert_concept("A", "d", source_episode_id=ep)
        res = engine.resolve_provenance(a)
        assert ("evidence", "ev_dangling") in {(m.kind, m.id) for m in res.missing}
        assert len(res.evidence) == 1

    def test_unknown_root_concept(self, engine):
        res = engine.resolve_provenance("con_nope")
        assert res.found is False and res.evidence == []

    def test_depth_limit_truncates_instead_of_hanging(self, engine):
        from echo_memory.storage.provenance import ProvenanceResolver

        sem = engine.semantic
        ids = [sem.upsert_concept(f"N{i}", "d") for i in range(6)]
        for lo, hi in zip(ids, ids[1:]):
            sem.add_derivation(lo, "concept", hi, "reflection")
        resolver = ProvenanceResolver(sem, engine.episodic.get, engine.evidence, max_depth=3)
        assert resolver.resolve(ids[0]).truncated is True


# ─── 版本綁定 ────────────────────────────────────────────────────────


class TestVersionBinding:
    def test_child_stays_bound_to_parent_version_after_parent_changes(self, engine, inhibitor):
        ep1 = _known_episode(engine, "舊來源")
        ep2 = _known_episode(engine, "後來才加的來源")
        b = _dream_concept(engine, inhibitor, "B", [ep1])
        v_before = engine.semantic.get(b).version
        c = _reflect_from(engine, inhibitor, "C", ["B"])

        # parent 之後被修改：新增來源 + 改描述
        inhibitor.process_extractor_patch(
            noun="B2", desc="x", episode_id=ep2,
            evidence_kind="extractor_synonym_merge", existing_concept_id=b,
        )
        engine.semantic.upsert_concept("B", "改過的描述")
        assert engine.semantic.get(b).version > v_before

        child = engine.resolve_provenance(c)
        assert child.evidence_ids == _evidence_ids(engine, ep1)  # 不含後來的 ep2
        assert child.paths[0].steps[1].version == v_before  # 路徑顯示綁定的版本
        # parent 本身看得到新來源；指定舊版本則重現當時
        assert set(engine.resolve_provenance(b).evidence_ids) == set(
            _evidence_ids(engine, ep1) + _evidence_ids(engine, ep2)
        )
        old = engine.resolve_provenance(b, at_version=v_before)
        assert old.evidence_ids == _evidence_ids(engine, ep1)
        assert old.stale is True and old.bound_version == v_before

    def test_derivation_records_parent_version_at_derivation_time(self, engine, inhibitor):
        ep = _known_episode(engine, "x")
        b = _dream_concept(engine, inhibitor, "B", [ep])
        pv = engine.semantic.get(b).version
        c = _reflect_from(engine, inhibitor, "C", ["B"])
        rec = engine.semantic.get(c).derivations[0]
        assert (rec["kind"], rec["id"], rec["producer"], rec["parent_version"]) == (
            "concept", b, "reflection", pv,
        )

    def test_unbound_parent_version_is_flagged(self, engine):
        ep = _known_episode(engine, "x")
        sem = engine.semantic
        b = sem.upsert_concept("B", "d", source_episode_id=ep)
        c = sem.upsert_concept("C", "d")
        # 直接以 Concept API 寫入未綁定版本（semantic.add_derivation 預設會補版本）
        sem.get(c).add_derivation("concept", b, "reflection")
        res = engine.resolve_provenance(c)
        assert res.unbound_versions == [b]
        assert res.evidence_ids == _evidence_ids(engine, ep)

    def test_rollback_does_not_reuse_versions(self, engine, inhibitor):
        ep1 = _known_episode(engine, "a")
        ep2 = _known_episode(engine, "b")
        b = _dream_concept(engine, inhibitor, "B", [ep1])
        _, tx = inhibitor.process_extractor_patch(
            noun="B2", desc="x", episode_id=ep2,
            evidence_kind="extractor_synonym_merge", existing_concept_id=b,
        )
        v_after = engine.semantic.get(b).version
        inhibitor._rollback_patch(tx.patches[0])
        assert engine.semantic.get(b).version >= v_after


# ─── 舊資料 ──────────────────────────────────────────────────────────


class TestLegacyData:
    def test_snapshot_concepts_resolve_as_legacy_unknown(self, tmp_path):
        dest = restore_snapshot(SNAPSHOT_DIR, tmp_path / "snap")
        engine = MemoryEngine(storage_path=dest)
        concepts = engine.semantic.list_concepts(limit=None)
        assert concepts
        for concept in concepts:
            assert concept.version == 1 and concept.derivations == []
            res = engine.resolve_provenance(concept.id)
            assert res.found
            assert all(u.provenance_status == "legacy_unknown" for u in res.evidence)
            sup = engine.independent_support([concept.id])
            assert sup.independent_count == 0 and sup.user_attested_count == 0
        assert any(engine.resolve_provenance(c.id).evidence for c in concepts)
