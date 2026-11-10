"""單字附和依上下文判斷（需 PostgreSQL）：走真的 Host／Orchestrator／rules.default，驗證 reply_text 一路接到 proposer。"""
from __future__ import annotations

import dataclasses

import pytest

from tests.uep.echo_support import build_rig
from uep.turn.decision_config import BotIdentityConfig, DecisionConfig, GateConfig
from uep.turn.models import ReplyMode, TurnOutcomeFacts
from uep.turn.rule_proposer import RuleProposer

pytestmark = pytest.mark.usefixtures("session_factory")


@pytest.fixture
def rig(session_factory, tmp_path):
    made = []

    def _build(**over):
        r = build_rig(session_factory, tmp_path, **over)
        made.append(r)
        return r

    yield _build
    for r in made:
        r.core.shutdown()


def _decision() -> DecisionConfig:
    return DecisionConfig(bot_identity=BotIdentityConfig(primary_name="Novalis"), gate=GateConfig(context_window_s=30.0))


@pytest.mark.parametrize("reply, expect_respond", [
    ("你今天過得好嗎？", True),
    ("今天天氣很好。我剛整理完資料。", False),
])
def test_ack_after_her_reply_end_to_end(rig, reply, expect_respond):
    r = rig(host_kwargs={"proposer": None, "decision": _decision()}, responses=(reply, reply))
    assert isinstance(r.host.orchestrator.proposer, RuleProposer)
    first = r.say("Novalis 你好")
    assert first.ticket is not None
    r.wait_terminal(first.ticket.turn_id)
    ack = r.say("嗯")
    if expect_respond:
        assert ack.ticket is not None and ack.proposal.reply_mode is ReplyMode.RESPOND
    else:
        assert ack.ticket is None and ack.proposal.reply_mode is ReplyMode.PASS


def test_reply_text_is_not_in_repr_or_serializable_fields():
    """reply_text 只供 proposer 在行程內使用：repr 不含，且 Orchestrator 之外沒有序列化 TurnOutcomeFacts 的路徑。"""
    facts = TurnOutcomeFacts(
        turn_id="t", candidate_id="c", speakers=("u",), reply_mode="respond", reason_code="x", status="completed",
        reply_text="私人內容不可落地",
    )
    assert "私人內容" not in repr(facts)
    field = next(f for f in dataclasses.fields(TurnOutcomeFacts) if f.name == "reply_text")
    assert field.repr is False
