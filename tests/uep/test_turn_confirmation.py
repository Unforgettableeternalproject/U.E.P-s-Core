"""結束階段分級＋確認流程在 orchestrator 的接線（PM-2b §3A.5）：提案→提示→確認→訊號、拒絕／逾時＝無事、addressed、D7。離線。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.uep.test_action_unavailable import AUTH, CTX
from uep.confirm import ConfirmationService
from uep.contracts.confirmation import ConfirmationSummary, PreparedConfirmation
from uep.contracts.presence import PresenceLocation
from uep.events.registry import CONFIRMATION_EVENT_TYPES, DEFAULT_REGISTRY
from uep.memory.identity import IdentityDirectory
from uep.turn.generations import TurnGenerations
from uep.turn.models import Proposal, ReplyMode, UtteranceFact
from uep.turn.orchestrator import TurnOrchestrator

OWNER = "discord:100"
OTHER = "discord:200"
NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)

DIRECT = "結束這次行程"
CONFIRM = "放棄這次工作"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Capture:
    def __init__(self) -> None:
        self.drafts: list = []

    def publish(self, draft, trusted):
        self.drafts.append(draft)
        return SimpleNamespace(duplicate=False, event=SimpleNamespace(event_id=f"evt_{len(self.drafts)}", trace_id="t"))

    def types(self) -> list[str]:
        return [d.event_type for d in self.drafts]


class Proposer:
    """可控制 addressed 與回覆模式的 proposer。"""

    proposer_id = "t.proposer"

    def __init__(self, *, addressed: bool = True, mode: ReplyMode = ReplyMode.PASS) -> None:
        self.addressed, self.mode = addressed, mode

    def propose(self, candidate):
        return Proposal(reply_mode=self.mode, addressed=self.addressed, reason_code="x",
                        proposer_id="t.proposer", proposer_version="1")


class Handler:
    action = "abandon_item"

    def __init__(self) -> None:
        self.item = "i1"
        self.available = True

    def prepare(self, *, actor_ref, now):
        if not self.available:
            return None
        return PreparedConfirmation({"item_ref": self.item}, ConfirmationSummary(action="abandon_item", activity_kind="work"))

    def validate(self, binding, *, now):
        return binding.get("item_ref") == self.item

    def render(self, summary, *, audience, locale):
        return "要結束這次的〔工作〕行程、回主機嗎？"


class Rig:
    def __init__(self, *, addressed=True, mode=ReplyMode.PASS, count=1, deliver=True, with_prompt=True):
        self.clock = Clock()
        self.pub = Capture()
        self.signals: list = []
        self.owners: list = []
        self.prompts: list = []
        self.handler = Handler()
        self.service = ConfirmationService(clock=self.clock)
        self.service.register_handler(self.handler)
        self.deliver = deliver
        self.proposer = Proposer(addressed=addressed, mode=mode)
        self.count = count

        def sink(signal, *, is_owner):
            self.signals.append(signal)
            self.owners.append(is_owner)

        def prompt(p):
            self.prompts.append(p)
            return self.deliver

        identity = IdentityDirectory({OWNER: "actor_owner", OTHER: "actor_guest"}, {"actor_owner": "owner1"})
        self.orch = TurnOrchestrator(
            self.pub, TurnGenerations(), auth_provider=lambda: AUTH,  # type: ignore[arg-type]
            proposer=self.proposer, identity=identity, host_location="desktop", run_id="run1",
            on_abandon_signal=sink, participants=lambda ctx: self.count, clock=self.clock,
            confirmation=self.service, confirmation_prompt=prompt if with_prompt else None,
        )
        self.seq = 0

    def say(self, text, speaker=OWNER, endpoint="ep"):
        self.seq += 1
        return self.orch.on_utterance(CTX, UtteranceFact(
            speaker_ref=speaker, text=text, seq=self.seq, source_endpoint_id=endpoint))


def test_needs_confirmation_prompts_forces_pass_then_yes_confirms():
    rig = Rig(mode=ReplyMode.RESPOND)
    result = rig.say(CONFIRM)
    assert rig.signals == []  # 未確認前沒有訊號
    assert len(rig.prompts) == 1 and rig.prompts[0].text == "要結束這次的〔工作〕行程、回主機嗎？"
    assert result.ticket is None and result.reason_code == "confirmation_prompted"  # 這輪不再另外回覆
    rig.say("好")
    assert len(rig.signals) == 1 and rig.owners == [True]
    sig = rig.signals[0]
    assert sig.signal_id.startswith("abandon:confirm:cfm_") and sig.path == "turn"
    assert sig.decider_id == "rules.abandon_work.confirmed" and sig.reason_ref.startswith("confirmation:cfm_")
    assert sig.actor_ref == "actor_owner" and sig.actor_authenticated is True
    types = rig.pub.types()
    for expected in ("confirmation.proposed", "confirmation.prompt_delivered", "confirmation.confirmed"):
        assert expected in types
    rig.say("好")  # 已確認過：同一個 proposal 不會再產生第二個訊號
    assert len(rig.signals) == 1


def test_confirmation_event_types_are_registered_and_payload_has_no_text():
    rig = Rig()
    rig.say(CONFIRM)
    rig.say("好")
    for t in CONFIRMATION_EVENT_TYPES:
        DEFAULT_REGISTRY.lookup(t)
    drafts = [d for d in rig.pub.drafts if d.event_type.startswith("confirmation.")]
    assert drafts
    for d in drafts:
        assert CONFIRM not in repr(d.payload) and "好" not in repr(d.payload)


def test_decline_and_no_pending_yes_produce_nothing():
    rig = Rig()
    rig.say("好")  # 沒有 pending 的「好」只是一般對話
    assert rig.signals == [] and rig.prompts == []
    rig.say(CONFIRM)
    rig.say("不要")
    rig.say("好")
    assert rig.signals == [] and "confirmation.declined" in rig.pub.types()


def test_other_actors_yes_is_ignored_even_in_same_conversation():
    rig = Rig()
    rig.say(CONFIRM)
    rig.say("好", speaker=OTHER)
    assert rig.signals == [] and "confirmation.foreign_actor_ignored" in rig.pub.types()
    rig.say("好")
    assert len(rig.signals) == 1


def test_undelivered_prompt_means_no_confirmation_possible():
    rig = Rig(deliver=False)
    rig.say(CONFIRM)
    rig.say("好")
    assert rig.signals == [] and rig.orch.metrics["intake.abandon_confirm_undelivered"] == 1


def test_without_prompt_pipeline_needs_confirmation_is_noop():
    rig = Rig(with_prompt=False)
    rig.say(CONFIRM)
    rig.say("好")
    assert rig.signals == [] and rig.prompts == []


def test_expired_pending_is_silent_and_tick_sweeps_with_event():
    rig = Rig()
    rig.say(CONFIRM)
    rig.clock.advance(95)
    rig.orch.tick()
    assert "confirmation.expired" in rig.pub.types() and rig.service.pending_count() == 0
    rig.say("好")
    assert rig.signals == []


def test_binding_changed_before_yes_does_not_execute():
    rig = Rig()
    rig.say(CONFIRM)
    rig.handler.item = "i2"
    rig.say("好")
    assert rig.signals == []
    assert "confirmation.superseded" in rig.pub.types()


def test_direct_supersedes_pending_and_still_signals():
    rig = Rig()
    rig.say(CONFIRM)
    rig.clock.advance(1)
    rig.say(DIRECT)
    assert len(rig.signals) == 1 and rig.signals[0].signal_id.startswith("abandon:turn:")
    assert rig.service.pending_count() == 0
    rig.say("好")
    assert len(rig.signals) == 1


def test_not_owner_does_not_propose():
    rig = Rig()
    rig.say(CONFIRM, speaker=OTHER)
    assert rig.prompts == [] and rig.service.pending_count() == 0


def test_handler_without_active_item_does_not_prompt():
    rig = Rig()
    rig.handler.available = False  # 目前格是 REST／不可放棄
    rig.say(CONFIRM)
    assert rig.prompts == [] and rig.signals == []


# ---- addressed（D3、D6）----


def test_direct_requires_addressed_or_one_on_one():
    rig = Rig(addressed=False, count=3)
    rig.say(DIRECT)
    assert rig.signals == [] and rig.orch.metrics["intake.abandon_unaddressed"] == 1
    solo = Rig(addressed=False, count=1)
    solo.say(DIRECT)
    assert len(solo.signals) == 1  # 1 對 1 視同對她說
    called = Rig(addressed=True, count=3)
    called.say(DIRECT)
    assert len(called.signals) == 1


def test_confirmation_not_proposed_when_not_addressed_in_group():
    rig = Rig(addressed=False, count=3)
    rig.say(CONFIRM)
    assert rig.prompts == [] and rig.service.pending_count() == 0


def test_unaddressed_yes_in_group_does_not_confirm():
    rig = Rig(addressed=True, count=3)
    rig.say(CONFIRM)
    rig.proposer.addressed = False  # 之後那句「好」不是對她說的
    rig.say("好")
    assert rig.signals == []
    rig.proposer.addressed = True
    rig.say("好")
    assert len(rig.signals) == 1


# ---- pause 與 absent（D1、D7）----


@pytest.mark.parametrize("text", ["今天先到這裡", "收工", "先這樣吧", "不用待在那邊了", "休息一下"])
def test_pause_phrases_do_not_abandon_or_prompt(text):
    rig = Rig()
    rig.say(text)
    assert rig.signals == [] and rig.prompts == [] and rig.service.pending_count() == 0
    assert rig.orch.metrics["intake.end_phrase_pause_ignored"] == 1


def test_absent_path_produces_no_abandon_and_no_confirmation():
    """D7：缺席端輸入（含 owner 已認證）不產生放棄訊號、提案或確認；確認話語在缺席端也不被處理。"""
    rig = Rig()
    rig.say(CONFIRM)  # 先在場提案
    n = len(rig.signals)
    for text in (DIRECT, CONFIRM, "好"):
        rig.seq += 1
        result = rig.orch.on_utterance(CTX, UtteranceFact(
            speaker_ref=OWNER, text=text, seq=rig.seq, source_location=PresenceLocation.DISCORD,
            source_endpoint_id="ep_discord"))
        assert result.reason_code == "absent_platform_input"
    # 在場時 host 是 desktop，discord 來源走缺席路徑
    assert len(rig.signals) == n == 0
    assert len(rig.prompts) == 1  # 只有在場那次提案送過提示
