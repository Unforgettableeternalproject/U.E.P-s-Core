"""Tool Gateway 薄層（契約 §4.2、TG-1、TG-9、TG-12、D61）。

只做三件事，不持有 DB、executor 或憑證：
1. ``check_intent``：``tool_intent`` 的 capability_set 檢查（工具不在當輪可見集合就拒絕，不建 action）。
2. ``filter_tool_result``：TG-9 過濾掛點，首版全放行（逐項補預設 ``scope``）。
3. ``result_view``／``outcome_view``：把 ``ActionOutcome``／拒絕原因組成 D61 的 ``submit_tool_result`` 內容。

結果內容只含項目引用與狀態短碼，**不放例外文字或參數內容**。參數 schema 驗證屬切片 1（S2）：目前
``ToolSpec`` 沒有 schema 欄位，這裡只做 capability 檢查。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from uep.tasks.manager import ActionOutcome
from uep.tasks.states import ActionStatus

ResultStatus = Literal["ok", "failed", "rejected", "blocked", "unknown"]
DEFAULT_SCOPE = "private"

# 拒絕原因碼（只進 log／稽核，不交給 Runtime）
CAPABILITY_MISSING = "capability_missing"
ARGS_NOT_MAPPING = "args_not_mapping"


class GatewayRejected(Exception):
    """Gateway 驗證不通過；``code`` 是機器可判讀的短碼，訊息不含參數內容。"""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def visible_tools(capability_set: Mapping[str, Any]) -> frozenset[str]:
    """``capability_set`` 的投影（TG-12：``{tools: [{tool, ...}]}``）轉成工具名集合。"""
    tools = capability_set.get("tools", ()) if isinstance(capability_set, Mapping) else ()
    names: set[str] = set()
    for entry in tools:
        name = entry.get("tool") if isinstance(entry, Mapping) else entry
        if isinstance(name, str) and name:
            names.add(name)
    return frozenset(names)


def filter_tool_result(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """TG-9 過濾掛點（D56：首版全放行）。resume 路徑日後共用同一個函式；逐項 ``scope`` 缺則補預設。"""
    return [{"scope": DEFAULT_SCOPE, **item} for item in items]


class ToolGateway:
    def check_intent(self, tool: str, args: Any, capability_set: Mapping[str, Any]) -> None:
        """通過回傳 None；不通過拋 ``GatewayRejected``（此時不得建 action）。"""
        if tool not in visible_tools(capability_set):
            raise GatewayRejected(CAPABILITY_MISSING, f"工具不在可見集合：{tool}")
        if not isinstance(args, Mapping):
            raise GatewayRejected(ARGS_NOT_MAPPING, "參數必須是物件")

    def result_view(
        self, status: ResultStatus, *, items: Sequence[Mapping[str, Any]] = (), degraded: bool = False,
    ) -> dict[str, Any]:
        """D61 內容：items／status／degraded／partial（首版 partial 恆 false，不含 excluded_summary）。"""
        return {
            "items": filter_tool_result(items), "status": status, "degraded": degraded, "partial": False,
        }

    def outcome_view(self, outcome: ActionOutcome) -> dict[str, Any]:
        """依 receipt／action 終態組結果：SUCCEEDED -> ok、FAILED -> failed、其餘（UNKNOWN 等）-> unknown。

        ``degraded`` 取 receipt 的來源未驗證（``source_verified=False``）。
        """
        receipt = outcome.receipt
        if outcome.status is ActionStatus.SUCCEEDED and receipt is not None:
            items = [{"ref": receipt.result_ref, "action_id": outcome.action_id}] if receipt.result_ref else []
            return self.result_view("ok", items=items, degraded=not receipt.source_verified)
        if outcome.status is ActionStatus.FAILED:
            return self.result_view("failed")
        return self.result_view("unknown")
