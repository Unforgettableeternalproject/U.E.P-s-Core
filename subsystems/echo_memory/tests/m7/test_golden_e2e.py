"""
M7 Golden 8 情境 E2E（經 Echo Stream 呼叫形狀的 EchoStreamSim；mock LLM／mock embedding）。

編號：M7-G1..G8 對應路線圖 note 407450444eb74774a4bff57c7a16cae0 的 8 項 Golden 情境；
R1..R9 對應 9 項放行門檻（見 test_release_gates.py）。每個測試的 docstring 標明對應編號。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from echo_memory.affect.concept_inhibitor import ConceptInhibitor
from echo_memory.dream.contradiction import ContradictionDetector
from echo_memory.dream.patch_coordinator import DreamPatchCoordinator
from echo_memory.schemas.evidence import EvidenceInput
from echo_memory.storage.conflict_ledger import ConflictLedger
from echo_memory.storage.locking import storage_lock
from tests.baseline._support import tx_audit
from tests.m7.sim import RoutedLLM, agent_ev, user_ev

APPROVE = json.dumps({"approve": True, "confidence": 0.9, "reason": "ok"})


def _distill(*items):
    return [
        {"name": n, "description": d, "concept_type": "rule", "subject": s, "source_indices": idx}
        for n, d, s, idx in items
    ]


def _review_reply(status: str, cited: list[str]) -> str:
    return json.dumps({
        "status": status, "claim_kind": "fact", "cited_unit_ids": cited,
        "corroborating_unit_ids": [], "scope": {"subject_ref": "user", "context": None},
        "reason": "mock",
    })


def _patch_actions(engine) -> list[tuple[str, str]]:
    """所有 Gate audit 的 (action, 最終 status)。"""
    last: dict[str, dict] = {}
    for tx in tx_audit(engine):
        last[tx["transaction_id"]] = tx
    return [(p["action"], p["status"]) for tx in last.values() for p in tx["patches"]]


# ━━━ M7-G1：使用者 A 與 Agent 推測 B 分離（R1） ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_g1_user_statement_and_agent_speculation_stay_separate(make_sim):
    """M7-G1／R1：使用者說的與 agent 猜的，經 store -> dream 後 prompt 與證據層仍分開標示。"""
    llm = RoutedLLM(distill=[])
    sim, engine = make_sim(llm)
    sim.store("我討厭香菜", "好的", salience=0.8, evidence=[user_ev("我討厭香菜"), agent_ev("好的")])
    c = sim.store("嗯", "我猜你也不愛蔥", salience=0.8,
                  evidence=[user_ev("嗯"), agent_ev("我猜你也不愛蔥")])
    sim.dream()

    prompt = llm.prompts("distill")[0]
    assert "使用者說（已確認）: 我討厭香菜" in prompt
    assert "助手說（已說出口）: 我猜你也不愛蔥" in prompt
    assert "使用者說（已確認）: 我猜你也不愛蔥" not in prompt
    users = engine.retrieve_evidence(episode_ids=[c["episode_id"]], source_types=["user"])
    assert [u.content for u in users] == ["嗯"]


def test_g1b_agent_only_concept_is_not_a_user_fact(make_sim):
    """M7-G1／R1：只由 agent 發言支撐的概念 support_source=agent_only、subject!=user、不進 profile。"""
    llm = RoutedLLM(distill=_distill(
        ("香菜厭惡", "使用者討厭香菜", "user", [0]),
        ("蔥偏好", "使用者不愛蔥", "user", [1]),   # LLM 把 agent 推測誤標 user，系統必須糾正
    ))
    sim, engine = make_sim(llm)
    sim.store("我討厭香菜", "", salience=0.8, evidence=[user_ev("我討厭香菜")])
    engine.store_episode(
        "", "我猜你也不愛蔥", "es-session", salience=0.8,
        evidence=[EvidenceInput(**agent_ev("我猜你也不愛蔥"))])
    sim.dream()

    user_c, agent_c = engine.semantic.get_by_name("香菜厭惡"), engine.semantic.get_by_name("蔥偏好")
    assert user_c.subject == "user" and user_c.metadata["support_source"] == "user"
    assert agent_c is not None
    assert agent_c.metadata["support_source"] == "agent_only" and agent_c.subject != "user"
    profile = engine.get_profile()
    assert "香菜" in profile and "蔥" not in profile


def test_g1c_mixed_episode_agent_guess_cannot_be_reviewed_as_supported(make_sim):
    """M7-G1／R1：Echo 實際形狀（同輪使用者說「嗯」、agent 猜蔥）蒸餾出的概念，證據審核引用 agent 單元 -> 不得 supported。"""
    llm = RoutedLLM(distill=_distill(("蔥偏好", "使用者不愛蔥", "user", [0])))
    sim, engine = make_sim(llm)
    r = sim.store("嗯", "我猜你也不愛蔥", salience=0.8,
                  evidence=[user_ev("嗯"), agent_ev("我猜你也不愛蔥")])
    sim.dream()
    c = engine.semantic.get_by_name("蔥偏好")
    assert c is not None
    agent_unit = [u for u in engine.evidence.get_by_episode(r["episode_id"])
                  if u.source_type == "agent"][0]
    engine.set_llm_fn(RoutedLLM(evidence_review=_review_reply("supported", [agent_unit.id])))
    engine.review_concept(c.id)
    assert engine.semantic.get(c.id).effective_review_status() != "supported"


# ━━━ M7-G2：force_remember 來源（R1） ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_g2_force_remember_is_tool_note_not_user_speech(make_sim):
    """M7-G2／R1：force_remember 的內容是 tool 來源，不被蒸餾 prompt 當成「使用者說過」。"""
    from llm_integration.tools.executor import ToolExecutor

    llm = RoutedLLM(distill=_distill(("生日", "使用者的生日是三月三日", "user", [0])))
    sim, engine = make_sim(llm)
    res = ToolExecutor(engine=engine, session_id="es-session").handle_force_remember(
        {"content": "使用者的生日是三月三日", "reason": "使用者明確要求記住", "salience": 0.9})
    assert res["success"]
    units = engine.evidence.get_by_episode(res["episode_id"])
    assert [u.source_type for u in units] == ["tool"]
    assert units[0].content_origin == "unknown"          # 未說明出處不得推定為 user
    sim.dream()
    prompt = llm.prompts("distill")[0]
    assert "使用者說（已確認）: 使用者的生日" not in prompt
    assert "工具記錄（工具 force_remember；內容出處 unknown）" in prompt
    c = engine.semantic.get_by_name("生日")
    if c is not None:
        assert c.metadata.get("support_source") != "user"
        assert c.subject != "user"


# ━━━ M7-G3：Dream -> Reflection 多階回溯（R2、R5） ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_g3_dream_then_reflection_chain_traces_back_to_original_evidence(make_sim):
    """M7-G3／R2、R5：Dream 蒸餾 B、Reflection 產 C、再由 C 產 D，D 回溯到原始 user evidence，只算 1 個獨立來源，且全經 Gate。"""
    llm = RoutedLLM(distill=_distill(("黑咖啡", "使用者喝黑咖啡", "user", [0])))
    sim, engine = make_sim(llm)
    r = sim.store("我都喝黑咖啡", "好", salience=0.8, evidence=[user_ev("我都喝黑咖啡"), agent_ev("好")])
    sim.dream()
    b = engine.semantic.get_by_name("黑咖啡")
    assert b is not None

    assert ("create", "applied") in _patch_actions(engine)       # Dream 蒸餾本身已經 Gate
    gate = ConceptInhibitor(semantic=engine.semantic, llm_fn=None)
    engine.set_concept_inhibitor(gate)
    ok, rc = gate.process_reflection_patch(
        operation="create", target_name="咖啡習慣",
        params={"description": "使用者的咖啡習慣", "parent_concepts": ["黑咖啡"]})
    assert ok, rc
    ok, rd = gate.process_reflection_patch(
        operation="create", target_name="飲品偏好",
        params={"description": "使用者的飲品偏好", "parent_concepts": ["咖啡習慣"]})
    assert ok, rd
    d_id = rd["concept_id"]

    res = engine.resolve_provenance(d_id)
    assert res.complete
    assert res.evidence_ids == r["all_unit_ids"]            # 原始 episode 的 user + agent 兩個 unit
    kinds = [s.kind for s in res.paths[0].steps]
    assert kinds[0] == "concept" and kinds[-2:] == ["episode", "evidence"]
    sup = engine.independent_support([b.id, rc["concept_id"], d_id])
    assert sup.independent_episode_count == 1 and sorted(sup.independent_ids) == sorted(r["all_unit_ids"])
    creates = [s for a, s in _patch_actions(engine) if a == "create"]
    assert len(creates) >= 3 and set(creates) == {"applied"}     # 蒸餾 B + 兩個 Reflection 產物


# ━━━ M7-G4：共享來源去重（R2） ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_g4_retry_and_shared_source_count_once(make_sim):
    """M7-G4／R2：Echo Stream 重送（同 idempotency_key）不重複寫入；兩個概念共用同一原始 evidence 只算 1。"""
    llm = RoutedLLM(distill=_distill(
        ("香菜厭惡", "使用者討厭香菜", "user", [0]),
        ("飲食禁忌", "使用者有飲食禁忌", "user", [0]),
    ))
    sim, engine = make_sim(llm)
    ev = [user_ev("我討厭香菜"), agent_ev("好的")]
    first = sim.store("我討厭香菜", "好的", salience=0.8, idempotency_key="turn-1", evidence=ev)
    again = sim.store("我討厭香菜", "好的", salience=0.8, idempotency_key="turn-1", evidence=ev)
    assert first["episode_id"] == again["episode_id"]
    assert len(engine.episodic.list_episodes()) == 1
    sim.dream()
    a, b = engine.semantic.get_by_name("香菜厭惡"), engine.semantic.get_by_name("飲食禁忌")
    assert a and b
    sup = engine.independent_support([a.id, b.id])
    assert sup.independent_episode_count == 1 and sup.shared_ids == sup.independent_ids


# ━━━ M7-G5：偏好更正保留歷史（R3） ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_g5_preference_correction_keeps_history(make_sim):
    """M7-G5／R3：新偏好取代舊偏好 -> 舊概念 deprecated + replaced_by（歷史可查）、無 CONTRADICTS 邊、經 Gate、預設檢索不召回。"""
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    sim, engine = make_sim(RoutedLLM())
    ea = sim.store("我喜歡 A 牌子", "", evidence=[user_ev("我喜歡 A 牌子", occurred_at=t0)])
    eb = sim.store("我改喜歡 B 牌子了", "",
                   evidence=[user_ev("我改喜歡 B 牌子了", occurred_at=t0 + timedelta(days=1))])
    gate = ConceptInhibitor(semantic=engine.semantic, llm_fn=lambda p: APPROVE)
    engine.set_concept_inhibitor(gate)
    ids = {}
    for name, ep, desc in (("喜歡A", ea, "使用者喜歡 A 牌子"), ("喜歡B", eb, "使用者喜歡 B 牌子")):
        cid, tx = gate.process_extractor_patch(
            noun=name, desc=desc, episode_id=ep["episode_id"], evidence_kind="distillation",
            triggered_by="dream", source_episode_ids=[ep["episode_id"]], subject="user",
            support_source="user")
        assert cid, tx
        ids[name] = cid
        engine.semantic.get(cid).embedding = [1.0, 0.0]
    detector = ContradictionDetector(
        semantic=engine.semantic, graph=engine.episodic.graph, similarity_threshold=0.5,
        llm_fn=lambda p: json.dumps({"relation": "supersedes", "newer": "a", "strength": 0.8,
                                     "scope_note": "", "reason": "偏好更正"}),
        conflict_ledger=ConflictLedger(engine.episodic.graph.storage_path),
        guard=lambda: storage_lock(engine.storage_path),
        patch_coordinator=DreamPatchCoordinator(inhibitor=gate),
        provenance=engine._provenance_resolver(),
    )
    assert detector.detect_and_register(engine.semantic.get(ids["喜歡B"])) == []
    old = engine.semantic.get(ids["喜歡A"])
    assert old is not None and old.status == "deprecated"
    assert old.metadata["replaced_by"] == ids["喜歡B"]
    assert not engine.episodic.graph.has_contradicts_edge(ids["喜歡A"], ids["喜歡B"])
    assert ("deprecate", "applied") in _patch_actions(engine)
    # 歷史證據仍在
    assert engine.retrieve_evidence(episode_ids=[ea["episode_id"]], source_types=["user"])
    ctx = engine.retrieve("我喜歡什麼牌子", top_k=10, min_similarity=0.0)
    assert "使用者喜歡 A 牌子" not in ctx.to_text()


# ━━━ M7-G6：Dream 缺來源不自動 verified（R5、R7） ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_g6_dream_concept_without_provenance_is_never_auto_verified(make_sim):
    """M7-G6／R5、R7：舊介面（無 evidence）寫入的 Dream 概念，即使審核 LLM 說 supported 也不得 supported；Self Model 不可標 verified。"""
    llm = RoutedLLM(distill=_distill(("寵物", "使用者養了一隻貓", "user", [0])))
    sim, engine = make_sim(llm)
    r = sim.store("我有一隻貓", "好可愛", salience=0.8)          # 無 evidence -> legacy_unknown
    sim.dream()
    c = engine.semantic.get_by_name("寵物")
    assert c is not None and c.metadata["support_source"] == "legacy_unknown"
    units = engine.evidence.get_by_episode(r["episode_id"])
    assert units and all(u.provenance_status == "legacy_unknown" for u in units)

    user_units = [u.id for u in units if u.source_type == "user"]
    review_llm = RoutedLLM(evidence_review=_review_reply("supported", user_units))
    gate = ConceptInhibitor(semantic=engine.semantic, llm_fn=review_llm)
    engine.set_concept_inhibitor(gate)       # 宿主必須注入 Gate，手動審核入口才不會 no_gate
    engine.set_llm_fn(review_llm)
    out = engine.review_concept(c.id)
    assert out["outcome"] != "skipped", out
    assert engine.semantic.get(c.id).effective_review_status() != "supported", out

    ok, _tx = gate.process_self_model_patch(c.id, user_units, verification="verified")
    assert not ok


# ━━━ M7-G7：LLM 審核中斷進 held，可重審（R4） ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_g7_llm_interruption_holds_then_review_recovers(make_sim):
    """M7-G7／R4：Gate 的 LLM 驗證中斷 -> patch held（概念不變）；重啟後 registry 還在，LLM 復原後重審才套用。"""
    from echo_memory.affect.concept_verifier import ConceptAssessment, ConceptVerification

    sim, engine = make_sim(RoutedLLM())
    ep = sim.store("我養了一隻貓叫小橘", "", evidence=[user_ev("我養了一隻貓叫小橘")])
    seed = ConceptInhibitor(semantic=engine.semantic, llm_fn=None)
    cid, _ = seed.process_extractor_patch(
        noun="小橘", desc="使用者養的貓", episode_id=ep["episode_id"], evidence_kind="distillation",
        triggered_by="dream", source_episode_ids=[ep["episode_id"]], subject="user", support_source="user")
    assert cid

    def down(prompt: str) -> str:
        raise TimeoutError("LLM 中斷")

    gate = ConceptInhibitor(semantic=engine.semantic, llm_fn=down)
    ver = ConceptVerification(assessments=[ConceptAssessment(
        concept_id=cid, concept_name="小橘", status="modify", reason="更正",
        suggested_update="使用者養的公貓", relevance=0.9)])
    tx = gate.process(ver, "小橘是公的", "了解", "s", "t1")
    assert tx.patches[0].status == "held" and gate.held_count() == 1
    assert engine.semantic.get(cid).description == "使用者養的貓"

    revived = ConceptInhibitor(semantic=engine.semantic, llm_fn=lambda p: APPROVE)
    assert revived.held_count() == 1
    revived.review_held_patches()
    assert revived.held_count() == 0
    assert engine.semantic.get(cid).description == "使用者養的公貓"


# ━━━ M7-G8：語音生成但未說出口不寫成已發生（R1） ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_g8_undelivered_agent_speech_is_not_recorded_as_happened(make_sim):
    """M7-G8／R1：agent 預定要說但未說出口（not_delivered）的內容，不進 prompt、證據取回、檢索文字，也不成為使用者事實。"""
    llm = RoutedLLM(distill=_distill(("承諾", "助手答應週五提醒使用者", "agent", [0])))
    sim, engine = make_sim(llm)
    planned = "我週五會提醒你帶傘"
    r = sim.store("週五要帶什麼", "", salience=0.8, evidence=[
        user_ev("週五要帶什麼"), agent_ev(planned, delivery_state="not_delivered")])
    assert r
    ep = engine.episodic.get(r["episode_id"])
    assert planned not in (ep.agent_response or "")
    delivered = engine.retrieve_evidence(episode_ids=[r["episode_id"]], require_delivered=True)
    assert [u.source_type for u in delivered] == ["user"]

    sim.dream()
    for kind in ("distill", "replay"):
        for p in llm.prompts(kind):
            assert planned not in p
    assert planned not in engine.retrieve("提醒 帶傘", top_k=10, min_similarity=0.0).to_text()
