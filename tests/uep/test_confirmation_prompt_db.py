"""確認提示經真實 Output Gate／Delivery／端點送出（PM-2b；需 PostgreSQL，沒有 UEP_TEST_DATABASE_URL 時自動 skip）。

鎖定：固定模板文字經 Gate 後由端點渲染；送達才可確認（「好」→ AbandonWorkSignal）；送不出去（無 Lease、audience 被擋）→
superseded(undelivered)，之後的「好」無事；暫存文字送出後清除。
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from tests.uep.test_agent_loop_entry_db import FakePort, _agent_rig, fin, text
from uep.confirm import ConfirmationService
from uep.contracts.confirmation import ConfirmationSummary, PreparedConfirmation
from uep.ids import new_id
from uep.memory.identity import IdentityDirectory
from uep.turn.generations import TurnGenerations
from uep.turn.models import Proposal, ReplyMode, SessionContext, UtteranceFact
from uep.turn.orchestrator import ConfirmationPrompt, TurnOrchestrator

pytestmark = pytest.mark.usefixtures("session_factory")

PROMPT = "要結束這次的〔工作〕行程、回主機嗎？"
CONFIRM_PHRASE = "放棄這次工作"


class _Handler:
    action = "abandon_item"

    def prepare(self, *, actor_ref, now):
        return PreparedConfirmation({"item_ref": "i1"}, ConfirmationSummary(action="abandon_item", activity_kind="work"))

    def validate(self, binding, *, now):
        return True

    def render(self, summary, *, audience, locale):
        return PROMPT


class _Pass:
    proposer_id = "t.pass"

    def propose(self, candidate):
        return Proposal(reply_mode=ReplyMode.PASS, reason_code="x", proposer_id="t.pass", proposer_version="1")


class _Pub:
    def __init__(self) -> None:
        self.drafts: list = []

    def publish(self, draft, trusted):
        self.drafts.append(draft)
        return SimpleNamespace(duplicate=False, event=SimpleNamespace(event_id=f"evt_{len(self.drafts)}", trace_id="t"))


def _rig(session_factory, tmp_path):
    rig, _ = _agent_rig(session_factory, tmp_path, route="direct_chat", flag=False, loop_port=FakePort([[text("好"), fin()]]))
    return rig


def _prompt_texts(rig):
    return [t.text for t in rig.host.endpoint.rendered_texts() if t.text == PROMPT]


def _ctx(rig, audience="scope_private_owner"):
    return SessionContext(conversation_id=new_id("conv"), segment_id=new_id("seg"), audience_scope=audience, session_ref="s1")


def _orch(rig, signals):
    service = ConfirmationService(clock=lambda: datetime.now(UTC))
    service.register_handler(_Handler())
    identity = IdentityDirectory({"discord:100": "actor_owner"}, {"actor_owner": "owner1"})
    auth = rig.host.endpoint.lease_auth() or SimpleNamespace()
    orch = TurnOrchestrator(
        _Pub(), TurnGenerations(), auth_provider=lambda: auth, proposer=_Pass(), identity=identity,  # type: ignore[arg-type]
        host_location="desktop", run_id="run1", confirmation=service,
        confirmation_prompt=rig.host._deliver_confirmation_prompt,
        on_abandon_signal=lambda signal, *, is_owner: signals.append((signal, is_owner)),
    )
    return orch, service


def _say(orch, ctx, text, seq):
    return orch.on_utterance(ctx, UtteranceFact(speaker_ref="discord:100", text=text, seq=seq, source_endpoint_id="ep"))


def test_prompt_goes_through_gate_and_is_rendered_then_text_is_cleared(session_factory, tmp_path):
    rig = _rig(session_factory, tmp_path)
    try:
        before = len(rig.deliveries())
        prompt = ConfirmationPrompt(proposal_id="cfm_X1", text=PROMPT, ctx=_ctx(rig), actor_id="actor_owner",
                                    source_platform="discord", reply_to_seq=1)
        assert rig.host._deliver_confirmation_prompt(prompt) is True
        assert _prompt_texts(rig) == [PROMPT] and len(rig.deliveries()) == before + 1
        assert len(rig.host.loop_text) == 0
    finally:
        rig.core.shutdown()


def test_prompt_not_delivered_without_lease_or_when_audience_is_wider_than_presence(session_factory, tmp_path):
    rig = _rig(session_factory, tmp_path)
    try:
        wide = ConfirmationPrompt(proposal_id="cfm_X2", text=PROMPT, ctx=_ctx(rig, "scope_public"), actor_id="actor_owner",
                                  source_platform="discord", reply_to_seq=1)
        assert rig.host._deliver_confirmation_prompt(wide) is False  # Gate 的 audience 過濾沒有被繞過
        assert _prompt_texts(rig) == []
        rig.host.endpoint.lease_auth = lambda: None
        ok = ConfirmationPrompt(proposal_id="cfm_X3", text=PROMPT, ctx=_ctx(rig), actor_id="actor_owner",
                                source_platform="discord", reply_to_seq=1)
        assert rig.host._deliver_confirmation_prompt(ok) is False and _prompt_texts(rig) == []
    finally:
        rig.core.shutdown()


def test_end_to_end_prompt_delivered_then_owner_yes_produces_signal(session_factory, tmp_path):
    rig = _rig(session_factory, tmp_path)
    try:
        signals: list = []
        orch, service = _orch(rig, signals)
        ctx = _ctx(rig)
        result = _say(orch, ctx, CONFIRM_PHRASE, 1)
        assert result.reason_code == "confirmation_prompted" and signals == []
        assert _prompt_texts(rig) == [PROMPT]  # 固定模板經 Gate 由端點渲染
        _say(orch, ctx, "好", 2)
        assert len(signals) == 1
        signal, is_owner = signals[0]
        assert is_owner is True and signal.signal_id.startswith("abandon:confirm:cfm_")
        assert signal.reason_ref.startswith("confirmation:cfm_") and service.pending_count() == 0
    finally:
        rig.core.shutdown()


def test_end_to_end_undelivered_prompt_supersedes_and_yes_does_nothing(session_factory, tmp_path):
    rig = _rig(session_factory, tmp_path)
    try:
        signals: list = []
        orch, service = _orch(rig, signals)
        rig.host.endpoint.lease_auth = lambda: None  # 送不出去
        ctx = _ctx(rig)
        result = _say(orch, ctx, CONFIRM_PHRASE, 1)
        assert result.reason_code != "confirmation_prompted"  # 沒送出就不強制 pass
        assert service.pending_count() == 0 and orch.metrics["intake.abandon_confirm_undelivered"] == 1
        _say(orch, ctx, "好", 2)
        assert signals == [] and _prompt_texts(rig) == []
    finally:
        rig.core.shutdown()


def test_host_wires_prompt_and_service_only_when_arbiter_flag_is_on(session_factory, tmp_path):
    rig = _rig(session_factory, tmp_path)
    try:
        assert rig.host.confirmation is None  # UEP_PRESENCE_ARBITER=off（預設）：行為完全不變
        assert rig.host.orchestrator._confirmation_prompt is None
    finally:
        rig.core.shutdown()
