"""
M-6c：concept 語意相近提示 + support_source 納入 patch trail。

重點：
  - 新概念 create 前以 embedding 找 active 相近概念，作為候選提示帶進 Gate 的 LLM prompt
  - LLM 回 expand 時改為 expand 既有概念；沒有 llm_fn 時只記 similar_candidates，不擋 create
  - 已合併／非 active 的概念不列入
  - embedding 計算不在 semantic／storage_lock 內
  - support_source 由 create／expand patch 攜帶：audit 可見、rollback 還原、只升不降

測試不載入真模型：embed_fn 是依名稱查表的確定性向量。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from echo_memory.affect.concept_inhibitor import ConceptInhibitor
from echo_memory.dream.distillation import KnowledgeDistillation
from echo_memory.dream.patch_coordinator import DreamPatchCoordinator
from echo_memory.schemas.episode import Episode
from echo_memory.storage import locking
from echo_memory.storage.graph import MemoryGraph
from echo_memory.storage.locking import storage_lock
from echo_memory.storage.semantic import SemanticMemory

_A = [1.0, 0.0, 0.0]
_SIMILAR = [0.95, 0.31, 0.0]   # 與 _A cosine 約 0.95
_OTHER = [0.0, 1.0, 0.0]       # 與 _A 正交


def _vec(name: str) -> list[float]:
    table = {
        "鋼琴": _A,
        "鋼琴演奏": _SIMILAR,
        "吉他": _OTHER,
    }
    return table.get(name.split(":")[0].strip(), _OTHER)


class _Llm:
    """假 llm_fn：記下 prompt，回傳預先給定的 JSON。"""

    def __init__(self, reply: object):
        self.prompts: list[str] = []
        self.reply = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.reply


@pytest.fixture
def semantic(tmp_path: Path) -> SemanticMemory:
    return SemanticMemory(storage_path=tmp_path / "semantic", embed_fn=_vec)


def _seed(semantic: SemanticMemory, name: str, desc: str = "既有概念", vec=None) -> str:
    return semantic.upsert_concept(
        name=name, description=desc, embedding=vec if vec is not None else _vec(name),
    )


def _audit_lines(semantic: SemanticMemory) -> list[dict]:
    path = semantic.storage_path / "audit_log.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _extract(inh: ConceptInhibitor, noun: str = "鋼琴演奏", **kw):
    return inh.process_extractor_patch(
        noun=noun,
        desc=kw.pop("desc", "彈鋼琴的活動"),
        episode_id=kw.pop("episode_id", "ep_new"),
        evidence_kind=kw.pop("evidence_kind", "extractor_polysemy"),
        **kw,
    )


# ─── 語意相近提示 ───────────────────────────────────────────────────────


class TestSimilarHint:
    def test_similar_concept_appears_in_gate_prompt(self, semantic):
        existing = _seed(semantic, "鋼琴", "一種鍵盤樂器")
        _seed(semantic, "吉他", "弦樂器")  # 正交，不得出現
        llm = _Llm({"decision": "create", "target_id": None, "reason": "不同概念"})
        inh = ConceptInhibitor(semantic=semantic, llm_fn=llm)

        cid, tx = _extract(inh)

        assert len(llm.prompts) == 1
        prompt = llm.prompts[0]
        assert f"[id={existing}] 鋼琴（cosine 0.9" in prompt
        assert "一種鍵盤樂器" in prompt
        assert "吉他" not in prompt
        # LLM 判定不同概念 → 照常 create，候選與判斷進 patch trail
        assert semantic.get(cid).name == "鋼琴演奏"
        new_value = tx.patches[0].new_value
        assert [c["concept_id"] for c in new_value["similar_candidates"]] == [existing]
        assert new_value["similar_decision"]["decision"] == "create"
        assert new_value["similar_decision"]["llm_used"] is True
        assert "similar_expand_target" not in new_value
        logged = _audit_lines(semantic)[-1]["patches"][0]["new_value"]
        assert logged["similar_candidates"][0]["concept_id"] == existing

    def test_llm_expand_redirects_to_existing_concept(self, semantic):
        existing = _seed(semantic, "鋼琴", "一種鍵盤樂器")
        llm = _Llm({"decision": "expand", "target_id": existing, "reason": "同一件事"})
        inh = ConceptInhibitor(semantic=semantic, llm_fn=llm)

        cid, tx = _extract(inh, desc="彈鋼琴的活動")

        assert cid == existing
        assert semantic.get_by_name("鋼琴演奏") is None
        primary = semantic.get(existing)
        assert any(
            f.get("source") == "similar_concept_expand" and f.get("text") == "彈鋼琴的活動"
            for f in primary.metadata["facts"]
        )
        assert "ep_new" in primary.source_episode_ids
        patch = tx.patches[0]
        assert patch.status == "applied"
        assert patch.concept_id == existing
        assert patch.rollback_payload["redirected_to"] == existing
        assert "同一件事" in patch.evidence.verifier_reason or "語意相近" in patch.evidence.verifier_reason

        # rollback 還原既有概念（不移除它）
        inh._rollback_patch(patch)
        restored = semantic.get(existing)
        assert restored is not None
        assert "ep_new" not in restored.source_episode_ids
        assert not restored.metadata.get("facts")

    def test_llm_expand_with_unknown_target_falls_back_to_create(self, semantic):
        _seed(semantic, "鋼琴")
        llm = _Llm({"decision": "expand", "target_id": "不存在的id", "reason": "亂猜"})
        inh = ConceptInhibitor(semantic=semantic, llm_fn=llm)

        cid, tx = _extract(inh)

        assert semantic.get(cid).name == "鋼琴演奏"
        assert tx.patches[0].new_value["similar_decision"]["decision"] == "create"

    def test_llm_failure_does_not_block_create(self, semantic):
        _seed(semantic, "鋼琴")

        def boom(_prompt: str) -> str:
            raise RuntimeError("llm down")

        inh = ConceptInhibitor(semantic=semantic, llm_fn=boom)
        cid, tx = _extract(inh)

        assert semantic.get(cid).name == "鋼琴演奏"
        assert tx.patches[0].new_value["similar_candidates"]

    def test_no_llm_records_candidates_only(self, semantic):
        existing = _seed(semantic, "鋼琴")
        inh = ConceptInhibitor(semantic=semantic, llm_fn=None)

        cid, tx = _extract(inh)

        # 不自動合併、不擋 create
        assert cid != existing
        assert semantic.get(cid).name == "鋼琴演奏"
        new_value = tx.patches[0].new_value
        assert new_value["similar_candidates"][0]["concept_id"] == existing
        assert new_value["similar_decision"]["llm_used"] is False
        assert "similar_expand_target" not in new_value
        assert tx.patches[0].status == "applied"

    def test_no_candidates_leaves_patch_untouched(self, semantic):
        _seed(semantic, "吉他")
        llm = _Llm({"decision": "create"})
        inh = ConceptInhibitor(semantic=semantic, llm_fn=llm)

        cid, tx = _extract(inh)

        assert cid is not None
        assert llm.prompts == []
        assert "similar_candidates" not in tx.patches[0].new_value

    def test_merged_and_inactive_concepts_excluded(self, semantic):
        primary = _seed(semantic, "吉他")          # 正交，會是別名的主概念
        _seed(semantic, "鋼琴")                    # 之後被合併
        semantic.mark_merged("鋼琴", primary)
        inh = ConceptInhibitor(semantic=semantic, llm_fn=None)
        hint = inh.evaluate_similar("鋼琴演奏", "彈鋼琴", embedding=_SIMILAR)
        assert hint.candidates == []

        # 非 active（deprecated）同樣排除
        other = _seed(semantic, "鋼琴2", vec=_A)
        semantic.get(other).status = "deprecated"
        hint = inh.evaluate_similar("鋼琴演奏", "彈鋼琴", embedding=_SIMILAR)
        assert hint.candidates == []

    def test_top_three_by_cosine(self, semantic):
        for i, x in enumerate([0.99, 0.97, 0.95, 0.92]):
            _seed(semantic, f"c{i}", vec=[x, (1 - x * x) ** 0.5, 0.0])
        inh = ConceptInhibitor(semantic=semantic, llm_fn=None)
        hint = inh.evaluate_similar("新", "新概念", embedding=_A)
        assert [c["name"] for c in hint.candidates] == ["c0", "c1", "c2"]

    def test_threshold_from_config(self, semantic):
        _seed(semantic, "鋼琴")
        inh = ConceptInhibitor(semantic=semantic, llm_fn=None)
        inh._similar_hint_threshold = 0.99
        assert inh.evaluate_similar("鋼琴演奏", "x", embedding=_SIMILAR).candidates == []

    def test_same_name_is_not_a_similar_candidate(self, semantic):
        _seed(semantic, "鋼琴")
        inh = ConceptInhibitor(semantic=semantic, llm_fn=None)
        assert inh.evaluate_similar("鋼琴", "x", embedding=_A).candidates == []

    def test_reflection_create_never_redirects_nor_asks_llm(self, semantic):
        existing = _seed(semantic, "鋼琴", "一種鍵盤樂器")
        llm = _Llm({"decision": "expand", "target_id": existing, "reason": "同一件事"})
        inh = ConceptInhibitor(semantic=semantic, llm_fn=llm)

        ok, result = inh.process_reflection_patch(
            "create", "鋼琴演奏", {"description": "彈鋼琴"}, "測試",
        )

        # Reflection 是明確提議新概念：維持 create，候選只記 audit，不問 LLM
        assert ok and result["concept_id"] != existing
        assert semantic.get_by_name("鋼琴演奏") is not None
        assert llm.prompts == []
        new_value = _audit_lines(semantic)[-1]["patches"][0]["new_value"]
        assert new_value["similar_candidates"][0]["concept_id"] == existing
        assert "similar_expand_target" not in new_value

    def test_executor_skips_precheck_when_name_exists(self, tmp_path):
        from echo_memory.affect.reflection_executor import ReflectionExecutor

        calls: list[str] = []
        semantic = SemanticMemory(tmp_path / "s", embed_fn=lambda t: calls.append(t) or _vec(t))
        _seed(semantic, "鋼琴")
        engine = MagicMock()
        engine.semantic = semantic
        engine.storage_path = tmp_path / "engine"
        engine._concept_inhibitor = ConceptInhibitor(semantic=semantic, llm_fn=None)
        executor = ReflectionExecutor(engine=engine)

        out = executor._with_similar_hint("鋼琴", {"description": "x"})

        assert "similar_hint" not in out
        assert calls == []

    def test_reflection_create_without_llm_records_candidates(self, semantic):
        existing = _seed(semantic, "鋼琴")
        inh = ConceptInhibitor(semantic=semantic, llm_fn=None)

        ok, result = inh.process_reflection_patch(
            "create", "鋼琴演奏", {"description": "彈鋼琴"}, "測試",
        )

        assert ok and result["concept_id"] != existing
        patch = _audit_lines(semantic)[-1]["patches"][0]
        assert patch["new_value"]["similar_candidates"][0]["concept_id"] == existing


# ─── embedding 不在鎖內 ─────────────────────────────────────────────────


def _storage_lock_depth() -> int:
    depths = getattr(locking._LOCK_STATE, "depths", None) or {}
    return sum(depths.values())


class TestEmbeddingOutsideLocks:
    def _recording_embed(self, holder: dict):
        def embed(text: str) -> list[float]:
            sem = holder["semantic"]
            holder.setdefault("calls", []).append({
                "semantic_lock": sem._lock._is_owned(),
                "mem_lock": sem._mem._owner == threading.get_ident(),
                "storage_lock": _storage_lock_depth() > 0,
            })
            return _vec(text)
        return embed

    def test_gate_embedding_not_under_semantic_lock(self, tmp_path):
        holder: dict = {}
        semantic = SemanticMemory(tmp_path / "s", embed_fn=self._recording_embed(holder))
        holder["semantic"] = semantic
        _seed(semantic, "鋼琴")
        holder["calls"] = []
        inh = ConceptInhibitor(semantic=semantic, llm_fn=None)

        _extract(inh)  # 未帶 embedding：Gate 自己算

        assert holder["calls"], "應該有計算 embedding"
        assert not any(c["semantic_lock"] or c["mem_lock"] or c["storage_lock"] for c in holder["calls"])

    def test_distillation_prechecks_before_storage_lock(self, tmp_path):
        holder: dict = {}
        semantic = SemanticMemory(tmp_path / "s", embed_fn=self._recording_embed(holder))
        holder["semantic"] = semantic
        _seed(semantic, "鋼琴")
        holder["calls"] = []
        graph = MemoryGraph(storage_path=tmp_path / "g")
        coordinator = DreamPatchCoordinator(
            semantic=semantic, inhibitor=ConceptInhibitor(semantic=semantic, llm_fn=None),
        )
        distiller = KnowledgeDistillation(
            episodic=MagicMock(), semantic=semantic, graph=graph,
            llm_fn=lambda p: "[]", patch_coordinator=coordinator,
            guard=lambda: storage_lock(tmp_path / "lock"),
        )
        ep = Episode(
            id="ep_d1", user_input="我在彈鋼琴", agent_response="好", content_summary="彈鋼琴",
            session_id="s", embedding=[0.1] * 3,
        )

        distiller._ingest_concept(
            {"name": "鋼琴演奏", "description": "彈鋼琴的活動", "concept_type": "abstract",
             "source_indices": [0]},
            [ep],
        )

        assert holder["calls"], "應該有計算 embedding"
        assert not any(c["semantic_lock"] or c["mem_lock"] or c["storage_lock"] for c in holder["calls"])
        created = semantic.get_by_name("鋼琴演奏")
        assert created is not None
        patch = _audit_lines(semantic)[-1]["patches"][0]
        assert patch["new_value"]["similar_candidates"]

    def test_reflection_executor_precomputes_before_storage_lock(self, tmp_path):
        from echo_memory.affect.reflection_executor import ReflectionExecutor

        holder: dict = {}
        semantic = SemanticMemory(tmp_path / "s", embed_fn=self._recording_embed(holder))
        holder["semantic"] = semantic
        _seed(semantic, "鋼琴")
        holder["calls"] = []
        engine = MagicMock()
        engine.semantic = semantic
        engine.storage_path = tmp_path / "engine"
        engine._concept_inhibitor = ConceptInhibitor(semantic=semantic, llm_fn=None)
        executor = ReflectionExecutor(engine=engine)

        result = executor.build_executor_fn()(
            "create_concept", "鋼琴演奏", {"description": "彈鋼琴"},
        )

        assert result["concept_id"]
        assert holder["calls"]
        assert not any(c["semantic_lock"] or c["mem_lock"] or c["storage_lock"] for c in holder["calls"])


# ─── support_source 納入 patch trail ────────────────────────────────────


class TestSupportSourceInPatchTrail:
    def test_create_carries_support_source_in_audit(self, semantic):
        inh = ConceptInhibitor(semantic=semantic, llm_fn=None)

        cid, tx = _extract(inh, noun="吉他", evidence_kind="distillation",
                           support_source="legacy_unknown")

        assert semantic.get(cid).metadata["support_source"] == "legacy_unknown"
        assert tx.patches[0].support_source == "legacy_unknown"
        logged = _audit_lines(semantic)[-1]["patches"][0]
        assert logged["support_source"] == "legacy_unknown"
        assert logged["rollback_payload"]["support_source_undo"] == {"present": False, "value": None}

    def test_invalid_support_source_rejected_before_any_write(self, semantic):
        inh = ConceptInhibitor(semantic=semantic, llm_fn=None)
        with pytest.raises(ValueError):
            _extract(inh, noun="吉他", support_source="bogus")
        assert semantic.get_by_name("吉他") is None

    def test_expand_raises_support_never_lowers_and_rolls_back(self, semantic):
        inh = ConceptInhibitor(semantic=semantic, llm_fn=None)
        cid, _ = _extract(inh, noun="吉他", evidence_kind="distillation",
                          support_source="agent_only")
        assert semantic.get(cid).metadata["support_source"] == "agent_only"

        # 升級：agent_only -> user（expand 攜帶）
        _, up = inh.process_extractor_patch(
            noun="吉他", desc="d", episode_id="ep_u", evidence_kind="extractor_synonym_merge",
            existing_concept_id=cid, support_source="user",
        )
        assert semantic.get(cid).metadata["support_source"] == "user"
        up_patch = up.patches[0]
        assert up_patch.rollback_payload["support_source_undo"] == {
            "present": True, "value": "agent_only",
        }

        # 不降：user 之後來 agent_only 不覆蓋，也不留 undo 記錄
        _, down = inh.process_extractor_patch(
            noun="吉他", desc="d", episode_id="ep_a", evidence_kind="extractor_synonym_merge",
            existing_concept_id=cid, support_source="agent_only",
        )
        assert semantic.get(cid).metadata["support_source"] == "user"
        assert "support_source_undo" not in (down.patches[0].rollback_payload or {})

        # rollback 升級那一筆 -> 還原 agent_only
        inh._rollback_patch(up_patch)
        assert semantic.get(cid).metadata["support_source"] == "agent_only"
        assert "ep_u" not in semantic.get(cid).source_episode_ids

    def test_support_only_expand_still_writes_and_rolls_back(self, semantic):
        inh = ConceptInhibitor(semantic=semantic, llm_fn=None)
        cid, tx0 = _extract(inh, noun="吉他", evidence_kind="distillation",
                            episode_id="ep_x", support_source="agent_only")
        # 同一個 episode 再次 expand（來源已存在），只有 support 升級
        _, tx = inh.process_extractor_patch(
            noun="吉他", desc="d", episode_id="ep_x", evidence_kind="extractor_synonym_merge",
            existing_concept_id=cid, support_source="user",
        )
        assert semantic.get(cid).metadata["support_source"] == "user"
        inh._rollback_patch(tx.patches[0])
        assert semantic.get(cid).metadata["support_source"] == "agent_only"
        assert "ep_x" in semantic.get(cid).source_episode_ids

    def test_create_rollback_removes_concept(self, semantic):
        inh = ConceptInhibitor(semantic=semantic, llm_fn=None)
        cid, tx = _extract(inh, noun="吉他", support_source="user")
        inh._rollback_patch(tx.patches[0])
        assert semantic.get(cid) is None

    def test_similar_expand_redirect_carries_and_restores_support(self, semantic):
        existing = _seed(semantic, "鋼琴")
        semantic.get(existing).metadata["support_source"] = "agent_only"
        llm = _Llm({"decision": "expand", "target_id": existing, "reason": "同一件事"})
        inh = ConceptInhibitor(semantic=semantic, llm_fn=llm)

        cid, tx = _extract(inh, support_source="user")

        assert cid == existing
        assert semantic.get(existing).metadata["support_source"] == "user"
        logged = _audit_lines(semantic)[-1]["patches"][0]
        assert logged["support_source"] == "user"
        assert logged["rollback_payload"]["support_source_undo"] == {
            "present": True, "value": "agent_only",
        }
        inh._rollback_patch(tx.patches[0])
        assert semantic.get(existing).metadata["support_source"] == "agent_only"

    def test_distillation_support_source_goes_through_patch(self, tmp_path):
        semantic = SemanticMemory(tmp_path / "s", embed_fn=_vec)
        graph = MemoryGraph(storage_path=tmp_path / "g")
        coordinator = DreamPatchCoordinator(
            semantic=semantic, inhibitor=ConceptInhibitor(semantic=semantic, llm_fn=None),
        )
        distiller = KnowledgeDistillation(
            episodic=MagicMock(), semantic=semantic, graph=graph,
            llm_fn=lambda p: "[]", patch_coordinator=coordinator,
        )
        assert not hasattr(SemanticMemory, "merge_support_source")
        ep = Episode(
            id="ep_d2", user_input="我在彈吉他", agent_response="好", content_summary="彈吉他",
            session_id="s", embedding=[0.1] * 3,
        )

        distiller._ingest_concept(
            {"name": "吉他", "description": "彈吉他", "concept_type": "abstract",
             "source_indices": [0]},
            [ep],
        )

        created = semantic.get_by_name("吉他")
        assert created.metadata["support_source"] == "legacy_unknown"
        logged = _audit_lines(semantic)[-1]["patches"][0]
        assert logged["support_source"] == "legacy_unknown"
        assert logged["new_value"]["subject"] == "world"
