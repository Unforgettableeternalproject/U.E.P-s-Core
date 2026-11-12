"""缺席端意圖回傳完整結果，以及仲裁用的選用 sink（離線，不需資料庫）。"""
from __future__ import annotations

from types import SimpleNamespace

from tests.uep.test_action_unavailable import AUTH, CTX
from uep.contracts.presence import PresenceLocation
from uep.decision.presence_absent_intent import AbsentIntentProposal
from uep.memory.identity import IdentityDirectory
from uep.turn.generations import TurnGenerations
from uep.turn.models import Proposal, ReplyMode, UtteranceFact
from uep.turn.orchestrator import UNVERIFIED_ACTOR, TurnOrchestrator

SECRET = "請你到 discord 來，這是只有缺席端才會講的秘密句子"
OWNER = "discord:100"


class Capture:
    def __init__(self) -> None:
        self.drafts: list = []

    def publish(self, draft, trusted):
        self.drafts.append(draft)
        return SimpleNamespace(duplicate=False, event=SimpleNamespace(event_id=f"evt_{len(self.drafts)}", trace_id="t"))

    def skipped(self) -> list[dict]:
        return [d.payload for d in self.drafts if d.event_type == "turn.skipped"]


class Pass:
    proposer_id = "t.pass"

    def propose(self, candidate):
        return Proposal(reply_mode=ReplyMode.PASS, reason_code="x", proposer_id="t.pass", proposer_version="1")


class FixedIntent:
    proposer_id = "t.intent"
    proposer_version = "1"

    def __init__(self, proposal: AbsentIntentProposal | None = None, boom: bool = False) -> None:
        self.proposal, self.boom = proposal, boom

    def propose(self, inp):
        if self.boom:
            raise RuntimeError("x")
        return self.proposal


def _orch(pub: Capture, **kw) -> TurnOrchestrator:
    identity = IdentityDirectory({OWNER: "actor_owner"}, {"actor_owner": "owner1"})
    return TurnOrchestrator(
        pub, TurnGenerations(), auth_provider=lambda: AUTH,  # type: ignore[arg-type]
        proposer=Pass(), identity=identity, host_location="desktop", run_id="run1", **kw,
    )


def _absent(orch: TurnOrchestrator, text: str = SECRET, speaker: str = OWNER, seq: int = 1, endpoint: str | None = "ep_discord"):
    return orch.on_utterance(CTX, UtteranceFact(
        speaker_ref=speaker, text=text, seq=seq, source_location=PresenceLocation.DISCORD, source_endpoint_id=endpoint,
    ))


def _request(compound=None) -> AbsentIntentProposal:
    return AbsentIntentProposal(
        intent="presence_request", confidence=0.83, target_location="discord", proposer_id="t.intent",
        proposer_version="1", compound=compound,
    )


def _with_intent(orch: TurnOrchestrator, intent: FixedIntent) -> TurnOrchestrator:
    orch._absent_intent_engine.proposer = intent  # type: ignore[assignment]
    return orch


def test_sink_receives_the_full_proposal_and_derived_ids():
    calls: list = []
    pub = Capture()
    orch = _with_intent(
        _orch(pub, on_presence_request=lambda p, **kw: calls.append((p, kw))), FixedIntent(_request(compound=True)),
    )
    _absent(orch)
    ((proposal, kw),) = calls
    assert (proposal.intent, proposal.target_location, proposal.compound) == ("presence_request", "discord", True)
    assert abs(proposal.confidence - 0.83) < 1e-9 and proposal.proposer_id == "t.intent"
    assert kw == {
        "request_id": f"{orch.run_id}:absent:1", "actor_ref": "actor_owner", "actor_authenticated": True,
        "is_owner": True, "source_platform": "discord",
    }
    _absent(orch)  # 同一句（同 seq）重送：request_id 不變
    assert calls[1][1]["request_id"] == calls[0][1]["request_id"]


def test_payload_carries_enum_values_only_and_no_raw_text():
    pub = Capture()
    orch = _with_intent(_orch(pub), FixedIntent(_request()))
    _absent(orch)
    (payload,) = pub.skipped()
    assert payload["presence_request"] is True and payload["presence_intent"] == "presence_request"
    assert payload["target_location"] == "discord"
    assert "秘密句子" not in repr(pub.drafts) and "discord 來" not in repr(payload)


def test_payload_without_target_or_decision_omits_the_new_keys():
    pub = Capture()
    orch = _with_intent(_orch(pub), FixedIntent(AbsentIntentProposal(intent="message", confidence=0.5)))
    _absent(orch)
    (payload,) = pub.skipped()
    assert payload["presence_request"] is False and payload["presence_intent"] == "message"
    assert "target_location" not in payload
    pub2 = Capture()
    _absent(_with_intent(_orch(pub2), FixedIntent(boom=True)))  # fallback：未判斷
    (payload2,) = pub2.skipped()
    assert payload2["presence_request"] is None and "presence_intent" not in payload2


def test_sink_not_called_for_other_intents_or_fallback():
    calls: list = []
    for intent in (AbsentIntentProposal(intent="message", confidence=0.5), AbsentIntentProposal(intent="other", confidence=0.1),
                   AbsentIntentProposal(intent="task", confidence=0.6), None):
        orch = _with_intent(_orch(Capture(), on_presence_request=lambda p, **kw: calls.append(p)),
                            FixedIntent(intent, boom=intent is None))
        _absent(orch)
    assert calls == []


def test_sink_exception_is_swallowed_counted_and_does_not_affect_the_turn():
    pub = Capture()

    def boom(p, **kw):
        raise RuntimeError("sink down")

    orch = _with_intent(_orch(pub, on_presence_request=boom), FixedIntent(_request()))
    result = _absent(orch)
    assert result.ticket is None and result.reason_code == "absent_platform_input"
    assert len(pub.skipped()) == 1 and orch.metrics["intake.presence_request_sink_error"] == 1


def test_behavior_is_identical_when_sinks_are_none():
    with_sink, without = Capture(), Capture()
    o1 = _with_intent(_orch(with_sink, on_presence_request=lambda p, **kw: None), FixedIntent(_request()))
    o2 = _with_intent(_orch(without), FixedIntent(_request()))
    r1, r2 = _absent(o1), _absent(o2)
    assert (r1.reason_code, r1.ticket) == (r2.reason_code, r2.ticket)
    assert with_sink.skipped() == without.skipped()


def test_sink_not_called_when_actor_is_unverified():
    calls: list = []
    pub = Capture()
    orch = _with_intent(_orch(pub, on_presence_request=lambda p, **kw: calls.append(kw)), FixedIntent(_request()))
    _absent(orch, speaker="discord:999", endpoint=None)  # 映射表沒有：actor 是占位
    assert calls == [] and pub.skipped()[0]["actor_id"] == UNVERIFIED_ACTOR
    assert orch.metrics["intake.presence_request_sink_skipped_unverified"] == 1


def test_default_rules_engine_also_reports_the_full_result():
    calls: list = []
    orch = _orch(Capture(), on_presence_request=lambda p, **kw: calls.append(p))
    _absent(orch, text="來桌面")  # 規則 proposer：到場請求＋目標
    assert calls and calls[0].intent == "presence_request" and calls[0].target_location == "desktop"
    assert calls[0].proposer_id.startswith("rules.")


# ---- 放棄工作 sink（judge 模組接上前只有接點）----


def test_real_abandon_judge_turn_path_signals_only_for_the_abandon_phrase():
    got: list = []
    orch = _orch(Capture(), on_abandon_signal=got.append)
    orch.on_utterance(CTX, UtteranceFact(speaker_ref=OWNER, text="放棄這次工作", seq=1, source_endpoint_id="ep"))
    assert len(got) == 1 and got[0].path == "turn" and got[0].actor_ref == "actor_owner"
    assert got[0].actor_authenticated is True and got[0].signal_id.startswith("abandon:turn:run1:")
    orch.on_utterance(CTX, UtteranceFact(speaker_ref=OWNER, text="不用做了", seq=2, source_endpoint_id="ep"))
    assert len(got) == 1  # 不是明確的放棄工作語句
    orch.on_utterance(CTX, UtteranceFact(speaker_ref=OWNER, text="放棄這次工作", seq=3))  # 沒有 endpoint：未認證
    orch.on_utterance(CTX, UtteranceFact(speaker_ref="discord:999", text="放棄這次工作", seq=4, source_endpoint_id="ep"))
    assert len(got) == 1  # 未認證與未映射 fail-closed


def test_real_abandon_judge_absent_path_and_no_sink_and_veto():
    got: list = []
    orch = _orch(Capture(), on_abandon_signal=got.append)
    _absent(orch, text="放棄這次工作")
    assert len(got) == 1 and got[0].path == "absent" and got[0].source_platform == "discord"
    _absent(orch, text="放棄這次工作", seq=2, endpoint=None)
    _absent(orch, text="不用做了", seq=3)
    assert len(got) == 1
    plain = _orch(Capture())  # 沒有 sink：不註冊決策點、不判斷
    assert plain.hub.engine("schedule.abandon_work") is None
    plain.on_utterance(CTX, UtteranceFact(speaker_ref=OWNER, text="放棄這次工作", seq=1, source_endpoint_id="ep"))
    vetoed_got: list = []
    vetoed = TurnOrchestrator(
        Capture(), TurnGenerations(), auth_provider=lambda: AUTH, proposer=Pass(),  # type: ignore[arg-type]
        identity=IdentityDirectory({OWNER: "actor_owner"}, {"actor_owner": "owner1"}),
        on_abandon_signal=vetoed_got.append, veto=lambda ctx: "audience_blocked",
    )
    vetoed.on_utterance(CTX, UtteranceFact(speaker_ref=OWNER, text="放棄這次工作", seq=1, source_endpoint_id="ep"))
    assert vetoed_got == []


def test_shared_hub_with_abandon_kind_already_registered_is_reused():
    from uep.decision import DecisionHub
    from uep.decision.schedule_abandon_work import register_abandon_work

    hub = DecisionHub()
    register_abandon_work(hub)
    got: list = []
    orch = _orch(Capture(), decision_hub=hub, on_abandon_signal=got.append)
    orch.on_utterance(CTX, UtteranceFact(speaker_ref=OWNER, text="放棄這次工作", seq=1, source_endpoint_id="ep"))
    assert len(got) == 1


def _wired(orch: TurnOrchestrator, calls: list) -> TurnOrchestrator:
    def judge(**kw):
        calls.append(kw)
        return "SIGNAL"

    orch._judge_abandon = judge  # type: ignore[method-assign]
    return orch


def test_abandon_sink_on_turn_path_only_for_authenticated_actor_after_veto():
    got, judged = [], []
    orch = _wired(_orch(Capture(), on_abandon_signal=got.append), judged)
    orch.on_utterance(CTX, UtteranceFact(speaker_ref=OWNER, text="算了不用做了", seq=1, source_endpoint_id="ep"))
    assert got == ["SIGNAL"] and judged[0]["path"] == "turn" and judged[0]["actor_ref"] == "actor_owner"
    assert judged[0]["utterance_id"] and judged[0]["actor_authenticated"] is True
    orch.on_utterance(CTX, UtteranceFact(speaker_ref=OWNER, text="算了", seq=2))  # 沒有簽發的 endpoint：未認證
    orch.on_utterance(CTX, UtteranceFact(speaker_ref="discord:999", text="算了", seq=3, source_endpoint_id="ep"))
    assert len(got) == 1  # 未認證與未映射都不判斷
    vetoed = _wired(TurnOrchestrator(
        Capture(), TurnGenerations(), auth_provider=lambda: AUTH, proposer=Pass(),  # type: ignore[arg-type]
        identity=IdentityDirectory({OWNER: "actor_owner"}, {"actor_owner": "owner1"}), on_abandon_signal=got.append,
        veto=lambda ctx: "audience_blocked",
    ), judged)
    n = len(judged)
    vetoed.on_utterance(CTX, UtteranceFact(speaker_ref=OWNER, text="算了", seq=1, source_endpoint_id="ep"))
    assert len(judged) == n  # 否決在前（audience 不允許等）：不判斷


def test_abandon_sink_on_absent_path_and_failures_are_isolated():
    got, judged = [], []
    orch = _wired(_orch(Capture(), on_abandon_signal=got.append), judged)
    _absent(orch)
    assert got == ["SIGNAL"] and judged[0]["path"] == "absent" and judged[0]["source_platform"] == "discord"

    def boom(signal):
        raise RuntimeError("down")

    bad = _wired(_orch(Capture(), on_abandon_signal=boom), [])
    result = _absent(bad)
    assert result.reason_code == "absent_platform_input" and bad.metrics["intake.abandon_signal_error"] == 1
    no_sink = _wired(_orch(Capture()), judged)
    n = len(judged)
    _absent(no_sink)
    assert len(judged) == n  # sink 為 None：完全不判斷
    unverified = _wired(_orch(Capture(), on_abandon_signal=got.append), judged)
    _absent(unverified, speaker="discord:999", endpoint=None)
    assert len(judged) == n
