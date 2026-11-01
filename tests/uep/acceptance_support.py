"""V19–V21 驗收測試共用 helper（需 PostgreSQL；由各 test_acceptance_*.py 匯入）。

「重啟」的模擬方式：丟棄 TaskManager／Bus／Publisher／DeliveryManager 實例、改建新的；
DB 與 executor／adapter 是外部世界，跨重啟存活。
"""
from __future__ import annotations

import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sqlalchemy import func, select

from uep.adapters.tool import ToolCall, ToolReceipt
from uep.events.bus import EventBus
from uep.events.models import EventRow
from uep.events.publisher import EventPublisher
from uep.fakes.crash import CrashInjector
from uep.fakes.tool import FakeToolExecutor
from uep.fakes.tool_spec import FakeToolSpecResolver
from uep.tasks.manager import TaskManager, TaskPolicy
from uep.tasks.models import ActionIntentRow, ReceiptRow
from uep.tasks.states import ActionStatus, InterruptPolicy

SCOPE = "scope_private_owner"
POLICY = TaskPolicy(presence_bound=False, interrupt_policy=InterruptPolicy.INTERRUPTIBLE, policy_ref="pol_1")
T0 = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


_RESOLVERS: weakref.WeakKeyDictionary[object, FakeToolSpecResolver] = weakref.WeakKeyDictionary()


def make_tm(session_factory, executor, *, crash: CrashInjector | None = None, clock=None, bus=None, **kw) -> TaskManager:
    """全新 TaskManager（含全新 Bus／Publisher）；crash 以 ``CrashInjector.hit`` 當 crash_hook。"""
    extra = dict(kw)
    if crash is not None:
        extra["crash_hook"] = crash.hit
    if clock is not None:
        extra["clock"] = clock
    publisher = EventPublisher(session_factory, bus or EventBus())
    # 註冊表跟著 executor 走（「重啟」建的新 tm 與舊 tm 看到同一份註冊表，如同部署時的設定檔）
    resolver = extra.pop("tool_resolver", None) or _RESOLVERS.setdefault(executor, FakeToolSpecResolver())
    return TaskManager(session_factory, publisher, {"fake": executor}, tool_resolver=resolver, **extra)


def declare_tool(tm: TaskManager, tool: str, *, executor="fake", side_effect="reversible_local",
                 idempotency="via_action_id", presence_bound=False, version=1) -> None:
    """在 tm 的 FakeToolSpecResolver 登記（或覆寫）工具分類；同名工具依案例改分類時重呼叫即可。"""
    tm.tool_resolver.register(
        tool, executor=executor, side_effect_class=side_effect, idempotency=idempotency,
        presence_bound=presence_bound, tool_version=version,
    )


@dataclass(frozen=True)
class Item:
    action_id: str
    params: dict


def setup_run(tm: TaskManager, count: int = 1, *, idempotency="via_action_id", side_effect="reversible_local",
              origin_ref=None, tool="files.write"):
    """建立 Task／Run／Step 與 count 個 INTENDED action；回傳 (task, run, step, [Item])。"""
    task = tm.create_task(
        persona_id="uep", goal="整理報告", owner_actor_id="owner", scope_ref=SCOPE, policy=POLICY,
        origin_ref=origin_ref,
    )
    run = tm.start_run(task.task_id, persona_version=1, policy_version=1)
    step = tm.begin_step(run.run_id, "work", worker_epoch=run.worker_epoch)
    items = []
    for i in range(count):
        params = {"path": f"f{i}"}
        declare_tool(tm, tool, side_effect=side_effect, idempotency=idempotency)
        act = tm.create_action(
            run_id=run.run_id, step_id=step.step_id, tool=tool, params=params, worker_epoch=run.worker_epoch,
        )
        items.append(Item(act.action_id, params))
    return task, run, step, items


def arm_next(injector: CrashInjector, point: str, nth: int = 1) -> None:
    """從現在起第 nth 次命中 point 時崩潰（CrashInjector 的 on_hit 是累計次數）。"""
    injector.arm(point, on_hit=injector.hits[point] + nth)


def drive(tm: TaskManager, items, epoch: int) -> None:
    """worker 恢復後的標準做法：UNKNOWN 先 resolve_unknown；INTENDED／已完成才 execute_action。

    永遠不對 UNKNOWN／EXECUTING 直接 execute（那是 ActionBlocked，也是契約要求）。
    """
    for item in items:
        if tm.get_action(item.action_id).status == ActionStatus.UNKNOWN.value:
            tm.resolve_unknown(item.action_id)
        if tm.get_action(item.action_id).status in (ActionStatus.INTENDED.value, ActionStatus.SUCCEEDED.value,
                                                    ActionStatus.FAILED.value):
            tm.execute_action(item.action_id, item.params, worker_epoch=epoch)


def status_of(tm: TaskManager, item: Item) -> str:
    return tm.get_action(item.action_id).status


def receipt_count(session_factory, action_id: str) -> int:
    with session_factory() as s:
        return s.scalar(select(func.count()).select_from(ReceiptRow).where(ReceiptRow.action_id == action_id))


def event_types(session_factory) -> list[str]:
    with session_factory() as s:
        return list(s.scalars(select(EventRow.event_type).order_by(EventRow.journal_seq)))


def action_row_count(session_factory) -> int:
    with session_factory() as s:
        return s.scalar(select(func.count()).select_from(ActionIntentRow))


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class HookedExecutor(FakeToolExecutor):
    """execute 進入後、實際執行前先跑 on_execute（可 sleep 或在執行途中觸發另一個 worker 的動作）。"""

    def __init__(self, *args, on_execute: Callable[[ToolCall], None] | None = None, **kw) -> None:
        super().__init__(*args, **kw)
        self.on_execute = on_execute

    def execute(self, call: ToolCall) -> ToolReceipt:
        if self.on_execute is not None:
            self.on_execute(call)
        return super().execute(call)


class LyingExecutor(FakeToolExecutor):
    """自稱 native／via_action_id 卻不真的去重的 executor（契約允許的風險邊界：Core 只能信任宣告）。"""

    def execute(self, call: ToolCall) -> ToolReceipt:
        return super().execute(replace(call, idempotency="none"))
