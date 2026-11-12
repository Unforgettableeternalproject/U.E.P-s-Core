"""Chain 契約 PR-B：契約欄位與日誌欄位（零行為變化）。

離線部分：PREFIXES、``Proposal.compound``、``DecisionPlan.chain`` 序列化相容、hub 守衛、shadow_compound、
decision_log 的 chain_id／chain_seq 取值與 ORM；DB 部分（無 UEP_TEST_DATABASE_URL 時 skip）驗證 0020 升降級。
"""
from __future__ import annotations

import dataclasses
from collections import Counter
from datetime import UTC, datetime

import pytest
from alembic.script import ScriptDirectory

from tests.uep.conftest import alembic_config
from uep.decision import DecisionHub, DecisionPlan, DecisionRequest
from uep.decision.chain import (
    CHAIN_REASON_CODES,
    CHAIN_STATES,
    CHAIN_SUB_KINDS,
    KIND_TURN_CHAIN,
    KIND_TURN_CHAIN_SEGMENT,
    chain_segment_spec,
    chain_spec,
)
from uep.decision.contracts import (
    DecisionChain,
    DecisionEngineInfo,
    DecisionResult,
    plan_from_dict,
    plan_to_dict,
)
from uep.decision.hub import REASON_PLAN_SECTION_REJECTED
from uep.decision.log import COLUMNS, primary_row, shadow_row
from uep.decision.registry import DecisionPointSpec
from uep.decision.turn_reply import (
    KIND_TURN_REPLY,
    TurnReplyEngine,
    proposal_to_result,
    result_to_proposal,
    turn_reply_spec,
)
from uep.ids import is_valid_id, new_id
from uep.turn.models import Proposal, ReplyMode, SessionContext, TurnCandidate
from uep.turn.proposer import validate_proposal

CTX = SessionContext(conversation_id="c1", segment_id="s1", audience_scope="aud", session_ref="sess")


def _cand() -> TurnCandidate:
    return TurnCandidate(
        candidate_id="cand1", utterance_refs=("e1",), speakers=("discord:1",), ctx=CTX, interaction_state="IDLE",
        interaction_version=1, presence_active=True, text="hi", participant_count=1, addressed_hint=True,
    )


def _request() -> DecisionRequest:
    return DecisionRequest(request_id="r1", kind=KIND_TURN_REPLY, native=_cand())


# ---- ids ----

def test_chain_prefix_registered_and_uppercase_ulid_valid():
    chain_id = new_id("chain")
    assert chain_id.startswith("chain_") and is_valid_id(chain_id, "chain")
    assert any(ch.isupper() for ch in chain_id.split("_", 1)[1])  # ULID 含大寫，所以不可走 _SAFE_TOKEN


# ---- Proposal／plan 序列化 ----

def test_proposal_compound_defaults_to_none_and_validates():
    p = Proposal(reply_mode=ReplyMode.RESPOND)
    assert p.compound is None and validate_proposal(p) is None
    assert validate_proposal(Proposal(reply_mode=ReplyMode.RESPOND, compound=True)) is None


def test_old_plan_roundtrip_unchanged_and_chain_absent():
    legacy = {"route": "direct_chat", "reply": {"mode": "respond", "interaction_to": "u1", "addressed": True}}
    plan = plan_from_dict(legacy)
    assert plan.chain is None and plan_to_dict(plan) == legacy
    assert "chain" not in plan_to_dict(DecisionPlan(route="ignore"))


@pytest.mark.parametrize("compound", [True, False, None])
def test_chain_section_roundtrip(compound):
    plan = DecisionPlan(route="direct_chat", chain=DecisionChain(compound=compound))
    data = plan_to_dict(plan)
    assert data["chain"] == {"compound": compound}
    assert plan_from_dict(data) == plan


def test_chain_section_ignores_unknown_keys():
    plan = plan_from_dict({"route": "x", "chain": {"compound": True, "future_key": 1}, "future_section": {}})
    assert plan.chain == DecisionChain(compound=True)


def test_compound_none_keeps_turn_reply_plan_identical_to_before():
    proposal = Proposal(reply_mode=ReplyMode.RESPOND, route="direct_chat", interaction_to="u1")
    result = proposal_to_result(proposal, _request())
    assert result.plan.chain is None
    assert plan_to_dict(result.plan) == {
        "route": "direct_chat", "reply": {"mode": "respond", "interaction_to": "u1", "addressed": True},
    }


@pytest.mark.parametrize("compound", [True, False, None])
def test_proposal_result_roundtrip_maps_compound(compound):
    proposal = Proposal(reply_mode=ReplyMode.RESPOND, compound=compound)
    result = proposal_to_result(proposal, _request())
    assert (result.plan.chain is None) == (compound is None)
    assert result_to_proposal(result).compound is compound


# ---- hub 守衛 ----

class _FixedProposer:
    proposer_id = "t.fixed"

    def __init__(self, proposal: Proposal) -> None:
        self.proposal = proposal

    def propose(self, candidate):
        return self.proposal


class _ChainEngine:
    def decide(self, request):
        return DecisionResult(
            decision_id="dec_x", request_id=request.request_id, kind=request.kind, outcome="ok",
            plan=DecisionPlan(route="direct_chat", chain=DecisionChain(compound=True)), confidence=1.0,
            reason_code="r", engine=DecisionEngineInfo(tier="rule", id="t", version="0"),
        )


def test_turn_reply_spec_declares_chain_and_other_kinds_get_it_stripped():
    assert "chain" in turn_reply_spec(300).plan_sections
    hub = DecisionHub()
    hub.register(DecisionPointSpec(kind="x.k", outcomes=("ok",)), _ChainEngine())
    result = hub.decide("x.k", DecisionRequest(request_id="r", kind="x.k"))
    assert result.plan.chain is None and result.fallback and result.reason_code == REASON_PLAN_SECTION_REJECTED
    hub2 = DecisionHub()
    hub2.register(DecisionPointSpec(kind="x.k", outcomes=("ok",), plan_sections=("chain",)), _ChainEngine())
    ok = hub2.decide("x.k", DecisionRequest(request_id="r", kind="x.k"))
    assert ok.plan.chain == DecisionChain(compound=True) and not ok.fallback


def test_turn_reply_engine_carries_compound_through_hub():
    hub = DecisionHub()
    metrics: Counter = Counter()
    engine = TurnReplyEngine(
        _FixedProposer(Proposal(reply_mode=ReplyMode.RESPOND, compound=True)), registry=hub.registry, metrics=metrics,
    )
    hub.register(turn_reply_spec(300), engine)
    try:
        result = hub.decide(KIND_TURN_REPLY, _request())
        assert not result.fallback and result.plan.chain == DecisionChain(compound=True)
        assert result_to_proposal(result).compound is True
    finally:
        engine.close()


def test_shadow_record_compound_defaults_none_and_is_recorded():
    hub = DecisionHub()
    records = []
    engine = TurnReplyEngine(
        _FixedProposer(Proposal(reply_mode=ReplyMode.RESPOND)), registry=hub.registry, metrics=Counter(),
        shadow_proposers=(_FixedProposer(Proposal(reply_mode=ReplyMode.RESPOND, compound=True)),),
        on_shadow=records.append,
    )
    hub.register(turn_reply_spec(300), engine)
    try:
        result = hub.decide(KIND_TURN_REPLY, _request())
        assert result.plan.chain is None  # shadow 的 compound 不進 primary 的 plan
        assert engine.wait_shadow(5)
        assert records and records[0].shadow_compound is True
    finally:
        engine.close()
    from uep.decision.turn_reply import ShadowRecord

    assert ShadowRecord(
        candidate_id="c", shadow_id="s", primary_mode="respond", primary_reason="r", shadow_mode=None,
        shadow_reason=None, agree=False, status="error", elapsed_ms=1,
    ).shadow_compound is None


# ---- decision_log 欄位 ----

def _primary_row(subject=None, facts=None) -> dict:
    result = proposal_to_result(Proposal(reply_mode=ReplyMode.RESPOND), _request())
    request = DecisionRequest(
        request_id="r1", kind=KIND_TURN_REPLY, native=_cand(), subject=subject or {}, facts=facts or {},
    )
    return primary_row(request, result, turn_reply_spec(300), 3)


def test_chain_columns_appended_and_row_has_all_keys():
    assert COLUMNS[-2:] == ("chain_id", "chain_seq")
    row = _primary_row()
    assert set(row) == set(COLUMNS) and row["chain_id"] is None and row["chain_seq"] is None
    srow = shadow_row(type("R", (), {
        "decision_id": "d", "request_id": "r", "kind": "turn.reply", "primary_decision_id": "p", "shadow_id": "s",
        "shadow_mode": "respond", "shadow_reason": "x", "elapsed_ms": 1, "status": "ok", "agree": True,
        "candidate_id": "c",
    })())
    assert srow["chain_id"] is None and srow["chain_seq"] is None


def test_chain_id_with_uppercase_is_kept_not_cleaned_to_none():
    chain_id = new_id("chain")
    row = _primary_row(subject={"chain_id": chain_id}, facts={"chain": {"seq": 2}})
    assert row["chain_id"] == chain_id and row["chain_seq"] == 2
    assert _primary_row(subject={"chain_id": chain_id}, facts={"chain": {"seq": 0}})["chain_seq"] == 0


@pytest.mark.parametrize("seq", [True, -1, 32768, "1", 1.5, None])
def test_bad_chain_seq_dropped(seq):
    assert _primary_row(facts={"chain": {"seq": seq}})["chain_seq"] is None


@pytest.mark.parametrize("bad", ["含中文", "has space", "", 123, None, "x" * 129])
def test_bad_chain_id_dropped(bad):
    assert _primary_row(subject={"chain_id": bad})["chain_id"] is None


def test_chain_facts_not_a_dict_is_ignored():
    assert _primary_row(facts={"chain": 3})["chain_seq"] is None


def test_orm_has_chain_columns_and_index():
    import uep.db.models_registry  # noqa: F401
    from uep.db.base import Base

    table = Base.metadata.tables["decision_log"]
    assert [c.name for c in table.columns] == list(COLUMNS)
    assert table.columns["chain_id"].nullable and table.columns["chain_seq"].nullable
    assert ("chain_id", "chain_seq") in {tuple(c.name for c in ix.columns) for ix in table.indexes}


def test_old_rows_without_chain_keys_still_readable_by_replay():
    from uep.decision.replay import analyze

    now = datetime(2026, 11, 1, tzinfo=UTC)
    base = dict.fromkeys(COLUMNS[:-2])
    row = {**base, "decision_id": "dec_p", "kind": KIND_TURN_REPLY, "created_at": now, "role": "primary",
           "outcome": "respond", "reason_code": "r", "status": "ok", "elapsed_ms": 4, "fallback": False}
    analyze([row])  # 舊式列（沒有 chain 兩鍵）不得拋例外


# ---- spec 常數（不掛 engine）----

def test_chain_specs_and_constants():
    seg, life = chain_segment_spec(), chain_spec()
    assert (seg.kind, seg.mode) == (KIND_TURN_CHAIN_SEGMENT, "sync") and seg.plan_sections == ()
    assert (life.kind, life.mode, life.outcomes) == (KIND_TURN_CHAIN, "publish", CHAIN_STATES)
    assert len(set(CHAIN_REASON_CODES)) == len(CHAIN_REASON_CODES) == 22
    assert len(set(CHAIN_SUB_KINDS)) == 8
    assert not dataclasses.is_dataclass(CHAIN_STATES)


def test_chain_kinds_not_registered_by_default_assembly():
    from uep.decision.registry import DecisionPointRegistry

    assert KIND_TURN_CHAIN not in DecisionPointRegistry().kinds()


# ---- migration 0020 ----

def test_migration_0020_revision_chain_and_head():
    script = ScriptDirectory.from_config(alembic_config("postgresql://x/y_test"))
    rev = script.get_revision("0020_decision_log_chain")
    assert rev is not None and rev.down_revision == "0019_decision_log_target"
    assert callable(rev.module.upgrade) and callable(rev.module.downgrade)
    assert script.get_heads() == ["0020_decision_log_chain"]


def test_db_migration_0020_roundtrip(alembic_cfg, engine, session_factory):
    from alembic import command
    from sqlalchemy import inspect, text

    def cols():
        return {c["name"] for c in inspect(engine).get_columns("decision_log")}

    def indexes():
        return {ix["name"] for ix in inspect(engine).get_indexes("decision_log")}

    try:
        command.downgrade(alembic_cfg, "0019_decision_log_target")
        assert not {"chain_id", "chain_seq"} & cols() and "ix_decision_log_chain" not in indexes()
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO decision_log (decision_id, kind, created_at, role, status) "
                "VALUES ('dec_old', 'turn.reply', now(), 'primary', 'ok')"))
        command.upgrade(alembic_cfg, "head")
        assert {"chain_id", "chain_seq"} <= cols() and "ix_decision_log_chain" in indexes()
        with engine.connect() as conn:
            assert conn.execute(text("SELECT chain_id, chain_seq FROM decision_log WHERE decision_id='dec_old'")).one() == (None, None)
        chain_id = new_id("chain")
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO decision_log (decision_id, kind, created_at, role, status, chain_id, chain_seq) "
                "VALUES ('dec_new', 'turn.chain_segment', now(), 'primary', 'ok', :c, 1)"), {"c": chain_id})
        with engine.connect() as conn:
            assert conn.execute(text("SELECT chain_seq FROM decision_log WHERE chain_id=:c"), {"c": chain_id}).scalar() == 1
    finally:
        command.upgrade(alembic_cfg, "head")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM decision_log"))
    assert {"chain_id", "chain_seq"} <= cols()
