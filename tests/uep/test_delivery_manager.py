"""最小 Delivery Manager（需 PostgreSQL；未設 UEP_TEST_DATABASE_URL 時 skip）。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest

from tests.uep.factories import make_intent
from uep.fakes.crash import SimulatedCrash
from uep.fakes.output import FakeOutputAdapter
from uep.output.delivery import DeliveryEvidence as E
from uep.output.delivery import DeliveryStatus as S
from uep.output.delivery import IllegalDeliveryTransition
from uep.output.manager import DeliveryManager

pytestmark = pytest.mark.usefixtures("session_factory")


def _mgr(session_factory, *, ttl=timedelta(seconds=30), hook=None, **kw):
    adapter = FakeOutputAdapter(**kw)
    return DeliveryManager(session_factory, adapter, claim_ttl=ttl, crash_hook=hook), adapter


def _crash_at(point):
    def hook(p):
        if p == point:
            raise SimulatedCrash(p)
    return hook


def test_played_adapter_reaches_rendered_played_once(session_factory):
    mgr, adapter = _mgr(session_factory)
    intent = make_intent()
    rec = mgr.dispatch(intent)
    assert rec.status is S.RENDERED_PLAYED and rec.delivery_evidence is E.LOCAL_PLAYBACK
    again = mgr.dispatch(intent)  # 冪等：不重送
    assert again.delivery_id == rec.delivery_id and adapter.send_count(intent.output_id, 0) == 1


def test_transport_only_stops_at_sent(session_factory):
    mgr, _ = _mgr(session_factory, default_mode="transport_only")
    rec = mgr.dispatch(make_intent())
    assert rec.status is S.SENT and rec.delivery_evidence is E.TRANSPORT_SENT  # VC-5-2


def test_lost_ack_becomes_unknown_then_resolves_without_resend(session_factory):
    mgr, adapter = _mgr(session_factory, default_mode="lose_ack")
    intent = make_intent()
    rec = mgr.dispatch(intent)
    assert rec.status is S.UNKNOWN  # VC-5-3
    resolved = mgr.resolve_unknown(rec.delivery_id)
    assert resolved.status in (S.SENT, S.RENDERED_PLAYED)
    assert adapter.send_count(intent.output_id, 0) == 1 and adapter.effect_count(intent.output_id, 0) == 1


def test_unknown_stays_when_lookup_lost_and_never_resends(session_factory):
    mgr, adapter = _mgr(session_factory, default_mode="lose_ack")
    intent = make_intent()
    rec = mgr.dispatch(intent)
    adapter.lookup_lost = True
    assert mgr.resolve_unknown(rec.delivery_id).status is S.UNKNOWN
    assert mgr.dispatch(intent).status is S.UNKNOWN  # 重複 dispatch 也不補送
    assert adapter.send_count(intent.output_id, 0) == 1


def test_unknown_with_confirmed_absence_fails_with_failure_code(session_factory):
    mgr, adapter = _mgr(session_factory, default_mode="lose_ack")
    rec = mgr.dispatch(make_intent())
    adapter.delivered.clear()  # 平台查無此 dispatch_id
    resolved = mgr.resolve_unknown(rec.delivery_id)
    assert resolved.status is S.FAILED and resolved.failure == {"code": "NOT_SENT", "retryable": True}


def test_send_failed_marks_failed(session_factory):
    mgr, _ = _mgr(session_factory, default_mode="fail")
    rec = mgr.dispatch(make_intent())
    assert rec.status is S.FAILED and rec.failure == {"code": "SEND_FAILED", "retryable": False}


def test_unknown_stays_when_adapter_not_authoritative(session_factory):
    mgr, adapter = _mgr(session_factory, default_mode="lose_ack")
    rec = mgr.dispatch(make_intent())
    adapter.delivered.clear()
    adapter.lookup_authoritative = False  # 查不到不等於沒送
    assert mgr.resolve_unknown(rec.delivery_id).status is S.UNKNOWN
    adapter.lookup_authoritative = True
    assert mgr.resolve_unknown(rec.delivery_id).status is S.FAILED


@pytest.mark.parametrize("point", ["delivery.after_prepare", "delivery.after_claim", "delivery.after_send"])
def test_crash_hooks_recover_with_single_effect(session_factory, point):
    intent = make_intent()
    mgr, adapter = _mgr(session_factory, hook=_crash_at(point))
    with pytest.raises(SimulatedCrash):
        mgr.dispatch(intent)
    # 重啟：舊 claim 視為過期（ttl=0）後接手，先 lookup 再決定是否送出
    mgr2 = DeliveryManager(session_factory, adapter, claim_ttl=timedelta(0))
    rec = mgr2.dispatch(intent)
    assert rec.status is S.RENDERED_PLAYED
    assert adapter.send_count(intent.output_id, 0) == 1 and adapter.effect_count(intent.output_id, 0) == 1


def test_live_claim_blocks_second_sender(session_factory):
    intent = make_intent()
    mgr, adapter = _mgr(session_factory, hook=_crash_at("delivery.after_claim"))
    with pytest.raises(SimulatedCrash):
        mgr.dispatch(intent)
    rec = DeliveryManager(session_factory, adapter).dispatch(intent)  # claim 未過期：不得搶送
    assert rec.status is S.ENQUEUED and adapter.send_count(intent.output_id, 0) == 0


def test_reclaim_with_undetermined_lookup_marks_unknown_not_resend(session_factory):
    intent = make_intent()
    mgr, adapter = _mgr(session_factory, hook=_crash_at("delivery.after_send"))
    with pytest.raises(SimulatedCrash):
        mgr.dispatch(intent)
    adapter.lookup_lost = True
    rec = DeliveryManager(session_factory, adapter, claim_ttl=timedelta(0)).dispatch(intent)
    assert rec.status is S.UNKNOWN and adapter.effect_count(intent.output_id, 0) == 1


def test_concurrent_dispatch_single_sender(session_factory):
    intent = make_intent()
    adapter = FakeOutputAdapter(default_mode="transport_only")  # 非冪等 adapter
    mgr = DeliveryManager(session_factory, adapter)
    mgr.prepare(intent)
    barrier = Barrier(4)

    def run(_):
        barrier.wait()
        return mgr.dispatch(intent)

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(run, range(4)))
    assert adapter.effect_count(intent.output_id, 0) == 1 and adapter.send_count(intent.output_id, 0) == 1
    assert {r.status for r in results} <= {S.ENQUEUED, S.SENT}


def test_partial_playback_stops_at_accepted(session_factory):
    mgr, _ = _mgr(session_factory, default_mode="played_partial")
    rec = mgr.dispatch(make_intent())
    assert rec.status is S.ACCEPTED and rec.delivery_evidence is E.LOCAL_PLAYBACK


def test_apply_result_replay_is_noop_on_terminal_and_passed_stages(session_factory):
    mgr, adapter = _mgr(session_factory)
    intent = make_intent()
    done = mgr.dispatch(intent)
    assert done.status is S.RENDERED_PLAYED
    replay = adapter.lookup(done.dispatch_id).result
    assert mgr._apply_result(done.delivery_id, replay).status is S.RENDERED_PLAYED  # 終態重放不拋錯
    mgr2, adapter2 = _mgr(session_factory, default_mode="played_partial")
    part = mgr2.dispatch(make_intent())
    sent_only = adapter2.lookup(part.dispatch_id).result
    assert mgr2._apply_result(part.delivery_id, sent_only).status is S.ACCEPTED  # 已越過 sent，略過


def test_audio_must_be_synthesized_before_dispatch(session_factory):
    mgr, _ = _mgr(session_factory)
    intent = make_intent(kind="audio_stream", content_format={"sample_rate": 24000, "channels": 1, "codec": "pcm"})
    rec = mgr.prepare(intent)
    with pytest.raises(IllegalDeliveryTransition):
        mgr.dispatch(intent)  # generated -> enqueued 不得略過 synthesized
    mgr.mark_synthesized(rec.delivery_id)
    assert mgr.dispatch(intent).status is S.RENDERED_PLAYED


# ---- O-16：report_evidence（非同步證據回報）----

def _sent_only(session_factory, kind="audio_stream"):
    mgr, _ = _mgr(session_factory, default_mode="transport_only")
    extra = {"content_format": {"sample_rate": 24000, "channels": 1, "codec": "pcm"}} if kind == "audio_stream" else {}
    intent = make_intent(kind=kind, **extra)
    if kind == "audio_stream":
        mgr.mark_synthesized(mgr.prepare(intent).delivery_id)
    return mgr, mgr.dispatch(intent)


_PARTIAL = {"from_sample": 0, "to_sample": 100, "complete": False}
_FULL = {"from_sample": 0, "to_sample": 200, "complete": True}


def test_report_evidence_advances_sent_to_accepted_then_played(session_factory):
    mgr, rec = _sent_only(session_factory)
    assert rec.status is S.SENT and rec.delivery_evidence is E.TRANSPORT_SENT
    partial = mgr.report_evidence(rec.delivery_id, E.LOCAL_PLAYBACK, _PARTIAL)
    assert partial.status is S.ACCEPTED and partial.delivery_evidence is E.LOCAL_PLAYBACK  # INV-05
    done = mgr.report_evidence(rec.delivery_id, E.LOCAL_PLAYBACK, _FULL)
    assert done.status is S.RENDERED_PLAYED and done.played_range["to_sample"] == 200


def test_report_evidence_audio_without_range_stops_at_accepted(session_factory):
    mgr, rec = _sent_only(session_factory)
    assert mgr.report_evidence(rec.delivery_id, E.REMOTE_RENDER).status is S.ACCEPTED


def test_report_evidence_text_without_range_reaches_played(session_factory):
    mgr, rec = _sent_only(session_factory, kind="text")
    assert mgr.report_evidence(rec.delivery_id, E.LOCAL_PLAYBACK).status is S.RENDERED_PLAYED


def test_report_evidence_is_idempotent_and_evidence_never_downgrades(session_factory):
    mgr, rec = _sent_only(session_factory)
    first = mgr.report_evidence(rec.delivery_id, E.LOCAL_PLAYBACK, _PARTIAL)
    again = mgr.report_evidence(rec.delivery_id, E.LOCAL_PLAYBACK, _PARTIAL)
    assert again.status is first.status is S.ACCEPTED
    with pytest.raises(IllegalDeliveryTransition):
        mgr.report_evidence(rec.delivery_id, E.TRANSPORT_SENT)  # 證據只升不降
    with pytest.raises(IllegalDeliveryTransition):
        mgr.report_evidence(rec.delivery_id, E.UNKNOWN)


def test_report_transport_sent_alone_never_reaches_accepted(session_factory):
    mgr, rec = _sent_only(session_factory)
    assert mgr.report_evidence(rec.delivery_id, E.TRANSPORT_SENT).status is S.SENT  # VC-5-2


def test_report_evidence_rejected_before_send(session_factory):
    mgr, _ = _mgr(session_factory)
    rec = mgr.prepare(make_intent())
    with pytest.raises(IllegalDeliveryTransition):
        mgr.report_evidence(rec.delivery_id, E.LOCAL_PLAYBACK)


def test_late_evidence_after_terminal_does_not_change_state(session_factory):
    mgr, _ = _mgr(session_factory, default_mode="fail")
    rec = mgr.dispatch(make_intent())
    assert rec.status is S.FAILED
    assert mgr.report_evidence(rec.delivery_id, E.LOCAL_PLAYBACK).status is S.FAILED


def test_report_evidence_unknown_delivery_raises(session_factory):
    mgr, _ = _mgr(session_factory)
    with pytest.raises(LookupError):
        mgr.report_evidence("dlv_nope", E.LOCAL_PLAYBACK)


def test_report_evidence_resolves_unknown_to_sent_path(session_factory):
    mgr, _ = _mgr(session_factory, default_mode="lose_ack")
    rec = mgr.dispatch(make_intent())
    assert rec.status is S.UNKNOWN
    assert mgr.report_evidence(rec.delivery_id, E.TRANSPORT_SENT).status is S.SENT


def _rng(to, complete=False):
    return {"from_sample": 0, "to_sample": to, "complete": complete}


def test_report_evidence_progress_moves_forward_never_back_and_is_idempotent(session_factory):
    mgr, rec = _sent_only(session_factory)
    first = mgr.report_evidence(rec.delivery_id, E.LOCAL_PLAYBACK, _rng(100))
    assert first.status is S.ACCEPTED and first.played_range["to_sample"] == 100
    fwd = mgr.report_evidence(rec.delivery_id, E.LOCAL_PLAYBACK, _rng(150))
    assert fwd.status is S.ACCEPTED and fwd.played_range["to_sample"] == 150  # 進度推進
    back = mgr.report_evidence(rec.delivery_id, E.LOCAL_PLAYBACK, _rng(100))
    assert back.played_range["to_sample"] == 150  # 倒退不更新
    same = mgr.report_evidence(rec.delivery_id, E.LOCAL_PLAYBACK, _rng(150))
    assert same.played_range["to_sample"] == 150 and same.status is S.ACCEPTED  # 同值冪等
    done = mgr.report_evidence(rec.delivery_id, E.LOCAL_PLAYBACK, _rng(200, True))
    assert done.status is S.RENDERED_PLAYED and done.played_range["to_sample"] == 200


def test_unknown_with_strong_evidence_and_complete_range_reaches_played(session_factory):
    mgr, _ = _mgr(session_factory, default_mode="lose_ack")
    intent = make_intent(kind="audio_stream", content_format={"sample_rate": 24000, "channels": 1, "codec": "pcm"})
    mgr.mark_synthesized(mgr.prepare(intent).delivery_id)
    rec = mgr.dispatch(intent)
    assert rec.status is S.UNKNOWN
    done = mgr.report_evidence(rec.delivery_id, E.LOCAL_PLAYBACK, _rng(200, True))  # 視同 O-6 found，不需 lookup
    assert done.status is S.RENDERED_PLAYED and done.delivery_evidence is E.LOCAL_PLAYBACK


def test_concurrent_report_evidence_serialized_without_exception(session_factory):
    mgr, rec = _sent_only(session_factory, kind="text")
    barrier = Barrier(4)

    def report(_):
        barrier.wait()
        return mgr.report_evidence(rec.delivery_id, E.LOCAL_PLAYBACK)

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(report, range(4)))  # 任一拋例外會在此上拋
    assert all(r.status is S.RENDERED_PLAYED for r in results)
