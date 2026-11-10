"""預設 proposer 的註冊（``proposer_id`` -> 工廠）。換 proposer = 註冊新 id、設定選用，Core 其他程式不動。"""
from __future__ import annotations

from uep.echo.laya_wiring import LAYA_PROPOSER_ID, create_laya_proposer
from uep.turn.proposer import ProposerRegistry, RespondProposer, TurnProposer
from uep.turn.rule_proposer import RuleProposer

PROPOSERS = ProposerRegistry()
PROPOSERS.register(RespondProposer.proposer_id, lambda **_: RespondProposer())
PROPOSERS.register(RuleProposer.proposer_id, lambda **kw: RuleProposer(**kw))
# DL-2：laya 套件只在工廠建立時檢查；未安裝時 ValueError（啟動即報錯），預設不啟用
PROPOSERS.register(LAYA_PROPOSER_ID, create_laya_proposer)


def create_proposer(proposer_id: str, **kwargs: object) -> TurnProposer:
    return PROPOSERS.create(proposer_id, **kwargs)
