"""決策插槽（P3接入方案 §4）：TurnProposer Port 與 S3 預設實作（一律 respond）。

S5 才會有 TurnAggregator 與真正的規則／模型 proposer；Orchestrator 不依賴任何具體實作。
"""
from __future__ import annotations

from typing import Protocol

from uep.turn.models import Proposal, ReplyMode, TurnCandidate


class TurnProposer(Protocol):
    def propose(self, candidate: TurnCandidate) -> Proposal: ...


class RespondProposer:
    """S3 最小 proposer：被送進來的每一句都回應。"""

    proposer_id = "core.decision"
    version = "s3-respond"

    def propose(self, candidate: TurnCandidate) -> Proposal:
        return Proposal(
            reply_mode=ReplyMode.RESPOND, reason_code="default_respond",
            proposer_id=self.proposer_id, proposer_version=self.version,
        )
