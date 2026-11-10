"""Supervisor（SYS 契約 §2.7、SV-1～SV-4；切片 F-3，已由組裝層接線，見 ``assemble_core``）：週期性監督 Run 的 deadline、核准過期與孤兒回合。

形式仿 ``PresenceMaintenance``：可手動 ``tick(now)``（測試不需要真時間），所有轉移都走 ``TaskManager`` 公開方法（SV-4），
單一 Run／Task 失敗只記 log 並計數，下個 tick 重試。

- **SV-2 deadline**：``run_deadline`` 或 ``Task.deadline`` 到期 ⇒ ``request_cancel``（只設旗標）。心跳新鮮時，在
  ``deadline_grace`` 內不 force（T-12）：worker 於下次 ``heartbeat`` 讀到 ``CancelSignal`` 後自行收尾；**超過 grace 就 force**
  （卡在同步 executor 的 worker 讀不到旗標，這是預期路徑），並以 ``worker_epoch`` 遞增圍堵慢 worker。心跳已過期
  （沿用 ``resume_run`` 的判準 ``TaskManager.heartbeat_stale_after``，含 park 後心跳為 None）則不必等 grace。
  force 用 ``force_transition_run(FAILED, reason=<cancel_reason>)``，隨後 ``transition_task(FAILED)``。有未決 action 時 ``run.ended``
  帶 ``reason_code=unresolved_actions``（T-13，由 TaskManager 寫入）。Run 已被 worker 自行轉成終態時，只補 Task 收尾。
- **SV-2(5) 核准過期**（at-least-once）：``approval_expiry`` 到期、Run 在 WAITING_APPROVAL 且 ``approval_expired_at`` 不等於該 expiry
  ⇒ 先 ``notify(run_id, idempotency_key)``，key 為 ``appr_expired:{run_id}:{expiry 的 epoch 秒整數}``（E-20）；通知成功後才
  ``mark_approval_expired``。``notify`` 失敗不標記，下個 tick 重試；標記失敗則下個 tick 以**同一個 key** 重送，由消費端去重。
  不自動轉失敗（SA-8）。
- **孤兒掃描**：每個 tick 呼叫 ``sweep_orphan_turn_tasks(older_than)``。
- **SV-1 ``pause_all``**：對未終態 Task 呼叫 ``pause_task``；UNKNOWN 的 Task／Run 無 PAUSED 邊，略過並記 warning；單一失敗不影響其餘；
  不寫 ``task.cancelled``／``task.failed``。``pause_all`` 本身不等待；``shutdown()`` 在其後等 EXECUTING action 的 receipt，
  總時間不超過 ``shutdown_grace``（SV-1(3)）。
- **接線**：``SupervisorLoop`` 以背景執行緒定期 ``tick``（仿 ``PresenceMaintenance``）；``CoreAssembly.shutdown`` 在關閉入口之前呼叫 ``shutdown()``。

不含：module.* 訂閱、AgentLoop 讀旗標後自行轉 FAILED（F-4）、step_timeout 的 Step 取消、SV-5 多程序領導權、重試政策、通知通道。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from uep.tasks.manager import TURN_MAX_DURATION, IllegalTransition, TaskManager
from uep.tasks.models import RunRow
from uep.tasks.states import TERMINAL_STATUSES, CancelReason, PauseReason, RunStatus

logger = logging.getLogger(__name__)

DEFAULT_DEADLINE_GRACE = timedelta(seconds=30)
DEFAULT_SHUTDOWN_GRACE = timedelta(seconds=10)  # SV-1(3)：關機暫停後等 EXECUTING receipt 的總上限（含 pause_all 本身）
DEFAULT_SUPERVISOR_INTERVAL = 30.0  # 背景 tick 間隔（秒）
_RECEIPT_POLL_S = 0.1
DEFAULT_ORPHAN_OLDER_THAN = TURN_MAX_DURATION * 3  # 必須大於 TURN_MAX_DURATION
_DEADLINE_REASONS = frozenset({CancelReason.RUN_DEADLINE.value, CancelReason.TASK_DEADLINE.value})
_FAILED_LIKE = frozenset({RunStatus.FAILED.value, RunStatus.CANCELLED.value})


@dataclass
class SupervisorStats:
    """一輪 tick 的統計。"""

    cancel_requested: int = 0  # 新設定的取消旗標
    forced: int = 0  # force 成 FAILED 的 Run
    tasks_failed: int = 0  # 收尾成 FAILED 的 Task
    approvals_expired: int = 0  # 新標記的過期 expiry
    notified: int = 0  # 成功呼叫 notify 的次數
    orphans_closed: int = 0  # 孤兒回合 Task 收尾數
    actions_cancelled: int = 0  # force 後對在途 interruptible action 呼叫 executor.cancel 的次數（D69，不論結果）
    failures: int = 0  # 失敗（記 log、下個 tick 重試）


class Supervisor:
    def __init__(
        self,
        tasks: TaskManager,
        *,
        deadline_grace: timedelta = DEFAULT_DEADLINE_GRACE,
        shutdown_grace: timedelta = DEFAULT_SHUTDOWN_GRACE,
        older_than: timedelta = DEFAULT_ORPHAN_OLDER_THAN,
        clock: Callable[[], datetime] | None = None,
        notify: Callable[[str, str], None] | None = None,
        cancel_actions: Callable[[str], Callable[[], Mapping[str, str]]] | None = None,
    ) -> None:
        if deadline_grace <= timedelta(0):
            raise ValueError("deadline_grace 必須 > 0")
        if shutdown_grace < timedelta(0):
            raise ValueError("shutdown_grace 必須 >= 0")
        if older_than <= TURN_MAX_DURATION:
            raise ValueError(f"older_than 必須大於 TURN_MAX_DURATION（{TURN_MAX_DURATION}）")
        self._tasks = tasks
        self._grace = deadline_grace
        self._shutdown_grace = shutdown_grace
        self._older_than = older_than
        self._clock = clock or (lambda: datetime.now(UTC))
        # None＝沒有通知通道：核准過期不處理也不標記（否則會在沒人收到通知的情況下被當成已通知），只記一次 warning
        self._notify = notify
        self._warned_no_notify: set[tuple[str, int]] = set()
        self._closing = False  # shutdown 開始後 tick 不再做任何事（尤其不 force）
        # D69：``ActionCanceller.prepare``。force 前呼叫它取 EXECUTING 快照，force 後執行回傳的函式對 interruptible action
        # 呼叫 executor.cancel（action_id -> 結果，僅用於統計與 log）；None＝不取消（維持 F-3 行為）。
        # 注意：這是 fallback（worker 卡住或旗標沒被讀到）；旗標路徑的 cancel 由 Agent Core 負責
        self._cancel_actions = cancel_actions
        self.ticks = 0
        self.failures = 0  # 累計失敗（tick 與 pause_all）

    # ---- tick ----

    def tick(self, now: datetime | None = None) -> SupervisorStats:
        """執行一輪；任何步驟的例外都不外洩（記 log、計數，下個 tick 重試）。回傳本輪統計。

        deadline 的 force 規則：心跳新鮮時，在 ``deadline_grace`` 內不 force；超過 grace 就 force，並以 epoch 遞增圍堵慢 worker。"""
        at = now or self._clock()
        stats = SupervisorStats()
        if self._closing:
            return stats
        try:
            due = self._tasks.list_runs_due(at)
        except Exception:  # noqa: BLE001  單一步驟失敗不外洩
            self._fail(stats, "查詢到期的 Run 失敗，下個 tick 重試")
            due = []
        for run in due:
            if self._closing:
                break
            try:
                self._handle_run(run, at, stats)
            except Exception:  # noqa: BLE001
                self._fail(stats, f"處理 Run 失敗，下個 tick 重試：{run.run_id}")
        try:
            stats.orphans_closed = len(self._tasks.sweep_orphan_turn_tasks(self._older_than, now=at))
        except Exception:  # noqa: BLE001
            self._fail(stats, "孤兒回合掃描失敗，下個 tick 重試")
        self.ticks += 1
        return stats

    def _fail(self, stats: SupervisorStats, message: str) -> None:
        stats.failures += 1
        self.failures += 1
        logger.exception(message)

    def _handle_run(self, run: RunRow, now: datetime, stats: SupervisorStats) -> None:
        if RunStatus(run.status) in TERMINAL_STATUSES:
            # worker 已自行把 Run 轉成 FAILED／CANCELLED（讀到旗標後）：Task 不會自動結束，由這裡收尾（SV-2(4)）。
            # SUCCEEDED 不動：Run 已成功完成，Task 留給擁有者收尾，不可判成失敗
            if run.cancel_reason in _DEADLINE_REASONS and run.status in _FAILED_LIKE:
                self._fail_task(run.task_id, stats)
            return
        forced = self._handle_deadline(run, now, stats)
        if not forced:
            self._handle_approval_expiry(run, now, stats)

    def _handle_deadline(self, run: RunRow, now: datetime, stats: SupervisorStats) -> bool:
        """設旗標，必要時 force；回傳是否已 force（Run 已終態，不再處理核准過期）。"""
        requested_at, reason = run.cancel_requested_at, run.cancel_reason
        if requested_at is None or reason == CancelReason.STEP_TIMEOUT.value:
            # 沒有旗標，或旗標只是 step_timeout（不使 Run 失敗）：deadline 到期時設／升級成 deadline 類旗標，
            # 否則 step_timeout 會把 deadline 永遠擋掉（request_cancel 允許 step_timeout -> deadline 類的升級）
            due_reason = self._deadline_reason(run, now)
            if due_reason is not None:
                if self._tasks.request_cancel(run.run_id, due_reason):
                    stats.cancel_requested += 1
                requested_at, reason = now, due_reason.value  # 剛設／升級（或別人剛設）：以本輪時間起算 grace，不會在同一輪因 grace 而 force
            elif requested_at is None:
                return False
        if reason not in _DEADLINE_REASONS:
            return False  # step_timeout 只取消 Step，Run 不因此失敗（Step 取消屬 F-4）
        beat = run.worker_heartbeat_at
        stale_after = self._tasks.heartbeat_stale_after
        worker_gone = beat is None or now - beat >= stale_after  # T-12：心跳新鮮 ⇒ worker 還活著，絕不 force
        if not (worker_gone or now - requested_at > self._grace):
            return False
        if self._closing:
            return False  # 關機開始：Run 已（或即將）PAUSED(core_shutdown)，不 force
        cancel_run = self._snapshot_in_flight(run.run_id)  # 先取 EXECUTING 快照；cancel 在 force 之後才呼叫，絕不延後 force／epoch 圍堵
        try:
            self._tasks.force_transition_run(run.run_id, RunStatus.FAILED, reason=str(reason))
            stats.forced += 1
        except IllegalTransition:
            # force 輸給 worker：只有 Run 現在是 FAILED／CANCELLED 才收尾 Task；SUCCEEDED 不動；其他狀態是真的失敗，上拋計數
            current = self._tasks.get_run(run.run_id)
            if current.status not in _FAILED_LIKE and current.status != RunStatus.SUCCEEDED.value:
                logger.warning(
                    "force 遇到非法轉移，Run 現況：run=%s status=%s worker_epoch=%s cancel_reason=%s",
                    run.run_id, current.status, getattr(current, "worker_epoch", None), getattr(current, "cancel_reason", None),
                )
                raise
            logger.debug("Run 已在別處轉成終態 %s，略過 force：%s", current.status, run.run_id)
            if current.status in _FAILED_LIKE:
                self._fail_task(run.task_id, stats)
            return True
        self._fail_task(run.task_id, stats)
        self._run_cancel(run.run_id, cancel_run, stats)
        return True

    def _snapshot_in_flight(self, run_id: str) -> Callable[[], Mapping[str, str]] | None:
        """D69：force 之前呼叫 ``cancel_actions(run_id)`` 取 EXECUTING action 快照，回傳「稍後執行 cancel」的函式。
        快照失敗只記 log 並回 None（不取消），絕不阻擋 force。"""
        if self._cancel_actions is None:
            return None
        try:
            return self._cancel_actions(run_id)
        except Exception:
            logger.exception("取得在途 action 快照失敗，略過取消、仍 force：%s", run_id)
            return None

    def _run_cancel(self, run_id: str, cancel_run: Callable[[], Mapping[str, str]] | None, stats: SupervisorStats) -> None:
        """force 之後才呼叫 executor.cancel（盡力而為，由 ``ActionCanceller`` 負責逾時）。任何失敗都只記 log，不影響 tick。
        action 的終態照舊由 force（EXECUTING -> UNKNOWN）與 lookup 決定，``stopped`` 目前不改變終態。"""
        if cancel_run is None:
            return
        try:
            results = cancel_run()
            count = sum(1 for outcome in results.values() if outcome in ("stopped", "isolated", "error"))
        except Exception:
            logger.exception("取消在途 action 失敗（force 已完成）：%s", run_id)
            return
        stats.actions_cancelled += count

    def _deadline_reason(self, run: RunRow, now: datetime) -> CancelReason | None:
        if run.status == RunStatus.PAUSED.value:
            return None  # 暫停中的 Run 不因 deadline 被取消；恢復後再判斷
        if run.run_deadline is not None and run.run_deadline <= now:
            return CancelReason.RUN_DEADLINE
        task = self._tasks.get_task(run.task_id)
        if task.deadline is not None and task.deadline <= now:
            return CancelReason.TASK_DEADLINE
        return None

    def _fail_task(self, task_id: str, stats: SupervisorStats) -> None:
        try:
            self._tasks.transition_task(task_id, RunStatus.FAILED)
            stats.tasks_failed += 1
        except IllegalTransition:
            logger.debug("Task 已是終態，略過收尾：%s", task_id)

    def _handle_approval_expiry(self, run: RunRow, now: datetime, stats: SupervisorStats) -> None:
        expiry = run.approval_expiry
        if run.status != RunStatus.WAITING_APPROVAL.value or expiry is None or expiry > now:
            return
        if run.approval_expired_at == expiry:
            return  # 同一 expiry 已通知並標記過
        if self._notify is None:
            token = (run.run_id, int(expiry.timestamp()))
            if token not in self._warned_no_notify:
                self._warned_no_notify.add(token)
                logger.warning("核准已過期但未接通知通道，略過且不標記已通知：run=%s expiry=%s", run.run_id, expiry.isoformat())
            return
        key = approval_expired_key(run.run_id, expiry)
        try:
            self._notify(run.run_id, key)
        except Exception:  # noqa: BLE001
            self._fail(stats, f"核准過期通知失敗，不標記，下個 tick 重試：{run.run_id}")
            return
        stats.notified += 1
        try:
            if self._tasks.mark_approval_expired(run.run_id, expiry):
                stats.approvals_expired += 1
        except Exception:  # noqa: BLE001
            self._fail(stats, f"標記核准過期失敗，下個 tick 以同一個 key 重送：{run.run_id} {key}")

    # ---- SV-1 ----

    def pause_all(self, reason: PauseReason = PauseReason.CORE_SHUTDOWN) -> list[str]:
        """對每個未終態 Task 呼叫 ``pause_task``；回傳本次實際轉入 PAUSED 的 task_id。

        UNKNOWN 的 Task 或 Run 無 PAUSED 邊（``RUN_TRANSITIONS[UNKNOWN]``），略過並記 warning，留待重啟後查核。
        已 PAUSED 者略過（不改寫原因）。單一 Task 失敗只記 log 並計數，不影響其餘。不寫 ``task.cancelled``／``task.failed``。"""
        runs_by_task: dict[str, list[RunRow]] = defaultdict(list)
        for run in self._tasks.list_resumable_runs():
            runs_by_task[run.task_id].append(run)
        paused: list[str] = []
        for task in self._tasks.list_live_tasks():
            if task.status == RunStatus.PAUSED.value:
                continue
            if task.status == RunStatus.UNKNOWN.value or any(
                r.status == RunStatus.UNKNOWN.value for r in runs_by_task[task.task_id]
            ):
                logger.warning("Task 或 Run 為 UNKNOWN，無法暫停，留待重啟後查核：task=%s", task.task_id)
                continue
            try:
                self._tasks.pause_task(task.task_id, reason)
                paused.append(task.task_id)
            except Exception:
                self.failures += 1
                logger.exception("暫停 Task 失敗：%s", task.task_id)
        return paused


    def shutdown(self, reason: PauseReason = PauseReason.CORE_SHUTDOWN) -> list[str]:
        """SV-1：同步 ``pause_all``，再等仍有 EXECUTING action 的 Run 收到 receipt；整體不超過 ``shutdown_grace``。

        逾時不再等（該 action 保持 EXECUTING，重啟後 ``resume_run`` 轉 UNKNOWN 查核，T-11）。永不拋例外（關機不可被它擋住）。"""
        self._closing = True
        deadline = time.monotonic() + self._shutdown_grace.total_seconds()
        try:
            paused = self.pause_all(reason)
        except Exception:
            self.failures += 1
            logger.exception("關機暫停失敗")
            return []
        try:
            self._wait_receipts(deadline)
        except Exception:
            self.failures += 1
            logger.exception("等待 EXECUTING action 的 receipt 失敗，繼續關機")
        return paused

    def _wait_receipts(self, deadline: float) -> None:
        run_ids = [r.run_id for r in self._tasks.list_resumable_runs()]
        while True:
            pending = [rid for rid in run_ids if self._tasks.list_executing_actions(rid)]
            if not pending:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning("shutdown_grace 逾時，仍有 EXECUTING action 的 Run：%s", ", ".join(pending))
                return
            run_ids = pending
            time.sleep(min(_RECEIPT_POLL_S, remaining))


class SupervisorLoop:
    """Supervisor 的背景 tick 執行緒（仿 ``PresenceMaintenance``）：每 ``interval`` 秒 ``supervisor.tick()``，例外隔離、不殺執行緒。

    ``tick()`` 可手動呼叫；等待函式可注入（預設 ``Event.wait``，回 True 表示被要求停止）。"""

    def __init__(
        self, supervisor: Supervisor, *, interval: float = DEFAULT_SUPERVISOR_INTERVAL,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval 必須 > 0")
        self._supervisor = supervisor
        self._interval = interval
        self._stop = threading.Event()
        self._wait = wait if wait is not None else self._stop.wait
        self._thread: threading.Thread | None = None
        self.ticks = 0
        self.failures = 0

    def tick(self) -> None:
        try:
            self._supervisor.tick()
        except Exception:
            self.failures += 1
            logger.exception("Supervisor tick 失敗，下個 tick 重試")
        self.ticks += 1

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="UepSupervisor")
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.error("Supervisor 執行緒未能正常結束")
                return  # 保留參照：start() 見到還活著的執行緒就不再開第二條 tick
        self._thread = None

    def _run(self) -> None:
        while not self._wait(self._interval):
            self.tick()


def approval_expired_key(run_id: str, expiry: datetime) -> str:
    """核准過期通知的冪等鍵（SV-2(5)）：``appr_expired:{run_id}:{expiry 的 epoch 秒整數}``，只含 ``[A-Za-z0-9_:.-]``（E-20）。"""
    return f"appr_expired:{run_id}:{int(expiry.timestamp())}"
