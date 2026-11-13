"""AR-3b 整合（需要 UEP_TEST_DATABASE_URL）：真的 Host＋core，仲裁 on，從 orchestrator 持有的 sink 送放棄訊號。

orchestrator 的 sink 是建構時就固定的 proxy 方法；仲裁器在 core 組裝後才 attach 並 bind。這裡驗證整條路：
orchestrator sink → proxy → 仲裁器佇列／背景執行緒 → 放棄執行器 → mark 寫入、journal 出現 abandon_work(accepted)。
"""
import time as _time
from datetime import UTC, datetime, time
from types import SimpleNamespace

from sqlalchemy import select

from tests.uep.echo_support import build_rig
from uep.config import get_settings
from uep.contracts.schedule import AbandonWorkSignal
from uep.echo.schedule_wiring import attach_presence_arbiter
from uep.events.models import EventRow
from uep.schedule.store import PlanActor, PlanItemCommand, PlanStore

OWNER = PlanActor("owner_console:dev", "owner_console", True, True)


def abandon_rows(session_factory):
    with session_factory() as s:
        return list(s.scalars(select(EventRow).where(EventRow.event_type == "schedule.abandon_work")))


def wait_for(predicate, timeout=5.0):
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if predicate():
            return True
        _time.sleep(0.02)
    return predicate()


def test_owner_abandon_signal_flows_from_the_orchestrator_sink_to_a_mark_and_a_journal_event(
        session_factory, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "presence_arbiter", "on")
    rig = build_rig(session_factory, tmp_path, start=False)
    host = rig.host
    assert host.presence_sinks is not None and host.orchestrator._on_abandon_signal is not None
    store = PlanStore(session_factory)
    # 整天都是 WORK 的行程項（行程時區的 00:00–23:59），讓即時時鐘下一定落在 WORK 格
    cmd = PlanItemCommand(activity_kind="work", location="discord", recurrence="weekly",
                          weekdays=(1, 2, 3, 4, 5, 6, 7), local_start=time(0, 0), local_end=time(23, 59),
                          tz="Asia/Taipei")
    item_id = store.create_item(OWNER, cmd, expected_revision=store.revision()).ref_id
    arbiter = attach_presence_arbiter(SimpleNamespace(presence_arbiter="on"), rig.core,
                                      session_factory=session_factory, reader=store)
    try:
        assert host.presence_sinks.bound
        signal = AbandonWorkSignal(
            signal_id="abandon:wire1", actor_ref="discord:1", actor_authenticated=True, source_platform="discord",
            path="turn", decided_at=datetime.now(UTC), decider_id="rules.abandon_work", decider_version="1")
        host.orchestrator._on_abandon_signal(signal, is_owner=True)  # orchestrator 持有的 sink（proxy 的方法）
        assert wait_for(lambda: len(store.active_marks()) == 1)
        [mark] = store.active_marks()
        assert (mark.kind, mark.item_ref, mark.cause_request_id) == ("abandon", item_id, "abandon:wire1")
        assert wait_for(lambda: len(abandon_rows(session_factory)) == 1)
        [row] = abandon_rows(session_factory)
        assert row.source == "core.schedule" and row.payload["result"] == "accepted"
        assert row.payload["signal_id"] == "abandon:wire1" and row.payload["item_ref"] == item_id
        # 非 owner（決策層判定 False）：IGNORED(not_owner)，不寫 mark
        host.orchestrator._on_abandon_signal(
            AbandonWorkSignal(
                signal_id="abandon:wire2", actor_ref="discord:2", actor_authenticated=True,
                source_platform="discord", path="turn", decided_at=datetime.now(UTC),
                decider_id="rules.abandon_work", decider_version="1"), is_owner=False)
        assert wait_for(lambda: len(abandon_rows(session_factory)) == 2)
        assert len(store.active_marks()) == 1
        reasons = sorted(r.payload["reason_code"] for r in abandon_rows(session_factory))
        assert reasons == ["accepted", "not_owner"]
    finally:
        arbiter.stop()
    assert not host.presence_sinks.bound  # stop 之後 sink 解除，之後的呼叫被丟棄並計數


def test_owner_utterance_in_work_slot_reaches_the_turn_context_through_judge_now(session_factory, tmp_path, monkeypatch):
    """端到端：工作格中 owner 發話 → orchestrator 經 proxy 呼叫 judge_now → REJECTED/work_in_progress →
    ticket.presence_busy → Host 的 turn_context 註記（只驗註記進到 think stage 的 Utterance，不啟動任何背景工作以外的東西）。"""
    from tests.uep.echo_support import make_session
    from uep.echo.host import PRESENCE_BUSY_TURN_CONTEXT

    monkeypatch.setattr(get_settings(), "presence_arbiter", "shadow")
    rig = build_rig(session_factory, tmp_path, session=make_session(platform="discord"))
    seen = []
    original = rig.think.stream

    def spy(utterance, token):
        seen.append(dict(utterance.metadata))
        return original(utterance, token)

    rig.think.stream = spy
    store = PlanStore(session_factory)
    cmd = PlanItemCommand(activity_kind="work", location="discord", recurrence="weekly",
                          weekdays=(1, 2, 3, 4, 5, 6, 7), local_start=time(0, 0), local_end=time(23, 59),
                          tz="Asia/Taipei")
    store.create_item(OWNER, cmd, expected_revision=store.revision())
    arbiter = attach_presence_arbiter(SimpleNamespace(presence_arbiter="shadow"), rig.core,
                                      session_factory=session_factory, reader=store)
    try:
        assert rig.host.presence_sinks.bound
        decision = rig.host.presence_sinks.judge_now(
            target="discord", is_owner=True, actor_ref="discord:111", actor_authenticated=True)
        assert (decision.outcome, decision.reason_code) == ("REJECTED", "work_in_progress")
        result = rig.say("你在忙嗎", source_endpoint_id="ep")  # 已認證需要 ingress 簽發的 endpoint
        assert result.ticket is not None and result.ticket.presence_busy == "work"
        rig.wait_terminal(result.ticket.turn_id)
        [meta] = seen
        assert meta["presence_busy"] == "work"
        assert PRESENCE_BUSY_TURN_CONTEXT["work"] in meta["turn_context"]
    finally:
        arbiter.stop()
        rig.core.shutdown()
