"""V18 缺席端輸入：source_location 契約與決策層否決（需 PostgreSQL）。

只看位置：Host 不持有 Lease，或輸入來源不是 Host 所在的 location，一律不開 turn、不進 buffer、proposer 看不到文字。
"""
from __future__ import annotations

import pytest

from tests.uep.echo_support import SPEAKER, build_rig
from tests.uep.test_p3_s5_slot import Fixed
from uep.assembly import AssemblyError, assemble_core, check_source_location_policy
from uep.contracts.presence import PresenceLocation
from uep.turn.aggregator import BufferedAggregator
from uep.turn.decision_config import AggregatorConfig
from uep.turn.models import UtteranceFact

pytestmark = pytest.mark.usefixtures("session_factory")

SECRET_TEXT = "只有缺席端才會講的秘密句子"


class Spy(Fixed):
    """記錄 propose 與 ListenerAware 看到的文字。"""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.listened: list[str] = []

    def propose(self, candidate):
        self.calls += 1
        return super().propose(candidate)

    def is_listener_continuation(self, text: str) -> bool:
        self.listened.append(text)
        return False


@pytest.fixture
def rig(session_factory, tmp_path):
    made = []

    def _build(**over):
        r = build_rig(session_factory, tmp_path, **over)
        made.append(r)
        return r

    yield _build
    for r in made:
        shutdown = getattr(r.core, "shutdown", None)
        if shutdown is not None:
            shutdown()


def shadow_records(r):
    return [e for e in r.events(types=("turn.skipped",)) if e.payload.get("source_location") is not None]


def assert_no_turn(r, spy):
    assert spy.calls == 0 and spy.listened == []
    assert not r.events(types=("turn.started", "output.intent_created", "output.admitted", "input.utterance_final"))


def test_wrong_platform_is_vetoed_with_a_shadow_record(rig):
    spy = Spy()
    r = rig(host_kwargs={"proposer": spy})
    result = r.say(SECRET_TEXT, source_location=PresenceLocation.DISCORD)
    assert result.ticket is None and result.reason_code == "absent_platform_input"
    assert_no_turn(r, spy)
    (rec,) = shadow_records(r)
    p = rec.payload
    assert p["reason_code"] == "absent_platform_input" and p["source_location"] == "discord"
    assert p["len"] == len(SECRET_TEXT) and p["speaker_ref"] == SPEAKER and p["actor_id"] == "actor_a"
    assert p["utterance_ref"].startswith("es:")
    assert r.host.orchestrator.metrics["intake.absent_input"] == 1
    assert SECRET_TEXT not in repr(rec.payload) and SECRET_TEXT not in repr(result)


def test_no_lease_vetoes_even_same_platform_input(rig):
    spy = Spy()
    r = rig(host_kwargs={"proposer": spy}, start=False)  # 她在主機：Host 沒有 Lease
    r.host.orchestrator._auth_provider = lambda: None
    result = r.host.orchestrator.on_utterance(
        r.host.session, UtteranceFact(SPEAKER, SECRET_TEXT, 1, source_location=PresenceLocation.DESKTOP)
    )
    assert result.reason_code == "presence_inactive" and result.ticket is None
    assert_no_turn(r, spy)
    (rec,) = shadow_records(r)
    assert rec.payload["reason_code"] == "presence_inactive" and rec.payload["source_location"] == "desktop"


def test_no_lease_has_priority_over_source_mismatch(rig):
    spy = Spy()
    r = rig(host_kwargs={"proposer": spy}, start=False)
    r.host.orchestrator._auth_provider = lambda: None
    result = r.host.orchestrator.on_utterance(
        r.host.session, UtteranceFact(SPEAKER, SECRET_TEXT, 1, source_location=PresenceLocation.DISCORD)
    )
    assert result.reason_code == "presence_inactive"


def test_owner_from_absent_platform_is_vetoed_and_marked_is_owner(rig):
    spy = Spy()
    r = rig(host_kwargs={"proposer": spy})
    result = r.say(SECRET_TEXT, speaker=SPEAKER, source_location=PresenceLocation.WEBSITE)  # SPEAKER 映射到 owner
    assert result.ticket is None and result.reason_code == "absent_platform_input"
    assert_no_turn(r, spy)
    (rec,) = shadow_records(r)
    assert rec.payload["is_owner"] == "true"


def test_unmapped_identity_is_not_marked_owner(rig):
    r = rig(host_kwargs={"proposer": Spy()})
    r.say(SECRET_TEXT, speaker="discord:999", source_location=PresenceLocation.DISCORD)
    (rec,) = shadow_records(r)
    assert rec.payload["is_owner"] == "false"


def test_is_owner_is_unknown_without_identity_directory(rig):
    r = rig(host_kwargs={"proposer": Spy()}, start=False)
    r.host.orchestrator._identity = None
    r.host.orchestrator._auth_provider = lambda: None
    r.host.orchestrator.on_utterance(r.host.session, UtteranceFact(SPEAKER, "x", 1))
    (rec,) = r.events(types=("turn.skipped",))
    assert rec.payload["is_owner"] == "unknown"


def test_same_platform_opens_a_turn(rig):
    spy = Spy()
    r = rig(host_kwargs={"proposer": spy})
    result = r.say("你好", source_location=PresenceLocation.DESKTOP)
    assert result.ticket is not None and spy.calls == 1
    assert r.host.orchestrator.metrics["intake.source_location_missing"] == 0
    assert not shadow_records(r)


def test_missing_source_location_keeps_status_quo_and_counts(rig):
    spy = Spy()
    r = rig(host_kwargs={"proposer": spy})
    result = r.say("你好")
    assert result.ticket is not None
    assert r.host.orchestrator.metrics["intake.source_location_missing"] == 1


def test_require_source_location_rejects_missing_but_not_matching(rig):
    spy = Spy()
    r = rig(host_kwargs={"proposer": spy}, assemble_over={"require_source_location": True})
    missing = r.say("你好")
    assert missing.ticket is None and missing.reason_code == "source_location_missing" and spy.calls == 0
    (rec,) = shadow_records(r)
    assert rec.payload["source_location"] == "missing"
    assert r.say("你好", source_location=PresenceLocation.DESKTOP).ticket is not None
    assert r.say("你好", source_location=PresenceLocation.DISCORD).reason_code == "absent_platform_input"


def test_invalid_source_location_is_treated_as_mismatch(rig):
    spy = Spy()
    r = rig(host_kwargs={"proposer": spy})
    result = r.say(SECRET_TEXT, source_location="mars")
    assert result.ticket is None and result.reason_code == "absent_platform_input"
    assert r.host.orchestrator.metrics["intake.invalid_source_location"] == 1
    assert spy.calls == 0


def test_string_value_of_a_valid_location_is_coerced(rig):
    r = rig(host_kwargs={"proposer": Spy()})
    assert r.say("你好", source_location="desktop").ticket is not None


def test_absent_text_never_reaches_the_aggregator_or_listener(rig):
    spy = Spy()
    agg = BufferedAggregator(AggregatorConfig())
    r = rig(host_kwargs={"proposer": spy, "aggregator": agg})
    fed: list[str] = []
    real_feed = agg.feed

    def spy_feed(seg):
        fed.append(seg.text)
        return real_feed(seg)

    agg.feed = spy_feed  # type: ignore[method-assign]
    r.say(SECRET_TEXT, source_location=PresenceLocation.DISCORD)
    assert fed == [] and spy.listened == []


def test_assembly_refuses_non_desktop_adapter_without_the_switch(session_factory):
    check_source_location_policy(("desktop",), False)
    check_source_location_policy(("desktop", "discord"), True)
    with pytest.raises(AssemblyError, match="UEP_REQUIRE_SOURCE_LOCATION"):
        check_source_location_policy(("desktop", "discord"), False)
    with pytest.raises(AssemblyError, match="UEP_REQUIRE_SOURCE_LOCATION"):
        assemble_core(
            session_factory, input_locations=("website",), require_source_location=False, bus=None, publisher=None,
            control=None, adapter=None, executors=None, tool_resolver=None, turn_generation_source=None,
            presence_audience=None,
        )


def test_shadow_record_carries_presence_request_and_actor_authenticated(rig):
    r = rig(host_kwargs={"proposer": Spy()})
    r.say(SECRET_TEXT, source_location=PresenceLocation.DISCORD)  # 無 ingress 簽發的 endpoint：資訊不足
    (rec,) = shadow_records(r)
    assert rec.payload["presence_request"] is None and rec.payload["actor_authenticated"] is False
    r.say(SECRET_TEXT, source_location=PresenceLocation.DISCORD, source_endpoint_id="ep_discord")
    assert shadow_records(r)[-1].payload["actor_authenticated"] is True  # 映射命中且 endpoint 已簽發


def test_presence_request_hook_is_replaceable_and_failure_is_isolated(rig):
    r = rig(host_kwargs={"proposer": Spy()})
    r.host.orchestrator._presence_request_hook = lambda fact: True
    r.say("到場", source_location=PresenceLocation.DISCORD)
    assert shadow_records(r)[-1].payload["presence_request"] is True

    def boom(fact):
        raise RuntimeError("x")

    r.host.orchestrator._presence_request_hook = boom
    result = r.say("到場", source_location=PresenceLocation.DISCORD)
    assert result.ticket is None and shadow_records(r)[-1].payload["presence_request"] is None
