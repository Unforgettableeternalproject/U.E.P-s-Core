"""預設 proposer 的註冊（``proposer_id`` -> 工廠）。換 proposer = 註冊新 id、設定選用，Core 其他程式不動。"""
from __future__ import annotations

from uep.turn.proposer import ProposerRegistry, RespondProposer, TurnProposer
from uep.turn.rule_proposer import RuleProposer

PROPOSERS = ProposerRegistry()
PROPOSERS.register(RespondProposer.proposer_id, lambda **_: RespondProposer())
PROPOSERS.register(RuleProposer.proposer_id, lambda **kw: RuleProposer(**kw))


def create_proposer(proposer_id: str, **kwargs: object) -> TurnProposer:
    return PROPOSERS.create(proposer_id, **kwargs)
