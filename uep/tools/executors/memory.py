"""記憶工具的 ``ToolExecutor``（切片 C2；Agent 架構提案 §9.1 C 列、D62）。

把 ``uep.echo.tools.CoreToolExecutor`` 的 audience／roster／execute_tool 判定搬到 ActionIntent 路徑，
語意與之逐欄一致（舊路徑本片不刪，兩者有對照測試）：

- 白名單只認 ``TOOL_CLASSES``；身分與 audience 只讀 Core 每輪新建的 ``tool_context``（由 ``context_provider`` 依
  action 取回），LLM 的 ``params`` 不能指定範圍；缺漏一律拒絕（fail-closed）。
- ``member_recall`` 的名稱解析用 roster；解析不到不碰 engine；呼叫當下重取在場名單並再比對一次 ``audience_ok``。
- 結果文字不進 receipt（receipt 只帶 ``result_ref``）：完整結果暫存在 ``outcome(action_id)``，由呼叫端
  經 Gateway／blob 路徑取用（C3 接線）。分類結果短碼放 ``meta["status"]``（executed／failed／denied／not_found）。
- 逾時（``code=timeout``）副作用不確定：拋 ``ToolResponseLost``，由 TaskManager 標 UNKNOWN，不得盲目重試。
- ``lookup_authoritative=False``：結果暫存只在記憶體，重啟後遺失，沒有紀錄時只能回 ``undetermined``。
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from uep.adapters.tool import ReceiptLookup, ToolCall, ToolReceipt, ToolResponseLost
from uep.ids import new_id
from uep.memory.access import MEMBER_RECALL, TOOL_CLASSES
from uep.memory.protocol import MemoryPort
from uep.stream.contracts.memory_tools import DEFAULT_TOOL_MAX_CHARS

logger = logging.getLogger("uep.tools.executors.memory")

MEMBER_NOT_FOUND = "無法確認是哪位成員，請先向使用者確認名字。"
_NAME_MAX = 80
ContextProvider = Callable[[ToolCall], "dict[str, Any] | None"]
ActionRecorder = Callable[[str, str, str, str], None]


class RosterPort(Protocol):
    """``member_recall`` 需要的成員名單（``uep.echo.memory.GuildRoster`` 滿足）。"""

    def resolve_member(self, guild_id: str, name: str) -> str | None: ...

    def present_uids(self, guild_id: str) -> Any: ...


def _denied(name: str) -> dict[str, Any]:
    return {"success": False, "result": f"Tool {name!r} is not available.", "code": "denied"}


class MemoryToolExecutor:
    """滿足 ``uep.adapters.tool.ToolExecutor``。"""

    def __init__(
        self, memory: MemoryPort, roster: RosterPort, context_provider: ContextProvider, *,
        audience_ok: Callable[[str], bool] | None = None, max_chars: int = DEFAULT_TOOL_MAX_CHARS,
        on_action: ActionRecorder | None = None,
    ) -> None:
        self._memory = memory
        self._roster = roster
        self._context = context_provider
        self._audience_ok = audience_ok
        self._max_chars = max_chars
        self._on_action = on_action
        self._receipts: dict[str, ToolReceipt] = {}
        self._outcomes: dict[str, dict[str, Any]] = {}

    @property
    def lookup_authoritative(self) -> bool:
        return False

    def outcome(self, action_id: str) -> dict[str, Any] | None:
        """該 action 的完整結果（``{"success", "result"}``，已截斷／脫敏）；沒有則 None。"""
        found = self._outcomes.get(action_id)
        return dict(found) if found is not None else None

    def lookup_receipt(self, action_id: str) -> ReceiptLookup:
        receipt = self._receipts.get(action_id)
        if receipt is not None:
            return ReceiptLookup("found", receipt)
        return ReceiptLookup("undetermined")

    def execute(self, call: ToolCall) -> ToolReceipt:
        existing = self._receipts.get(call.action_id)
        if existing is not None and call.idempotency in ("native", "via_action_id"):
            return existing
        context = self._context(call)
        outcome = self._run(call.tool, call.params, context)
        code = outcome.pop("code", None)
        status = self._status(outcome, code)
        if status not in ("denied", "not_found"):
            turn_id = context.get("ticket_ref") if isinstance(context, dict) else None
            if self._on_action is not None and isinstance(turn_id, str):
                try:
                    self._on_action(turn_id, call.tool, TOOL_CLASSES[call.tool], status)
                except Exception:
                    logger.exception("補記工具 action 事件失敗")
        self._outcomes[call.action_id] = outcome
        if status == "unknown":
            raise ToolResponseLost(f"{call.action_id}: 記憶工具逾時，結果未明")
        receipt = ToolReceipt(
            receipt_id=new_id("rcpt"), action_id=call.action_id,
            succeeded=bool(outcome.get("success")) and status != "failed",
            result_ref=f"blob_{call.action_id}", recorded_at=datetime.now(UTC), source_verified=True,
            error_code=None if status in ("executed", "not_found") else status,
            meta={"status": status},
        )
        self._receipts[call.action_id] = receipt
        return receipt

    @staticmethod
    def _status(outcome: Mapping[str, Any], code: object) -> str:
        if code in ("denied", "not_found"):
            return str(code)
        if code == "timeout":
            return "unknown"
        if code == "failed" or not outcome.get("success"):
            return "failed"
        return "executed"

    def _run(self, name: str, args: Mapping[str, Any], context: dict[str, Any] | None) -> dict[str, Any]:
        if name not in TOOL_CLASSES or not isinstance(args, Mapping) or not isinstance(context, dict):
            return _denied(name)
        actor_id, audience = context.get("actor_id"), context.get("audience")
        if not isinstance(audience, str) or not audience:
            return _denied(name)
        if self._audience_ok is not None and not self._audience_ok(audience):
            return _denied(name)  # Gate 目前允許的 audience 已收窄：不依 context 的舊值放行
        session_id = context.get("segment_id")
        call_context: dict[str, Any] | None = None
        if name == MEMBER_RECALL:
            guild_id = context.get("guild_id")
            if not isinstance(guild_id, str) or not guild_id:
                return _denied(name)
            member = " ".join(str(args.get("member") or "").split())[:_NAME_MAX]
            uid = self._roster.resolve_member(guild_id, member) if member else None
            if uid is None:
                return {"success": True, "result": MEMBER_NOT_FOUND, "code": "not_found"}
            call_context = {
                "guild_id": guild_id, "member_uid": uid, "present_uids": self._roster.present_uids(guild_id),
            }
        try:
            outcome: dict[str, Any] = self._memory.execute_tool(
                name, dict(args), actor_id=actor_id if isinstance(actor_id, str) else None, audience=audience,
                session_id=session_id if isinstance(session_id, str) else None, context=call_context,
            )
        except Exception as exc:  # noqa: BLE001 - 單一工具失敗不拖垮整輪；只記例外類別
            logger.warning("記憶工具執行失敗：%s %s", name, type(exc).__name__)
            return {"success": False, "result": "tool failed", "code": "failed"}
        return self._finish(name, outcome, context)

    def _finish(self, name: str, outcome: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        outcome = dict(outcome)
        ids = outcome.pop("shared_ids", None)
        text = str(outcome.get("result", ""))
        if len(text) > self._max_chars:
            outcome["result"] = text[: self._max_chars] + "…"
            ids = None  # 結果被截斷：模型看到的投影不保證對得上 id，寧缺勿濫
        if name == MEMBER_RECALL and ids and outcome.get("success"):
            bucket = context.setdefault("member_recall_ids", [])
            for pid in ids:
                if pid not in bucket:
                    bucket.append(pid)
        return outcome
