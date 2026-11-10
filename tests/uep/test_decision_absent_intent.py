"""presence.absent_intent 決策點（V18 後續；不需資料庫）：rules proposer、shadow-only、fallback 映射、原文不落地。"""
from __future__ import annotations

import dataclasses
import threading
from datetime import UTC, datetime

import pytest

from uep.contracts.platform import AbsentInboundItem
from uep.decision import DecisionHub, DecisionRequest
from uep.decision.log import DecisionLogWriter
from uep.decision.presence_absent_intent import (
    INTENT_MESSAGE,
    INTENT_OTHER,
    INTENT_PRESENCE_REQUEST,
    KIND_PRESENCE_ABSENT_INTENT,
    AbsentIntentEngine,
    AbsentIntentInput,
    AbsentIntentProposal,
    AbsentIntentProposerRegistry,
    RulesAbsentIntentProposer,
    absent_intent_input_from_item,
    default_proposer_registry,
    judge_absent_intent,
    presence_absent_intent_spec,
    presence_request_from_result,
    validate_proposal,
)

SECRET = "只有缺席端才會講的秘密句子"


def _inp(text: str) -> AbsentIntentInput:
    return AbsentIntentInput(
        text=text, source_platform="discord", actor_ref="discord:123", actor_authenticated=True, is_owner=True,
    )


def _hub(engine: AbsentIntentEngine | None = None) -> tuple[DecisionHub, AbsentIntentEngine]:
    engine = engine or AbsentIntentEngine()
    hub = DecisionHub()
    hub.register(engine.spec, engine)
    return hub, engine


def _decide(hub: DecisionHub, text: str, n: int = 1):
    return judge_absent_intent(hub, _inp(text), request_id=f"r{n}", ref=f"es:run:s:{n}")


# ---- rules proposer ----


@pytest.mark.parametrize(("text", "target"), [
    ("過來", None), ("你過來一下", None), ("來桌面", "desktop"), ("到 discord", "discord"), ("回主機", "host"),
    ("移到網站", "website"), ("來這裡陪我", None), ("到場", None), ("切換到桌面", "desktop"),
])
def test_rules_detect_presence_requests(text, target):
    p = RulesAbsentIntentProposer().propose(_inp(text))
    assert p.intent == INTENT_PRESENCE_REQUEST and p.target_location == target
    assert validate_proposal(p) is None


@pytest.mark.parametrize("text", [
    "", "   ", "今天天氣如何", "我在 discord 看到一個梗", "主機的風扇好吵", "不要過來", "別回主機", "昨天她回主機了",
    "她為什麼不到 discord？", "你可以過來嗎？", "有沒有辦法來桌面", "哈", "過來" * 41,
])
def test_rules_stay_conservative(text):
    p = RulesAbsentIntentProposer().propose(_inp(text))
    assert p.intent == INTENT_OTHER and p.target_location is None


def test_rules_ignore_actor_fields():
    # 授權不看意圖；意圖也不看 actor：同一句話不因 actor 而改變
    base = RulesAbsentIntentProposer().propose(_inp("到 discord"))
    other = RulesAbsentIntentProposer().propose(dataclasses.replace(_inp("到 discord"), actor_authenticated=False, is_owner=False))
    assert base == other


def test_proposer_registry_replaceable():
    reg = default_proposer_registry()
    assert reg.ids() == ["rules.absent_intent"]

    class Fake:
        proposer_id = "laya.absent_intent"
        proposer_version = "1"

        def propose(self, inp):
            return AbsentIntentProposal(INTENT_MESSAGE, 0.9, None, self.proposer_id, self.proposer_version)

    reg.register("laya.absent_intent", Fake)
    with pytest.raises(ValueError):
        reg.register("laya.absent_intent", Fake)
    engine = AbsentIntentEngine(reg.create("laya.absent_intent"))
    hub, _ = _hub(engine)
    res = _decide(hub, "隨便")
    assert res.outcome == INTENT_MESSAGE and res.engine.id == "laya.absent_intent" and res.engine.tier == "model"
    assert presence_request_from_result(res) is False
    with pytest.raises(KeyError):
        AbsentIntentProposerRegistry().create("nope")


# ---- 決策點：只產生意圖，沒有授權 ----

_AUTH_WORDS = ("auth", "grant", "permit", "allow", "approv", "owner", "priority")


def test_result_is_intent_only_and_has_no_authorization_fields():
    hub, _ = _hub()
    res = _decide(hub, "到 discord")
    assert res.outcome == INTENT_PRESENCE_REQUEST and res.reason_code == "presence_request.discord"
    assert res.plan.route == "ignore" and res.plan.reply is None and res.plan.policy is None and res.plan.capability is None
    assert res.plan.placement.target_platform == "discord" and res.plan.placement.presence_intent == "request_move"
    names = {f.name for f in dataclasses.fields(AbsentIntentProposal)} | {f.name for f in dataclasses.fields(res)}
    names |= {f.name for f in dataclasses.fields(res.plan)}
    assert not [n for n in names if any(w in n for w in _AUTH_WORDS)]
    spec = presence_absent_intent_spec()
    assert spec.shadow and spec.mode == "sync" and spec.plan_sections == ()


def test_proposer_cannot_smuggle_authorization_via_extra_attributes():
    class Sneaky:
        proposer_id = "sneaky"
        proposer_version = "1"

        def propose(self, inp):
            p = AbsentIntentProposal(INTENT_PRESENCE_REQUEST, 1.0, "host", "sneaky", "1")
            object.__setattr__(p, "authorized", True)
            return p

    hub, _ = _hub(AbsentIntentEngine(Sneaky()))
    res = _decide(hub, "隨便")
    assert not hasattr(res, "authorized") and not hasattr(res.plan, "authorized")
    assert presence_request_from_result(res) is True  # 只是意圖


def test_outcome_to_presence_request_mapping():
    hub, _ = _hub()
    assert presence_request_from_result(_decide(hub, "過來")) is True
    assert presence_request_from_result(_decide(hub, "今天天氣如何")) is None  # other = 未判斷


# ---- fallback：例外／逾時／busy／無效 ----


class _Raises:
    proposer_id = "t.raise"
    proposer_version = "1"

    def propose(self, inp):
        raise RuntimeError("boom")


class _Slow:
    proposer_id = "t.slow"
    proposer_version = "1"

    def __init__(self):
        self.release = threading.Event()

    def propose(self, inp):
        self.release.wait(5)
        return AbsentIntentProposal(INTENT_OTHER, 0.5, None, self.proposer_id, self.proposer_version)


class _Invalid:
    proposer_id = "t.invalid"
    proposer_version = "1"

    def __init__(self, proposal):
        self.proposal = proposal

    def propose(self, inp):
        return self.proposal


def test_exception_maps_to_none():
    hub, engine = _hub(AbsentIntentEngine(_Raises()))
    res = _decide(hub, "過來")
    assert res.fallback and res.reason_code == engine.spec.on_error_reason and res.confidence == 0.0
    assert presence_request_from_result(res) is None


def test_timeout_then_busy_map_to_none():
    slow = _Slow()
    engine = AbsentIntentEngine(slow, spec=presence_absent_intent_spec(50))
    hub, _ = _hub(engine)
    try:
        first = _decide(hub, "過來", 1)
        assert first.fallback and first.reason_code == engine.spec.on_timeout_reason
        second = _decide(hub, "過來", 2)  # 上一個呼叫還在跑：立即 busy，不入佇列
        assert second.fallback and second.reason_code == engine.spec.on_busy_reason
        assert presence_request_from_result(first) is None and presence_request_from_result(second) is None
    finally:
        slow.release.set()


@pytest.mark.parametrize("bad", [
    None, "presence_request", AbsentIntentProposal("bogus", 0.5), AbsentIntentProposal(INTENT_OTHER, float("nan")),
    AbsentIntentProposal(INTENT_OTHER, 1.5), AbsentIntentProposal(INTENT_PRESENCE_REQUEST, 0.5, "mars"),
    AbsentIntentProposal(INTENT_OTHER, 0.5, "host"), AbsentIntentProposal(INTENT_OTHER, 0.5, None, "Bad Id"),
])
def test_invalid_output_maps_to_none(bad):
    hub, engine = _hub(AbsentIntentEngine(_Invalid(bad)))
    res = _decide(hub, "過來")
    assert res.fallback and res.reason_code == engine.spec.on_invalid_reason
    assert presence_request_from_result(res) is None


def test_missing_native_is_invalid_fallback():
    hub, _ = _hub()
    res = hub.decide(KIND_PRESENCE_ABSENT_INTENT, DecisionRequest(
        request_id="x", kind=KIND_PRESENCE_ABSENT_INTENT, native=None))
    assert res.fallback and presence_request_from_result(res) is None


# ---- 原文不落地 ----


class _ListSink:
    def __init__(self):
        self.rows = []

    def write(self, rows):
        self.rows.extend(dict(r) for r in rows)


def test_raw_text_never_reaches_decision_log_or_result():
    sink = _ListSink()
    writer = DecisionLogWriter(sink)
    hub, _ = _hub()
    writer.attach(hub)
    res = _decide(hub, SECRET + " 過來 到 discord")
    assert writer.flush()
    assert res.outcome == INTENT_PRESENCE_REQUEST
    assert len(sink.rows) == 1 and sink.rows[0]["kind"] == KIND_PRESENCE_ABSENT_INTENT
    assert sink.rows[0]["outcome"] == INTENT_PRESENCE_REQUEST and sink.rows[0]["role"] == "primary"
    assert SECRET not in repr(sink.rows)
    assert SECRET not in repr(res)
    # 請求與輸入本身的 repr 也不含原文（text 欄位 repr=False）
    req = DecisionRequest(request_id="r", kind=KIND_PRESENCE_ABSENT_INTENT, native=_inp(SECRET))
    assert SECRET not in repr(req) and SECRET not in repr(_inp(SECRET))
    writer.close()


# ---- 中性入口與 AbsentInboundItem adapter（只轉換欄位，不接通道）----


def _item(**kw) -> AbsentInboundItem:
    base = {
        "source_platform": "discord", "surface_ref": "dc_monitor", "actor_ref": "discord:123", "actor_authenticated": True,
        "is_owner": True, "raw_ref": "blob:abc", "length": 8, "received_at": datetime(2026, 11, 10, tzinfo=UTC),
    }
    return AbsentInboundItem(**{**base, **kw})


def test_adapter_maps_fields_only_and_text_stays_in_memory():
    inp = absent_intent_input_from_item(_item(), SECRET)
    assert (inp.source_platform, inp.surface_ref, inp.actor_ref) == ("discord", "dc_monitor", "discord:123")
    assert inp.actor_authenticated is True and inp.is_owner is True
    assert SECRET not in repr(inp) and inp.text == SECRET
    unauth = absent_intent_input_from_item(_item(actor_authenticated=False, is_owner=False, actor_ref="guest_1"), "過來")
    assert unauth.actor_authenticated is False and unauth.is_owner is False


def test_neutral_entry_accepts_item_input_and_logs_no_text():
    sink = _ListSink()
    writer = DecisionLogWriter(sink)
    hub, _ = _hub()
    writer.attach(hub)
    res = judge_absent_intent(hub, absent_intent_input_from_item(_item(), SECRET + " 到 discord"), request_id="r9", ref="blob:abc")
    assert writer.flush() and res.outcome == INTENT_PRESENCE_REQUEST
    assert sink.rows[0]["candidate_id"] == "blob:abc" and SECRET not in repr(sink.rows) + repr(res)
    writer.close()


def test_neutral_input_enforces_actor_invariants():
    with pytest.raises(ValueError):
        AbsentIntentInput(text="x", actor_ref="discord:1", actor_authenticated=False, is_owner=True)
    with pytest.raises(ValueError):
        AbsentIntentInput(text="x", actor_ref="Bernie")  # 顯示名稱
