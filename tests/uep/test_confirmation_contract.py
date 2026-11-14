"""確認提案契約（PM-2b §3A.2）：建構驗證、終態規則、政策。離線。"""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from uep.contracts.confirmation import (
    ConfirmationPolicy,
    ConfirmationProposal,
    ConfirmationSummary,
)
from uep.contracts.permission import RequesterTier
from uep.ids import new_id

NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)


def make(**kw) -> ConfirmationProposal:
    base = {
        "proposal_id": new_id("cfm"), "action": "abandon_item", "nonce": "abcd1234", "requester_ref": "discord:1",
        "source_platform": "discord", "conversation_ref": "conv1:seg1", "path": "turn",
        "summary": ConfirmationSummary(action="abandon_item", activity_kind="work"), "binding": {"item_ref": "i1"},
        "source_utterance_ref": "utt1", "created_at": NOW, "expires_at": NOW + timedelta(seconds=90),
    }
    base.update(kw)
    return ConfirmationProposal(**base)


def test_valid_pending_proposal_and_reason_ref():
    p = make()
    assert p.status == "pending" and p.prompt_delivered_at is None
    assert p.reason_ref == f"confirmation:{p.proposal_id.lower()}"
    import re
    assert re.fullmatch(r"^[a-z0-9_.:-]{1,128}$", p.reason_ref)  # 符合 AbandonWorkSignal 的 blob 引用格式


@pytest.mark.parametrize("kw", [
    {"proposal_id": "cfm_bad"}, {"proposal_id": "req_" + "0" * 26}, {"action": "other"}, {"nonce": "short"},
    {"nonce": "ABCD1234"}, {"requester_ref": ""}, {"requester_ref": "a b"}, {"source_platform": ""},
    {"conversation_ref": "中文"}, {"path": "absent"}, {"status": "weird"},
    {"created_at": datetime(2026, 11, 13, 9, 0)}, {"expires_at": NOW}, {"expires_at": NOW - timedelta(seconds=1)},
])
def test_invalid_fields_are_rejected(kw):
    with pytest.raises(ValueError):
        make(**kw)


def test_absent_path_is_not_a_valid_confirmation_path():
    """D7：不在場路徑沒有口語確認。"""
    with pytest.raises(ValueError):
        make(path="absent")


def test_terminal_status_requires_resolution_fields():
    with pytest.raises(ValueError):
        make(status="confirmed")
    with pytest.raises(ValueError):
        make(status="expired", resolved_at=NOW, resolved_reason="free text")
    done = make(status="declined", resolved_at=NOW, resolved_reason="declined")
    assert done.status == "declined"


def test_proposal_is_frozen():
    p = make()
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.status = "confirmed"  # type: ignore[misc]


def test_policy_validation():
    ConfirmationPolicy(ttl_s=90, allow_colloquial=True, require_human_origin=False,
                       min_tier=RequesterTier.OWNER, max_intervening=2)
    with pytest.raises(ValueError):
        ConfirmationPolicy(ttl_s=0, allow_colloquial=True, require_human_origin=False,
                           min_tier=RequesterTier.OWNER, max_intervening=2)
    with pytest.raises(ValueError):
        ConfirmationPolicy(ttl_s=10, allow_colloquial=True, require_human_origin=False,
                           min_tier=RequesterTier.OWNER, max_intervening=-1)
