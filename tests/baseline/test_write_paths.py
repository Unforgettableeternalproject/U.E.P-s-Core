"""EM-002：四條 Concept 寫入路徑的可獨立重播整合測試。

四條路徑：Conversation（ConceptInhibitor.process）、Extractor（ConceptExtractor）、
Dream（MemoryEngine.dream 蒸餾）、Reflection（ReflectionActionQueue + ReflectionExecutor）。

每條路徑都：
  - 從固定快照（或空 engine）還原到 tmp 目錄
  - 使用確定性 fake LLM（不依賴真實模型判斷）
  - 斷言 Concept / Relation(Graph) / Audit / PatchTransaction 的實際狀態

只走公開介面；只讀取（不 patch）內部鎖。
"""

from __future__ import annotations

import numpy as np
import pytest

from echo_memory.affect.concept_extractor import ConceptExtractor
from echo_memory.affect.concept_inhibitor import ConceptInhibitor
from echo_memory.affect.concept_verifier import ConceptAssessment, ConceptVerification
from tests.baseline._support import (
    directed_edges,
    dream_audit,
    edge_pairs,
    make_fake_llm,
    read_jsonl,
    tx_audit,
)


def _assessment(engine, name, status, reason, update=None, relevance=0.9):
    c = engine.semantic.get_by_name(name)
    return ConceptAssessment(
        concept_id=c.id, concept_name=name, status=status, reason=reason,
        suggested_update=update, relevance=relevance,
    )


def _new_rows(engine, before: int):
    return tx_audit(engine)[before:]


# ═══════════════════════════════════════════════════════════════════════════
# 路徑 1：Conversation（ConceptVerification -> ConceptInhibitor.process）
# ═══════════════════════════════════════════════════════════════════════════


class TestConversationPath:
    def _inh(self, engine, **fake_kwargs):
        return ConceptInhibitor(semantic=engine.semantic, llm_fn=make_fake_llm(**fake_kwargs))

    def test_expand_strong_evidence_approved_by_rule(self, restored_engine):
        e = restored_engine
        before = len(tx_audit(e))
        calls: list[str] = []
        inh = ConceptInhibitor(semantic=e.semantic, llm_fn=make_fake_llm(calls=calls))
        coffee = e.semantic.get_by_name("手沖咖啡")
        old_facts, old_hist, old_conf = len(coffee.facts), len(coffee.metadata["patch_history"]), coffee.confidence

        tx = inh.process(
            ConceptVerification(assessments=[
                _assessment(e, "手沖咖啡", "expand", "使用者提到新的沖煮器具", "也使用 V60 濾杯"),
            ]),
            user_input="我最近買了 V60", llm_response="很棒", session_id="s1", turn_id="ep_x",
        )

        assert tx.status == "committed" and tx.triggered_by == "conversation"
        assert [p.status for p in tx.patches] == ["applied"]
        assert "corroborate" not in calls, "規則層強證據 expand 不應呼叫 LLM"
        coffee = e.semantic.get_by_name("手沖咖啡")
        assert len(coffee.facts) == old_facts + 1
        assert coffee.facts[-1]["text"] == "也使用 V60 濾杯"
        assert len(coffee.metadata["patch_history"]) == old_hist + 1
        assert coffee.metadata["patch_history"][-1]["patch_id"] == tx.patches[0].patch_id
        assert coffee.confidence == pytest.approx(min(old_conf + 0.05, 0.95))

        rows = _new_rows(e, before)
        assert len(rows) == 1
        assert rows[0]["triggered_by"] == "conversation"
        assert rows[0]["session_id"] == "s1" and rows[0]["turn_id"] == "ep_x"
        assert rows[0]["patches"][0]["status"] == "applied"
        assert rows[0]["patches"][0]["evidence"]["evidence_kind"] == "explicit_user_claim"

    def test_modify_with_llm_corroboration_applies_and_reembeds(self, restored_engine):
        e = restored_engine
        inh = self._inh(e, corroborate={"approve": True, "confidence": 0.9, "reason": "用戶明說"})
        c = e.semantic.get_by_name("小橘")
        old_emb = list(c.embedding)

        tx = inh.process(
            ConceptVerification(assessments=[
                _assessment(e, "小橘", "modify", "使用者更正：小橘是公貓", "使用者養的公貓"),
            ]),
            user_input="小橘是公的", llm_response="了解", session_id="s1", turn_id="ep_y",
        )

        assert [p.status for p in tx.patches] == ["applied"]
        assert tx.patches[0].evidence.llm_corroboration == "用戶明說"
        c = e.semantic.get_by_name("小橘")
        assert c.description == "使用者養的公貓"
        assert c.embedding != old_emb, "description 變了，embedding 應重算"
        assert c.metadata["patch_history"][-1]["action"] == "modify"

    def test_modify_rejected_by_llm_leaves_concept_untouched(self, restored_engine):
        e = restored_engine
        before = len(tx_audit(e))
        inh = self._inh(e, corroborate={"approve": False, "confidence": 0.9, "reason": "過度推論"})

        tx = inh.process(
            ConceptVerification(assessments=[
                _assessment(e, "小橘", "modify", "疑似修正", "使用者養的狗"),
            ]),
            user_input="嗯", llm_response="好", session_id="s1", turn_id="ep_z",
        )

        assert [p.status for p in tx.patches] == ["rejected"]
        assert tx.patches[0].rejection_reason.startswith("LLM 不佐證")
        assert e.semantic.get_by_name("小橘").description == "使用者養的貓"
        rows = _new_rows(e, before)
        assert rows[0]["patches"][0]["status"] == "rejected"

    def test_deprecate_marks_status_not_delete(self, restored_engine):
        e = restored_engine
        inh = self._inh(e)
        tx = inh.process(
            ConceptVerification(assessments=[
                _assessment(e, "夜貓作息", "deprecate", "使用者說已經改成早睡早起"),
            ]),
            user_input="我現在早睡了", llm_response="很好", session_id="s1", turn_id="ep_d",
        )
        assert [p.status for p in tx.patches] == ["applied"]
        c = e.semantic.get_by_name("夜貓作息")
        assert c is not None, "deprecate 不是 delete"
        assert c.status == "deprecated"
        assert c.metadata["deprecated_reason"] == "使用者說已經改成早睡早起"

    def test_apply_failure_rolls_back_earlier_patches(self, restored_engine):
        """第二個 patch apply 失敗 -> 第一個已 applied 的 patch 必須 rollback。"""
        e = restored_engine
        before = len(tx_audit(e))
        inh = self._inh(e)
        coffee_facts = list(e.semantic.get_by_name("手沖咖啡").facts)

        def _boom(_text):
            raise RuntimeError("embed 失敗")

        e.semantic._embed_fn = _boom  # modify 重算 embedding 時會失敗

        tx = inh.process(
            ConceptVerification(assessments=[
                _assessment(e, "手沖咖啡", "expand", "新資訊足夠長的理由說明文字", "加一條 fact"),
                _assessment(e, "小橘", "modify", "修正", "使用者養的公貓"),
            ]),
            user_input="x", llm_response="y", session_id="s1", turn_id="ep_r",
        )

        assert tx.status == "failed"
        assert [p.status for p in tx.patches] == ["rolled_back", "rejected"]
        assert e.semantic.get_by_name("手沖咖啡").facts == coffee_facts
        assert _new_rows(e, before)[0]["status"] == "failed"
        # 注意：失敗 patch 自身的半套變更是否清乾淨，見 test_known_defects.py
        # 的 test_failed_modify_leaves_no_partial_mutation（額外發現，xfail）。

    def test_unknown_concept_produces_no_patch_and_no_audit(self, restored_engine):
        e = restored_engine
        before = len(tx_audit(e))
        inh = self._inh(e)
        tx = inh.process(
            ConceptVerification(assessments=[ConceptAssessment(
                concept_id="con_ghost", concept_name="不存在", status="modify",
                reason="x", suggested_update="y", relevance=0.9,
            )]),
            user_input="x", llm_response="y", session_id="s1", turn_id="t",
        )
        assert tx.patches == [] and tx.status == "committed"
        assert len(tx_audit(e)) == before

    def test_valid_only_verification_is_noop(self, restored_engine):
        e = restored_engine
        before = len(tx_audit(e))
        tx = self._inh(e).process(
            ConceptVerification(assessments=[_assessment(e, "小橘", "valid", "ok")]),
            user_input="x", llm_response="y", session_id="s1", turn_id="t",
        )
        assert tx.patches == []
        assert len(tx_audit(e)) == before


# ═══════════════════════════════════════════════════════════════════════════
# 路徑 2：Extractor（ConceptExtractor 背景提取 -> process_extractor_patch）
# ═══════════════════════════════════════════════════════════════════════════


def _near_vector(base: list[float], cosine: float, seed_text: str) -> list[float]:
    """產生與 base 餘弦相似度恰為 cosine 的向量。"""
    from tests.conftest import _deterministic_embedding

    b = np.array(base, dtype=np.float64)
    b /= np.linalg.norm(b)
    h = np.array(_deterministic_embedding(seed_text), dtype=np.float64)
    o = h - np.dot(h, b) * b
    o /= np.linalg.norm(o)
    return (cosine * b + np.sqrt(1.0 - cosine**2) * o).tolist()


@pytest.fixture
def extractor_env(restored_engine):
    from tests.conftest import _deterministic_embedding

    e = restored_engine
    inhibitor = ConceptInhibitor(semantic=e.semantic, llm_fn=make_fake_llm())
    vectors: dict[str, list[float]] = {}  # 文字特徵 -> 指定向量
    failing: set[str] = set()

    def embed_fn(text: str):
        for key in failing:
            if key in text:
                raise RuntimeError("embed 失敗")
        for key, vec in vectors.items():
            if key in text:
                return vec
        return _deterministic_embedding(text)

    created: list[ConceptExtractor] = []

    def build(items, inhibitor_=inhibitor):
        ex = ConceptExtractor(
            llm_fn=make_fake_llm(extract_items=items),
            semantic=e.semantic, graph=e.episodic.graph,
            embed_fn=embed_fn, inhibitor=inhibitor_,
        )
        created.append(ex)
        return ex

    yield SimpleEnv(engine=e, build=build, vectors=vectors, failing=failing)
    for ex in created:
        ex.shutdown()


class SimpleEnv:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class TestExtractorPath:
    def test_new_concepts_created_with_graph_edges_and_audit(self, extractor_env):
        env, e = extractor_env, extractor_env.engine
        before = len(tx_audit(e))
        ep = e.episodic.get("ep_base_009")
        ex = env.build([
            {"noun": "天氣", "desc": "今天的天氣很好"},
            {"noun": "出門", "desc": "適合出門散步"},
        ])

        ex.extract_in_background(ep, salience=0.6)
        assert ex.flush(timeout=10) == 1

        weather, outing = e.semantic.get_by_name("天氣"), e.semantic.get_by_name("出門")
        assert weather and outing
        assert weather.source_episode_ids == ["ep_base_009"]
        assert weather.embedding, "新概念應帶 embedding"

        # Relation：DERIVED_FROM(concept -> episode) + 相鄰概念 RELATES_TO
        g = e.episodic.graph
        assert (weather.id, "ep_base_009") in directed_edges(e, "DERIVED_FROM")
        assert (outing.id, "ep_base_009") in directed_edges(e, "DERIVED_FROM")
        assert frozenset((weather.id, outing.id)) in edge_pairs(e, "RELATES_TO")
        assert g.has_node(weather.id)

        # Audit：每個概念一筆 extractor tx
        rows = _new_rows(e, before)
        assert len(rows) == 2
        for row in rows:
            assert row["triggered_by"] == "extractor"
            assert row["session_id"] == "extractor-bg" and row["turn_id"] == "ep_base_009"
            assert row["status"] == "committed"
            patch = row["patches"][0]
            assert patch["action"] == "create" and patch["status"] == "applied"
            assert patch["evidence"]["evidence_kind"] == "extractor_new_concept"
        assert {r["patches"][0]["concept_name"] for r in rows} == {"天氣", "出門"}

    def test_synonym_merge_adds_source_only(self, extractor_env):
        env, e = extractor_env, extractor_env.engine
        before = len(tx_audit(e))
        coffee = e.semantic.get_by_name("手沖咖啡")
        env.vectors["咖啡豆"] = list(coffee.embedding)  # cosine = 1.0 -> >= 0.92
        count_before = e.semantic.count()
        old_sources = list(coffee.source_episode_ids)
        ex = env.build([{"noun": "咖啡豆", "desc": "同義詞"}])

        ex.extract_in_background(e.episodic.get("ep_base_002"))
        ex.flush(timeout=10)

        assert e.semantic.count() == count_before, "同義詞不得新增概念"
        coffee = e.semantic.get_by_name("手沖咖啡")
        assert coffee.source_episode_ids == old_sources + ["ep_base_002"]
        rows = _new_rows(e, before)
        assert len(rows) == 1
        patch = rows[0]["patches"][0]
        assert patch["action"] == "expand" and patch["status"] == "applied"
        assert patch["evidence"]["evidence_kind"] == "extractor_synonym_merge"
        assert patch["concept_id"] == coffee.id
        assert coffee.metadata["patch_history"][-1]["patch_id"] == patch["patch_id"]

    def test_polysemy_creates_new_concept_and_relates_to_edge(self, extractor_env):
        env, e = extractor_env, extractor_env.engine
        before = len(tx_audit(e))
        coffee = e.semantic.get_by_name("手沖咖啡")
        env.vectors["咖啡館"] = _near_vector(coffee.embedding, 0.88, "polysemy-seed")
        ex = env.build([{"noun": "咖啡館", "desc": "喝咖啡的地方"}])

        ex.extract_in_background(e.episodic.get("ep_base_002"))
        ex.flush(timeout=10)

        new = e.semantic.get_by_name("咖啡館")
        assert new is not None and new.id != coffee.id
        assert frozenset((new.id, coffee.id)) in edge_pairs(e, "RELATES_TO")
        rel = [r for r in e.episodic.graph.get_concept_relations(new.id, "RELATES_TO") if r[0] == coffee.id]
        assert rel and rel[0][2] == pytest.approx(0.88, abs=0.01)
        rows = _new_rows(e, before)
        assert rows[0]["patches"][0]["evidence"]["evidence_kind"] == "extractor_polysemy"
        assert rows[0]["patches"][0]["action"] == "create"

    def test_embed_failure_falls_back_without_graph_edges(self, extractor_env):
        env, e = extractor_env, extractor_env.engine
        before = len(tx_audit(e))
        env.failing.add("離線概念")
        ex = env.build([{"noun": "離線概念", "desc": "embed 失敗時的降級"}])

        ex.extract_in_background(e.episodic.get("ep_base_010"))
        ex.flush(timeout=10)

        c = e.semantic.get_by_name("離線概念")
        assert c is not None
        assert (c.id, "ep_base_010") not in directed_edges(e, "DERIVED_FROM"), "無 embedding 時不建 DERIVED_FROM"
        rows = _new_rows(e, before)
        assert rows[0]["patches"][0]["evidence"]["evidence_kind"] == "extractor_fallback"

    def test_malformed_llm_output_writes_nothing(self, extractor_env):
        e = extractor_env.engine
        before_audit, before_count = len(tx_audit(e)), e.semantic.count()
        ex = ConceptExtractor(
            llm_fn=lambda _p: "這不是 JSON",
            semantic=e.semantic, graph=e.episodic.graph,
            embed_fn=lambda t: [0.0], inhibitor=ConceptInhibitor(semantic=e.semantic),
        )
        try:
            ex.extract_in_background(e.episodic.get("ep_base_009"))
            ex.flush(timeout=10)
        finally:
            ex.shutdown()
        assert len(tx_audit(e)) == before_audit
        assert e.semantic.count() == before_count


# ═══════════════════════════════════════════════════════════════════════════
# 路徑 3：Dream（MemoryEngine.dream -> 蒸餾 -> DreamPatchCoordinator -> Inhibitor）
# ═══════════════════════════════════════════════════════════════════════════


class TestDreamPath:
    def _dream(self, engine, **fake_kwargs):
        engine._concept_inhibitor = ConceptInhibitor(
            semantic=engine.semantic, llm_fn=make_fake_llm(),
        )
        engine.set_llm_fn(make_fake_llm(**fake_kwargs))
        return engine.dream(triggered_by="manual")

    DISTILLED = [
        {"name": "家人關係", "description": "使用者重視家人與寵物", "concept_type": "abstract",
         "subject": "user", "source_indices": [0, 1]},
        {"name": "晝夜節律", "description": "使用者作息偏晚", "concept_type": "pattern",
         "subject": "user", "source_indices": [2]},
    ]

    def test_distillation_creates_concepts_with_provenance_and_audit(self, restored_engine):
        e = restored_engine
        before = len(tx_audit(e))
        before_dream_audit = len(dream_audit(e))

        report = self._dream(
            e, distill_concepts=self.DISTILLED,
            reflections=[{"index": 0, "reflection": "家人很重要", "importance": 0.9, "related_indices": [1]}],
        )

        assert report["error"] is None
        assert report["distillation"]["concepts_created"] == 2
        assert report["distillation"]["concepts_rejected"] == 0

        # Concept：存在、帶來源（候選依 salience 降序：0=ep_005, 1=ep_003, 2=ep_001）
        family = e.semantic.get_by_name("家人關係")
        assert family.source_episode_ids == ["ep_base_005", "ep_base_003"]
        assert family.subject == "user"
        assert family.concept_type == "abstract"
        assert e.semantic.get_by_name("晝夜節律").source_episode_ids == ["ep_base_001"]

        # Relation：DERIVED_FROM 溯源邊
        derived = directed_edges(e, "DERIVED_FROM")
        assert (family.id, "ep_base_005") in derived and (family.id, "ep_base_003") in derived

        # Audit：PatchTransaction（triggered_by=dream, evidence=distillation）
        # Dream 的證據審核 phase 另有自己的 audit（evidence_review），這裡只看蒸餾寫入
        rows = [
            r for r in _new_rows(e, before)
            if r["patches"][0]["evidence"]["evidence_kind"] != "evidence_review"
        ]
        assert len(rows) == 2
        for row in rows:
            assert row["triggered_by"] == "dream"
            assert row["status"] == "committed"
            assert row["patches"][0]["evidence"]["evidence_kind"] == "distillation"
            assert row["patches"][0]["status"] == "applied"
        # Dream 圖譜 audit（add_derived_from_edges 屬於 dream_audit_log）
        ops = [r["operation"] for r in dream_audit(e)[before_dream_audit:]]
        assert ops.count("add_derived_from_edges") == 2
        assert "batch_weight_update" in ops

    def test_dream_marks_distilled_episodes_and_leaves_low_salience(self, restored_engine):
        e = restored_engine
        self._dream(e, distill_concepts=self.DISTILLED)

        dreamed = {i for i, ep in e.episodic._episodes.items() if ep.is_dreamed}
        # 候選 = salience >= 0.5 的 8 筆；ep_009(0.30)/ep_010(0.45) 不在其中
        assert dreamed == {f"ep_base_00{i}" for i in range(1, 9)}
        assert not e.episodic.get("ep_base_009").is_dreamed
        assert not e.episodic.get("ep_base_010").is_dreamed

    def test_graph_persisted_after_dream(self, restored_engine):
        e = restored_engine
        self._dream(e, distill_concepts=self.DISTILLED)
        family_id = e.semantic.get_by_name("家人關係").id

        from echo_memory.engine import MemoryEngine
        reopened = MemoryEngine(storage_path=e.storage_path)
        assert (family_id, "ep_base_005") in directed_edges(reopened, "DERIVED_FROM")
        assert reopened.semantic.get_by_name("家人關係") is not None

    def test_no_llm_dream_changes_no_concepts(self, restored_engine):
        e = restored_engine
        names = sorted(c.name for c in e.semantic.list_concepts(limit=None))
        before = len(tx_audit(e))
        e._concept_inhibitor = ConceptInhibitor(semantic=e.semantic)
        report = e.dream(triggered_by="manual")  # llm_fn=None
        assert report["error"] is None
        assert sorted(c.name for c in e.semantic.list_concepts(limit=None)) == names
        assert len(tx_audit(e)) == before
        assert report["distillation"]["skipped_reason"] == "no_llm"

    def test_garbage_distillation_output_creates_nothing(self, restored_engine):
        e = restored_engine
        before_count = e.semantic.count()
        e._concept_inhibitor = ConceptInhibitor(semantic=e.semantic)
        e.set_llm_fn(lambda _p: "not json at all")
        report = e.dream(triggered_by="manual")
        assert report["error"] is None
        assert e.semantic.count() == before_count
        # 蒸餾無產出時不得標記 is_dreamed（允許下輪重試）
        assert not any(ep.is_dreamed for ep in e.episodic._episodes.values())


# ═══════════════════════════════════════════════════════════════════════════
# 路徑 4：Reflection（ReflectionModule -> ActionQueue -> ReflectionExecutor）
# ═══════════════════════════════════════════════════════════════════════════


class TestReflectionPath:
    @pytest.fixture
    def env(self, restored_engine, tmp_path):
        from echo_memory.affect.action_queue import ReflectionActionQueue
        from echo_memory.affect.reflection import ReflectionModule
        from echo_memory.affect.reflection_executor import ReflectionExecutor

        e = restored_engine
        inh = ConceptInhibitor(semantic=e.semantic, llm_fn=make_fake_llm())
        executor = ReflectionExecutor(engine=e, concept_inhibitor=inh)
        queue = ReflectionActionQueue(audit_path=tmp_path / "reflection_audit.jsonl")
        return SimpleEnv(
            engine=e, inhibitor=inh, executor=executor, queue=queue,
            module_for=lambda ops: ReflectionModule(llm_fn=make_fake_llm(reflect={
                "action": "optimize", "content": "整理概念", "reasoning": "有重複",
                "memory_ops": ops,
            })),
            audit_path=tmp_path / "reflection_audit.jsonl",
        )

    def _run(self, env, ops):
        """走完整路徑：反思輸出 -> parse -> enqueue -> AUTO 自動執行 / CONFIRM 確認後執行。"""
        from echo_memory.affect.reflection_executor import parse_reflection_to_actions

        output = env.module_for(ops).reflect(["最近聊到寵物與作息"], known_concepts=["小橘"])
        actions = parse_reflection_to_actions(output)
        executor_fn = env.executor.build_executor_fn()
        queued = []
        for a in actions:
            act = env.queue.enqueue(**a)
            act.pre_state = env.executor.capture_pre_state(a["action_type"], a["target_id"], a["params"])
            env.queue.persist_action(act)
            queued.append(act)
        env.queue.process_auto_actions(executor_fn)
        for act in queued:
            if act.permission.value == "confirm":
                assert env.queue.confirm(act.id)
                env.queue.execute(act, executor_fn)
        return queued

    def test_tag_create_merge_full_path(self, env):
        e = env.engine
        before = len(tx_audit(e))
        queued = self._run(env, [
            {"op": "tag_concept", "target": "小橘", "params": {"tags": ["寵物"]}, "reason": "分類"},
            {"op": "create_concept", "target": "睡眠品質", "params": {"description": "使用者的睡眠品質"}, "reason": "新主題"},
            {"op": "merge_concepts", "target": "手沖咖啡", "params": {"merge_with": "夜貓作息"}, "reason": "相近"},
        ])

        assert [a.status.value for a in queued] == ["executed"] * 3

        # Concept 狀態
        assert e.semantic.get_by_name("小橘").metadata["tags"] == ["寵物"]
        created = e.semantic.get_by_name("睡眠品質")
        assert created and created.concept_type == "reflection"
        primary = e.semantic.get_by_name("手沖咖啡")
        assert "（合併自「夜貓作息」）使用者習慣凌晨才睡" in primary.description
        assert primary.metadata["merged_from"] == "夜貓作息"
        assert {"ep_base_001", "ep_base_008"} <= set(primary.metadata["merged_source_episodes"])

        # PatchTransaction / Audit
        rows = _new_rows(e, before)
        assert [r["triggered_by"] for r in rows] == ["reflection"] * 3
        assert all(r["status"] == "committed" for r in rows)
        kinds = [[p["evidence"]["evidence_kind"] for p in r["patches"]] for r in rows]
        assert kinds == [
            ["reflection_tag"], ["reflection_create"], ["reflection_merge", "reflection_merge"],
        ]
        merge_tx = rows[2]
        assert [p["action"] for p in merge_tx["patches"]] == ["modify", "merge"]
        assert [p["status"] for p in merge_tx["patches"]] == ["applied", "applied"]
        assert merge_tx["patches"][1]["rollback_payload"]["secondary_name"] == "夜貓作息"
        assert primary.metadata["patch_history"][-1]["patch_id"] == merge_tx["patches"][0]["patch_id"]

        # ActionQueue audit：每個行動至少 pending + executed 兩筆
        lines = read_jsonl(env.audit_path)
        for act in queued:
            statuses = [r["status"] for r in lines if r["id"] == act.id]
            assert statuses[0] == "pending" and statuses[-1] == "executed"

    def test_tag_rollback_restores_previous_tags(self, env):
        e = env.engine
        queued = self._run(env, [
            {"op": "tag_concept", "target": "小橘", "params": {"tags": ["寵物"]}, "reason": "x"},
        ])
        assert e.semantic.get_by_name("小橘").metadata["tags"] == ["寵物"]

        assert env.queue.rollback(queued[0].id, env.executor.build_rollback_fn())
        assert e.semantic.get_by_name("小橘").metadata["tags"] == []

    def test_create_concept_refuses_overwrite_of_existing_name(self, env):
        e = env.engine
        before = len(tx_audit(e))
        desc_before = e.semantic.get_by_name("小橘").description
        queued = self._run(env, [
            {"op": "create_concept", "target": "小橘", "params": {"description": "覆寫"}, "reason": "x"},
        ])
        assert queued[0].status.value == "failed"
        assert "已存在" in queued[0].error
        assert e.semantic.get_by_name("小橘").description == desc_before
        assert len(tx_audit(e)) == before, "被拒絕的建立不應進 patch pipeline"

    def test_unknown_and_denied_ops_never_reach_semantic(self, env):
        e = env.engine
        names = sorted(c.name for c in e.semantic.list_concepts(limit=None))
        before = len(tx_audit(e))
        queued = self._run(env, [
            {"op": "delete_concept", "target": "小橘", "params": {}, "reason": "x"},
            {"op": "totally_unknown", "target": "小橘", "params": {}, "reason": "x"},
        ])
        assert queued == [], "不在白名單的操作在 parse 階段即被丟棄"
        assert sorted(c.name for c in e.semantic.list_concepts(limit=None)) == names
        assert len(tx_audit(e)) == before

    def test_confirm_action_does_not_execute_without_confirmation(self, env):
        from echo_memory.affect.reflection_executor import parse_reflection_to_actions

        e = env.engine
        output = env.module_for([
            {"op": "create_concept", "target": "未確認概念", "params": {"description": "d"}, "reason": "x"},
        ]).reflect(["x"])
        act = env.queue.enqueue(**parse_reflection_to_actions(output)[0])
        assert env.queue.process_auto_actions(env.executor.build_executor_fn()) == []
        assert env.queue.execute(act, env.executor.build_executor_fn()) is False
        assert e.semantic.get_by_name("未確認概念") is None
