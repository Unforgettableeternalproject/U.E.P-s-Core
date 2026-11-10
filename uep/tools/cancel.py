"""在途 action 的 executor 取消（D69、提案 §4.5／§6.4）。

只對 ``interruptible == "interruptible"`` 且 executor 實作 ``cancel`` 的 EXECUTING action 呼叫 ``executor.cancel``：
``checkpoint_only`` 要等安全點、``non_interruptible`` 等完成，兩者都不在這裡處理。
本模組不改任何 action 狀態：``stopped`` 之後的 receipt 由 worker 的 ``execute_action`` 照常入庫（R-2），
``isolated``／例外／非法回傳則結果未明，action 維持 EXECUTING 並由 Run 的 force 轉 UNKNOWN、經 ``resolve_unknown`` 查核（T-3），不重送。
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Literal

from uep.adapters.tool import ToolExecutor, ToolSpecResolver, supports_cancel

logger = logging.getLogger(__name__)

# stopped／isolated 為 executor 回報；skipped＝未呼叫（非 interruptible、無 cancel、找不到 executor 或註冊表解析失敗）；
# error＝cancel 拋例外或回傳非法值（視同結果未明）
CancelResult = Literal["stopped", "isolated", "skipped", "error"]


class ActionCanceller:
    """``Supervisor(cancel_actions=canceller.cancel_run_actions)`` 的實作；``tasks`` 需提供 ``list_executing_actions(run_id)``。"""

    def __init__(self, tasks: Any, executors: Mapping[str, ToolExecutor], resolver: ToolSpecResolver) -> None:
        self._tasks = tasks
        self._executors = dict(executors)
        self._resolver = resolver

    def cancel_run_actions(self, run_id: str) -> dict[str, CancelResult]:
        """對該 Run 所有 EXECUTING action 逐一嘗試；單一失敗不影響其餘。回傳 action_id -> 結果。"""
        return {a.action_id: self._cancel_one(a) for a in self._tasks.list_executing_actions(run_id)}

    def _cancel_one(self, action: Any) -> CancelResult:
        executor = self._executors.get(action.executor)
        if executor is None or not supports_cancel(executor):
            return "skipped"
        try:
            interruptible = self._resolver.resolve(action.tool).interruptible
        except Exception:  # noqa: BLE001  解析不到宣告就當不可中斷（fail-closed）
            logger.warning("取消前解析工具宣告失敗，略過：action=%s tool=%s", action.action_id, action.tool)
            return "skipped"
        if interruptible != "interruptible":
            return "skipped"
        try:
            outcome = executor.cancel(action.action_id)  # type: ignore[attr-defined]
        except Exception:
            logger.exception("executor.cancel 失敗，視為結果未明：action=%s", action.action_id)
            return "error"
        if outcome not in ("stopped", "isolated"):
            logger.warning("executor.cancel 回傳非法值 %r，視為結果未明：action=%s", outcome, action.action_id)
            return "error"
        return outcome
