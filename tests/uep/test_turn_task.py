"""D62：回合 Task 懶建立／收尾、TC-3 單 EXECUTING 強制、孤兒掃描（切片 C1）。

DB 測試需 PostgreSQL（同 test_task_model；未設 UEP_TEST_DATABASE_URL 時 skip）。
"""
# ruff: noqa: F811  fixture（mgr、executor）自 test_task_model 匯入後以參數名使用
from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from tests.uep.test_task_model import (  # noqa: F401  fixture 與輔助函式沿用 test_task_model
    add_action,
    declare,
    executor,
    mgr,
    new_task,
    running,
)
from uep.events.models import EventRow
from uep.ids import derived_id, is_valid_id, new_id
from uep.tasks.manager import TURN_TASK_GOAL, ActionBlocked
from uep.tasks.models import ActionIntentRow, ActivityRow, RunRow, TaskRow
from uep.tasks.states import ActionStatus


def turn_task(mgr, turn_id=None):
    return mgr.get_or_create_turn_task(
        turn_id or new_id("turn"), persona_id="uep", owner_actor_id="owner", scope_ref="scope_private_owner",
        policy_ref="pol_1", persona_version=1, policy_version=1,
    )


def force_status(session_factory, action_id, status):
    with session_factory.begin() as s:
        s.execute(update(ActionIntentRow).where(ActionIntentRow.action_id == action_id).values(status=status))


def events_of(session_factory, event_type):
    with session_factory() as s:
        return list(s.scalars(select(EventRow).where(EventRow.event_type == event_type).order_by(EventRow.journal_seq)))


# ---- 不需 DB ----

def test_derived_id_is_deterministic_and_valid():
    turn_id = new_id("turn")
    assert derived_id("task", turn_id) == derived_id("task", turn_id)
    assert derived_id("task", turn_id)[5:] != derived_id("run", turn_id)[4:]
    assert is_valid_id(derived_id("task", turn_id), "task")
    assert is_valid_id(derived_id("run", turn_id), "run")
    assert derived_id("run", turn_id) != derived_id("run", new_id("turn"))
    with pytest.raises(ValueError):
        derived_id("nope", turn_id)


def test_migration_0011_chain_and_index_declared():
    from importlib import import_module

    mod = import_module("uep.db.migrations.versions.0011_one_executing_per_run")
    assert mod.revision == "0011_one_executing_per_run"
    assert mod.down_revision == "0010_decision_log"
    index = next(i for i in ActionIntentRow.__table__.indexes if i.name == "uq_action_intents_one_executing_per_run")
    assert index.unique and "EXECUTING" in str(index.dialect_options["postgresql"]["where"])


# ---- 回合 Task ----

def test_turn_task_is_lazy_deterministic_and_idempotent(mgr, session_factory):
    turn_id = new_id("turn")
    task, run = turn_task(mgr, turn_id)
    assert task.task_id == derived_id("task", turn_id) and run.run_id == derived_id("run", turn_id)
    assert task.goal == TURN_TASK_GOAL and task.presence_bound is False
    assert task.interrupt_policy == "interruptible" and task.origin_ref["turn_id"] == turn_id
    assert task.status == "RUNNING" and run.status == "RUNNING" and run.task_id == task.task_id
    task2, run2 = turn_task(mgr, turn_id)
    assert (task2.task_id, run2.run_id) == (task.task_id, run.run_id)
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(TaskRow)) == 1
        assert s.scalar(select(func.count()).select_from(RunRow)) == 1
        assert s.scalar(select(func.count()).select_from(ActivityRow)) == 1
    assert len(events_of(session_factory, "task.created")) == 1
    assert len(events_of(session_factory, "run.started")) == 1


def test_turn_task_concurrent_creates_only_one(mgr, session_factory):
    turn_id = new_id("turn")
    results, errors = [], []
    barrier = threading.Barrier(8)

    def worker():
        try:
            barrier.wait()
            results.append(turn_task(mgr, turn_id))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert {(t.task_id, r.run_id) for t, r in results} == {(derived_id("task", turn_id), derived_id("run", turn_id))}
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(TaskRow)) == 1
        assert s.scalar(select(func.count()).select_from(RunRow)) == 1


def test_finish_turn_task_succeeds_when_clean(mgr, session_factory):
    task, run = turn_task(mgr)
    done = mgr.finish_turn_task(task.task_id)
    assert done.status == "SUCCEEDED" and mgr.get_run(run.run_id).status == "SUCCEEDED"
    assert mgr.finish_turn_task(task.task_id).status == "SUCCEEDED"  # 冪等
    with session_factory() as s:
        assert s.get(ActivityRow, task.activity_id).status == "ENDED"


def test_finish_turn_task_fails_with_unresolved_unknown(mgr, session_factory):
    task, run = turn_task(mgr)
    step = mgr.begin_step(run.run_id, "work", worker_epoch=run.worker_epoch)
    action = add_action(mgr, run, step, {"a": 1})
    force_status(session_factory, action.action_id, "UNKNOWN")
    done = mgr.finish_turn_task(task.task_id)
    assert done.status == "FAILED" and mgr.get_run(run.run_id).status == "FAILED"
    ended = events_of(session_factory, "run.ended")[-1]
    assert ended.payload["reason_code"] == "unresolved_actions" and ended.payload["action_id"] == action.action_id
    assert events_of(session_factory, "task.failed")[-1].payload["reason_code"] == "unresolved_actions"


def test_finish_turn_task_fails_with_executing(mgr, session_factory):
    task, run = turn_task(mgr)
    step = mgr.begin_step(run.run_id, "work", worker_epoch=run.worker_epoch)
    action = add_action(mgr, run, step, {"a": 1})
    force_status(session_factory, action.action_id, "EXECUTING")
    assert mgr.finish_turn_task(task.task_id).status == "FAILED"
    assert events_of(session_factory, "run.ended")[-1].payload["reason_code"] == "unresolved_actions"


# ---- TC-3 ----

def two_actions(mgr, side_a="internal_read", side_b="internal_read"):
    _task, run, step = running(mgr)
    a = add_action(mgr, run, step, {"n": "a"}, side_effect=side_a)
    b = add_action(mgr, run, step, {"n": "b"}, side_effect=side_b)
    return run, a, b


def test_second_action_blocked_while_another_executing(mgr, session_factory, executor):
    run, a, b = two_actions(mgr)
    force_status(session_factory, a.action_id, "EXECUTING")
    with pytest.raises(ActionBlocked, match="TC-3"):
        mgr.execute_action(b.action_id, {"n": "b"}, worker_epoch=run.worker_epoch)
    assert mgr.get_action(b.action_id).status == "INTENDED" and not executor.calls


def test_other_run_executing_does_not_block(mgr, session_factory):
    _run, a, _b = two_actions(mgr)
    force_status(session_factory, a.action_id, "EXECUTING")
    _t2, run2, step2 = running(mgr)
    c = add_action(mgr, run2, step2, {"n": "c"})
    assert mgr.execute_action(c.action_id, {"n": "c"}, worker_epoch=run2.worker_epoch).status is ActionStatus.SUCCEEDED


def test_non_internal_read_unknown_blocks_new_action(mgr, session_factory):
    run, a, b = two_actions(mgr, side_a="reversible_local")
    force_status(session_factory, a.action_id, "UNKNOWN")
    with pytest.raises(ActionBlocked, match="TC-3"):
        mgr.execute_action(b.action_id, {"n": "b"}, worker_epoch=run.worker_epoch)


def test_internal_read_unknown_does_not_block(mgr, session_factory):
    run, a, b = two_actions(mgr, side_a="internal_read")
    force_status(session_factory, a.action_id, "UNKNOWN")
    assert mgr.execute_action(b.action_id, {"n": "b"}, worker_epoch=run.worker_epoch).status is ActionStatus.SUCCEEDED


def test_db_rejects_two_executing_in_same_run(mgr, session_factory):
    _run, a, b = two_actions(mgr)
    force_status(session_factory, a.action_id, "EXECUTING")
    with pytest.raises(IntegrityError):
        force_status(session_factory, b.action_id, "EXECUTING")
    # 不同 Run 不受限；終態不計
    force_status(session_factory, a.action_id, "SUCCEEDED")
    force_status(session_factory, b.action_id, "EXECUTING")


def test_vc_s3_4_concurrent_intended_one_blocked(mgr, executor):
    """VC-S3-4：同一 Run 兩個 INTENDED 並發進入執行，其中一個 ActionBlocked。"""
    run, a, b = two_actions(mgr)
    executor.delay = 0.6
    outcomes: dict[str, object] = {}

    def go(action, name):
        try:
            outcomes[name] = mgr.execute_action(action.action_id, {"n": name}, worker_epoch=run.worker_epoch)
        except ActionBlocked as exc:
            outcomes[name] = exc

    threads = [threading.Thread(target=go, args=(a, "a")), threading.Thread(target=go, args=(b, "b"))]
    for t in threads:
        t.start()
        time.sleep(0.05)
    for t in threads:
        t.join()
    blocked = [v for v in outcomes.values() if isinstance(v, ActionBlocked)]
    ok = [v for v in outcomes.values() if not isinstance(v, ActionBlocked)]
    assert len(blocked) == 1 and len(ok) == 1 and len(executor.calls) == 1


# ---- 孤兒掃描 ----

def test_sweep_fails_only_stale_non_terminal_turn_tasks(mgr, session_factory):
    old, _ = turn_task(mgr)
    done, _ = turn_task(mgr)
    mgr.finish_turn_task(done.task_id)
    other = new_task(mgr)  # 非回合 Task 不碰
    later = datetime.now(UTC) + timedelta(hours=1)
    assert mgr.sweep_orphan_turn_tasks(timedelta(minutes=30), now=datetime.now(UTC)) == []  # 還沒逾時
    assert mgr.sweep_orphan_turn_tasks(timedelta(minutes=30), now=later) == [old.task_id]
    assert mgr.get_task(old.task_id).status == "FAILED"
    assert mgr.get_task(done.task_id).status == "SUCCEEDED" and mgr.get_task(other.task_id).status == "PENDING"
    assert events_of(session_factory, "task.failed")[-1].payload["reason_code"] == "orphan_turn_task"
    assert mgr.sweep_orphan_turn_tasks(timedelta(minutes=30), now=later) == []  # 冪等


def test_sweep_records_unresolved_actions_on_run(mgr, session_factory):
    task, run = turn_task(mgr)
    step = mgr.begin_step(run.run_id, "work", worker_epoch=run.worker_epoch)
    action = add_action(mgr, run, step, {"a": 1})
    force_status(session_factory, action.action_id, "UNKNOWN")
    assert mgr.sweep_orphan_turn_tasks(timedelta(minutes=1), now=datetime.now(UTC) + timedelta(hours=1)) == [task.task_id]
    assert events_of(session_factory, "run.ended")[-1].payload["reason_code"] == "unresolved_actions"

