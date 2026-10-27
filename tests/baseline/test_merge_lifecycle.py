"""M3 審查修正（一）：合併生命週期、回滾完整性、held 並行。

審查者的失敗情境逐一落成回歸測試：
  - merge 後同名 create（別名導向主概念）
  - merge 後主概念被 expand，再 undo 必須拒絕（版本不符）
  - undo 中途失敗（index 更新／存檔／restore_merged）不留半套
  - 對已合併概念再合併
  - mark_merged 拋例外的補償、圖譜快照可 JSON 序列化
  - 兩個 inhibitor 實例共用 held 檔、並行 review 同一筆 expand 不重複套用
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager

import pytest

from echo_memory.affect.concept_inhibitor import ConceptInhibitor
from echo_memory.affect.concept_patch import (
    ConceptPatch,
    ExpandMode,
    PatchEvidence,
    PatchTransaction,
)
from echo_memory.affect.reflection_executor import ReflectionExecutor
from echo_memory.storage.semantic import SemanticMemory
from tests.baseline._support import tx_audit

PRIMARY = "手沖咖啡"
SECONDARY = "夜貓作息"


def _merge(engine, inh=None):
    inh = inh or ConceptInhibitor(semantic=engine.semantic, llm_fn=None)
    ok, result = inh.process_reflection_patch(
        "merge", PRIMARY, {"merge_with": SECONDARY}, "test",
    )
    assert ok, result
    return inh


def _executor(engine, inh=None):
    return ReflectionExecutor(
        engine=engine,
        concept_inhibitor=inh or ConceptInhibitor(semantic=engine.semantic, llm_fn=None),
    )


def _reload(engine) -> SemanticMemory:
    """從磁碟重新載入，用來比對記憶體與磁碟是否分歧。"""
    return SemanticMemory(engine.semantic.storage_path, embed_fn=engine.semantic._embed_fn)


def _disk_state(engine):
    fresh = _reload(engine)
    p, s = fresh.get_by_name(PRIMARY), fresh.get_by_name(SECONDARY)
    return {
        "p_desc": p.description, "p_version": p.version,
        "s_status": s.status, "s_merged_into": s.metadata.get("merged_into"),
    }


def _mem_state(engine):
    p, s = engine.semantic.get_by_name(PRIMARY), engine.semantic.get_by_name(SECONDARY)
    return {
        "p_desc": p.description, "p_version": p.version,
        "s_status": s.status, "s_merged_into": s.metadata.get("merged_into"),
    }


# ═══════════════════════════════════════════════════════════════════════════
# #1 merge 後同名 create
# ═══════════════════════════════════════════════════════════════════════════


class TestCreateAfterMerge:
    def test_is_merged_helper(self, restored_engine):
        e = restored_engine
        s = e.semantic.get_by_name(SECONDARY)
        assert not SemanticMemory.is_merged(s)
        _merge(e)
        assert SemanticMemory.is_merged(e.semantic.get_by_name(SECONDARY))
        assert not SemanticMemory.is_merged(e.semantic.get_by_name(PRIMARY))
        assert e.semantic.list_concepts(status="active", limit=None)

    def test_same_name_create_redirects_to_primary(self, restored_engine):
        e = restored_engine
        inh = _merge(e)
        primary = e.semantic.get_by_name(PRIMARY)
        secondary_desc = e.semantic.get_by_name(SECONDARY).description

        cid, tx = inh.process_extractor_patch(
            noun=SECONDARY, desc="夜貓作息的新知識", episode_id="ep-redirect",
            evidence_kind="extractor_new_concept",
        )

        assert cid == primary.id
        patch = tx.patches[0]
        assert patch.status == "applied"
        # 新知識落在主概念：fact + source + derivation
        assert any(f.get("text") == "夜貓作息的新知識" for f in primary.facts)
        assert "ep-redirect" in primary.source_episode_ids
        assert any(d.get("id") == "ep-redirect" for d in primary.derivations)
        # 已合併的次概念不被覆寫，仍是非 active
        s = e.semantic.get_by_name(SECONDARY)
        assert s.status != "active" and s.metadata.get("merged_into") == primary.id
        assert s.description == secondary_desc
        # audit 記錄導向原因
        row = [t for t in tx_audit(e) if t["transaction_id"] == tx.transaction_id][-1]
        assert "改以 expand 補入主概念" in row["patches"][0]["rollback_payload"]["redirect_reason"]
        # 磁碟一致
        assert any(
            f.get("text") == "夜貓作息的新知識"
            for f in _reload(e).get_by_name(PRIMARY).facts
        )

    def test_redirect_is_rollbackable_without_removing_secondary(self, restored_engine):
        e = restored_engine
        inh = _merge(e)
        primary = e.semantic.get_by_name(PRIMARY)
        facts_before = list(primary.facts)
        sources_before = list(primary.source_episode_ids)
        _, tx = inh.process_extractor_patch(
            noun=SECONDARY, desc="另一則", episode_id="ep-rb",
            evidence_kind="extractor_new_concept",
        )
        inh._rollback_patch(tx.patches[0])
        assert e.semantic.get_by_name(SECONDARY) is not None  # 沒被當成 created 概念移除
        assert primary.facts == facts_before
        assert primary.source_episode_ids == sources_before

    def test_redirect_rejected_when_primary_not_active(self, restored_engine):
        e = restored_engine
        inh = _merge(e)
        e.semantic.get_by_name(PRIMARY).status = "deprecated"
        cid, tx = inh.process_extractor_patch(
            noun=SECONDARY, desc="x", episode_id="ep-rej",
            evidence_kind="extractor_new_concept",
        )
        assert cid is None and tx.status == "failed"
        assert tx.patches[0].status == "rejected"
        assert "主概念不存在或非 active" in tx.patches[0].rejection_reason
        row = [t for t in tx_audit(e) if t["transaction_id"] == tx.transaction_id][-1]
        assert row["patches"][0]["status"] == "rejected"
        assert e.semantic.get_by_name(SECONDARY).status != "active"

    def test_redirect_rejected_when_primary_missing(self, restored_engine):
        e = restored_engine
        inh = _merge(e)
        e.semantic.get_by_name(SECONDARY).metadata["merged_into"] = "no-such-id"
        cid, tx = inh.process_extractor_patch(
            noun=SECONDARY, desc="x", episode_id="ep-miss",
            evidence_kind="extractor_new_concept",
        )
        assert cid is None and tx.patches[0].status == "rejected"

    def test_reflection_create_treats_merged_as_placeholder(self, restored_engine):
        e = restored_engine
        inh = _merge(e)
        ok, result = inh.process_reflection_patch(
            "create", SECONDARY, {"description": "重建"}, "t",
        )
        assert not ok and "不重建同名概念" in result["error"]
        assert e.semantic.get_by_name(SECONDARY).description != "重建"
        with pytest.raises(ValueError, match="已存在"):
            _executor(e, inh).build_executor_fn()(
                "create_concept", SECONDARY, {"description": "重建"},
            )

    def test_invalid_subject_validated_before_upsert(self, restored_engine):
        e = restored_engine
        inh = ConceptInhibitor(semantic=e.semantic, llm_fn=None)
        cid, tx = inh.process_extractor_patch(
            noun="全新概念X", desc="d", episode_id="ep-s",
            evidence_kind="extractor_new_concept", subject="alien",
        )
        assert cid is None and tx.status == "failed"
        assert e.semantic.get_by_name("全新概念X") is None  # 沒留下半成品

    def test_set_subject_bumps_version_on_change(self, restored_engine):
        e = restored_engine
        c = e.semantic.get_by_name(PRIMARY)
        target = "agent" if c.subject != "agent" else "user"
        v = c.version
        assert e.semantic.set_subject(c.id, target)
        assert c.version == v + 1
        assert e.semantic.set_subject(c.id, target)  # 未變更不升版
        assert c.version == v + 1


# ═══════════════════════════════════════════════════════════════════════════
# #2 / #3 undo_merge
# ═══════════════════════════════════════════════════════════════════════════


class TestUndoMerge:
    def test_undo_rejected_after_primary_expand(self, restored_engine):
        e = restored_engine
        inh = _merge(e)
        primary = e.semantic.get_by_name(PRIMARY)
        snap = e.semantic.get_by_name(SECONDARY).metadata["merge_primary_snapshot"]
        assert snap["merged_primary_version"] == primary.version

        # 合併後主概念 expand（description 不變、version 變）
        _, tx = inh.process_extractor_patch(
            noun=PRIMARY, desc="", episode_id="ep-after", evidence_kind="extractor_synonym_merge",
            existing_concept_id=primary.id,
        )
        assert tx.patches[0].status == "applied"
        assert primary.version != snap["merged_primary_version"]

        ex = _executor(e, inh)
        with pytest.raises(RuntimeError, match="已被修改"):
            ex._gated_undo("undo_merge", PRIMARY, {"merge_with": SECONDARY})
        assert e.semantic.get_by_name(SECONDARY).metadata.get("merged_into") == primary.id

    def test_undo_succeeds_when_untouched(self, restored_engine):
        e = restored_engine
        before = e.semantic.get_by_name(PRIMARY).description
        inh = _merge(e)
        v_merged = e.semantic.get_by_name(PRIMARY).version
        _executor(e, inh)._gated_undo("undo_merge", PRIMARY, {"merge_with": SECONDARY})
        assert e.semantic.get_by_name(PRIMARY).description == before
        assert e.semantic.get_by_name(PRIMARY).version > v_merged  # 單調
        assert e.semantic.get_by_name(SECONDARY).status == "active"
        assert _mem_state(e) == _disk_state(e)

    @pytest.mark.parametrize("failure", ["index", "save", "restore_merged", "restore_raises"])
    def test_undo_failure_leaves_no_partial_state(self, restored_engine, monkeypatch, failure):
        e = restored_engine
        inh = _merge(e)
        ex = _executor(e, inh)
        primary = e.semantic.get_by_name(PRIMARY)
        derivations_before = json.dumps(primary.derivations, sort_keys=True, default=str)
        embedding_before = list(primary.embedding)
        before_mem, before_disk = _mem_state(e), _disk_state(e)

        sem = e.semantic
        state = {"n": 0}
        if failure == "index":
            orig = sem._update_embedding_index

            def boom(*a, **k):
                state["n"] += 1
                if state["n"] == 1:
                    raise RuntimeError("index 更新失敗")
                return orig(*a, **k)
            monkeypatch.setattr(sem, "_update_embedding_index", boom)
        elif failure == "save":
            orig = sem._save

            def boom_save():
                state["n"] += 1
                if state["n"] == 1:
                    raise OSError("存檔失敗")
                return orig()
            monkeypatch.setattr(sem, "_save", boom_save)
        elif failure == "restore_merged":
            monkeypatch.setattr(sem, "restore_merged", lambda name: False)
        else:
            def raiser(name):
                raise OSError("restore 拋例外")
            monkeypatch.setattr(sem, "restore_merged", raiser)

        with pytest.raises(RuntimeError, match="回滾 undo_merge 失敗"):
            ex._gated_undo("undo_merge", PRIMARY, {"merge_with": SECONDARY})

        monkeypatch.undo()
        assert _mem_state(e) == before_mem
        assert _disk_state(e) == before_disk
        assert primary.embedding == embedding_before
        assert json.dumps(primary.derivations, sort_keys=True, default=str) == derivations_before
        assert "merge_primary_snapshot" in sem.get_by_name(SECONDARY).metadata
        # 補償後版本回到原值，之後重試仍可成功
        ex._gated_undo("undo_merge", PRIMARY, {"merge_with": SECONDARY})
        assert sem.get_by_name(SECONDARY).status == "active"
        assert _mem_state(e) == _disk_state(e)


# ═══════════════════════════════════════════════════════════════════════════
# #5 merge 前置條件與補償
# ═══════════════════════════════════════════════════════════════════════════


class TestMergePreconditions:
    def test_merge_rejects_already_merged_secondary(self, restored_engine):
        e = restored_engine
        inh = _merge(e)
        others = [c.name for c in e.semantic.list_concepts(status="active", limit=None)
                  if c.name not in (PRIMARY, SECONDARY)]
        assert others
        ok, result = inh.process_reflection_patch(
            "merge", others[0], {"merge_with": SECONDARY}, "t",
        )
        assert not ok and "已被合併" in result["error"]

    def test_merge_rejects_merged_or_inactive_primary(self, restored_engine):
        e = restored_engine
        inh = _merge(e)
        others = [c.name for c in e.semantic.list_concepts(status="active", limit=None)
                  if c.name not in (PRIMARY, SECONDARY)]
        ok, result = inh.process_reflection_patch(
            "merge", SECONDARY, {"merge_with": others[0]}, "t",
        )
        assert not ok and "已被合併" in result["error"]
        e.semantic.get_by_name(others[0]).status = "deprecated"
        ok, _ = inh.process_reflection_patch(
            "merge", others[0], {"merge_with": PRIMARY}, "t",
        )
        assert not ok

    def test_mark_merged_exception_compensates_primary(self, restored_engine, monkeypatch):
        e = restored_engine
        inh = ConceptInhibitor(semantic=e.semantic, llm_fn=None)
        primary, secondary = e.semantic.get_by_name(PRIMARY), e.semantic.get_by_name(SECONDARY)
        desc, meta = primary.description, json.dumps(primary.metadata, sort_keys=True, default=str)
        derivations = json.dumps(primary.derivations, sort_keys=True, default=str)

        def boom(*a, **k):
            raise OSError("mark_merged 失敗")
        monkeypatch.setattr(e.semantic, "mark_merged", boom)
        ok, result = inh.process_reflection_patch(
            "merge", PRIMARY, {"merge_with": SECONDARY}, "t",
        )
        assert not ok
        assert primary.description == desc
        assert json.dumps(primary.metadata, sort_keys=True, default=str) == meta
        assert json.dumps(primary.derivations, sort_keys=True, default=str) == derivations
        assert "merge_primary_snapshot" not in secondary.metadata
        assert secondary.status == "active"
        fresh = _reload(e)
        assert fresh.get_by_name(PRIMARY).description == desc
        assert "merge_primary_snapshot" not in fresh.get_by_name(SECONDARY).metadata


# ═══════════════════════════════════════════════════════════════════════════
# #7 存檔順序與圖譜快照
# ═══════════════════════════════════════════════════════════════════════════


class TestPersistenceOrder:
    def test_graph_saved_only_after_concepts_saved(self, restored_engine, monkeypatch):
        e = restored_engine
        sem = e.semantic
        events: list[str] = []
        orig_save, graph = sem._save, sem._graph
        orig_gsave = graph.save
        monkeypatch.setattr(sem, "_save", lambda: (events.append("concepts"), orig_save())[1])
        monkeypatch.setattr(graph, "save", lambda: (events.append("graph"), orig_gsave())[1])
        inh = ConceptInhibitor(semantic=sem, llm_fn=None)
        inh.process_reflection_patch("merge", PRIMARY, {"merge_with": SECONDARY}, "t")
        merged_events = events[:]
        assert "graph" in merged_events
        assert merged_events.index("graph") > max(
            i for i, ev in enumerate(merged_events[:merged_events.index("graph")]) if ev == "concepts"
        )
        events.clear()
        sem.restore_merged(SECONDARY)
        assert events.index("graph") > events.index("concepts")

    def test_concept_save_failure_keeps_graph_node(self, restored_engine, monkeypatch):
        e = restored_engine
        sem, graph = e.semantic, e.semantic._graph
        sec = sem.get_by_name(SECONDARY)
        assert graph.has_node(sec.id)
        monkeypatch.setattr(sem, "_save", lambda: (_ for _ in ()).throw(OSError("存檔失敗")))
        with pytest.raises(OSError):
            sem.mark_merged(SECONDARY, "any", "p")
        assert graph.has_node(sec.id)  # 概念檔沒存成功，圖譜不動
        assert sec.status == "active" and "merged_into" not in sec.metadata

    def test_graph_snapshot_with_unserializable_attrs_is_json_safe(self, restored_engine):
        import numpy as np

        e = restored_engine
        sem, graph = e.semantic, e.semantic._graph
        sec = sem.get_by_name(SECONDARY)
        other = sem.get_by_name(PRIMARY).id
        graph.graph.add_edge(sec.id, other, weight=np.float32(0.25), tags={"a", "b"}, obj=object())
        sem.mark_merged(SECONDARY, other, "p")
        snap = sem.get_by_name(SECONDARY).metadata["merge_graph_snapshot"]
        json.dumps(snap)  # 不可拋例外
        assert _reload(e).get_by_name(SECONDARY).metadata["merge_graph_snapshot"]
        assert sem.restore_merged(SECONDARY)
        assert graph.has_node(sec.id)


# ═══════════════════════════════════════════════════════════════════════════
# Minor：rollback 的 version 與 history
# ═══════════════════════════════════════════════════════════════════════════


class TestRollbackMinor:
    def test_rollback_bumps_version_and_pops_only_own_history(self, restored_engine):
        e = restored_engine
        inh = ConceptInhibitor(semantic=e.semantic, llm_fn=None)
        coffee = e.semantic.get_by_name(PRIMARY)
        tx = PatchTransaction(patches=[], user_input="", llm_response="", session_id="s", turn_id="t")
        patch = _expand_patch(coffee, tx, {"text": "rb-fact"})
        patch.status = "approved"
        assert inh._apply_patch(patch)
        v_applied = coffee.version
        inh._rollback_patch(patch)
        assert coffee.version > v_applied
        assert {"text": "rb-fact"} not in coffee.facts

    def test_noop_source_expand_rollback_keeps_foreign_history(self, restored_engine):
        e = restored_engine
        inh = ConceptInhibitor(semantic=e.semantic, llm_fn=None)
        coffee = e.semantic.get_by_name(PRIMARY)
        coffee.metadata["patch_history"] = [{"patch_id": "foreign", "action": "modify"}]
        ep = coffee.source_episode_ids[0]  # 已存在 -> apply 是 no-op，不寫 history
        patch = ConceptPatch(
            transaction_id="tx", concept_id=coffee.id, concept_name=coffee.name,
            action="expand", expand_mode=ExpandMode.ADD_FACT, target_path="source_episode_ids",
            old_value=list(coffee.source_episode_ids), new_value=ep,
            patch_confidence=0.8, risk_level="low",
            evidence=PatchEvidence(
                evidence_kind="extractor_synonym_merge", evidence_strength=0.7,
                verifier_status="valid", verifier_reason="t",
            ),
            rollback_payload={"old_source_episode_ids": list(coffee.source_episode_ids)},
        )
        assert inh._apply_expand_source(patch)
        inh._rollback_patch(patch)
        assert coffee.metadata["patch_history"] == [{"patch_id": "foreign", "action": "modify"}]


def _expand_patch(concept, tx, fact, strength=0.5) -> ConceptPatch:
    return ConceptPatch(
        transaction_id=tx.transaction_id, concept_id=concept.id, concept_name=concept.name,
        action="expand", expand_mode=ExpandMode.ADD_FACT, target_path="metadata.facts",
        old_value=list(concept.facts), new_value=fact,
        old_confidence=concept.confidence, new_confidence=concept.confidence,
        patch_confidence=strength, risk_level="low",
        evidence=PatchEvidence(
            evidence_kind="verifier_expand", evidence_strength=strength,
            verifier_status="valid", verifier_reason="t",
        ),
        rollback_payload={"old_facts": list(concept.facts)},
    )


# ═══════════════════════════════════════════════════════════════════════════
# #6 held 並行
# ═══════════════════════════════════════════════════════════════════════════

_APPROVE = json.dumps({"approve": True, "confidence": 0.9, "reason": "ok"})


def _register_expand(inh, concept, fact):
    tx = PatchTransaction(patches=[], user_input="u", llm_response="r", session_id="s", turn_id="t")
    patch = _expand_patch(concept, tx, fact)
    patch.status = "held"
    inh._register_held(patch, tx, "u", "r")
    return patch


class TestHeldConcurrency:
    def test_second_instance_does_not_reapply_after_first_finalized(self, restored_engine):
        e = restored_engine
        a = ConceptInhibitor(semantic=e.semantic, llm_fn=lambda _p: _APPROVE)
        b = ConceptInhibitor(semantic=e.semantic, llm_fn=lambda _p: _APPROVE)
        coffee = e.semantic.get_by_name(PRIMARY)
        fact = {"text": "held-fact", "source": "t"}
        _register_expand(a, coffee, fact)
        assert b.held_count() == 1  # 共用同一份檔

        a.review_held_patches()
        b.review_held_patches()
        assert coffee.facts.count(fact) == 1
        assert a.held_count() == b.held_count() == 0

    def test_in_flight_entry_not_reviewed_by_other_instance(self, restored_engine):
        e = restored_engine
        a = ConceptInhibitor(semantic=e.semantic, llm_fn=lambda _p: _APPROVE)
        b = ConceptInhibitor(semantic=e.semantic, llm_fn=lambda _p: _APPROVE)
        coffee = e.semantic.get_by_name(PRIMARY)
        fact = {"text": "inflight-fact", "source": "t"}
        _register_expand(a, coffee, fact)

        entered, release = threading.Event(), threading.Event()

        @contextmanager
        def blocking_guard():
            entered.set()
            assert release.wait(10)
            yield

        result_a: list = []
        t = threading.Thread(
            target=lambda: result_a.append(a.review_held_patches(guard=blocking_guard)),
        )
        t.start()
        assert entered.wait(10)
        # A 已認領並卡在 apply 前；B 此時 review 必須略過這筆
        assert b.review_held_patches() == []
        release.set()
        t.join(10)
        assert len(result_a[0]) == 1
        assert coffee.facts.count(fact) == 1
        assert b.held_count() == 0

    def test_parallel_reviews_apply_once(self, restored_engine):
        e = restored_engine
        insts = [ConceptInhibitor(semantic=e.semantic, llm_fn=lambda _p: _APPROVE) for _ in range(4)]
        coffee = e.semantic.get_by_name(PRIMARY)
        fact = {"text": "parallel-fact", "source": "t"}
        _register_expand(insts[0], coffee, fact)
        barrier = threading.Barrier(len(insts))

        def run(inh):
            barrier.wait()
            inh.review_held_patches()

        threads = [threading.Thread(target=run, args=(i,)) for i in insts]
        [t.start() for t in threads]
        [t.join(15) for t in threads]
        assert coffee.facts.count(fact) == 1

    def test_expand_already_present_is_stale(self, restored_engine):
        e = restored_engine
        inh = ConceptInhibitor(semantic=e.semantic, llm_fn=lambda _p: _APPROVE)
        coffee = e.semantic.get_by_name(PRIMARY)
        fact = {"text": "dup-fact", "source": "t"}
        coffee.metadata.setdefault("facts", []).append(fact)  # 他處已套用
        pid = _register_expand(inh, coffee, fact).patch_id
        inh.review_held_patches()
        assert coffee.facts.count(fact) == 1
        last = [p for t in tx_audit(e) for p in t["patches"] if p["patch_id"] == pid][-1]
        assert last["status"] == "rejected" and "前提失效" in last["rejection_reason"]

    def test_still_held_entry_is_put_back_unclaimed(self, restored_engine):
        e = restored_engine

        def fail(_p):
            raise RuntimeError("LLM down")
        inh = ConceptInhibitor(semantic=e.semantic, llm_fn=fail, held_max_retries=5)
        coffee = e.semantic.get_by_name(PRIMARY)
        _register_expand(inh, coffee, {"text": "later", "source": "t"})
        inh.review_held_patches()
        assert inh.held_count() == 1
        entry = next(iter(inh._held_registry().values()))
        assert "claimed_at" not in entry and entry["retries"] == 1

    def test_corrupt_held_file_is_backed_up_and_treated_empty(self, restored_engine):
        e = restored_engine
        inh = ConceptInhibitor(semantic=e.semantic, llm_fn=None)
        path = inh._held_path()
        path.write_text("{ not json", encoding="utf-8")
        assert inh.held_count() == 0
        backups = list(path.parent.glob("held_patches.corrupt-*.json"))
        assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "{ not json"
        # 之後可正常寫入，且沒有殘留暫存檔
        _register_expand(inh, e.semantic.get_by_name(PRIMARY), {"text": "ok", "source": "t"})
        assert json.loads(path.read_text(encoding="utf-8"))
        assert not list(path.parent.glob("held_patches*.tmp*"))


# ═══════════════════════════════════════════════════════════════════════════
# held 登記簿：暫時性 I/O 與 storage_lock
# ═══════════════════════════════════════════════════════════════════════════


class TestHeldIoAndLock:
    def test_transient_permission_error_does_not_rename_or_clear(self, restored_engine, monkeypatch):
        from pathlib import Path

        e = restored_engine
        inh = ConceptInhibitor(semantic=e.semantic, llm_fn=None)
        coffee = e.semantic.get_by_name(PRIMARY)
        _register_expand(inh, coffee, {"text": "keep", "source": "t"})
        path = inh._held_path()
        before = path.read_text(encoding="utf-8")

        orig = Path.read_text
        state = {"n": 0}

        def flaky(self, *a, **k):
            if self == path and state["n"] < 2:
                state["n"] += 1
                raise PermissionError("暫時被佔用")
            return orig(self, *a, **k)
        monkeypatch.setattr(Path, "read_text", flaky)
        assert inh.held_count() == 1  # 短暫重試後讀到
        assert not list(path.parent.glob("held_patches.corrupt-*.json"))

        # 持續失敗：略過本次，不改名、不覆寫
        def always(self, *a, **k):
            if self == path:
                raise PermissionError("持續被佔用")
            return orig(self, *a, **k)
        monkeypatch.setattr(Path, "read_text", always)
        monkeypatch.setattr("echo_memory.affect.concept_inhibitor.time.sleep", lambda *_: None)
        assert inh.review_held_patches() == []
        _register_expand(inh, coffee, {"text": "dropped", "source": "t"})  # 略過，不寫回
        monkeypatch.undo()
        assert path.read_text(encoding="utf-8") == before
        assert not list(path.parent.glob("held_patches.corrupt-*.json"))

    def test_held_critical_sections_take_storage_lock(self, restored_engine, monkeypatch):
        import echo_memory.affect.concept_inhibitor as ci

        e = restored_engine
        taken: list[str] = []
        real = ci.storage_lock

        @contextmanager
        def spy(path, *a, **k):
            taken.append(str(path))
            with real(path, *a, **k):
                yield
        monkeypatch.setattr(ci, "storage_lock", spy)
        inh = ConceptInhibitor(semantic=e.semantic, llm_fn=lambda _p: _APPROVE)
        coffee = e.semantic.get_by_name(PRIMARY)
        _register_expand(inh, coffee, {"text": "lk", "source": "t"})
        assert taken, "登記未取 storage_lock"
        taken.clear()
        inh.review_held_patches()
        assert len(taken) >= 3  # 認領 + 放回 + finalize
        # 與 engine 同一把：namespace 根目錄，而非 semantic 子目錄
        assert all(t == str(e.storage_path) for t in taken)
