"""已知缺陷的重現測試（xfail strict）。

每條都已先在目前程式碼上確認缺陷真實存在（前提由 fixture 斷言，
fixture 失敗會是 ERROR 而不是被 xfail 吞掉）。
修好缺陷後 strict=True 會讓測試 XPASS -> FAIL，提醒移除 xfail 標記。

對照卡片 EM-002：
  a) force_remember 來源被標成 user_input
  b) Reflection merge 硬刪次概念且不可回滾
  c) held patch 沒有重審機制
  d) 自動來源繞過 Patch Gate 直接寫入（d1~d5 為不同入口）
  額外發現（卡片外）：
  e1) patch apply 失敗時，失敗 patch 自身的半套變更殘留
  e2) merge 後圖譜殘留已不存在概念的節點
"""

from __future__ import annotations

import json

import pytest

from echo_memory.affect.action_queue import ReflectionActionQueue
from echo_memory.affect.concept_extractor import ConceptExtractor
from echo_memory.affect.concept_inhibitor import ConceptInhibitor
from echo_memory.affect.concept_verifier import ConceptAssessment, ConceptVerification
from echo_memory.affect.reflection_executor import ReflectionExecutor
from tests.baseline._support import make_fake_llm, tx_audit

DISTILLED = [
    {"name": "家人關係", "description": "使用者重視家人與寵物", "concept_type": "abstract",
     "subject": "user", "source_indices": [0, 1]},
]


# ═══════════════════════════════════════════════════════════════════════════
# a) force_remember 的來源被標成 user_input
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def force_remembered(empty_engine):
    from llm_integration.tools.executor import ToolExecutor

    tool = ToolExecutor(engine=empty_engine, session_id="sess_tool")
    result = tool.handle_force_remember({
        "content": "使用者的生日是三月三日", "reason": "LLM 判斷很重要", "salience": 0.9,
    })
    assert result["success"], result
    ep = empty_engine.episodic.get(result["episode_id"])
    # EM-004 修正後：內容不再借用 Episode.user_input，來源由 evidence 表示
    assert ep.user_input == ""
    return empty_engine, ep


def test_a1_force_remember_episode_carries_non_user_provenance(force_remembered):
    _engine, ep = force_remembered
    units = _engine.evidence.get_by_episode(ep.id)
    assert units, "Episode 應有證據單位"
    assert all(u.source_type != "user" for u in units), "force_remember 不得標成 user"
    assert any(u.source_type == "tool" for u in units)


def test_a2_distillation_prompt_does_not_present_force_remember_as_user_speech(force_remembered):
    engine, _ep = force_remembered
    prompts: list[str] = []

    def spy(prompt: str) -> str:
        prompts.append(prompt)
        return "[]"

    engine.set_llm_fn(spy)
    engine.dream(triggered_by="manual")

    distill_prompts = [p for p in prompts if "知識蒸餾專家" in p]
    assert distill_prompts, "前提：蒸餾有被觸發"
    prompt = distill_prompts[0]
    assert "使用者: [force_remember]" not in prompt
    # 內容不得出現在「使用者:」那一行（仍可經由摘要欄位被蒸餾）
    assert "使用者: 使用者的生日" not in prompt


# ═══════════════════════════════════════════════════════════════════════════
# b) Reflection 合併硬刪次概念（不可回滾）
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def merged(restored_engine, tmp_path):
    e = restored_engine
    inh = ConceptInhibitor(semantic=e.semantic, llm_fn=make_fake_llm())
    executor = ReflectionExecutor(engine=e, concept_inhibitor=inh)
    queue = ReflectionActionQueue(audit_path=tmp_path / "reflection_audit.jsonl")

    secondary = e.semantic.get_by_name("夜貓作息")
    primary_desc_before = e.semantic.get_by_name("手沖咖啡").description
    params = {"merge_with": "夜貓作息"}
    act = queue.enqueue("merge_concepts", "手沖咖啡", params, "合併")
    act.pre_state = executor.capture_pre_state("merge_concepts", "手沖咖啡", params)
    assert queue.confirm(act.id)
    assert queue.execute(act, executor.build_executor_fn()), act.error
    # 前提：合併確實發生（primary 吸收了 secondary 描述）
    assert "（合併自「夜貓作息」）" in e.semantic.get_by_name("手沖咖啡").description
    return SimpleNS(
        engine=e, queue=queue, executor=executor, action=act,
        secondary_id=secondary.id, secondary_desc=secondary.description,
        primary_desc_before=primary_desc_before,
    )


class SimpleNS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "缺陷 b：concept_inhibitor.py _reflection_merge 以 semantic.remove_concept(merge_with) 硬刪次概念"
    "（semantic.py remove_concept 直接 del）；違反「deprecate ≠ delete」，"
    "次概念不留 deprecated/replaced_by 軌跡。"
))
def test_b1_merge_keeps_secondary_as_non_active(merged):
    c = merged.engine.semantic.get_by_name("夜貓作息")
    assert c is not None, "次概念被硬刪"
    assert c.status != "active"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "缺陷 b：ReflectionExecutor.build_rollback_fn 的 merge_concepts 分支只記 warning "
    "（「rollback 尚不完整」），且 ConceptInhibitor._rollback_patch 沒有 merge 分支；"
    "但 ActionQueue.rollback 仍回報成功並標 ROLLED_BACK -> 靜默的假回滾。"
))
def test_b2_merge_can_be_rolled_back(merged):
    e = merged.engine
    assert merged.queue.rollback(merged.action.id, merged.executor.build_rollback_fn())
    restored = e.semantic.get_by_name("夜貓作息")
    assert restored is not None, "回滾後次概念應回來"
    assert restored.description == merged.secondary_desc
    assert e.semantic.get_by_name("手沖咖啡").description == merged.primary_desc_before


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "額外發現 e2：remove_concept 不處理 MemoryGraph；merge 後次概念的圖譜節點"
    "（含 DERIVED_FROM / RELATES_TO 邊）仍殘留，指向已不存在的 Concept。"
))
def test_e2_merge_leaves_no_orphan_graph_node(merged):
    g = merged.engine.episodic.graph
    assert not g.has_node(merged.secondary_id)


# ═══════════════════════════════════════════════════════════════════════════
# c) held patch 沒有重審機制
# ═══════════════════════════════════════════════════════════════════════════


def _unresolved_held_patch_ids(audit: list[dict]) -> set[str]:
    """audit 中曾為 held、之後沒有任何非 held 記錄的 patch_id。"""
    latest: dict[str, str] = {}
    for tx in audit:
        for p in tx["patches"]:
            latest[p["patch_id"]] = p["status"]
    return {pid for pid, st in latest.items() if st == "held"}


@pytest.fixture
def held_then_llm_recovers(restored_engine):
    """第一次 LLM 佐證失敗 -> patch 進 held；之後 LLM 恢復並持續有新對話輪次。"""
    e = restored_engine
    state = {"corroborations": 0}

    def flaky(prompt: str) -> str:
        if "概念變更驗證器" in prompt:
            state["corroborations"] += 1
            if state["corroborations"] == 1:
                raise RuntimeError("暫時性故障")
            return json.dumps({"approve": True, "confidence": 0.9, "reason": "ok"})
        return "[]"

    inh = ConceptInhibitor(semantic=e.semantic, llm_fn=flaky)
    c = e.semantic.get_by_name("小橘")
    tx = inh.process(
        ConceptVerification(assessments=[ConceptAssessment(
            concept_id=c.id, concept_name="小橘", status="modify",
            reason="使用者更正", suggested_update="使用者養的公貓", relevance=0.9,
        )]),
        user_input="小橘是公的", llm_response="了解", session_id="s", turn_id="t1",
    )
    held_id = tx.patches[0].patch_id
    assert tx.patches[0].status == "held"  # 前提
    assert e.semantic.get_by_name("小橘").description == "使用者養的貓"
    # 後續 3 輪正常對話（各自有強證據 expand，會走完整 pipeline）
    for i in range(3):
        inh.process(
            ConceptVerification(assessments=[ConceptAssessment(
                concept_id=e.semantic.get_by_name("手沖咖啡").id, concept_name="手沖咖啡",
                status="expand", reason=f"第 {i} 輪補充資訊", suggested_update=f"fact {i}",
                relevance=0.9,
            )]),
            user_input="x", llm_response="y", session_id="s", turn_id=f"t{i + 2}",
        )
    return e, inh, held_id


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "缺陷 c：concept_inhibitor.py _evaluate_patch 將 patch 設為 held 後，held 只被寫進 "
    "audit_log.jsonl；全 repo 沒有任何程式讀回 held 重審（grep held 僅見賦值與 Entry.py 的計數顯示）。"
    "LLM 恢復後 patch 仍永遠 held。"
))
def test_c_held_patch_is_eventually_reviewed(held_then_llm_recovers):
    e, _inh, held_id = held_then_llm_recovers
    assert held_id not in _unresolved_held_patch_ids(tx_audit(e)), "held patch 永久懸置"


# ═══════════════════════════════════════════════════════════════════════════
# d) 自動來源繞過 Patch Gate 直接寫入
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def dreamed_default_engine(restored_engine):
    """預設 engine（沒有注入 inhibitor，例如 MCP server 的用法）跑 dream()。"""
    e = restored_engine
    assert e._concept_inhibitor is None  # 前提：預設就是 None
    before = len(tx_audit(e))
    e.set_llm_fn(make_fake_llm(distill_concepts=DISTILLED))
    report = e.dream(triggered_by="manual")
    assert report["error"] is None
    assert report["distillation"]["concepts_created"] == 1  # 前提：概念確實寫入了
    assert e.semantic.get_by_name("家人關係") is not None
    return e, before


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "缺陷 d1：engine.py 預設 _concept_inhibitor=None（只有 Entry.py 注入）-> "
    "DreamEngine -> DreamPatchCoordinator(inhibitor=None).submit_distillation_concept 回 (None, None) "
    "-> distillation._ingest_concept 退回 semantic.upsert_concept 直寫；"
    "audit_log.jsonl 沒有任何 PatchTransaction。"
))
def test_d1_default_engine_dream_writes_through_patch_gate(dreamed_default_engine):
    e, before = dreamed_default_engine
    rows = [r for r in tx_audit(e)[before:] if r["triggered_by"] == "dream"]
    assert rows, "Dream 蒸餾寫入 Concept 卻沒有 PatchTransaction audit"


@pytest.fixture
def legacy_extracted(restored_engine):
    e = restored_engine
    before = len(tx_audit(e))
    ex = ConceptExtractor(
        llm_fn=make_fake_llm(extract_items=[{"noun": "天氣", "desc": "今天的天氣很好"}]),
        semantic=e.semantic, graph=e.episodic.graph,
        embed_fn=lambda t: e.episodic._generate_embedding(t),
        inhibitor=None,
    )
    try:
        ex.extract_in_background(e.episodic.get("ep_base_009"))
        ex.flush(timeout=10)
    finally:
        ex.shutdown()
    assert e.semantic.get_by_name("天氣") is not None  # 前提：概念確實寫入
    return e, before


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "缺陷 d2：concept_extractor.py inhibitor=None 時走 _upsert_legacy，直接 semantic.upsert_concept，"
    "同義詞分支甚至直接改 best_match.source_episode_ids 並呼叫 semantic._save()；無 audit。"
))
def test_d2_extractor_without_inhibitor_still_audited(legacy_extracted):
    e, before = legacy_extracted
    assert [r for r in tx_audit(e)[before:] if r["triggered_by"] == "extractor"]


@pytest.fixture
def legacy_reflection_created(restored_engine):
    e = restored_engine
    before = len(tx_audit(e))
    executor = ReflectionExecutor(engine=e, concept_inhibitor=None)
    result = executor.build_executor_fn()("create_concept", "睡眠品質", {"description": "d"})
    assert result["name"] == "睡眠品質"
    assert e.semantic.get_by_name("睡眠品質") is not None  # 前提
    return e, before


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "缺陷 d3：reflection_executor.py _exec_create_concept/_exec_tag_concept/_exec_merge_concepts "
    "在 inhibitor=None 時退回 semantic.upsert_concept / remove_concept 直寫，無 audit。"
))
def test_d3_reflection_without_inhibitor_still_audited(legacy_reflection_created):
    e, before = legacy_reflection_created
    assert [r for r in tx_audit(e)[before:] if r["triggered_by"] == "reflection"]


@pytest.fixture
def dreamed_with_inhibitor(restored_engine):
    e = restored_engine
    e._concept_inhibitor = ConceptInhibitor(semantic=e.semantic, llm_fn=make_fake_llm())
    e.set_llm_fn(make_fake_llm(distill_concepts=DISTILLED))
    report = e.dream(triggered_by="manual")
    assert report["distillation"]["concepts_created"] == 1
    c = e.semantic.get_by_name("家人關係")
    assert c.subject == "user"  # 前提：subject 確實被寫入
    return e, c


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "缺陷 d4：即使 Inhibitor 在場，distillation._ingest_concept 仍在 patch pipeline 之後呼叫 "
    "semantic.set_subject()（semantic.py 註解自承「subject 不穿那四層」）-> "
    "subject 變更沒有 patch / audit / rollback 軌跡。"
))
def test_d4_subject_assignment_is_recorded_in_patch_trail(dreamed_with_inhibitor):
    e, c = dreamed_with_inhibitor
    recorded = any(
        p["target_path"] == "subject" or "subject" in json.dumps(p["new_value"], ensure_ascii=False)
        for tx in tx_audit(e) for p in tx["patches"] if p["concept_name"] == c.name
    )
    assert recorded, "subject 寫入未進入任何 PatchTransaction"


@pytest.fixture
def created_then_rolled_back(restored_engine, tmp_path):
    e = restored_engine
    inh = ConceptInhibitor(semantic=e.semantic, llm_fn=make_fake_llm())
    executor = ReflectionExecutor(engine=e, concept_inhibitor=inh)
    queue = ReflectionActionQueue(audit_path=tmp_path / "reflection_audit.jsonl")
    params = {"description": "使用者的睡眠品質"}
    act = queue.enqueue("create_concept", "睡眠品質", params, "x")
    act.pre_state = executor.capture_pre_state("create_concept", "睡眠品質", params) or {"already_exists": False}
    assert queue.confirm(act.id)
    assert queue.execute(act, executor.build_executor_fn()), act.error
    after_create = len(tx_audit(e))
    assert queue.rollback(act.id, executor.build_rollback_fn())
    assert e.semantic.get_by_name("睡眠品質") is None  # 前提：回滾確實刪了概念
    return e, after_create


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "缺陷 d5：reflection_executor.py build_rollback_fn 的 create_concept / tag_concept 分支"
    "直接呼叫 semantic.remove_concept / upsert_concept，繞過 Inhibitor；"
    "回滾造成的 Concept 變更沒有對應 PatchTransaction。"
))
def test_d5_reflection_rollback_goes_through_patch_gate(created_then_rolled_back):
    e, after_create = created_then_rolled_back
    assert len(tx_audit(e)) > after_create, "回滾刪除概念卻沒有 audit"


# ═══════════════════════════════════════════════════════════════════════════
# e1) 額外發現：patch apply 失敗時自身半套變更殘留
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def failed_modify_after_expand(restored_engine):
    e = restored_engine
    inh = ConceptInhibitor(semantic=e.semantic, llm_fn=make_fake_llm())

    def boom(_text):
        raise RuntimeError("embed 失敗")

    e.semantic._embed_fn = boom
    tx = inh.process(
        ConceptVerification(assessments=[
            ConceptAssessment(
                concept_id=e.semantic.get_by_name("手沖咖啡").id, concept_name="手沖咖啡",
                status="expand", reason="新資訊足夠長的理由說明文字", suggested_update="fact",
                relevance=0.9,
            ),
            ConceptAssessment(
                concept_id=e.semantic.get_by_name("小橘").id, concept_name="小橘",
                status="modify", reason="修正", suggested_update="使用者養的公貓", relevance=0.9,
            ),
        ]),
        user_input="x", llm_response="y", session_id="s", turn_id="t",
    )
    assert tx.status == "failed"  # 前提：transaction 回報失敗
    assert [p.status for p in tx.patches] == ["rolled_back", "rejected"]
    return e


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "額外發現 e1：concept_inhibitor._apply_modify 先改 concept.description 才重算 embedding；"
    "embedding 失敗時 patch 被標 rejected、_apply_patch 回 False，但沒有還原已改的 description；"
    "隨後 rollback 其他 patch 的 _save() 會把半套變更一起寫入磁碟。tx 說失敗了，資料卻已變。"
))
def test_e1_failed_modify_leaves_no_partial_mutation(failed_modify_after_expand):
    e = failed_modify_after_expand
    assert e.semantic.get_by_name("小橘").description == "使用者養的貓"
    # 重開（磁碟狀態）也要乾淨
    from echo_memory.engine import MemoryEngine
    reopened = MemoryEngine(storage_path=e.storage_path)
    assert reopened.semantic.get_by_name("小橘").description == "使用者養的貓"
