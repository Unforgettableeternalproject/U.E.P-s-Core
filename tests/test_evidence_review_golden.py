"""
tests/test_evidence_review_golden.py — M4 Golden 驗收（包 B 範圍：G1–G5、G9）

G6（版本綁定）、G7（側向抑制）、G8（相容）屬包 A／C。
G2 的「預設檢索只回 B、include_history 才回 A」屬包 C 的檢索層，這裡只驗證資料層結果
（A deprecated + replaced_by、無 CONTRADICTS 邊、conflict_count 不增、rollback 恢復）。
G4 的 held 重審由審核器自管的待審登記簿承擔（Gate 的 review_held_patches 無法重審
尚無內容的 review）。
全部使用假 llm_fn，不載入真模型。
"""

from __future__ import annotations

import ast
import json
from datetime import timedelta
from pathlib import Path

import pytest

from echo_memory.affect.concept_inhibitor import ConceptInhibitor
from echo_memory.dream.contradiction import ContradictionDetector
from echo_memory.dream.patch_coordinator import DreamPatchCoordinator
from echo_memory.engine import MemoryEngine
from echo_memory.schemas.evidence import EvidenceInput
from echo_memory.storage.conflict_ledger import ConflictLedger
from echo_memory.storage.locking import storage_lock
from tests.test_evidence_reviewer import (
    T0,
    agent_ep,
    audits,
    make_concept,
    make_reviewer,
    reply,
    unit_ids,
    user_ep,
)

ROOT = Path(__file__).resolve().parent.parent
APPROVE = json.dumps({"approve": True, "confidence": 0.9, "reason": "偏好確實隨時間更正"})
REJECT = json.dumps({"approve": False, "confidence": 0.9, "reason": "證據不足"})


@pytest.fixture
def engine(tmp_path: Path) -> MemoryEngine:
    return MemoryEngine(storage_path=tmp_path / "data", namespace_id="ns1")


@pytest.fixture
def gate(engine) -> ConceptInhibitor:
    return ConceptInhibitor(semantic=engine.semantic, llm_fn=None)


def _relation_llm(relation: str, newer: str | None = None, calls: list | None = None):
    def llm(prompt: str) -> str:
        if calls is not None:
            calls.append(prompt)
        return json.dumps({
            "relation": relation, "newer": newer, "strength": 0.8,
            "scope_note": "不同情境", "reason": f"測試 {relation}",
        }, ensure_ascii=False)

    return llm


def _detector(engine, detector_llm, gate_reply: str | None = APPROVE, provenance=True):
    """組出矛盾偵測器；gate_reply=None 表示 Gate 沒有 LLM。"""
    gate_llm = None if gate_reply is None else (lambda prompt: gate_reply)
    gate = ConceptInhibitor(semantic=engine.semantic, llm_fn=gate_llm)
    coordinator = DreamPatchCoordinator(inhibitor=gate)
    graph = engine.episodic.graph
    ledger = ConflictLedger(graph.storage_path)
    detector = ContradictionDetector(
        semantic=engine.semantic,
        graph=graph,
        similarity_threshold=0.5,
        llm_fn=detector_llm,
        conflict_ledger=ledger,
        guard=lambda: storage_lock(engine.storage_path),
        patch_coordinator=coordinator,
        provenance=engine._provenance_resolver() if provenance else None,
    )
    return detector, coordinator, ledger, gate


def _same_embedding(engine, *cids: str) -> None:
    for cid in cids:
        engine.semantic.get(cid).embedding = [1.0, 0.0]


def _pref_pair(engine, gate, *, newer_is_user=True, newer_later=True):
    """A（舊偏好）與 B（新偏好）；回傳 (a_id, b_id)。"""
    t_old, t_new = (T0, T0 + timedelta(days=1)) if newer_later else (T0 + timedelta(days=1), T0)
    ep_a = user_ep(engine, "我喜歡 A 牌子", at=t_old)
    a = make_concept(engine, gate, "喜歡A", [ep_a], desc="使用者喜歡 A 牌子")
    if newer_is_user:
        ep_b = user_ep(engine, "我改喜歡 B 牌子了", at=t_new)
        b = make_concept(engine, gate, "喜歡B", [ep_b], desc="使用者喜歡 B 牌子")
    else:
        ep_b = agent_ep(engine, "你應該改喜歡 B 牌子", at=t_new)
        b = make_concept(
            engine, gate, "喜歡B", [ep_b], desc="使用者喜歡 B 牌子", support_source="agent_only",
        )
    _same_embedding(engine, a, b)
    return a, b


# ━━━ G1：使用者陳述與 agent 推測分離 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestG1UserVsAgent:
    def test_agent_guess_is_capped_and_user_statement_lifts_it(self, engine, gate):
        ep_agent = agent_ep(engine, "你可能喜歡咖啡")
        cid = make_concept(
            engine, gate, "咖啡", [ep_agent], desc="使用者喜歡咖啡", support_source="agent_only",
        )
        agent_uid = unit_ids(engine, ep_agent)[0]
        out = make_reviewer(engine, gate, lambda p: reply(cited=[agent_uid])).review(cid)
        concept = engine.semantic.get(cid)
        assert out.status == "insufficient"
        assert concept.confidence <= 0.45 + 1e-9
        assert concept.metadata["support_source"] == "agent_only"
        assert "咖啡" not in engine.get_profile()  # agent_only 不進 profile

        # 使用者後來明說：同名概念補上使用者來源 -> support_source 升為 user
        ep_user = user_ep(engine, "我就是喜歡咖啡")
        user_uid = unit_ids(engine, ep_user)[0]
        cid2, _tx = gate.process_extractor_patch(
            noun="咖啡", desc="使用者喜歡咖啡", episode_id=ep_user,
            evidence_kind="distillation", triggered_by="dream",
            source_episode_ids=[ep_user], subject="user", support_source="user",
        )
        concept = engine.semantic.get_by_name("咖啡")
        assert concept.metadata["support_source"] == "user"
        out2 = make_reviewer(
            engine, gate, lambda p: reply(cited=[user_uid]),
        ).review(concept.id)
        assert out2.status == "supported"
        assert engine.semantic.get(concept.id).effective_review_status() == "supported"
        assert cid2 is not None


# ━━━ G2：偏好更正保留歷史 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestG2Supersede:
    def test_supersede_keeps_history_without_conflict_edge(self, engine, gate):
        a, b = _pref_pair(engine, gate)
        detector, coordinator, ledger, _g = _detector(engine, _relation_llm("supersedes", "a"))
        # concept=B（新）-> prompt 中的概念 A 是 B，newer="a"
        edges = detector.detect_and_register(engine.semantic.get(b))

        assert edges == []
        result = detector.last_results[0]
        assert result.relation == "supersedes" and result.action == "superseded"
        old, new = engine.semantic.get(a), engine.semantic.get(b)
        assert old is not None  # 歷史保留，仍可 get
        assert old.status == "deprecated"
        assert old.metadata["replaced_by"] == b
        assert old.metadata["deprecated_reason"] == "superseded"
        assert old.metadata["supersession"]["by"] == b
        assert new.status == "active"
        assert not engine.episodic.graph.has_contradicts_edge(a, b)
        assert old.conflict_count == 0 and new.conflict_count == 0
        assert ledger.find_between(a, b).status == "temporal_evolution"

        # rollback 後 A 恢復
        coordinator.rollback_supersede(result.transaction)
        restored = engine.semantic.get(a)
        assert restored.status == "active"
        assert "replaced_by" not in restored.metadata
        assert "supersession" not in restored.metadata

    def test_supersede_audited(self, engine, gate):
        a, b = _pref_pair(engine, gate)
        detector, _c, _l, g = _detector(engine, _relation_llm("supersedes", "a"))
        detector.detect_and_register(engine.semantic.get(b))
        deprecates = [
            p for tx in audits_of(g) for p in tx["patches"] if p["action"] == "deprecate"
        ]
        assert deprecates and deprecates[-1]["status"] == "applied"
        assert deprecates[-1]["new_value"]["replaced_by"] == b

    def test_agent_cannot_supersede_user_statement(self, engine, gate):
        a, b = _pref_pair(engine, gate, newer_is_user=False)
        detector, _c, _l, _g = _detector(engine, _relation_llm("supersedes", "a"))
        edges = detector.detect_and_register(engine.semantic.get(b))
        result = detector.last_results[0]
        assert result.relation == "contradicts" and "newer_has_no_user_claim" in result.reason
        assert engine.semantic.get(a).status == "active"
        assert len(edges) == 1 and engine.episodic.graph.has_contradicts_edge(a, b)

    def test_older_cannot_supersede_newer_by_time(self, engine, gate):
        a, b = _pref_pair(engine, gate, newer_later=False)  # B 其實比 A 早
        detector, _c, _l, _g = _detector(engine, _relation_llm("supersedes", "a"))
        edges = detector.detect_and_register(engine.semantic.get(b))
        assert detector.last_results[0].relation == "contradicts"
        assert "newer_not_later" in detector.last_results[0].reason
        assert engine.semantic.get(a).status == "active" and len(edges) == 1

    def test_no_time_information_never_supersedes(self, engine, gate):
        a, b = _pref_pair(engine, gate)
        detector, _c, _l, _g = _detector(
            engine, _relation_llm("supersedes", "a"), provenance=False,
        )
        detector.detect_and_register(engine.semantic.get(b))
        assert detector.last_results[0].relation == "contradicts"
        assert engine.semantic.get(a).status == "active"

    def test_supersede_without_newer_label_downgrades(self, engine, gate):
        a, b = _pref_pair(engine, gate)
        detector, _c, _l, _g = _detector(engine, _relation_llm("supersedes", None))
        detector.detect_and_register(engine.semantic.get(b))
        assert detector.last_results[0].relation == "contradicts"
        assert engine.semantic.get(a).status == "active"

    def test_gate_rejection_changes_nothing(self, engine, gate):
        a, b = _pref_pair(engine, gate)
        detector, _c, _l, _g = _detector(
            engine, _relation_llm("supersedes", "a"), gate_reply=REJECT,
        )
        edges = detector.detect_and_register(engine.semantic.get(b))
        assert edges == [] and detector.last_results[0].action == "supersede_rejected"
        assert engine.semantic.get(a).status == "active"
        assert not engine.episodic.graph.has_contradicts_edge(a, b)
        assert engine.semantic.get(a).conflict_count == 0

    def test_gate_without_llm_holds_for_review(self, engine, gate):
        a, b = _pref_pair(engine, gate)
        detector, _c, _l, g = _detector(
            engine, _relation_llm("supersedes", "a"), gate_reply=None,
        )
        detector.detect_and_register(engine.semantic.get(b))
        assert detector.last_results[0].action == "supersede_held"
        assert engine.semantic.get(a).status == "active"
        assert g.held_count() == 1

    def test_reevaluate_clears_existing_marks_and_edge(self, engine, gate):
        a, b = _pref_pair(engine, gate)
        # 先前被判為 contradicts：邊與雙方標記都存在
        first, coordinator, ledger, g = _detector(engine, _relation_llm("contradicts"))
        assert len(first.detect_and_register(engine.semantic.get(b))) == 1
        assert engine.semantic.get(a).conflict_count == 1
        assert engine.semantic.get(b).conflict_count == 1

        # 之後重新判斷為 supersedes（concept_a=B 較新）
        detector = ContradictionDetector(
            semantic=engine.semantic, graph=engine.episodic.graph,
            llm_fn=_relation_llm("supersedes", "a"), conflict_ledger=ledger,
            guard=lambda: storage_lock(engine.storage_path),
            patch_coordinator=DreamPatchCoordinator(
                inhibitor=ConceptInhibitor(semantic=engine.semantic, llm_fn=lambda p: APPROVE),
            ),
            provenance=engine._provenance_resolver(),
        )
        result = detector.reevaluate_pair(b, a)
        assert result.action == "superseded"
        assert not engine.episodic.graph.has_contradicts_edge(a, b)
        assert engine.semantic.get(a).conflict_count == 0
        assert engine.semantic.get(b).conflict_count == 0
        assert "has_contradiction" not in engine.semantic.get(a).metadata
        assert ledger.find_between(a, b).status == "temporal_evolution"

        # rollback：A 恢復 active，被扣回的標記補回
        detector._get_patch_coordinator().rollback_supersede(result.transaction)
        assert engine.semantic.get(a).status == "active"
        assert engine.semantic.get(a).conflict_count == 1
        assert engine.semantic.get(b).conflict_count == 1


def audits_of(gate: ConceptInhibitor) -> list[dict]:
    path = gate._semantic.storage_path / "audit_log.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# ━━━ G3：Dream 缺來源不 verified ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestG3NoVerifiedWithoutSource:
    def test_legacy_distilled_concept_forced_insufficient(self, engine, gate):
        legacy_ep = engine.store_episode("我喜歡咖啡", "好", "s1")  # legacy_unknown
        cid = make_concept(engine, gate, "咖啡", [legacy_ep], support_source="legacy_unknown")
        cited = engine.resolve_provenance(cid).evidence_ids
        out = make_reviewer(engine, gate, lambda p: reply(cited=cited)).review(cid)
        assert out.status == "insufficient"
        assert engine.semantic.get(cid).last_verified_at is None

    def test_unresolvable_provenance_is_insufficient(self, engine, gate):
        cid = make_concept_with_missing_source(gate)
        out = make_reviewer(engine, gate, lambda p: reply(status="supported")).review(cid)
        assert out.outcome == "reviewed" and out.status == "insufficient"
        assert engine.semantic.get(cid).last_verified_at is None

    def test_no_llm_stays_not_checked(self, engine, gate):
        legacy_ep = engine.store_episode("我喜歡咖啡", "好", "s1")
        cid = make_concept(engine, gate, "咖啡", [legacy_ep], support_source="legacy_unknown")
        out = make_reviewer(engine, gate, None).review(cid)
        assert out.outcome == "skipped"
        concept = engine.semantic.get(cid)
        assert concept.effective_review_status() == "not_checked"
        assert concept.last_verified_at is None


def make_concept_with_missing_source(gate: ConceptInhibitor) -> str:
    cid, _tx = gate.process_extractor_patch(
        noun="幽靈", desc="沒有來源的概念", episode_id="ep_missing",
        evidence_kind="distillation", triggered_by="dream",
        source_episode_ids=["ep_missing"], subject="user", support_source="user",
    )
    assert cid is not None
    return cid


# ━━━ G4：LLM 審核中斷進 held 可重審 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestG4Held:
    def _setup(self, engine, gate):
        ep = user_ep(engine, "我喜歡咖啡")
        cid = make_concept(engine, gate, "咖啡", [ep])
        return cid, unit_ids(engine, ep)[0]

    def test_exception_held_then_recovers(self, engine, gate):
        cid, uid = self._setup(engine, gate)
        state = {"down": True}

        def llm(p):
            if state["down"]:
                raise RuntimeError("down")
            return reply(cited=[uid])

        reviewer = make_reviewer(engine, gate, llm)
        assert reviewer.review(cid).outcome == "held"
        assert reviewer.pending_ids() == [cid]  # 在登記簿
        assert any(
            p["status"] == "held" for tx in audits(engine) for p in tx["patches"]
            if p["action"] == "review"
        )
        assert engine.semantic.get(cid).effective_review_status() == "not_checked"

        state["down"] = False
        assert reviewer.review_batch().reviewed == 1  # 下次 Dream／批次重審
        assert engine.semantic.get(cid).effective_review_status() == "supported"
        assert reviewer.pending_ids() == []

    def test_description_changed_while_held_is_rereviewed_on_new_basis(self, engine, gate):
        cid, uid = self._setup(engine, gate)
        reviewer = make_reviewer(engine, gate, lambda p: "壞掉")
        assert reviewer.review(cid).outcome == "held"
        old_digest = reviewer._load_pending()[cid]["digest"]

        engine.semantic.get(cid).description = "使用者喜歡手沖咖啡"
        reviewer._llm_fn = lambda p: reply(cited=[uid])
        # 舊待審記錄對應舊內容；批次把它當新內容重審，審核綁定的是新 digest
        assert reviewer.review_batch().reviewed == 1
        review = engine.semantic.get(cid).evidence_review
        assert review["basis_digest"] != old_digest
        assert engine.semantic.get(cid).effective_review_status() == "supported"
        assert reviewer.pending_ids() == []

    def test_stale_review_never_written(self, engine, gate):
        cid, uid = self._setup(engine, gate)

        def llm(p):
            engine.semantic.get(cid).description = "使用者改口了"
            return reply(cited=[uid])

        assert make_reviewer(engine, gate, llm).review(cid).outcome == "stale"
        assert engine.semantic.get(cid).evidence_review == {}

    def test_over_retry_limit_is_rejected_with_audit(self, engine, gate):
        cid, _uid = self._setup(engine, gate)
        reviewer = make_reviewer(engine, gate, lambda p: "壞掉", max_attempts=2)
        reviewer.review(cid)
        reviewer.review(cid)
        assert reviewer.pending_ids() == []
        assert reviewer._load_pending()[cid]["abandoned"] is True
        rejected = [
            p for tx in audits(engine) for p in tx["patches"]
            if p["action"] == "review" and p["status"] == "rejected"
        ]
        assert rejected and "上限" in rejected[-1]["rejection_reason"]


# ━━━ G5：主張範圍 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestG5Scope:
    def _pair(self, engine, gate, subject_b="user"):
        ep_a = user_ep(engine, "我不吃香菜", at=T0)
        ep_b = user_ep(engine, "我朋友很愛香菜", at=T0 + timedelta(hours=1))
        a = make_concept(engine, gate, "不吃香菜", [ep_a], subject="user")
        b = make_concept(engine, gate, "愛香菜", [ep_b], subject=subject_b)
        _same_embedding(engine, a, b)
        return a, b

    def test_different_subject_pair_is_not_sent_to_llm(self, engine, gate):
        a, b = self._pair(engine, gate, subject_b="world")
        calls: list[str] = []
        detector, _c, _l, _g = _detector(engine, _relation_llm("contradicts", calls=calls))
        assert detector.detect_and_register(engine.semantic.get(b)) == []
        assert calls == []

    def test_different_scope_builds_no_edge_and_is_deduped(self, engine, gate):
        a, b = self._pair(engine, gate)
        calls: list[str] = []
        detector, _c, ledger, _g = _detector(engine, _relation_llm("different_scope", calls=calls))
        assert detector.detect_and_register(engine.semantic.get(b)) == []
        assert detector.last_results[0].action == "scope_recorded"
        assert not engine.episodic.graph.has_contradicts_edge(a, b)
        assert engine.semantic.get(a).conflict_count == 0
        assert engine.semantic.get(b).conflict_count == 0
        record = ledger.find_between(a, b)
        assert record.status == "accepted_dual_truth"
        assert record.metadata["relation"] == "different_scope"

        # 同一對、內容不變：下個週期不再送審
        detector.detect_and_register(engine.semantic.get(b))
        assert len(calls) == 1
        # 內容變動後（digest 改變）才重新送審
        engine.semantic.get(b).description = "朋友愛香菜，而且常吃"
        detector.detect_and_register(engine.semantic.get(b))
        assert len(calls) == 2

    def test_true_contradiction_builds_edge(self, engine, gate):
        a, b = self._pair(engine, gate)
        detector, _c, ledger, _g = _detector(engine, _relation_llm("contradicts"))
        edges = detector.detect_and_register(engine.semantic.get(b))
        assert len(edges) == 1 and engine.episodic.graph.has_contradicts_edge(a, b)
        assert engine.semantic.get(a).conflict_count == 1
        assert ledger.find_between(a, b).status == "unresolved"

    def test_compatible_and_unrelated_do_nothing(self, engine, gate):
        a, b = self._pair(engine, gate)
        for relation in ("compatible", "unrelated"):
            detector, _c, ledger, _g = _detector(engine, _relation_llm(relation))
            assert detector.detect_and_register(engine.semantic.get(b)) == []
            assert not engine.episodic.graph.has_contradicts_edge(a, b)
            assert ledger.find_between(a, b) is None

    def test_legacy_is_contradiction_format_still_works(self, engine, gate):
        a, b = self._pair(engine, gate)
        legacy = lambda p: json.dumps({"is_contradiction": True, "strength": 0.9, "reason": "x"})  # noqa: E731
        detector, _c, _l, _g = _detector(engine, legacy)
        assert len(detector.detect_and_register(engine.semantic.get(b))) == 1


# ━━━ G9：架構禁令 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_DECISION_MODULES = [
    "echo_memory/affect/evidence_reviewer.py",
    "echo_memory/dream/contradiction.py",
    "echo_memory/retrieval/standing.py",
    # M6 Self Model／事件契約
    "echo_memory/schemas/events.py",
    "echo_memory/affect/event_emitter.py",
    "echo_memory/affect/self_model_gate.py",
]
# 這些函式只做 LLM 回應的 JSON 抽取，不做記憶決策
_REGEX_ALLOWED_FUNCS = {"_parse_llm_response"}
_RE_FUNCS = {"compile", "match", "search", "findall", "finditer", "fullmatch", "sub", "split"}


def _has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


class TestG9ArchitectureBan:
    @pytest.mark.parametrize("rel", _DECISION_MODULES)
    def test_no_regex_or_keyword_lists_in_decision_code(self, rel):
        path = ROOT / rel
        if not path.exists():
            pytest.skip(f"{rel} 尚未存在")
        tree = ast.parse(path.read_text(encoding="utf-8"))

        offenders: list[str] = []
        for func in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            if func.name in _REGEX_ALLOWED_FUNCS:
                continue
            for node in ast.walk(func):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "re"
                    and node.func.attr in _RE_FUNCS
                ):
                    offenders.append(f"{func.name}: re.{node.func.attr}")
        # 模組層級／類別層級：不得用 re，也不得有中文關鍵字清單
        for node in ast.walk(tree):
            if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                strs = [
                    e.value for e in node.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                ]
                if len(strs) >= 2 and any(_has_cjk(s) for s in strs):
                    offenders.append(f"中文關鍵字清單: {strs[:3]}")
        assert offenders == []

    @pytest.mark.parametrize("rel", _DECISION_MODULES)
    def test_salience_is_not_a_gate(self, rel):
        path = ROOT / rel
        if not path.exists():
            pytest.skip(f"{rel} 尚未存在")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
        } | {
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
        } | {
            a.arg for n in ast.walk(tree) if isinstance(n, ast.arguments)
            for a in [*n.args, *n.kwonlyargs]
        }
        assert not [n for n in names if "salience" in n.lower()]

    def test_review_ignores_episode_salience(self, engine, gate):
        outcomes = []
        for salience in (0.0, 1.0):
            ep = engine.store_episode(
                f"我喜歡咖啡{salience}", "", "s1", salience=salience,
                evidence=[EvidenceInput(
                    source_type="user", content=f"我喜歡咖啡{salience}", speaker_id="u1",
                    occurred_at=T0, attrs={"channel_kind": "dm"},
                )],
            )
            cid = make_concept(engine, gate, f"咖啡{salience}", [ep])
            uid = unit_ids(engine, ep)[0]
            outcomes.append(
                make_reviewer(engine, gate, lambda p, u=uid: reply(cited=[u])).review(cid).status
            )
        assert outcomes == ["supported", "supported"]


# ━━━ Gate 收編：process_supersede_patch／Dream 重判舊邊 ━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSupersedeGate:
    def test_audit_has_supersession_and_rollback_restores_everything(self, engine, gate):
        a, b = _pref_pair(engine, gate)
        # 先造出舊的互相矛盾標記與邊
        first, _c, ledger, _g = _detector(engine, _relation_llm("contradicts"))
        first.detect_and_register(engine.semantic.get(b))
        detector = ContradictionDetector(
            semantic=engine.semantic, graph=engine.episodic.graph,
            llm_fn=_relation_llm("supersedes", "a"), conflict_ledger=ledger,
            guard=lambda: storage_lock(engine.storage_path),
            patch_coordinator=DreamPatchCoordinator(
                inhibitor=ConceptInhibitor(semantic=engine.semantic, llm_fn=lambda p: APPROVE),
            ),
            provenance=engine._provenance_resolver(),
        )
        result = detector.reevaluate_pair(b, a)
        assert result.action == "superseded"
        old = engine.semantic.get(a)
        assert old.conflict_count == 0 and old.metadata["supersession"]["by"] == b

        rows = [
            p for tx in audits_of(detector._get_patch_coordinator().inhibitor)
            for p in tx["patches"] if p["action"] == "deprecate" and p["status"] == "applied"
        ]
        assert rows
        nv = rows[-1]["new_value"]
        assert nv["supersession"]["by"] == b and nv["replaced_by"] == b
        assert nv["clear_conflict_marks"] == [a, b]
        assert rows[-1]["rollback_payload"]["cleared_conflict_marks"] == [a, b]

        cleared = detector._get_patch_coordinator().rollback_supersede(result.transaction)
        assert cleared == [a, b]
        old, new = engine.semantic.get(a), engine.semantic.get(b)
        assert old.status == "active"
        assert not {"replaced_by", "supersession", "deprecated_reason"} & set(old.metadata)
        assert old.conflict_count == 1 and new.conflict_count == 1
        assert old.metadata["has_contradiction"] is True

    def test_coordinator_does_not_touch_gate_private_methods(self):
        src = (ROOT / "echo_memory/dream/patch_coordinator.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        private = {
            "_evaluate_patch", "_register_held", "_try_write_audit",
            "_apply_patch", "_rollback_patch", "_write_audit", "_semantic",
        }
        used = {
            n.attr for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and n.attr in private
        }
        # _get_audit_path 為取 storage_path 讀 semantic 的既有用法，其餘不得出現
        assert used <= {"_semantic"}
        assert "_semantic" not in src.split("def _get_audit_path")[0]


class TestDreamReevaluation:
    def _dream_llm(self, calls, relation="supersedes"):
        def llm(p: str) -> str:
            if "知識一致性檢驗員" in p:
                calls.append(p)
                line_a = [ln for ln in p.splitlines() if ln.startswith("概念 A")][0]
                newer = "a" if "喜歡B" in line_a else "b"
                return json.dumps({"relation": relation, "newer": newer, "strength": 0.8,
                                   "scope_note": "", "reason": "後來改口"}, ensure_ascii=False)
            if "概念變更驗證器" in p:
                return APPROVE
            return "{}"
        return llm

    def _engine(self, engine, gate, llm):
        from echo_memory.dream.engine import DreamEngine
        return DreamEngine(
            graph=engine.episodic.graph, episodic=engine.episodic, semantic=engine.semantic,
            llm_fn=llm, concept_inhibitor=ConceptInhibitor(semantic=engine.semantic, llm_fn=llm),
            enable_idle=False, enable_cron=False,
            lock_factory=lambda: storage_lock(engine.storage_path),
            evidence_store=engine.evidence,
        )

    def _old_edge(self, engine, gate):
        a, b = _pref_pair(engine, gate)
        first, _c, _l, _g = _detector(engine, _relation_llm("contradicts"))
        assert len(first.detect_and_register(engine.semantic.get(b))) == 1
        # 相似度壓在 LATENT_BRIDGE 門檻（0.85）以下：pruning 目前會把 >=0.85 配對的
        # CONTRADICTS 邊覆寫成 LATENT_BRIDGE（pruning.py 既有問題，非本包範圍）
        engine.semantic.get(b).embedding = [0.8, 0.6]
        return a, b

    def test_dream_reevaluates_old_edge_and_supersedes(self, engine, gate):
        a, b = self._old_edge(engine, gate)
        calls: list[str] = []
        report = self._engine(engine, gate, self._dream_llm(calls)).dream_now("manual")
        assert report.error is None
        assert report.edges_reevaluated == 1 and report.edges_superseded == 1
        assert engine.semantic.get(a).status == "deprecated"
        assert engine.semantic.get(a).conflict_count == 0
        assert not engine.episodic.graph.has_contradicts_edge(a, b)
        assert "矛盾重判" in report.summary()

    def test_unchanged_pair_is_deduped_across_cycles(self, engine, gate):
        a, b = self._old_edge(engine, gate)
        calls: list[str] = []
        dream = self._engine(engine, gate, self._dream_llm(calls, relation="contradicts"))
        assert dream.dream_now("manual").edges_reevaluated == 1
        assert engine.episodic.graph.has_contradicts_edge(a, b)
        n = len(calls)
        assert dream.dream_now("manual").edges_reevaluated == 0
        assert len(calls) == n
        # 內容變動後才再重判
        engine.semantic.get(b).description = "使用者喜歡 B 牌子，而且很常買"
        assert dream.dream_now("manual").edges_reevaluated == 1

    def test_batch_limited_by_config(self, engine, gate):
        a, b = self._old_edge(engine, gate)
        calls: list[str] = []
        dream = self._engine(engine, gate, self._dream_llm(calls))
        from echo_memory.config import get_config
        cfg = get_config()
        old = cfg.contradiction_reevaluate_batch
        cfg.contradiction_reevaluate_batch = 0
        try:
            assert dream.dream_now("manual").edges_reevaluated == 0
        finally:
            cfg.contradiction_reevaluate_batch = old
        assert calls == []


# ━━━ 整合審查修補：完整 rollback／LLM 失敗不去重／held 套用收尾／Gate 鎖內前提 ━━━━━━━━


def _edge_weight(engine, a, b):
    g = engine.episodic.graph
    for src, dst in ((a, b), (b, a)):
        if g.graph.has_edge(src, dst):
            return g.graph[src][dst].get("weight")
    return None


def _supersede_detector(engine, ledger, gate_llm, relation_llm=None):
    gate = ConceptInhibitor(semantic=engine.semantic, llm_fn=gate_llm)
    det = ContradictionDetector(
        semantic=engine.semantic, graph=engine.episodic.graph,
        llm_fn=relation_llm or _relation_llm("supersedes", "a"), conflict_ledger=ledger,
        guard=lambda: storage_lock(engine.storage_path),
        patch_coordinator=DreamPatchCoordinator(inhibitor=gate),
        provenance=engine._provenance_resolver(),
    )
    return det, gate


class TestSupersedeFullRollback:
    def test_rollback_restores_edge_weight_marks_and_ledger(self, engine, gate):
        a, b = _pref_pair(engine, gate)
        first, _c, ledger, _g = _detector(engine, _relation_llm("contradicts"))
        first.detect_and_register(engine.semantic.get(b))
        weight = _edge_weight(engine, a, b)
        assert weight == pytest.approx(0.8)
        assert ledger.find_between(a, b).status == "unresolved"

        det, _gate = _supersede_detector(engine, ledger, lambda p: APPROVE)
        result = det.reevaluate_pair(b, a)
        assert result.action == "superseded"
        assert not engine.episodic.graph.has_contradicts_edge(a, b)
        assert ledger.find_between(a, b).status == "temporal_evolution"

        cleared = det.rollback_supersede(result)
        assert cleared == [a, b]
        assert engine.semantic.get(a).status == "active"
        assert engine.semantic.get(a).conflict_count == 1
        assert engine.semantic.get(b).conflict_count == 1
        assert engine.episodic.graph.has_contradicts_edge(a, b)
        assert _edge_weight(engine, a, b) == pytest.approx(weight)
        record = ledger.find_between(a, b)
        assert record.status == "unresolved" and record.resolved_at is None
        assert "pair_digest" not in record.metadata and "pre_resolution" not in record.metadata

    def test_rollback_of_fresh_supersede_removes_ledger_record(self, engine, gate):
        a, b = _pref_pair(engine, gate)
        ledger = ConflictLedger(engine.episodic.graph.storage_path)
        det, _gate = _supersede_detector(engine, ledger, lambda p: APPROVE)
        det.detect_and_register(engine.semantic.get(b))
        result = det.last_results[0]
        assert result.action == "superseded" and ledger.find_between(a, b) is not None
        det.rollback_supersede(result)
        assert engine.semantic.get(a).status == "active"
        assert ledger.find_between(a, b) is None
        assert not engine.episodic.graph.has_contradicts_edge(a, b)


class TestReevaluateDedupe:
    def _edge(self, engine, gate):
        a, b = _pref_pair(engine, gate)
        first, _c, ledger, _g = _detector(engine, _relation_llm("contradicts"))
        first.detect_and_register(engine.semantic.get(b))
        return a, b, ledger

    def _reevaluated(self, engine, det, ledger, a, b):
        return ledger.is_pair_reevaluated(
            a, b, det._pair_digest(engine.semantic.get(a), engine.semantic.get(b)),
        )

    def test_llm_failure_is_not_deduped(self, engine, gate):
        a, b, ledger = self._edge(engine, gate)

        def boom(p):
            raise RuntimeError("down")

        det, _g = _supersede_detector(engine, ledger, lambda p: APPROVE, relation_llm=boom)
        results = det.reevaluate_batch(limit=5)
        assert results[0].llm_failed is True
        assert not self._reevaluated(engine, det, ledger, a, b)

        garbage, _g2 = _supersede_detector(
            engine, ledger, lambda p: APPROVE, relation_llm=lambda p: "不是 JSON",
        )
        assert garbage.reevaluate_batch(limit=5)[0].llm_failed is True
        assert not self._reevaluated(engine, garbage, ledger, a, b)

        # LLM 恢復後會再被重判，這次才算判過
        ok, _g3 = _supersede_detector(
            engine, ledger, lambda p: APPROVE, relation_llm=_relation_llm("contradicts"),
        )
        assert len(ok.reevaluate_batch(limit=5)) == 1
        assert ok.reevaluate_batch(limit=5) == []

    def test_supersede_held_is_not_deduped(self, engine, gate):
        a, b, ledger = self._edge(engine, gate)
        det, g = _supersede_detector(engine, ledger, None)  # Gate 無 LLM -> held
        results = det.reevaluate_batch(limit=5)
        assert results[0].action == "supersede_held"
        assert not self._reevaluated(engine, det, ledger, a, b)
        assert g.held_count() == 1


class TestHeldSupersedeApplyPath:
    def test_generic_held_review_finishes_marks_and_edge(self, engine, gate):
        a, b = _pref_pair(engine, gate)
        first, _c, ledger, _g = _detector(engine, _relation_llm("contradicts"))
        first.detect_and_register(engine.semantic.get(b))
        assert getattr(engine.semantic, "_graph", None) is engine.episodic.graph

        det, held_gate = _supersede_detector(engine, ledger, None)
        assert det.reevaluate_pair(b, a).action == "supersede_held"
        assert engine.semantic.get(a).status == "active"

        held_gate._llm_fn = lambda p: APPROVE
        txs = held_gate.review_held_patches()
        assert txs and txs[0].patches[0].status == "applied"
        old = engine.semantic.get(a)
        assert old.status == "deprecated" and old.metadata["replaced_by"] == b
        assert old.metadata["supersession"]["by"] == b
        assert old.conflict_count == 0 and engine.semantic.get(b).conflict_count == 0
        assert not engine.episodic.graph.has_contradicts_edge(a, b)

        # 這條路徑套用的 supersede 一樣可完整 rollback（原 weight 補回）
        held_gate.rollback_supersede_patch(txs[0])
        assert engine.semantic.get(a).status == "active"
        assert engine.semantic.get(a).conflict_count == 1
        assert _edge_weight(engine, a, b) == pytest.approx(0.8)

    def test_held_supersede_is_stale_when_newer_no_longer_active(self, engine, gate):
        a, b = _pref_pair(engine, gate)
        ledger = ConflictLedger(engine.episodic.graph.storage_path)
        det, held_gate = _supersede_detector(engine, ledger, None)
        det.detect_and_register(engine.semantic.get(b))
        assert held_gate.held_count() == 1
        engine.semantic.get(b).status = "deprecated"  # 較新者已不 active
        held_gate._llm_fn = lambda p: APPROVE
        txs = held_gate.review_held_patches()
        assert txs and txs[0].patches[0].status == "rejected"
        assert engine.semantic.get(a).status == "active"


class TestGateLockedPreconditions:
    def test_supersede_rejected_when_basis_changed_or_verify_fails(self, engine, gate):
        a, b = _pref_pair(engine, gate)
        g = ConceptInhibitor(semantic=engine.semantic, llm_fn=lambda p: APPROVE)
        state, proposal = g.prepare_supersede_patch(a, b, "更正", {}, graph=engine.episodic.graph)
        assert state == "approved"
        engine.semantic.get(b).description = "使用者其實喜歡 C 牌子"  # 佐證期間改了
        ok, tx = g.process_supersede_patch(proposal)
        assert not ok and "stale" in tx.patches[0].rejection_reason
        assert engine.semantic.get(a).status == "active"

        engine.semantic.get(b).description = "使用者喜歡 B 牌子"
        state, proposal = g.prepare_supersede_patch(a, b, "更正", {})
        ok, tx = g.process_supersede_patch(
            proposal, verify_fn=lambda: (False, "newer_lost_user_claim"),
        )
        assert not ok and "newer_lost_user_claim" in tx.patches[0].rejection_reason
        assert engine.semantic.get(a).status == "active"

    def test_review_supported_below_threshold_rejected_but_corroborated_intention_ok(
        self, engine, gate,
    ):
        from echo_memory.schemas.evidence_review import EvidenceReview, compute_basis_digest

        ep = user_ep(engine, "我喜歡咖啡")
        cid = make_concept(engine, gate, "咖啡", [ep])
        c = engine.semantic.get(cid)
        low = EvidenceReview(
            status="supported", support_strength=0.5,
            basis_digest=compute_basis_digest(c), reviewer="llm",
        )
        ok, tx = gate.process_review_patch(cid, low)
        assert not ok and "門檻" in tx.patches[0].rejection_reason
        intention = EvidenceReview(
            status="supported", support_strength=0.5, claim_kind="intention",
            corroborated_by=["obs1"], basis_digest=compute_basis_digest(c), reviewer="llm",
        )
        ok, _tx = gate.process_review_patch(cid, intention)
        assert ok


class TestSearchConceptsFilter:
    def test_superseded_and_merged_hidden(self, engine, gate, mcp_handlers):
        import asyncio

        _handle_search_concepts = mcp_handlers._handle_search_concepts

        ep = user_ep(engine, "x")
        live = make_concept(engine, gate, "現行", [ep])
        old = make_concept(engine, gate, "被取代", [ep])
        merged = make_concept(engine, gate, "已合併", [ep])
        engine.semantic.get(old).status = "deprecated"
        engine.semantic.get(old).metadata["replaced_by"] = live
        engine.semantic.get(merged).metadata["merged_into"] = live
        engine.semantic.search_by_vector = lambda **kw: [
            (engine.semantic.get(c), 0.9) for c in (live, old, merged)
        ]
        engine.episodic.generate_query_embedding = lambda q: [0.0]
        text = asyncio.run(_handle_search_concepts(lambda ns: engine, {"query": "q"}))
        assert "現行" in text and "被取代" not in text and "已合併" not in text


# ━━━ 複審修補：圖持久化／held 套用後 ledger／pre_resolution／no_context 名額 ━━━━━━━━


def _reload_graph(engine):
    from echo_memory.storage.graph import MemoryGraph

    return MemoryGraph(storage_path=engine.episodic.graph.storage_path)


class TestGraphPersistence:
    def _old_edge(self, engine, gate):
        a, b = _pref_pair(engine, gate)
        first, _c, ledger, _g = _detector(engine, _relation_llm("contradicts"))
        first.detect_and_register(engine.semantic.get(b))
        engine.episodic.graph.save()  # 邊先落地，之後才能驗證「移除也落地」
        assert _reload_graph(engine).has_contradicts_edge(a, b)
        return a, b, ledger

    def test_held_supersede_applied_by_generic_review_persists_edge_removal(self, engine, gate):
        a, b, ledger = self._old_edge(engine, gate)
        det, held_gate = _supersede_detector(engine, ledger, None)
        assert det.reevaluate_pair(b, a).action == "supersede_held"

        held_gate._llm_fn = lambda p: APPROVE
        txs = held_gate.review_held_patches()
        assert txs and txs[0].patches[0].status == "applied"
        assert not _reload_graph(engine).has_contradicts_edge(a, b)

        held_gate.rollback_supersede_patch(txs[0])
        reloaded = _reload_graph(engine)
        assert reloaded.has_contradicts_edge(a, b)
        assert reloaded.graph[a][b]["weight"] == pytest.approx(0.8)

    def test_detached_graph_is_saved_by_gate_and_rollback(self, engine, gate):
        a, b, ledger = self._old_edge(engine, gate)
        engine.semantic._graph = None  # semantic 沒掛這張圖：由傳入的 graph 另外存
        det, _g = _supersede_detector(engine, ledger, lambda p: APPROVE)
        result = det.reevaluate_pair(b, a)
        assert result.action == "superseded"
        assert not _reload_graph(engine).has_contradicts_edge(a, b)

        det.rollback_supersede(result)
        assert _reload_graph(engine).has_contradicts_edge(a, b)

    def test_graph_save_failure_does_not_fail_patch(self, engine, gate, monkeypatch):
        a, b, ledger = self._old_edge(engine, gate)
        det, _g = _supersede_detector(engine, ledger, lambda p: APPROVE)

        def boom():
            raise OSError("disk full")

        monkeypatch.setattr(engine.episodic.graph, "save", boom)
        result = det.reevaluate_pair(b, a)
        assert result.action == "superseded"
        assert engine.semantic.get(a).status == "deprecated"
        assert not engine.episodic.graph.has_contradicts_edge(a, b)


class TestHeldSupersedeLedger:
    def test_coordinator_review_held_marks_ledger_resolved(self, engine, gate):
        a, b = _pref_pair(engine, gate)
        first, _c, ledger, _g = _detector(engine, _relation_llm("contradicts"))
        first.detect_and_register(engine.semantic.get(b))

        det, held_gate = _supersede_detector(engine, ledger, None)
        assert det.reevaluate_pair(b, a).action == "supersede_held"
        assert ledger.find_between(a, b).status == "unresolved"

        coordinator = det._get_patch_coordinator()
        coordinator.conflict_ledger = ledger
        held_gate._llm_fn = lambda p: APPROVE
        assert coordinator.review_held() == 1
        record = ledger.find_between(a, b)
        assert record.status == "temporal_evolution"
        assert record.metadata["relation"] == "supersedes"
        assert ledger.is_pair_resolved(
            a, b, ContradictionDetector._pair_digest(
                engine.semantic.get(a), engine.semantic.get(b),
            ),
        )

    def test_repeated_verdict_keeps_first_pre_resolution(self, engine, gate):
        a, b = _pref_pair(engine, gate)
        ledger = ConflictLedger(engine.episodic.graph.storage_path)
        ledger.register(a, b, 0.8)
        ledger.record_resolved(a, b, "accepted_dual_truth", "r1", "d1", relation="different_scope")
        ledger.record_resolved(a, b, "temporal_evolution", "r2", "d2", relation="supersedes")
        assert ledger.unresolve(a, b)
        record = ledger.find_between(a, b)
        assert record.status == "unresolved" and "pair_digest" not in record.metadata


class TestNoContextQuota:
    def test_no_context_skips_do_not_use_batch_slots(self, engine, gate):
        stale_eps = [user_ep(engine, f"舊偏好{i}", attrs={}) for i in range(2)]
        stale = [make_concept(engine, gate, f"舊{i}", [ep]) for i, ep in enumerate(stale_eps)]
        for cid in stale:
            engine.semantic.get(cid).access_count = 9  # 排在前面
        ep = user_ep(engine, "有情境的新偏好")
        good = make_concept(engine, gate, "新偏好", [ep])
        uid = unit_ids(engine, ep)[0]
        reviewer = make_reviewer(engine, gate, lambda p: reply(cited=[uid]))
        report = reviewer.review_batch(limit=1)
        assert report.reviewed == 1 and report.skipped == 2
        assert engine.semantic.get(good).effective_review_status() == "supported"
