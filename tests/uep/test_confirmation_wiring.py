"""確認流程的組裝層：``AbandonConfirmHandler``（行程投影）、事件鍵鎖定、設定預設。離線，不需資料庫。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from uep.config import UepSettings
from uep.confirm import ConfirmationService, bounded_ttl
from uep.confirm.service import CONFIRMATION_EVENT_KEYS, CONFIRMATION_EVENT_TYPES
from uep.contracts.confirmation import ConfirmationActionHandler
from uep.contracts.permission import RequesterTier
from uep.contracts.schedule import ActivityKind, PlanSource
from uep.echo.schedule_wiring import AbandonConfirmHandler
from uep.events import registry as event_registry

NOW = datetime(2026, 11, 13, 1, 30, tzinfo=UTC)  # 台北 09:30


def slot(**kw):
    base = {
        "availability": "available", "source": PlanSource.ITEM, "activity_kind": ActivityKind.WORK, "slot_state": "normal",
        "item_ref": "plan_1", "blueprint_start_at": NOW - timedelta(hours=1), "starts_at": NOW - timedelta(hours=1),
        "ends_at": NOW + timedelta(hours=2), "location": "discord",
    }
    base.update(kw)
    return SimpleNamespace(**base)


class Reader:
    def __init__(self, current) -> None:
        self.current = current

    def resolve(self, at=None):
        return self.current


def handler(current=None):
    reader = Reader(current or slot())
    return AbandonConfirmHandler(reader, tz_fn=lambda: "Asia/Taipei"), reader


def test_handler_satisfies_protocol_and_prepares_binding_for_active_work():
    h, _ = handler()
    assert isinstance(h, ConfirmationActionHandler)
    prepared = h.prepare(actor_ref="discord:1", now=NOW)
    assert prepared is not None
    assert prepared.binding["item_ref"] == "plan_1" and set(prepared.binding) == {"item_ref", "planned_start_at", "round_start"}
    assert prepared.summary.activity_kind == "work" and prepared.summary.location == "discord"


def test_handler_does_not_prepare_for_rest_unavailable_abandoned_or_default():
    for override in (
        {"activity_kind": ActivityKind.REST}, {"availability": "unavailable"}, {"slot_state": "abandoned"},
        {"source": PlanSource.DEFAULT}, {"source": PlanSource.OVERRIDE}, {"item_ref": None}, {"blueprint_start_at": None},
    ):
        h, _ = handler(slot(**override))
        assert h.prepare(actor_ref="discord:1", now=NOW) is None, override


def test_handler_prepare_fails_closed_when_reader_raises():
    h, reader = handler()
    reader.resolve = lambda at=None: (_ for _ in ()).throw(RuntimeError("down"))  # type: ignore[method-assign]
    assert h.prepare(actor_ref="discord:1", now=NOW) is None
    assert h.validate({"item_ref": "plan_1"}, now=NOW) is False


def test_validate_requires_same_item_planned_start_and_round():
    h, reader = handler()
    binding = dict(h.prepare(actor_ref="discord:1", now=NOW).binding)
    assert h.validate(binding, now=NOW) is True
    reader.current = slot(item_ref="plan_2")
    assert h.validate(binding, now=NOW) is False  # 行程項已變
    reader.current = slot(blueprint_start_at=NOW - timedelta(hours=3))
    assert h.validate(binding, now=NOW) is False  # 同項但不同次出現
    reader.current = slot()
    assert h.validate(binding, now=NOW + timedelta(days=1)) is False  # 跨輪次
    reader.current = slot(activity_kind=ActivityKind.REST)
    assert h.validate(binding, now=NOW) is False  # 已非工作格


def test_render_private_and_public_templates():
    h, _ = handler()
    summary = h.prepare(actor_ref="discord:1", now=NOW).summary
    assert h.render(summary, audience="scope_private", locale="zh-TW") == "要結束這次的〔工作〕行程（08:30–11:30）、回主機嗎？"
    assert h.render(summary, audience="scope_group_g1", locale="zh-TW") == "要結束目前的行程、回主機嗎？"
    assert h.render(summary, audience="scope_public", locale="zh-TW") == "要結束目前的行程、回主機嗎？"
    assert "好" not in h.render(summary, audience="scope_private", locale="zh-TW")  # 不誘導回覆
    assert h.required_terms(summary, audience="scope_private") == ("回主機", "工作", "08:30–11:30")


def test_service_with_real_handler_end_to_end():
    clock = [NOW]
    h, _ = handler()
    s = ConfirmationService(clock=lambda: clock[0])
    s.register_handler(h)
    r = s.propose(action="abandon_item", requester_ref="discord:1", source_platform="discord", conversation_ref="c:s",
                  source_utterance_ref="u1", actor_tier=RequesterTier.OWNER)
    assert r.status == "created"
    s.mark_prompt_delivered(r.proposal.proposal_id)
    res = s.try_resolve(conversation_ref="c:s", actor_ref="discord:1", actor_authenticated=True, source_platform="discord",
                        text="好", addressed=True, solo=False, actor_tier=RequesterTier.OWNER)
    assert res.outcome == "confirmed"


def test_event_registry_keys_match_service_constants():
    assert tuple(event_registry.CONFIRMATION_EVENT_TYPES) == CONFIRMATION_EVENT_TYPES
    for t in CONFIRMATION_EVENT_TYPES:
        spec = event_registry.DEFAULT_REGISTRY.lookup(t)
        assert set(CONFIRMATION_EVENT_KEYS) <= set(spec.payload_keys)
        assert "summary" not in spec.payload_keys and "expires_at" not in spec.payload_keys


def test_settings_defaults_and_bounds():
    s = UepSettings(_env_file=None)
    assert s.confirm_ttl_abandon_s == 90.0 and s.confirm_ttl_forced_sleep_s == 120.0
    assert s.confirm_polish_enabled is False
    assert bounded_ttl(s.confirm_ttl_abandon_s, 90.0) == 90.0 and bounded_ttl(5, 90.0) == 90.0
