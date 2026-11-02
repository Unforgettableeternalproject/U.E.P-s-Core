"""
tests/test_evidence_reviewer.py — M4 包 B：EvidenceReviewer

涵蓋：三步流程（LLM 在鎖外）、無 LLM 不寫 patch、引用不存在 id／LLM 失敗 -> held 與重審、
學習權重 noisy-OR 與 SUPPORT_THRESHOLD、agent_only／legacy 封頂、intention 需佐證、
digest 變動 -> stale、Dream 批次優先序與 DreamEngine 整合。
全部使用假 llm_fn，不載入真模型。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from echo_memory.affect.concept_inhibitor import ConceptInhibitor
from echo_memory.affect.evidence_reviewer import EvidenceReviewer
from echo_memory.engine import MemoryEngine
from echo_memory.schemas.evidence import EvidenceInput, EvidenceUnit, compute_content_hash
from echo_memory.storage import locking
from echo_memory.storage.locking import storage_lock

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

DM = {"channel_kind": "dm", "audience_size": 1}


# ─── 輔助 ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def engine(tmp_path: Path) -> MemoryEngine:
    return MemoryEngine(storage_path=tmp_path / "data", namespace_id="ns1")


@pytest.fixture
def gate(engine) -> ConceptInhibitor:
    return ConceptInhibitor(semantic=engine.semantic, llm_fn=None)


def user_ep(engine, text: str, *, at: datetime = T0, attrs: dict | None = None,
            speaker: str = "u1") -> str:
    return engine.store_episode(
        text, "", "s1",
        evidence=[EvidenceInput(
            source_type="user", content=text, speaker_id=speaker,
            occurred_at=at, attrs=dict(DM if attrs is None else attrs),
        )],
    )


def agent_ep(engine, text: str, *, at: datetime = T0) -> str:
    return engine.store_episode(
        "", text, "s1",
        evidence=[EvidenceInput(
            source_type="agent", content=text, speaker_id="agent", occurred_at=at,
        )],
    )


def unit_ids(engine, ep_id: str) -> list[str]:
    return list(engine.episodic.get(ep_id).evidence_ids)


def make_concept(engine, gate, name: str, eps: list[str], *, desc: str | None = None,
                 support_source: str | None = "user", subject: str = "user") -> str:
    cid, tx = gate.process_extractor_patch(
        noun=name, desc=desc or f"{name} 的描述", episode_id=eps[0],
        evidence_kind="distillation", triggered_by="dream",
        source_episode_ids=eps, subject=subject, support_source=support_source,
    )
    assert cid is not None and tx.status == "committed"
    return cid


def reply(status="supported", cited=None, claim_kind="fact", corro=None, reason="原文明確",
          scope=None) -> str:
    return json.dumps({
        "status": status,
        "claim_kind": claim_kind,
        "cited_unit_ids": list(cited or []),
        "corroborating_unit_ids": list(corro or []),
        "scope": scope or {"subject_ref": "user", "context": None},
        "reason": reason,
    }, ensure_ascii=False)


def make_reviewer(engine, gate, llm_fn, **kw) -> EvidenceReviewer:
    return EvidenceReviewer(
        llm_fn=llm_fn,
        guard=gate,
        semantic=engine.semantic,
        provenance=engine._provenance_resolver(),
        evidence=engine.evidence,
        lock_factory=lambda: storage_lock(engine.storage_path),
        **kw,
    )


def audits(engine) -> list[dict]:
    path = engine.semantic.storage_path / "audit_log.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def lock_is_free(storage_path: Path, wait: float = 1.0) -> bool:
    """從另一條執行緒探測 storage_lock 的 thread lock 是否空閒。"""
    lock = locking._thread_lock(Path(storage_path) / ".echo-memory.lock")
    result: dict[str, bool] = {}

    def _probe() -> None:
        got = lock.acquire(timeout=wait)
        result["free"] = got
        if got:
            lock.release()

    t = threading.Thread(target=_probe)
    t.start()
    t.join(timeout=wait + 5)
    return result.get("free", False)


# ─── 無 LLM／基本流程 ─────────────────────────────────────────────────────────


class TestBasics:
    def test_no_llm_is_skipped_and_writes_nothing(self, engine, gate):
        ep = user_ep(engine, "我喜歡咖啡")
        cid = make_concept(engine, gate, "咖啡", [ep])
        before = len(audits(engine))
        outcome = make_reviewer(engine, gate, None).review(cid)
        assert outcome.outcome == "skipped" and outcome.reason == "no_llm"
        concept = engine.semantic.get(cid)
        assert concept.evidence_review == {}
        assert concept.effective_review_status() == "not_checked"
        assert concept.last_verified_at is None
        assert len(audits(engine)) == before

    def test_missing_concept_skipped(self, engine, gate):
        outcome = make_reviewer(engine, gate, lambda p: reply()).review("con_nope")
        assert outcome.outcome == "skipped"

    def test_single_dm_statement_is_supported(self, engine, gate):
        ep = user_ep(engine, "我喜歡咖啡")
        cid = make_concept(engine, gate, "咖啡", [ep])
        uid = unit_ids(engine, ep)[0]
        outcome = make_reviewer(engine, gate, lambda p: reply(cited=[uid])).review(cid)
        assert outcome.outcome == "reviewed" and outcome.status == "supported"
        concept = engine.semantic.get(cid)
        assert concept.effective_review_status() == "supported"
        review = concept.evidence_review
        assert review["support_strength"] == pytest.approx(1.0)
        assert review["cited_evidence_ids"] == [uid]
        assert review["reviewer"] == "llm"
        assert review["claim_kind"] == "fact"
        assert concept.last_verified_at is not None
        assert outcome.patch_id == review["patch_id"]
        assert 0.0 < concept.confidence <= 0.95

    def test_prompt_contains_evidence_ids_and_times(self, engine, gate):
        ep = user_ep(engine, "我喜歡咖啡")
        cid = make_concept(engine, gate, "咖啡", [ep])
        uid = unit_ids(engine, ep)[0]
        prompts: list[str] = []

        def llm(p):
            prompts.append(p)
            return reply(cited=[uid])

        make_reviewer(engine, gate, llm).review(cid)
        assert uid in prompts[0] and "2026-01-01" in prompts[0] and "我喜歡咖啡" in prompts[0]

    def test_contradicted_is_capped(self, engine, gate):
        ep = user_ep(engine, "我不喝咖啡")
        cid = make_concept(engine, gate, "咖啡", [ep], desc="使用者喜歡咖啡")
        uid = unit_ids(engine, ep)[0]
        outcome = make_reviewer(
            engine, gate, lambda p: reply(status="contradicted", cited=[uid]),
        ).review(cid)
        assert outcome.status == "contradicted"
        assert engine.semantic.get(cid).confidence <= 0.30 + 1e-9

    def test_no_resolvable_evidence_is_insufficient_without_llm_call(self, engine, gate):
        cid = engine.semantic.upsert_concept("孤兒", "沒有來源的概念")
        calls: list[str] = []
        outcome = make_reviewer(engine, gate, lambda p: calls.append(p) or reply()).review(cid)
        assert outcome.outcome == "reviewed" and outcome.status == "insufficient"
        assert calls == []
        review = engine.semantic.get(cid).evidence_review
        assert review["reviewer"] == "none"
        assert engine.semantic.get(cid).last_verified_at is None


# ─── 學習權重與門檻（§8）──────────────────────────────────────────────────────


class TestLearningThreshold:
    def _run(self, engine, gate, attrs_list):
        eps = [user_ep(engine, f"陳述{i}", attrs=a, at=T0 + timedelta(hours=i))
               for i, a in enumerate(attrs_list)]
        cid = make_concept(engine, gate, "偏好", eps)
        cited = [unit_ids(engine, e)[0] for e in eps]
        outcome = make_reviewer(engine, gate, lambda p: reply(cited=cited)).review(cid)
        return outcome, engine.semantic.get(cid)

    def test_two_person_single_statement_is_insufficient(self, engine, gate):
        outcome, concept = self._run(engine, gate, [{"audience_size": 2}])
        assert outcome.status == "insufficient"
        assert concept.evidence_review["support_strength"] == pytest.approx(0.6)
        assert concept.last_verified_at is None  # insufficient 不蓋驗證章

    def test_addressed_to_agent_boosts_but_single_still_insufficient(self, engine, gate):
        outcome, concept = self._run(
            engine, gate, [{"audience_size": 2, "addressed_to_agent": True}],
        )
        assert concept.evidence_review["support_strength"] == pytest.approx(0.8)
        assert outcome.status == "insufficient"

    def test_accumulating_statements_reach_threshold(self, engine, gate):
        a = {"audience_size": 2, "addressed_to_agent": True}
        outcome, concept = self._run(engine, gate, [a, a])  # 1 - 0.2*0.2 = 0.96
        assert concept.evidence_review["support_strength"] == pytest.approx(0.96)
        assert outcome.status == "supported"

    def test_crowd_cannot_support_alone(self, engine, gate):
        outcome, concept = self._run(engine, gate, [{"audience_size": 6}])
        assert concept.evidence_review["support_strength"] == pytest.approx(0.08)
        assert outcome.status == "insufficient"

    def test_all_cited_units_without_context_is_skipped_no_context(self, engine, gate):
        outcome, concept = self._run(engine, gate, [{}])
        assert outcome.outcome == "skipped" and outcome.reason == "no_context"
        assert concept.evidence_review == {}
        assert concept.effective_review_status() == "not_checked"

    def test_no_context_not_resent_by_batch_until_content_changes(self, engine, gate):
        ep = user_ep(engine, "舊的偏好", attrs={})
        make_concept(engine, gate, "舊偏好", [ep])
        calls: list[str] = []
        reviewer = make_reviewer(engine, gate, lambda p: calls.append(p) or reply())
        assert reviewer.review_batch().skipped == 1
        assert reviewer.review_batch().reviewed == 0
        assert calls == []  # 事前以證據結構判斷，連 LLM 都不用叫
        assert reviewer.pending_ids() == []

    def test_partial_context_still_computes_missing_at_default_weight(self, engine, gate):
        # 一筆帶情境（dm=1.0）、一筆缺情境（0.6）-> 照常計算
        outcome, concept = self._run(engine, gate, [{"audience_size": 2}, {}])
        assert concept.evidence_review["support_strength"] == pytest.approx(1 - 0.4 * 0.4)
        assert outcome.outcome == "reviewed"

    def test_same_episode_units_do_not_compound(self, engine, gate):
        ep = engine.store_episode(
            "一\n二", "", "s1",
            evidence=[
                EvidenceInput(source_type="user", content="一", speaker_id="u1",
                              occurred_at=T0, attrs={"audience_size": 2}),
                EvidenceInput(source_type="user", content="二", speaker_id="u1",
                              occurred_at=T0, attrs={"audience_size": 2}),
            ],
        )
        cid = make_concept(engine, gate, "偏好", [ep])
        cited = unit_ids(engine, ep)
        make_reviewer(engine, gate, lambda p: reply(cited=cited)).review(cid)
        assert engine.semantic.get(cid).evidence_review["support_strength"] == pytest.approx(0.6)


# ─── 來源封頂 ─────────────────────────────────────────────────────────────────


class TestSourceCaps:
    def test_agent_only_capped_at_insufficient(self, engine, gate):
        ep = agent_ep(engine, "你可能喜歡咖啡")
        cid = make_concept(engine, gate, "咖啡", [ep], support_source="agent_only")
        uid = unit_ids(engine, ep)[0]
        outcome = make_reviewer(engine, gate, lambda p: reply(cited=[uid])).review(cid)
        assert outcome.outcome == "reviewed" and outcome.status == "insufficient"
        concept = engine.semantic.get(cid)
        assert concept.confidence <= 0.45 + 1e-9
        assert concept.last_verified_at is None

    def test_user_cannot_be_supported_by_citing_agent_unit(self, engine, gate):
        ep = engine.store_episode(
            "問", "你喜歡咖啡吧", "s1",
            evidence=[
                EvidenceInput(source_type="user", content="問", speaker_id="u1",
                              occurred_at=T0, attrs=dict(DM)),
                EvidenceInput(source_type="agent", content="你喜歡咖啡吧", speaker_id="agent",
                              occurred_at=T0),
            ],
        )
        cid = make_concept(engine, gate, "咖啡", [ep], desc="使用者喜歡咖啡")
        agent_uid = [
            u for u in engine.get_episode_evidence(ep) if u.source_type == "agent"
        ][0].id
        outcome = make_reviewer(engine, gate, lambda p: reply(cited=[agent_uid])).review(cid)
        assert outcome.status == "insufficient"

    def test_legacy_only_capped_and_never_stamps_verified(self, engine, gate):
        legacy_ep = engine.store_episode("舊的我喜歡咖啡", "好", "s1")  # legacy_unknown
        cid = make_concept(engine, gate, "咖啡", [legacy_ep], support_source="legacy_unknown")
        cited = engine.resolve_provenance(cid).evidence_ids
        assert cited
        outcome = make_reviewer(engine, gate, lambda p: reply(cited=cited)).review(cid)
        assert outcome.status == "insufficient"
        assert engine.semantic.get(cid).last_verified_at is None

    def test_supported_without_citation_is_insufficient(self, engine, gate):
        ep = user_ep(engine, "我喜歡咖啡")
        cid = make_concept(engine, gate, "咖啡", [ep])
        outcome = make_reviewer(engine, gate, lambda p: reply(cited=[])).review(cid)
        assert outcome.status == "insufficient"


# ─── intention 需佐證（§8.3）─────────────────────────────────────────────────


class TestIntention:
    def _setup(self, engine, gate):
        ep = user_ep(engine, "我明天會去健身房", at=T0)
        cid = make_concept(engine, gate, "明天健身", [ep])
        return cid, unit_ids(engine, ep)[0]

    def test_self_report_alone_is_insufficient(self, engine, gate):
        cid, uid = self._setup(engine, gate)
        outcome = make_reviewer(
            engine, gate, lambda p: reply(cited=[uid], claim_kind="intention"),
        ).review(cid)
        assert outcome.status == "insufficient"
        assert engine.semantic.get(cid).evidence_review["claim_kind"] == "intention"

    def test_observation_corroborates(self, engine, gate):
        cid, uid = self._setup(engine, gate)
        obs = EvidenceUnit(
            id="obs1", episode_id=None, source_type="observation",
            content="使用者在健身 App 簽到", occurred_at=T0 + timedelta(days=1),
            content_hash=compute_content_hash("使用者在健身 App 簽到"),
        )
        engine.evidence.append([obs])
        engine.semantic.add_derivation(cid, "evidence", "obs1", "dream")
        prompts: list[str] = []

        def llm(p):
            prompts.append(p)
            return reply(cited=[uid], claim_kind="intention", corro=["obs1"])

        outcome = make_reviewer(engine, gate, llm).review(cid)
        assert "平台觀察" in prompts[0] and "obs1" in prompts[0]
        assert outcome.status == "supported"
        review = engine.semantic.get(cid).evidence_review
        assert review["corroborated_by"] == ["obs1"]

    def test_later_user_statement_corroborates_earlier_one_does_not(self, engine, gate):
        ep_claim = user_ep(engine, "我明天會去健身房", at=T0)
        ep_later = user_ep(engine, "我今天去了健身房", at=T0 + timedelta(days=1))
        ep_earlier = user_ep(engine, "我最近想健身", at=T0 - timedelta(days=1))
        cid = make_concept(engine, gate, "明天健身", [ep_claim, ep_later, ep_earlier])
        claim, later, earlier = (unit_ids(engine, e)[0] for e in (ep_claim, ep_later, ep_earlier))

        out_bad = make_reviewer(
            engine, gate,
            lambda p: reply(cited=[claim], claim_kind="intention", corro=[earlier]),
        ).review(cid)
        assert out_bad.status == "insufficient"

        out_ok = make_reviewer(
            engine, gate,
            lambda p: reply(cited=[claim], claim_kind="intention", corro=[later]),
        ).review(cid)
        assert out_ok.status == "supported"

    def test_agent_content_cannot_corroborate(self, engine, gate):
        ep_claim = user_ep(engine, "我明天會去健身房", at=T0)
        ep_agent = agent_ep(engine, "你明天會去健身房對吧", at=T0 + timedelta(days=1))
        cid = make_concept(engine, gate, "明天健身", [ep_claim, ep_agent])
        claim, agent_uid = unit_ids(engine, ep_claim)[0], unit_ids(engine, ep_agent)[0]
        outcome = make_reviewer(
            engine, gate,
            lambda p: reply(cited=[claim], claim_kind="intention", corro=[agent_uid]),
        ).review(cid)
        assert outcome.status == "insufficient"


# ─── held／重審／過期 ─────────────────────────────────────────────────────────


class TestHeldAndStale:
    def _concept(self, engine, gate):
        ep = user_ep(engine, "我喜歡咖啡")
        cid = make_concept(engine, gate, "咖啡", [ep])
        return cid, unit_ids(engine, ep)[0]

    def test_unknown_evidence_id_is_held_not_supported(self, engine, gate):
        cid, _uid = self._concept(engine, gate)
        reviewer = make_reviewer(engine, gate, lambda p: reply(cited=["ev_made_up"]))
        outcome = reviewer.review(cid)
        assert outcome.outcome == "held" and "unknown_evidence_id" in outcome.reason
        assert engine.semantic.get(cid).evidence_review == {}
        assert reviewer.pending_ids() == [cid]
        assert any(
            p["status"] == "held" for tx in audits(engine) for p in tx["patches"]
            if p["action"] == "review"
        )

    def test_llm_exception_and_garbage_are_held(self, engine, gate):
        cid, _uid = self._concept(engine, gate)

        def boom(p):
            raise RuntimeError("quota")

        assert make_reviewer(engine, gate, boom).review(cid).outcome == "held"
        assert make_reviewer(engine, gate, lambda p: "不是 JSON").review(cid).outcome == "held"
        assert make_reviewer(
            engine, gate, lambda p: reply(claim_kind="weird"),
        ).review(cid).outcome == "held"
        assert engine.semantic.get(cid).evidence_review == {}

    def test_held_concept_is_retried_by_batch_and_cleared(self, engine, gate):
        cid, uid = self._concept(engine, gate)
        state = {"ok": False}

        def llm(p):
            if not state["ok"]:
                raise RuntimeError("down")
            return reply(cited=[uid])

        reviewer = make_reviewer(engine, gate, llm)
        assert reviewer.review(cid).outcome == "held"
        assert reviewer.pending_ids() == [cid]
        state["ok"] = True
        report = reviewer.review_batch()
        assert report.reviewed == 1
        assert reviewer.pending_ids() == []
        assert engine.semantic.get(cid).effective_review_status() == "supported"

    def test_attempt_limit_abandons_with_audit_and_batch_skips(self, engine, gate):
        cid, uid = self._concept(engine, gate)
        calls = {"n": 0}

        def llm(p):
            calls["n"] += 1
            raise RuntimeError("down")

        reviewer = make_reviewer(engine, gate, llm, max_attempts=2)
        reviewer.review(cid)
        reviewer.review(cid)
        assert reviewer.pending_ids() == []  # abandoned 不再列入待審
        assert any(
            p["status"] == "rejected" and "上限" in (p.get("rejection_reason") or "")
            for tx in audits(engine) for p in tx["patches"] if p["action"] == "review"
        )
        n = calls["n"]
        report = reviewer.review_batch()
        assert calls["n"] == n and report.reviewed == 0
        # 手動審核不受 abandoned 限制，成功後清除登記
        reviewer._llm_fn = lambda p: reply(cited=[uid])
        assert reviewer.review(cid).outcome == "reviewed"
        assert reviewer._load_pending() == {}

    def test_content_change_during_llm_is_stale(self, engine, gate):
        cid, uid = self._concept(engine, gate)

        def llm(p):
            engine.semantic.get(cid).description = "使用者其實討厭咖啡"
            return reply(cited=[uid])

        outcome = make_reviewer(engine, gate, llm).review(cid)
        assert outcome.outcome == "stale"
        assert engine.semantic.get(cid).evidence_review == {}

    def test_description_change_invalidates_review_and_batch_rereviews(self, engine, gate):
        cid, uid = self._concept(engine, gate)
        reviewer = make_reviewer(engine, gate, lambda p: reply(cited=[uid]))
        assert reviewer.review(cid).outcome == "reviewed"
        concept = engine.semantic.get(cid)
        concept.description = "使用者喜歡手沖咖啡"
        assert concept.effective_review_status() == "not_checked"
        assert reviewer.review_batch().reviewed == 1
        assert concept.effective_review_status() == "supported"


# ─── 鎖範圍 ───────────────────────────────────────────────────────────────────


class TestLockScope:
    def test_llm_runs_outside_storage_lock(self, engine, gate):
        ep = user_ep(engine, "我喜歡咖啡")
        cid = make_concept(engine, gate, "咖啡", [ep])
        uid = unit_ids(engine, ep)[0]
        seen: list[bool] = []

        def llm(p):
            seen.append(lock_is_free(engine.storage_path))
            return reply(cited=[uid])

        assert make_reviewer(engine, gate, llm).review(cid).outcome == "reviewed"
        assert seen == [True]


# ─── Dream 批次 ───────────────────────────────────────────────────────────────


class TestBatch:
    def test_priority_fresh_then_invalidated_then_retry_and_limit(self, engine, gate):
        names = ["甲", "乙", "丙"]
        cids = {}
        for n in names:
            ep = user_ep(engine, f"{n}的陳述")
            cids[n] = make_concept(engine, gate, n, [ep])
        uids = {n: unit_ids(engine, engine.semantic.get(cids[n]).source_episode_ids[0])[0]
                for n in names}
        prompted: list[str] = []

        def llm(p):
            for n in names:
                if f"概念名稱：{n}\n" in p:
                    prompted.append(n)
                    return reply(cited=[uids[n]])
            raise AssertionError("未知概念")

        reviewer = make_reviewer(engine, gate, llm)
        # 甲先審過，之後內容變動 -> digest 失效；乙、丙是新概念，丙被召回較多
        assert reviewer.review(cids["甲"]).outcome == "reviewed"
        prompted.clear()
        engine.semantic.get(cids["甲"]).description = "甲的新描述"
        engine.semantic.get(cids["丙"]).access_count = 9

        report = reviewer.review_batch(limit=2)
        assert report.reviewed == 2
        assert prompted == ["丙", "乙"]  # 新概念優先，且 access_count 高者在前；限額 2 不含失效的甲
        assert reviewer.review_batch(limit=5).reviewed == 1  # 剩下甲
        assert prompted[-1] == "甲"

    def test_batch_without_llm_is_skipped(self, engine, gate):
        ep = user_ep(engine, "我喜歡咖啡")
        make_concept(engine, gate, "咖啡", [ep])
        report = make_reviewer(engine, gate, None).review_batch()
        assert report.reviewed == 0 and report.skipped == 1

    def test_batch_uses_config_limit(self, engine, gate, monkeypatch):
        for i in range(3):
            ep = user_ep(engine, f"陳述{i}")
            make_concept(engine, gate, f"概念{i}", [ep])
        reviewer = make_reviewer(engine, gate, lambda p: "壞掉")
        reviewer._cfg.evidence_review_batch = 2
        report = reviewer.review_batch()
        assert report.held == 2

    def test_deprecated_and_merged_concepts_not_reviewed(self, engine, gate):
        ep = user_ep(engine, "舊的")
        cid = make_concept(engine, gate, "舊", [ep])
        engine.semantic.get(cid).status = "deprecated"
        calls: list[str] = []
        report = make_reviewer(
            engine, gate, lambda p: calls.append(p) or reply(),
        ).review_batch()
        assert calls == [] and report.reviewed == 0


# ─── DreamEngine 整合 ─────────────────────────────────────────────────────────


class TestDreamIntegration:
    def _dream(self, engine, gate, llm_fn):
        from echo_memory.dream.engine import DreamEngine

        return DreamEngine(
            graph=engine.episodic.graph,
            episodic=engine.episodic,
            semantic=engine.semantic,
            llm_fn=llm_fn,
            concept_inhibitor=gate,
            enable_idle=False,
            enable_cron=False,
            lock_factory=lambda: storage_lock(engine.storage_path),
            evidence_store=engine.evidence,
        )

    def test_dream_runs_review_phase(self, engine, gate):
        ep = user_ep(engine, "我喜歡咖啡")
        cid = make_concept(engine, gate, "咖啡", [ep])
        uid = unit_ids(engine, ep)[0]

        def llm(p):
            if "證據審核員" in p:
                return reply(cited=[uid])
            return "{}"

        report = self._dream(engine, gate, llm).dream_now("manual")
        assert report.error is None
        assert report.evidence_review is not None and report.evidence_review.reviewed == 1
        assert engine.semantic.get(cid).effective_review_status() == "supported"
        assert "證據審核" in report.summary()

    def test_dream_without_llm_leaves_not_checked(self, engine, gate):
        ep = user_ep(engine, "我喜歡咖啡")
        cid = make_concept(engine, gate, "咖啡", [ep])
        report = self._dream(engine, gate, None).dream_now("manual")
        assert report.error is None
        assert engine.semantic.get(cid).effective_review_status() == "not_checked"
        assert engine.semantic.get(cid).last_verified_at is None

    def test_dream_without_evidence_store_has_no_review_phase(self, engine, gate):
        from echo_memory.dream.engine import DreamEngine

        eng = DreamEngine(
            graph=engine.episodic.graph, episodic=engine.episodic, semantic=engine.semantic,
            llm_fn=lambda p: "{}", concept_inhibitor=gate,
            enable_idle=False, enable_cron=False,
        )
        assert eng.dream_now("manual").evidence_review is None

    def test_manual_review_concept_wiring(self, engine):
        # MemoryEngine.review_concept（包 C 接線）能建構審核器並回傳 outcome
        ep = user_ep(engine, "我喜歡咖啡")
        gate = ConceptInhibitor(semantic=engine.semantic, llm_fn=None)
        cid = make_concept(engine, gate, "咖啡", [ep])
        uid = unit_ids(engine, ep)[0]
        engine._llm_fn = lambda p: reply(cited=[uid])
        engine._concept_inhibitor = gate
        result = engine.review_concept(cid)
        assert result["outcome"] == "reviewed"
