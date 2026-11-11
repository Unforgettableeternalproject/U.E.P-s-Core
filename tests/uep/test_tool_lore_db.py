"""lore.* 寫入對真實 TaskManager 的 DB 測試（需 PostgreSQL；沒有 UEP_TEST_DATABASE_URL 時自動 skip）。

- 寫入工具是 external_effect＋approval_level=none＋native：不經核准直接執行（不 park、沒有 approval 列），留 receipt。
- 唯讀工具同樣走 create_action -> execute_action -> receipt。
- 逾時（寫入結果未明）action 轉 UNKNOWN，不重送。
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import func, select

from tests.uep.acceptance_support import POLICY, SCOPE
from tests.uep.test_tool_lore import TOKEN, FakeHttp, make_config
from uep.events.bus import EventBus
from uep.events.publisher import EventPublisher
from uep.tasks.manager import TaskManager
from uep.tasks.models import ActionIntentRow, ApprovalRow, ReceiptRow
from uep.tasks.states import ActionStatus
from uep.tools.catalog.lore import (
    LORE_EXECUTOR,
    TOOL_RECALL,
    TOOL_UPDATE,
    TOOL_WRITE,
    with_lore,
)
from uep.tools.catalog.memory import memory_registry
from uep.tools.executors.lore import LoreTimeout, LoreToolExecutor

pytestmark = pytest.mark.usefixtures("session_factory")


class LoreDb:
    def __init__(self, session_factory, http: FakeHttp) -> None:
        self.session_factory = session_factory
        self.executor = LoreToolExecutor(make_config(), transport=http, allow_write=True)
        self.tm = TaskManager(
            session_factory, EventPublisher(session_factory, EventBus()), {LORE_EXECUTOR: self.executor},
            tool_resolver=with_lore(memory_registry(), configured=True),
        )
        task = self.tm.create_task(persona_id="uep", goal="lore", owner_actor_id="owner", scope_ref=SCOPE, policy=POLICY)
        self.run = self.tm.start_run(task.task_id, persona_version=1, policy_version=1)

    def act(self, tool: str, params: dict[str, Any]):
        step = self.tm.begin_step(self.run.run_id, "lore", worker_epoch=self.run.worker_epoch)
        action = self.tm.create_action(
            run_id=self.run.run_id, step_id=step.step_id, tool=tool, params=params, worker_epoch=self.run.worker_epoch,
        )
        return action, self.tm.execute_action(action.action_id, params, worker_epoch=self.run.worker_epoch)

    def count(self, model) -> int:
        with self.session_factory() as s:
            return s.scalar(select(func.count()).select_from(model))


def test_write_executes_without_approval_and_leaves_receipt(session_factory):
    http = FakeHttp({"write": (201, {"id": "n9", "updated": "u1", "author": "測試角色"})})
    db = LoreDb(session_factory, http)
    action, outcome = db.act(TOOL_WRITE, {"title": "t", "body": "b"})
    assert outcome.status is ActionStatus.SUCCEEDED and outcome.executed
    assert outcome.receipt is not None and outcome.receipt.succeeded and outcome.receipt.source_verified
    assert db.count(ApprovalRow) == 0  # 免 Core 核准
    assert db.count(ReceiptRow) == 1
    with session_factory() as s:
        assert s.get(ActionIntentRow, action.action_id).side_effect_class == "external_effect"  # 分類如實落庫
    assert http.calls[0][2]["author"] == "測試角色"
    assert json.loads(db.executor.outcome(action.action_id)["result"])["id"] == "n9"
    assert TOKEN not in json.dumps(outcome.receipt.meta or {})


def test_update_conflict_leaves_failed_receipt(session_factory):
    http = FakeHttp({"update": (409, {"error": {"code": "version_conflict", "message": "x"}})})
    db = LoreDb(session_factory, http)
    _, outcome = db.act(TOOL_UPDATE, {"id": "n1", "expected_updated": "u0", "body": "新"})
    assert outcome.status is ActionStatus.FAILED and outcome.receipt is not None and not outcome.receipt.succeeded
    assert db.count(ReceiptRow) == 1


def test_read_goes_through_same_path(session_factory):
    http = FakeHttp({"recall": (200, {"items": [{"id": "n1", "title": "標題"}]})})
    db = LoreDb(session_factory, http)
    _, outcome = db.act(TOOL_RECALL, {"query": "q"})
    assert outcome.status is ActionStatus.SUCCEEDED and db.count(ApprovalRow) == 0


def test_write_timeout_marks_unknown_and_does_not_resend(session_factory):
    http = FakeHttp(raises=LoreTimeout())
    db = LoreDb(session_factory, http)
    _, outcome = db.act(TOOL_WRITE, {"title": "t", "body": "b"})
    assert outcome.status is ActionStatus.UNKNOWN
    assert len(http.calls) == 1
