"""Tool Gateway 薄層（契約 §4.2、TG-1、TG-9、TG-12、D61）。

只做三件事，不持有 DB、executor 或憑證：
1. ``check_intent``：``tool_intent`` 的 capability_set 檢查（工具不在當輪可見集合就拒絕，不建 action）。
2. ``filter_tool_result``：TG-9 過濾掛點，首版全放行（逐項補預設 ``scope``）。
3. ``result_view``／``outcome_view``：把 ``ActionOutcome``／拒絕原因組成 D61 的 ``submit_tool_result`` 內容。

結果內容只含項目引用與狀態短碼，**不放例外文字或參數內容**。

切片 B（§6.3）：建構時給了 ``registry`` 就在 capability 檢查之後做參數驗證（JSON Schema 子集、``injected_params``），
失敗拋帶 ``fields``（違規欄位的 JSON Pointer，**不含值**）的 ``GatewayRejected``，由 ``rejection_view`` 組成回餵。
沒給 ``registry`` 時維持切片 A 的行為（只做 capability 檢查，``rejection_view`` 也回舊形狀：不帶 ``reason_code``／``fields``），
既有呼叫端不受影響；帶 ``registry`` 時才會出現這兩個新欄位。
"""
from __future__ import annotations

import copy
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Literal

from uep.tasks.manager import ActionOutcome, PolicyViolation
from uep.tasks.states import ActionStatus
from uep.tools.registry import ToolRegistry
from uep.tools.schema import validate_args

ResultStatus = Literal["ok", "failed", "rejected", "blocked", "unknown"]
DEFAULT_SCOPE = "private"

# 拒絕原因碼（§6.3-5 的固定短碼詞彙；rejection_view 會把它交給 Runtime）
CAPABILITY_MISSING = "capability_missing"
ARGS_NOT_MAPPING = "args_not_mapping"
INVALID_TOOL_ARGS = "invalid_tool_args"
TOOL_UNKNOWN = "tool_unknown"
INJECTED_PARAM_MISSING = "injected_param_missing"  # Core 組裝錯誤：宣告了 injected_params 卻沒提供值
TOOL_NOT_READY = "tool_not_ready"  # §5.8：已投出的工具在執行前轉為非 ready（warming／unavailable）


class GatewayRejected(Exception):
    """Gateway 驗證不通過；``code`` 是機器可判讀的短碼（即 ``reason_code``），訊息不含參數內容。
    ``fields`` 是違規欄位的 JSON Pointer 清單，只含路徑、不含值。"""

    def __init__(self, code: str, message: str = "", *, fields: Sequence[str] = ()) -> None:
        super().__init__(message or code)
        self.code = code
        self.fields: tuple[str, ...] = tuple(fields)


def visible_tools(capability_set: Mapping[str, Any]) -> frozenset[str]:
    """``capability_set`` 的投影（TG-12：``{tools: [{tool, ...}]}``）轉成工具名集合。"""
    tools = capability_set.get("tools", ()) if isinstance(capability_set, Mapping) else ()
    names: set[str] = set()
    for entry in tools:
        name = entry.get("tool") if isinstance(entry, Mapping) else entry
        if isinstance(name, str) and name:
            names.add(name)
    return frozenset(names)


def _variant_of(capability_set: Mapping[str, Any], tool: str) -> str | None:
    """以 ``capability_set`` 查回變體（§5.6-3）：不信任模型自帶。"""
    for item in capability_set.get("tools", ()):
        if isinstance(item, Mapping) and item.get("tool") == tool:
            variant = item.get("variant")
            return variant if isinstance(variant, str) else None
    return None


def filter_tool_result(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """TG-9 過濾掛點（D56：首版全放行）。resume 路徑日後共用同一個函式；逐項 ``scope`` 缺則補預設。"""
    return [{"scope": DEFAULT_SCOPE, **item} for item in items]


class ToolGateway:
    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self._registry = registry

    def check_intent(
        self, tool: str, args: Any, capability_set: Mapping[str, Any], *, injected: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """通過回傳**驗證並注入後**的參數（新 dict）；不通過拋 ``GatewayRejected``（此時不得建 action）。

        順序（SA-3）：capability -> readiness（``tool_not_ready``，§5.8；只在 registry 有 ``readiness`` 時）-> 參數為物件 -> 以有效 schema 驗證（``invalid_tool_args``）-> 注入 Core 提供的值。
        ``injected_params`` 不在 LLM 面 schema 內，LLM 若提供就是一般多餘欄位（同樣回 ``invalid_tool_args`` 與
        ``/<unknown>``，不洩漏哪些欄位是注入的）。回傳值是深複本，呼叫端改它不會影響模型原始的 args。注入發生在 ``params_hash`` 計算之前，
        所以呼叫端必須用回傳值建 action（核准所見＝實際執行）。``injected`` 是 Core 持有的值（``venue`` 等）。"""
        if tool not in visible_tools(capability_set):
            raise GatewayRejected(CAPABILITY_MISSING, f"工具不在可見集合：{tool}")
        self._check_ready(tool)
        if not isinstance(args, Mapping):
            raise GatewayRejected(ARGS_NOT_MAPPING, "參數必須是物件")
        if self._registry is None:
            return copy.deepcopy(dict(args))
        try:
            entry = self._registry.entry(tool)
            schema = self._registry.effective_input_schema(tool, _variant_of(capability_set, tool))
        except PolicyViolation:
            raise GatewayRejected(TOOL_UNKNOWN, f"工具未註冊：{tool}") from None
        injected_names = entry.descriptor.injected_params
        violations = validate_args(schema, args)
        if violations:
            raise GatewayRejected(INVALID_TOOL_ARGS, "參數不符合 schema", fields=violations)
        values = copy.deepcopy(dict(args))
        provided = injected or {}
        for name in injected_names:
            if name not in provided:
                raise GatewayRejected(INJECTED_PARAM_MISSING, f"缺少注入值：{name}")
            values[name] = provided[name]
        return values

    def _check_ready(self, tool: str) -> None:
        """§5.8 執行前再檢查：投出之後 readiness 可能已轉為 warming／unavailable ⇒ 拒絕、不建 action。
        沒有 registry、或 registry 沒有 ``readiness``（例如 ``StaticToolRegistry``）時不檢查（舊路徑不變）；
        工具未註冊交給後面的 ``tool_unknown`` 處理。``readiness`` 是純記憶體查詢（不做 I/O）。"""
        readiness = getattr(self._registry, "readiness", None)
        if not callable(readiness):
            return
        try:
            state = readiness(tool)
        except PolicyViolation:
            return
        if state != "ready":
            raise GatewayRejected(TOOL_NOT_READY, f"工具尚未就緒（{state}）：{tool}")

    def rejection_view(self, exc: GatewayRejected) -> dict[str, Any]:
        """``check_intent`` 失敗的回餵（D61 只加不改）：``status=rejected`` 加選填 ``reason_code``／``fields``。
        只有短碼與欄位路徑，沒有值、例外文字。沒給 registry 的 Gateway 回切片 A 的舊形狀（不帶新欄位）。"""
        if self._registry is None:
            return self.result_view("rejected")
        return self.result_view("rejected", reason_code=exc.code, fields=exc.fields)

    def result_view(
        self, status: ResultStatus, *, items: Sequence[Mapping[str, Any]] = (), degraded: bool = False,
        reason_code: str | None = None, fields: Sequence[str] = (),
    ) -> dict[str, Any]:
        """D61 內容：items／status／degraded／partial（首版 partial 恆 false，不含 excluded_summary）。
        選填 ``reason_code``／``fields``（§6.3-5）只在提供時才出現，既有消費方看到的形狀不變。"""
        view: dict[str, Any] = {
            "items": filter_tool_result(items), "status": status, "degraded": degraded, "partial": False,
        }
        if reason_code is not None:
            view["reason_code"] = reason_code
        if fields:
            view["fields"] = list(fields)
        return view

    def outcome_view(
        self, outcome: ActionOutcome, *, content_lookup: Callable[[str], str | None] | None = None,
    ) -> dict[str, Any]:
        """依 receipt／action 終態組結果：SUCCEEDED -> ok、FAILED -> failed、其餘（UNKNOWN 等）-> unknown。

        ``degraded`` 取 receipt 的來源未驗證（``source_verified=False``）。
        ``content_lookup``：給了就從回合範圍的結果來源取文字放進 item 的 ``content``（internal_read 的原文不進 receipt／DB，
        D33）；成功但取不到文字時回 ``unknown``，不編造內容。
        """
        receipt = outcome.receipt
        if outcome.status is ActionStatus.SUCCEEDED and receipt is not None:
            items = [{"ref": receipt.result_ref, "action_id": outcome.action_id}] if receipt.result_ref else []
            if content_lookup is not None:
                content = content_lookup(outcome.action_id)
                if content is None:
                    return self.result_view("unknown")
                items = [{**item, "content": content} for item in items] or [
                    {"ref": "", "action_id": outcome.action_id, "content": content}
                ]
            return self.result_view("ok", items=items, degraded=not receipt.source_verified)
        if outcome.status is ActionStatus.FAILED:
            return self.result_view("failed")
        return self.result_view("unknown")
