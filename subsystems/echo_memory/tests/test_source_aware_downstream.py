"""
M-9a：下游（蒸餾／replay／ConceptExtractor／Verifier／Reflection）依來源可信度過濾。

重點：
  - agent 的推測不會被當成「使用者說過」；只由 agent 支撐的概念不得標成使用者偏好
  - 未交付的 agent 內容不進 prompt
  - tool_note 標示工具與內容出處；legacy_unknown 標「來源未確認」
  - Reflection 帶 parent_concepts 時 resolve_provenance 可回溯
  - Distillation 的 coordinator 沒有 inhibitor 時走降級 Gate（留 audit），不直寫
"""

from __future__ import annotations

import json

import pytest

from echo_memory.affect.concept_extractor import ConceptExtractor
from echo_memory.affect.concept_inhibitor import ConceptInhibitor
from echo_memory.affect.reflection import ReflectionModule
from echo_memory.affect.reflection_executor import (
    ReflectionExecutor,
    parse_reflection_to_actions,
)
from echo_memory.affect.verifier import ResponseVerifier
from echo_memory.dream.distillation import KnowledgeDistillation
from echo_memory.dream.patch_coordinator import DreamPatchCoordinator
from echo_memory.dream.replay import LiDERReplay
from echo_memory.engine import MemoryEngine
from echo_memory.neurochem.state import NeurochemState
from echo_memory.schemas.evidence import EvidenceInput, SourceRef
from echo_memory.schemas.memory_context import MemoryContext


@pytest.fixture
def engine(tmp_path):
    return MemoryEngine(storage_path=tmp_path / "data", namespace_id="ns1")


def _store(engine, user="", agent="", evidence=None, **kw) -> str:
    return engine.store_episode(user, agent, "s1", salience=0.9, evidence=evidence, **kw)


def _user(text: str) -> EvidenceInput:
    return EvidenceInput(source_type="user", content=text, speaker_id="u1")


def _agent(text: str, **kw) -> EvidenceInput:
    return EvidenceInput(source_type="agent", content=text, speaker_id="agent", **kw)


def _distiller(engine, llm_fn, **kw) -> KnowledgeDistillation:
    return KnowledgeDistillation(
        episodic=engine.episodic,
        semantic=engine.semantic,
        graph=engine.episodic.graph,
        llm_fn=llm_fn,
        evidence_store=engine.evidence,
        **kw,
    )


class _Capture:
    """假 llm_fn：記下 prompt，回傳預先給定的 JSON。"""

    def __init__(self, reply: object = "[]"):
        self.prompts: list[str] = []
        self.reply = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.reply


# ─── Distillation prompt 標示 ───────────────────────────────────────────


class TestDistillationPrompt:
    def test_agent_speculation_not_labelled_as_user_statement(self, engine):
        _store(engine, "", "你大概不喜歡香菜吧", evidence=[_agent("你大概不喜歡香菜吧")])
        cap = _Capture()
        _distiller(engine, cap).run()

        prompt = cap.prompts[0]
        assert "助手說（已說出口）: 你大概不喜歡香菜吧" in prompt
        assert "使用者說（已確認）: 你大概不喜歡香菜吧" not in prompt
        assert "使用者說（已確認）" not in prompt.split("## 對話記錄", 1)[1]

    def test_user_statement_labelled_confirmed(self, engine):
        _store(engine, "我討厭香菜", "好的", evidence=[_user("我討厭香菜"), _agent("好的")])
        cap = _Capture()
        _distiller(engine, cap).run()
        assert "使用者說（已確認）: 我討厭香菜" in cap.prompts[0]

    @pytest.mark.parametrize("kw", [
        {"delivery_state": "not_delivered"},
        {"delivery_state": "partial"},  # 被打斷且進度未知
    ])
    def test_undelivered_agent_content_excluded(self, engine, kw):
        text = "這是一段從未說出口的預定回覆"
        # engine 一致性檢查把 partial 視為 agent_response 的一部分
        _store(
            engine, "你好", text if kw["delivery_state"] == "partial" else "",
            evidence=[_user("你好"), _agent(text, **kw)],
        )
        cap = _Capture()
        _distiller(engine, cap).run()
        prompt = cap.prompts[0]
        assert "從未說出口的預定回覆" not in prompt
        assert "未說出口，內容已略去" in prompt

    def test_tool_note_labelled_with_tool_and_origin(self, engine):
        _store(
            engine, episode_kind="tool_note",
            evidence=[EvidenceInput(
                source_type="tool", content="筆記內容 ABC", speaker_id="agent",
                source_ref=SourceRef(kind="tool_call", name="write_note"),
            )],
        )
        cap = _Capture()
        _distiller(engine, cap).run()
        prompt = cap.prompts[0]
        assert "工具記錄（工具 write_note；內容出處 unknown）: 筆記內容 ABC" in prompt
        assert "使用者說（已確認）" not in prompt.split("## 對話記錄", 1)[1]

    def test_legacy_unknown_labelled_unconfirmed(self, engine):
        _store(engine, "我有一隻貓", "好可愛")  # 舊介面：legacy_unknown
        cap = _Capture()
        _distiller(engine, cap).run()
        body = cap.prompts[0].split("## 對話記錄", 1)[1]
        assert "使用者輸入（來源未確認）: 我有一隻貓" in body
        assert "助手回覆（來源未確認，不確定是否說出口）: 好可愛" in body
        assert "使用者說（已確認）" not in body

    def test_evidence_read_once_for_whole_batch(self, engine, monkeypatch):
        for i in range(3):
            _store(engine, f"事實{i}", "", evidence=[_user(f"事實{i}")])
        calls = {"n": 0}
        orig = engine.evidence._build_units

        def counting():
            calls["n"] += 1
            return orig()

        monkeypatch.setattr(engine.evidence, "_build_units", counting)
        _distiller(engine, _Capture()).run()
        assert calls["n"] == 1


# ─── 只由 agent 支撐的概念 ──────────────────────────────────────────────


class TestSupportSource:
    def test_agent_only_concept_not_marked_user(self, engine):
        _store(engine, "", "你應該喜歡香菜", evidence=[_agent("你應該喜歡香菜")])
        reply = [{
            "name": "香菜偏好", "description": "使用者喜歡香菜",
            "concept_type": "rule", "subject": "user", "source_indices": [0],
        }]
        report = _distiller(engine, _Capture(reply)).run()
        assert report.concepts_created == 1
        c = engine.semantic.get_by_name("香菜偏好")
        assert c.subject == "world"
        assert c.metadata["support_source"] == "agent_only"

    def test_user_supported_concept_keeps_user_subject(self, engine):
        _store(engine, "我討厭香菜", "", evidence=[_user("我討厭香菜")])
        reply = [{
            "name": "香菜厭惡", "description": "使用者討厭香菜",
            "concept_type": "rule", "subject": "user", "source_indices": [0],
        }]
        _distiller(engine, _Capture(reply)).run()
        c = engine.semantic.get_by_name("香菜厭惡")
        assert c.subject == "user"
        assert c.metadata["support_source"] == "user"

    def test_legacy_supported_concept_marked_legacy(self, engine):
        _store(engine, "我有一隻貓", "")
        reply = [{
            "name": "寵物", "description": "使用者有貓",
            "concept_type": "entity", "subject": "user", "source_indices": [0],
        }]
        _distiller(engine, _Capture(reply)).run()
        c = engine.semantic.get_by_name("寵物")
        assert c.subject == "user"
        assert c.metadata["support_source"] == "legacy_unknown"

    def test_support_source_never_downgraded(self, engine):
        # support_source 由 create／expand patch 攜帶（Gate 內只升不降）
        inh = ConceptInhibitor(semantic=engine.semantic, llm_fn=None)
        cid, _ = inh.process_extractor_patch(
            noun="X", desc="d", episode_id="e1", evidence_kind="extractor_new_concept",
            support_source="user",
        )
        sem = engine.semantic
        inh.process_extractor_patch(
            noun="X", desc="d", episode_id="e2", evidence_kind="extractor_synonym_merge",
            existing_concept_id=cid, support_source="agent_only",
        )
        assert sem.get(cid).metadata["support_source"] == "user"
        with pytest.raises(ValueError):
            inh.process_extractor_patch(
                noun="Y", desc="d", episode_id="e3", evidence_kind="extractor_new_concept",
                support_source="bogus",
            )


# ─── Distillation 無 inhibitor：降級 Gate ─────────────────────────────


class TestDistillationDegradedGate:
    def test_coordinator_without_inhibitor_still_audited(self, engine):
        _store(engine, "我喜歡茶", "", evidence=[_user("我喜歡茶")])
        reply = [{
            "name": "茶偏好", "description": "使用者喜歡茶",
            "concept_type": "rule", "subject": "user", "source_indices": [0],
        }]
        coordinator = DreamPatchCoordinator()  # 沒有 inhibitor、沒有 semantic
        assert not coordinator.has_inhibitor
        distiller = _distiller(engine, _Capture(reply), patch_coordinator=coordinator)
        assert coordinator.has_inhibitor  # 已補降級 Gate

        report = distiller.run()
        assert report.concepts_created == 1
        assert engine.semantic.get_by_name("茶偏好") is not None
        audit = engine.semantic.storage_path / "audit_log.jsonl"
        assert audit.exists() and "茶偏好" in audit.read_text(encoding="utf-8")

    def test_no_direct_upsert_fallback(self, engine, monkeypatch):
        """Gate 拒絕時不得退回 semantic.upsert_concept 直寫。"""
        _store(engine, "我喜歡茶", "", evidence=[_user("我喜歡茶")])
        reply = [{"name": "茶偏好", "description": "d", "source_indices": [0]}]
        coordinator = DreamPatchCoordinator(semantic=engine.semantic)
        monkeypatch.setattr(
            coordinator, "submit_distillation_concept", lambda **kw: (None, None),
        )
        direct = []
        orig = engine.semantic.upsert_concept
        monkeypatch.setattr(
            engine.semantic, "upsert_concept",
            lambda *a, **k: direct.append(a) or orig(*a, **k),
        )
        report = _distiller(engine, _Capture(reply), patch_coordinator=coordinator).run()
        assert direct == []
        assert report.concepts_rejected == 1
        assert engine.semantic.get_by_name("茶偏好") is None


# ─── replay／ConceptExtractor／Verifier ─────────────────────────────────


class TestOtherDownstream:
    def test_replay_prompt_marks_sources(self, engine):
        _store(engine, "", "猜你愛吃辣", evidence=[_agent("猜你愛吃辣")])
        _store(
            engine, "你好", "",
            evidence=[_user("你好"), _agent("沒說出口", delivery_state="not_delivered")],
        )
        cap = _Capture('{"reflections": []}')
        LiDERReplay(
            engine.episodic, engine.episodic.graph, llm_fn=cap,
            evidence_store=engine.evidence,
        ).run()
        prompt = cap.prompts[0]
        assert "助手說（已說出口）: 猜你愛吃辣" in prompt
        assert "沒說出口" not in prompt.replace("未說出口", "")
        assert "使用者說（已確認）: 你好" in prompt

    def test_concept_extractor_prompt_marks_sources(self, engine):
        ep_id = _store(
            engine, "我養貓", "",
            evidence=[_user("我養貓"), _agent("藏起來", delivery_state="not_delivered")],
        )
        cap = _Capture("[]")
        extractor = ConceptExtractor(
            llm_fn=cap, semantic=engine.semantic, graph=engine.episodic.graph,
            evidence_store=engine.evidence,
        )
        try:
            extractor.extract_in_background(engine.episodic.get(ep_id)).result(timeout=10)
        finally:
            extractor.shutdown(wait=True)
        prompt = cap.prompts[0]
        assert "使用者說（已確認）: 我養貓" in prompt
        assert "藏起來" not in prompt

    def test_verifier_contradiction_prompt_marks_sources(self, engine):
        ep_id = _store(
            engine, "我對蝦過敏", "",
            evidence=[_user("我對蝦過敏"), _agent("私下推測", delivery_state="not_delivered")],
        )
        ep = engine.episodic.get(ep_id)
        ctx = MemoryContext(
            episodes=[ep],
            evidence_by_episode=engine.evidence.units_for_episodes([ep]),
        )
        cap = _Capture('{"has_contradiction": false, "reason": ""}')
        verifier = ResponseVerifier(NeurochemState(), llm_fn=cap)
        verifier._llm_contradiction_check("推薦你吃蝦", ctx)
        prompt = cap.prompts[0]
        assert "使用者說（已確認）: 我對蝦過敏" in prompt
        assert "私下推測" not in prompt

    def test_verifier_semantic_ref_text_skips_undelivered(self, engine):
        ep_id = _store(
            engine, "", "",
            evidence=[_agent("從未說出口的內容", delivery_state="not_delivered")],
        )
        ep = engine.episodic.get(ep_id)
        ctx = MemoryContext(
            episodes=[ep],
            evidence_by_episode=engine.evidence.units_for_episodes([ep]),
        )
        embedded: list[str] = []

        def embed(text: str):
            embedded.append(text)
            return [1.0, 0.0]

        verifier = ResponseVerifier(NeurochemState(), embed_fn=embed)
        verifier._score_consistency("任意回覆", ctx)
        assert all("從未說出口的內容" not in t for t in embedded)


# ─── Reflection parent_concepts ─────────────────────────────────────────


class TestReflectionParentConcepts:
    def _reflect(self, reply: dict):
        cap = _Capture(reply)
        module = ReflectionModule(llm_fn=cap)
        out = module.reflect(["摘要"], known_concepts=["貓", "狗"])
        return cap, out

    def test_prompt_asks_for_parent_concepts(self):
        cap, _ = self._reflect({"action": "none", "content": "", "reasoning": ""})
        assert "parent_concepts" in cap.prompts[0]

    def test_parent_concepts_flow_into_derivation(self, engine):
        ep_id = _store(engine, "貓咪", "", evidence=[_user("貓咪")])
        inhibitor = ConceptInhibitor(semantic=engine.semantic, llm_fn=None)
        parent, _tx = inhibitor.process_extractor_patch(
            noun="貓", desc="貓的描述", episode_id=ep_id,
            evidence_kind="distillation", triggered_by="dream",
            source_episode_ids=[ep_id],
        )
        _, out = self._reflect({
            "action": "none", "content": "", "reasoning": "",
            "memory_ops": [{
                "op": "create_concept", "target": "寵物",
                "params": {"description": "寵物總稱", "parent_concepts": ["貓"]},
                "reason": "歸納",
            }],
        })
        actions = parse_reflection_to_actions(out)
        executor = ReflectionExecutor(engine=engine, concept_inhibitor=inhibitor)
        fn = executor.build_executor_fn()
        a = actions[0]
        result = fn(a["action_type"], a["target_id"], a["params"])

        res = engine.resolve_provenance(result["concept_id"])
        assert res.found and res.complete
        assert res.evidence_ids == list(engine.episodic.get(ep_id).evidence_ids)
        assert engine.semantic.get_by_name("寵物").derivations[0]["id"] == parent

    def test_missing_parent_concepts_does_not_crash(self, engine):
        _, out = self._reflect({
            "action": "none", "content": "", "reasoning": "",
            "memory_ops": [{
                "op": "create_concept", "target": "孤兒",
                "params": {"description": "沒有 parent"}, "reason": "x",
            }],
        })
        a = parse_reflection_to_actions(out)[0]
        fn = ReflectionExecutor(engine=engine).build_executor_fn()
        result = fn(a["action_type"], a["target_id"], a["params"])
        assert engine.resolve_provenance(result["concept_id"]).found
