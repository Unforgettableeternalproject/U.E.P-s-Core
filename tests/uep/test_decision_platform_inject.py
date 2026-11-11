"""平台宣告注入第 5 步（決策層側）：loop.consult 的 current_platform／available_executors 由系統注入。

注入策略是「覆蓋」：設定了 platform_facts 後，模型自帶的注入鍵一律被剝除再由系統重寫（不是拒收），
因此偽造值無效；來源失敗／Presence 非 ACTIVE 時不注入，規則 fail-closed。不需資料庫。
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from tests.uep.test_assembly_decision_log import _host, _kw
from tests.uep.test_platform_context import FakePresence, _view
from uep.assembly import assemble_core
from uep.contracts.presence import PresenceStatus
from uep.decision import DecisionHub, DecisionRequest
from uep.decision.log import primary_row
from uep.decision.loop_consult import (
    ESC_PLACEMENT_REVIEW,
    ESC_SCOPE_EXCEEDED,
    KIND_LOOP_CONSULT,
    LoopConsultEngine,
    placement_facts,
    register_loop_consult,
)
from uep.echo.platform_context import CoarseExecutorSource, PlatformContextProvider
from uep.events.bus import EventBus
from uep.events.publisher import EventPublisher

SCOPE = {"capability_tags": ["decision.consult"], "modalities": ["text"], "platforms": ["desktop", "website"]}


def _facts(sub_kind: str = "placement", **over):
    base = {
        "sub_kind": sub_kind, "run_id": "run_1", "step_id": "step_1", "ordinal": 0,
        "frozen": SCOPE, "capability_snapshot": SCOPE,
        "target_platform": "desktop", "executor_ref": "desktop",
    }
    base.update(over)
    return base


def _hub(view, executors=None):
    """用真正的 PlatformContextProvider 接 hub，與 assembly 同一條注入路徑。"""
    presence = FakePresence(view)
    provider = PlatformContextProvider(presence, executors or CoarseExecutorSource())
    hub = DecisionHub()
    register_loop_consult(hub, platform_facts=lambda: placement_facts(provider.snapshot()))
    return hub, provider


def _decide(hub, facts):
    request = DecisionRequest(request_id="act_1", kind=KIND_LOOP_CONSULT, facts=facts)
    return request, hub.decide(KIND_LOOP_CONSULT, request)


def test_active_desktop_placement_is_evaluable():
    hub, _ = _hub(_view(location="desktop"))
    _, result = _decide(hub, _facts())
    assert result.outcome == "placement_set" and result.plan.placement.executor_ref == "desktop"
    assert result.reason_code == "placement_same_platform"


def test_active_desktop_cross_platform_still_escalates():
    hub, _ = _hub(_view(location="desktop"))
    _, result = _decide(hub, _facts(target_platform="website", executor_ref="website"))
    assert result.outcome == "escalate" and result.reason_code == ESC_PLACEMENT_REVIEW


def test_active_desktop_executor_not_in_injected_list_is_scope_exceeded():
    hub, _ = _hub(_view(location="desktop"))
    _, result = _decide(hub, _facts(executor_ref="other.exec"))
    assert result.outcome == "escalate" and result.reason_code == ESC_SCOPE_EXCEEDED


def test_move_intent_still_escalates():
    hub, _ = _hub(_view(location="desktop"))
    _, result = _decide(hub, _facts(presence_intent="request_move"))
    assert result.outcome == "escalate" and result.reason_code == ESC_SCOPE_EXCEEDED


def test_presence_not_active_is_placement_review():
    for status in (PresenceStatus.NONE, PresenceStatus.RECOVERING):
        hub, _ = _hub(_view(status=status, location=None))
        _, result = _decide(hub, _facts())
        assert result.outcome == "escalate" and result.reason_code == ESC_PLACEMENT_REVIEW


def test_expired_lease_is_placement_review():
    hub, _ = _hub(_view(expires_at=datetime(2000, 1, 1, tzinfo=UTC)))
    _, result = _decide(hub, _facts())
    assert result.reason_code == ESC_PLACEMENT_REVIEW


def test_forged_current_platform_has_no_effect_when_presence_none():
    hub, _ = _hub(_view(status=PresenceStatus.NONE, location=None))
    forged = _facts(current_platform="desktop", available_executors=["desktop"])
    request, result = _decide(hub, forged)
    assert result.outcome == "escalate" and result.reason_code == ESC_PLACEMENT_REVIEW
    assert "current_platform" not in request.facts  # 偽造值被剝除，不是留著


def test_forged_values_are_overwritten_when_active():
    hub, _ = _hub(_view(location="desktop"))
    forged = _facts(
        target_platform="website", executor_ref="website", current_platform="website",
        available_executors=["website"], platform_context_version=999,
    )
    request, result = _decide(hub, forged)
    assert result.outcome == "escalate" and result.reason_code == ESC_PLACEMENT_REVIEW
    assert request.facts["current_platform"] == "desktop" and request.facts["available_executors"] == ["desktop"]
    assert request.facts["platform_context_version"] != 999


def test_forged_executors_do_not_widen():
    hub, _ = _hub(_view(location="desktop"))
    _, result = _decide(hub, _facts(executor_ref="evil.exec", available_executors=["evil.exec"]))
    assert result.outcome == "escalate" and result.reason_code == ESC_SCOPE_EXCEEDED


def test_missing_available_executors_is_scope_exceeded():
    engine = LoopConsultEngine(platform_facts=lambda: {"current_platform": "desktop"})
    result = engine.consult(_facts(available_executors=["desktop"]))  # 模型自帶的清單也被剝除
    assert result.escalate.needed and result.reason_code == ESC_SCOPE_EXCEEDED


def test_provider_failure_or_none_is_fail_closed():
    def boom():
        raise RuntimeError("x")

    for source in (boom, lambda: None):
        engine = LoopConsultEngine(platform_facts=source)
        result = engine.consult(_facts(current_platform="desktop", available_executors=["desktop"]))
        assert result.escalate.needed and result.reason_code == ESC_PLACEMENT_REVIEW


def test_non_placement_keys_stripped_and_version_not_injected():
    hub, _ = _hub(_view(location="desktop"))
    request, result = _decide(hub, _facts("speak", user_present=True, result_ready=True, current_platform="x"))
    assert result.outcome == "respond"
    assert not {"current_platform", "available_executors", "platform_context_version"} & set(request.facts)


def test_decision_log_row_has_platform_context_version():
    hub, provider = _hub(_view(location="desktop"))
    request, result = _decide(hub, _facts())
    row = primary_row(request, result, hub.registry.get(KIND_LOOP_CONSULT), 1)
    assert row["platform_context_version"] == provider.snapshot().version >= 1
    assert row["outcome"] == "placement_set" and row["sub_kind"] == "placement"


def test_decision_log_observer_sees_injected_facts():
    hub, provider = _hub(_view(location="desktop"))
    seen = []
    hub.add_observer(lambda req, res, spec, ms: seen.append(primary_row(req, res, spec, ms)))
    _decide(hub, _facts())
    assert seen[0]["platform_context_version"] == provider.snapshot().version


def test_log_version_follows_snapshot_change():
    presence = FakePresence(_view(location="desktop", row_version=3))
    provider = PlatformContextProvider(presence)
    hub = DecisionHub()
    register_loop_consult(hub, platform_facts=lambda: placement_facts(provider.snapshot()))
    spec = hub.registry.get(KIND_LOOP_CONSULT)
    r1, res1 = _decide(hub, _facts())
    presence.view = _view(location="website", row_version=4)
    r2, res2 = _decide(hub, _facts(target_platform="website", executor_ref="website"))
    v1 = primary_row(r1, res1, spec, 1)["platform_context_version"]
    v2 = primary_row(r2, res2, spec, 1)["platform_context_version"]
    assert v2 == v1 + 1 and res2.outcome == "placement_set"


def test_direct_consult_does_not_mutate_callers_facts():
    engine = LoopConsultEngine(platform_facts=lambda: {"current_platform": "desktop", "available_executors": ["desktop"]})
    facts = _facts(current_platform="website")
    result = engine.consult(facts)
    assert result.outcome == "placement_set" and facts["current_platform"] == "website"


def test_without_provider_facts_untouched_for_standalone_use():
    engine = LoopConsultEngine()
    result = engine.consult(_facts(current_platform="desktop", available_executors=["desktop"]))
    assert result.outcome == "placement_set"
    assert engine.consult(_facts()).reason_code == ESC_PLACEMENT_REVIEW


def test_placement_facts_pure_function():
    ctx = PlatformContextProvider(FakePresence(_view(location="desktop"))).snapshot()
    assert placement_facts(ctx) == {
        "available_executors": ["desktop"], "platform_context_version": ctx.version, "current_platform": "desktop",
    }
    none_ctx = PlatformContextProvider(FakePresence(_view(status=PresenceStatus.NONE, location=None))).snapshot()
    assert "current_platform" not in placement_facts(none_ctx) and placement_facts(none_ctx)["available_executors"] == []


# ---- 組裝層 ----

def _assembled(tmp_path):
    bus = EventBus()
    publisher = EventPublisher(MagicMock(), bus)
    host = _host(publisher)
    core = assemble_core(MagicMock(), **_kw(tmp_path, publisher, bus, host, decision_log=False))
    return core, host


def test_assembly_registers_loop_consult_with_injection(tmp_path):
    core, host = _assembled(tmp_path)
    try:
        engine = host.decision_hub.engine(KIND_LOOP_CONSULT)
        assert isinstance(engine, LoopConsultEngine) and engine._platform_facts is not None
        # 組裝後 Presence 非 ACTIVE（無 Lease）：模型偽造的平台值無效，走 placement_review
        request = DecisionRequest(
            request_id="act_9", kind=KIND_LOOP_CONSULT,
            facts=_facts(current_platform="desktop", available_executors=["desktop"]),
        )
        result = host.decision_hub.decide(KIND_LOOP_CONSULT, request)
        assert result.outcome == "escalate" and result.reason_code == ESC_PLACEMENT_REVIEW
    finally:
        core.shutdown()


def test_assembly_without_echo_registers_nothing(tmp_path):
    bus = EventBus()
    publisher = EventPublisher(MagicMock(), bus)
    core = assemble_core(MagicMock(), **_kw(tmp_path, publisher, bus, None, decision_log=False))
    core.shutdown()
