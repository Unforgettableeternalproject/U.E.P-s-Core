"""确認服務（PM-2b §3A.2～§3A.4）：狀態機、辨認規則、冷卻、冪等、重啟＝過期。離線，不需資料庫。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from uep.confirm import (
    ConfirmationService,
    InMemoryConfirmationStore,
    bounded_ttl,
    default_policies,
)
from uep.confirm.lexicon import classify, parse_nonce
from uep.contracts.confirmation import ConfirmationSummary, PreparedConfirmation
from uep.contracts.permission import RequesterTier

NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
CONV = "conv1:seg1"
OWNER = "discord:1"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Handler:
    def __init__(self, action="abandon_item") -> None:
        self.action = action
        self.item = "i1"
        self.valid = True
        self.available = True

    def prepare(self, *, actor_ref, now):
        if not self.available:
            return None
        return PreparedConfirmation({"item_ref": self.item}, ConfirmationSummary(action=self.action, activity_kind="work"))

    def validate(self, binding, *, now):
        return self.valid and binding.get("item_ref") == self.item

    def render(self, summary, *, audience, locale):
        return "要結束這次的〔工作〕行程、回主機嗎？"


def svc(**kw):
    clock = Clock()
    s = ConfirmationService(clock=clock, **kw)
    h = Handler()
    s.register_handler(h)
    return s, h, clock


def propose(s, *, utt="utt1", actor=OWNER, tier=RequesterTier.OWNER, conv=CONV, platform="discord"):
    return s.propose(action="abandon_item", requester_ref=actor, source_platform=platform, conversation_ref=conv,
                     source_utterance_ref=utt, actor_tier=tier)


def delivered(s, **kw):
    r = propose(s, **kw)
    assert r.status == "created"
    return s.mark_prompt_delivered(r.proposal.proposal_id)


def resolve(s, text, *, actor=OWNER, auth=True, platform="discord", conv=CONV, addressed=True, solo=False,
            tier=RequesterTier.OWNER, **kw):
    return s.try_resolve(conversation_ref=conv, actor_ref=actor, actor_authenticated=auth, source_platform=platform,
                         text=text, addressed=addressed, solo=solo, actor_tier=tier, **kw)


# ---- 詞庫 ----


@pytest.mark.parametrize("text,kind", [
    ("好", "confirm"), ("好的。", "confirm"), ("是", "confirm"), ("OK", "confirm"), ("嗯對", "confirm"), ("Yes!", "confirm"),
    ("不", "decline"), ("不要", "decline"), ("算了", "decline"), ("先不要", "decline"), ("never mind", "decline"),
    ("好但是我再想想", "other"), ("好嗎？", "other"), ("不過", "other"), ("", "other"), ("今天天氣不錯", "other"),
    ("好" * 30, "other"),
])
def test_lexicon_whole_sentence(text, kind):
    assert classify(text) == kind


def test_nonce_channel_parsing():
    assert parse_nonce("確認 abcd1234") == "abcd1234" and parse_nonce("confirm ABCD1234") == "abcd1234"
    assert parse_nonce("確認") is None and parse_nonce("confirm abc") is None and parse_nonce("好") is None


def test_ttl_is_bounded_with_default_fallback():
    assert bounded_ttl(60, 90) == 60.0 and bounded_ttl(29, 90) == 90.0 and bounded_ttl(301, 90) == 90.0
    assert bounded_ttl("x", 90) == 90.0 and bounded_ttl(True, 90) == 90.0
    p = default_policies(ttl_abandon_s=45, ttl_forced_sleep_s=9999)
    assert p["abandon_item"].ttl_s == 45.0 and p["forced_sleep"].ttl_s == 120.0
    assert default_policies()["abandon_item"].ttl_s == 90.0
    assert p["forced_sleep"].allow_colloquial is False and p["forced_sleep"].max_intervening == 0


# ---- 提案與送達 ----


def test_propose_creates_pending_and_events_without_text():
    s, _, _ = svc()
    r = propose(s)
    assert r.status == "created" and r.send_prompt and r.proposal.status == "pending"
    assert r.proposal.expires_at - r.proposal.created_at == timedelta(seconds=90)
    ev = s.drain_events()
    assert [e.event_type for e in ev] == ["confirmation.proposed"] and ev[0].payload["ttl_s"] == 90
    assert s.drain_events() == []


def test_not_confirmable_before_prompt_delivered():
    s, _, _ = svc()
    propose(s)
    assert resolve(s, "好").outcome == "none"  # 提示尚未送達：「好」只是一般對話


def test_confirm_after_delivery_by_same_actor():
    s, _, _ = svc()
    delivered(s)
    res = resolve(s, "好")
    assert res.outcome == "confirmed" and res.consumed and res.proposal.status == "confirmed"
    assert res.proposal.resolved_reason == "confirmed"
    # 終態不可再變：再說一次「好」沒有 pending
    assert resolve(s, "好").outcome == "none"


def test_no_pending_means_yes_is_just_conversation():
    s, _, _ = svc()
    assert resolve(s, "好").outcome == "none" and resolve(s, "是").consumed is False


def test_other_actor_cannot_confirm_or_decline_and_is_only_counted():
    s, _, _ = svc()
    p = delivered(s)
    s.drain_events()
    for text in ("好", "不要"):
        res = resolve(s, text, actor="discord:2")
        assert res.outcome == "foreign_ignored" and not res.consumed
    ev = s.drain_events()
    assert [e.event_type for e in ev] == ["confirmation.foreign_actor_ignored"] * 2
    assert s.get(p.proposal_id).status == "pending" and s.metrics["confirmation.foreign_actor_ignored"] == 2


def test_unauthenticated_or_wrong_platform_or_low_tier_is_foreign():
    s, _, _ = svc()
    delivered(s)
    assert resolve(s, "好", auth=False).outcome == "foreign_ignored"
    assert resolve(s, "好", platform="website").outcome == "foreign_ignored"
    assert resolve(s, "好", tier=RequesterTier.ADMIN).outcome == "foreign_ignored"
    assert resolve(s, "好", conv="other:conv").outcome == "none"  # 不同對話
    assert resolve(s, "好").outcome == "confirmed"


def test_unaddressed_sentence_is_not_processed_unless_solo_or_reply():
    s, _, _ = svc()
    delivered(s)
    assert resolve(s, "好", addressed=False).outcome == "ignored"
    assert resolve(s, "好", addressed=False, solo=True).outcome == "confirmed"


def test_prompt_reply_counts_as_addressed():
    s, _, _ = svc()
    delivered(s)
    assert resolve(s, "好", addressed=False, is_prompt_reply=True).outcome == "confirmed"


def test_decline_and_decline_cooldown():
    s, _, clock = svc()
    delivered(s)
    res = resolve(s, "不要")
    assert res.outcome == "declined" and res.consumed and res.proposal.status == "declined"
    clock.advance(60)
    assert propose(s, utt="utt2").status == "declined_cooldown"  # 5 分鐘內被拒絕過：不再提示
    clock.advance(300)
    assert propose(s, utt="utt3").status == "created"


def test_expired_by_ttl_is_silent_no_op():
    s, _, clock = svc()
    p = delivered(s)
    clock.advance(91)
    assert resolve(s, "好").outcome == "none"
    got = s.get(p.proposal_id)
    assert got.status == "expired" and got.resolved_reason == "ttl"
    assert "confirmation.expired" in [e.event_type for e in s.drain_events()]


def test_expire_due_sweep_for_tick():
    s, _, clock = svc()
    delivered(s)
    clock.advance(95)
    assert len(s.expire_due()) == 1 and s.pending_count() == 0


def test_intervening_sentences_expire_after_max():
    s, _, _ = svc()  # abandon_item max_intervening=2
    p = delivered(s)
    assert resolve(s, "今天天氣不錯").outcome == "unrelated"
    assert resolve(s, "對了你吃飯了嗎").outcome == "unrelated"
    res = resolve(s, "還有一件事")
    assert res.outcome == "expired" and res.proposal.resolved_reason == "intervening"
    assert s.get(p.proposal_id).status == "expired"
    assert resolve(s, "好").outcome == "none"  # 很久之後的「好」不會誤確認


def test_trigger_sentences_do_not_count_as_intervening():
    s, _, _ = svc()
    delivered(s)
    for _ in range(5):
        assert resolve(s, "放棄這次工作", is_trigger=True).outcome == "unrelated"
    assert resolve(s, "好").outcome == "confirmed"


def test_binding_stale_supersedes_without_action():
    s, h, _ = svc()
    delivered(s)
    h.item = "i2"  # 行程項已變
    res = resolve(s, "好")
    assert res.outcome == "superseded" and res.consumed and res.proposal.resolved_reason == "binding_stale"
    assert res.proposal.status == "superseded"


def test_validate_exception_is_treated_as_stale():
    s, h, _ = svc()
    delivered(s)

    def boom(binding, *, now):
        raise RuntimeError("x")

    h.validate = boom  # type: ignore[method-assign]
    assert resolve(s, "好").outcome == "superseded"


def test_nonce_channel_confirms_and_wrong_nonce_is_unrelated():
    s, _, _ = svc()
    p = delivered(s)
    assert resolve(s, "確認 deadbeef").outcome == "unrelated"
    assert resolve(s, f"確認 {p.nonce}").outcome == "confirmed"


def test_forced_sleep_policy_only_accepts_nonce_and_any_unrelated_expires():
    s, _, clock = svc()
    s.register_handler(Handler("forced_sleep"))
    r = s.propose(action="forced_sleep", requester_ref=OWNER, source_platform="discord", conversation_ref=CONV,
                  source_utterance_ref="fs1", actor_tier=RequesterTier.OWNER)
    assert r.status == "created" and r.proposal.expires_at - r.proposal.created_at == timedelta(seconds=120)
    p = s.mark_prompt_delivered(r.proposal.proposal_id)
    assert resolve(s, "好").outcome == "expired"  # 口語確認不被接受＝無關發言，max_intervening=0 立即作廢
    clock.advance(25)  # 過了提示冷卻
    r2 = s.propose(action="forced_sleep", requester_ref=OWNER, source_platform="discord", conversation_ref=CONV,
                   source_utterance_ref="fs2", actor_tier=RequesterTier.OWNER)
    p2 = s.mark_prompt_delivered(r2.proposal.proposal_id)
    assert p2 is not None and p is not None
    assert resolve(s, f"confirm {p2.nonce}").outcome == "confirmed"


def test_same_utterance_is_idempotent():
    s, _, _ = svc()
    first = propose(s)
    again = propose(s)
    assert again.status == "duplicate" and again.proposal.proposal_id == first.proposal.proposal_id
    assert not again.send_prompt and s.pending_count() == 1


def test_same_key_pending_prompt_cooldown_then_refresh():
    s, _, clock = svc()
    p = delivered(s)
    clock.advance(5)
    assert propose(s, utt="utt2").status == "cooldown"  # 20 秒內不重發
    clock.advance(30)
    r = propose(s, utt="utt3")
    assert r.status == "refreshed" and r.send_prompt and r.proposal.proposal_id == p.proposal_id
    assert r.proposal.expires_at > p.expires_at and s.pending_count() == 1


def test_different_binding_replaces_old_pending():
    s, h, _ = svc()
    first = delivered(s)
    h.item = "i2"
    r = propose(s, utt="utt2")
    assert r.status == "created" and s.get(first.proposal_id).resolved_reason == "replaced"
    assert s.pending_count() == 1


def test_handler_unavailable_or_low_tier_does_not_propose():
    s, h, _ = svc()
    h.available = False
    assert propose(s).status == "unavailable"
    h.available = True
    assert propose(s, tier=RequesterTier.ADMIN).status == "unavailable"
    s2 = ConfirmationService(clock=Clock())  # 沒有 handler
    assert propose(s2).status == "unavailable"


def test_pending_limit():
    s, _, _ = svc(max_pending=2)
    for i in range(2):
        assert propose(s, utt=f"u{i}", actor=f"discord:{i + 10}").status == "created"
    assert propose(s, utt="u9", actor="discord:99").status == "limit"


def test_supersede_for_direct_and_undelivered():
    s, _, _ = svc()
    p = delivered(s)
    out = s.supersede_for_direct(conversation_ref=CONV, requester_ref=OWNER)
    assert [x.resolved_reason for x in out] == ["direct_issued"] and s.get(p.proposal_id).status == "superseded"
    s2, _, _ = svc()
    r = propose(s2)
    assert s2.mark_undelivered(r.proposal.proposal_id).resolved_reason == "undelivered"
    assert s2.mark_prompt_delivered(r.proposal.proposal_id) is None  # 終態不可再變


def test_restart_means_all_expired():
    """記憶體 store：新服務沒有任何 pending（重啟視同過期，不補發事件）。"""
    s, _, _ = svc()
    delivered(s)
    fresh = ConfirmationService(clock=Clock(), store=InMemoryConfirmationStore())
    fresh.register_handler(Handler())
    assert resolve(fresh, "好").outcome == "none" and fresh.drain_events() == []


def test_render_prompt_uses_template_and_polish_is_off_by_default():
    class Polisher:
        def polish(self, template_text, summary):
            return "隨便潤飾，沒有必要語意詞"

    s, _, _ = svc(polisher=Polisher())
    p = delivered(s)
    assert s.render_prompt(p, audience="private") == "要結束這次的〔工作〕行程、回主機嗎？"


def test_polish_enabled_falls_back_when_terms_missing_or_raises():
    class Handler2(Handler):
        def required_terms(self, summary, *, audience):
            return ("回主機",)

    class Good:
        def polish(self, template_text, summary):
            return "要不要結束這次的工作行程、回主機呢"

    class Bad:
        def polish(self, template_text, summary):
            return "要不要結束呢"

    class Boom:
        def polish(self, template_text, summary):
            raise RuntimeError("x")

    for polisher, expected_polished in ((Good(), True), (Bad(), False), (Boom(), False)):
        s = ConfirmationService(clock=Clock(), polisher=polisher, polish_enabled=True)
        s.register_handler(Handler2())
        p = delivered(s)
        out = s.render_prompt(p, audience="private")
        assert (out == "要不要結束這次的工作行程、回主機呢") is expected_polished
