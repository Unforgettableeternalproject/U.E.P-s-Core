"""在途 action 的 executor 取消（D69、提案 §4.5／§6.4）。

只對 ``interruptible == "interruptible"`` 且 executor 實作 ``cancel`` 的 EXECUTING action 呼叫 ``executor.cancel``：
``checkpoint_only`` 要等安全點、``non_interruptible`` 等完成，兩者都不在這裡處理。
本模組不改任何 action 狀態：``stopped`` 之後的 receipt 由 worker 的 ``execute_action`` 照常入庫（R-2），
``isolated``／逾時／例外／非法回傳則結果未明，action 由 Run 的 force 轉 UNKNOWN、經 ``resolve_unknown`` 查核（T-3），不重送。

``prepare(run_id)`` 先取 EXECUTING 快照並回傳「稍後執行 cancel」的函式，讓呼叫端（Supervisor）能在 force 之後才取消。
cancel 在執行緒池平行呼叫，並有總逾時；逾時視為 ``isolated``，不累加阻塞（卡住的 cancel 執行緒會留在背景，不影響呼叫端）。
已知限制：``action_intents`` 沒有保存建立時的 ``interruptible``，這裡以當前註冊表宣告判定（``tool_version`` 漂移時可能不一致，
但漂移時只會多／少一次盡力而為的 cancel，不影響終態）。
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, Literal

from uep.adapters.tool import ToolExecutor, ToolSpecResolver, supports_cancel

logger = logging.getLogger(__name__)

# stopped／isolated 為 executor 回報；skipped＝未呼叫（非 interruptible、無 cancel、找不到 executor 或註冊表解析失敗）；
# error＝cancel 拋例外或回傳非法值（視同結果未明）
CancelResult = Literal["stopped", "isolated", "skipped", "error"]
DEFAULT_CANCEL_TIMEOUT_S = 5.0  # 整批 cancel 的總逾時；保守預設，Supervisor tick 最多被拖這麼久


class ActionCanceller:
    """``Supervisor(cancel_actions=canceller.prepare)`` 的實作；``tasks`` 需提供 ``list_executing_actions(run_id)``。"""

    def __init__(
        self, tasks: Any, executors: Mapping[str, ToolExecutor], resolver: ToolSpecResolver,
        *, timeout_s: float = DEFAULT_CANCEL_TIMEOUT_S,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s 必須 > 0")
        self._tasks = tasks
        self._executors = dict(executors)
        self._resolver = resolver
        self._timeout_s = timeout_s

    def prepare(self, run_id: str) -> Callable[[], dict[str, CancelResult]]:
        """現在取 EXECUTING 快照，回傳稍後才執行 cancel 的函式。"""
        snapshot = list(self._tasks.list_executing_actions(run_id))
        return lambda: self._cancel_all(snapshot)

    def cancel_run_actions(self, run_id: str) -> dict[str, CancelResult]:
        """取快照並立刻取消（便利方法）。"""
        return self.prepare(run_id)()

    def _cancel_all(self, actions: list[Any]) -> dict[str, CancelResult]:
        results: dict[str, CancelResult] = {}
        targets: list[tuple[Any, ToolExecutor]] = []
        for a in actions:
            executor = self._eligible(a)
            if executor is None:
                results[a.action_id] = "skipped"
            else:
                targets.append((a, executor))
        if targets:
            pool = ThreadPoolExecutor(max_workers=len(targets), thread_name_prefix="tool-cancel")
            try:
                futures = {a.action_id: pool.submit(self._cancel_one, a.action_id, ex) for a, ex in targets}
                wait(list(futures.values()), timeout=self._timeout_s)
                for action_id, future in futures.items():
                    if future.done():
                        results[action_id] = future.result()
                    else:
                        logger.warning("executor.cancel 逾時（%.1fs），視為 isolated：action=%s", self._timeout_s, action_id)
                        results[action_id] = "isolated"
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
        for action_id, outcome in results.items():
            logger.info("D69 cancel 結果：action=%s outcome=%s", action_id, outcome)
        return results

    def _eligible(self, action: Any) -> ToolExecutor | None:
        executor = self._executors.get(action.executor)
        if executor is None or not supports_cancel(executor):
            return None
        try:
            interruptible = self._resolver.resolve(action.tool).interruptible
        except Exception:  # noqa: BLE001  解析不到宣告就當不可中斷（fail-closed）
            logger.warning("取消前解析工具宣告失敗，略過：action=%s tool=%s", action.action_id, action.tool)
            return None
        return executor if interruptible == "interruptible" else None

    @staticmethod
    def _cancel_one(action_id: str, executor: ToolExecutor) -> CancelResult:
        try:
            outcome = executor.cancel(action_id)  # type: ignore[attr-defined]
        except Exception:
            logger.exception("executor.cancel 失敗，視為結果未明：action=%s", action_id)
            return "error"
        if outcome not in ("stopped", "isolated"):
            logger.warning("executor.cancel 回傳非法值 %r，視為結果未明：action=%s", outcome, action_id)
            return "error"
        return outcome
