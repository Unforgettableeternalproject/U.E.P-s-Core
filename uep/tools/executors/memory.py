"""記憶工具的 ``ToolExecutor``（切片 C2／C3；Agent 架構提案 §9.1 C 列、D62、PD-12／PD-13）。

把 ``uep.echo.tools.CoreToolExecutor`` 的 audience／roster／execute_tool 判定搬到 ActionIntent 路徑，
語意與之逐欄一致（舊路徑本片不刪，兩者有對照測試）：

- 白名單只認 ``TOOL_CLASSES``。身分與場域（``actor_id``／``audience``／``segment_id``／``guild_id``）是 Gateway 依 Core
  每輪的 ``tool_context`` 注入的 ``injected_params``（PD-12），executor 只從 ``call.params`` 讀這些欄位，其餘才是
  LLM 的業務引數；缺漏一律拒絕（fail-closed）。
- 執行當下再驗一次 audience（PD-13，``audience_ok``）並重取在場名單；``member_recall`` 的名稱解析不到不碰 engine。
- 不改任何共用狀態：``member_recall`` 讀到的投影 id 放在結果的 ``shared_ids``，由 stage 套用回 turn context。
- 結果文字只放回合範圍的 ``TurnOutcomeCache``（D33），receipt 只帶 ``result_ref`` 與 ``meta["status"]``
  （executed／failed／denied／not_found）。
- 逾時（``code=timeout``）副作用不確定：拋 ``ToolResponseLost``，由 TaskManager 標 UNKNOWN。
- ``lookup_authoritative=False``：結果暫存只在記憶體，沒有 receipt 時只能回 ``undetermined``。
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
from uep.stream.contracts.memory_tools import (
    ABOUT_BAD_REF,
    ABOUT_ME_TOOLS,
    ABOUT_NO_REF,
    DEFAULT_TOOL_MAX_CHARS,
    OPT_OUT_ABOUT_ME,
)
from uep.tools.outcomes import TurnOutcomeCache

logger = logging.getLogger("uep.tools.executors.memory")

MEMBER_NOT_FOUND = "無法確認是哪位成員，請先向使用者確認名字。"
_NAME_MAX = 80
INJECTED_KEYS = ("actor_id", "audience", "segment_id", "guild_id")


class RosterPort(Protocol):
    """``member_recall`` 需要的成員名單（``uep.echo.memory.GuildRoster`` 滿足）。"""

    def resolve_member(self, guild_id: str, name: str) -> str | None: ...

    def present_uids(self, guild_id: str) -> Any: ...


def _denied(name: str) -> dict[str, Any]:
    return {"success": False, "result": f"Tool {name!r} is not available.", "code": "denied"}


class MemoryToolExecutor:
    """滿足 ``uep.adapters.tool.ToolExecutor``。"""

    def __init__(
        self, memory: MemoryPort, roster: RosterPort, *, cache: TurnOutcomeCache | None = None,
        audience_ok: Callable[[str], bool] | None = None, max_chars: int = DEFAULT_TOOL_MAX_CHARS,
    ) -> None:
        self._memory = memory
        self._roster = roster
        self.cache = cache if cache is not None else TurnOutcomeCache()
        self._audience_ok = audience_ok
        self._max_chars = max_chars

    @property
    def lookup_authoritative(self) -> bool:
        return False

    def outcome(self, action_id: str) -> dict[str, Any] | None:
        """該 action 的結果（``{"success", "result"[, "shared_ids"]}``，已截斷）；回合結束清掉後為 None。"""
        return self.cache.get(action_id)

    def lookup_receipt(self, action_id: str) -> ReceiptLookup:
        receipt = self.cache.get_receipt(action_id)  # receipt 查核表隨回合清除（與結果暫存同生命週期）
        if receipt is not None:
            return ReceiptLookup("found", receipt)
        return ReceiptLookup("undetermined")

    def execute(self, call: ToolCall) -> ToolReceipt:
        existing = self.cache.get_receipt(call.action_id)
        if existing is not None and call.idempotency in ("native", "via_action_id"):
            return existing
        params = call.params if isinstance(call.params, Mapping) else None
        outcome = self._run(call.tool, params)
        code = outcome.pop("code", None)
        status = self._status(outcome, code)
        self.cache.put(call.action_id, outcome)
        if status == "unknown":
            raise ToolResponseLost(f"{call.action_id}: 記憶工具逾時，結果未明")
        receipt = ToolReceipt(
            receipt_id=new_id("rcpt"), action_id=call.action_id,
            succeeded=bool(outcome.get("success")) and status != "failed",
            result_ref=f"blob_{call.action_id}", recorded_at=datetime.now(UTC), source_verified=True,
            error_code=None if status in ("executed", "not_found") else status,
            meta={"status": status},
        )
        self.cache.put_receipt(call.action_id, receipt)
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

    def _run(self, name: str, params: Mapping[str, Any] | None) -> dict[str, Any]:
        if name not in TOOL_CLASSES or params is None:
            return _denied(name)
        actor_id, audience = params.get("actor_id"), params.get("audience")
        args = {k: v for k, v in params.items() if k not in INJECTED_KEYS}
        if not isinstance(audience, str) or not audience:
            return _denied(name)
        if self._audience_ok is not None and not self._audience_ok(audience):
            return _denied(name)  # PD-13：Gate 目前允許的 audience 已收窄，不依注入當下的舊值放行
        session_id = params.get("segment_id")
        call_context: dict[str, Any] | None = None
        if name in ABOUT_ME_TOOLS:
            guild_id = params.get("guild_id")
            if not isinstance(guild_id, str) or not guild_id:
                return _denied(name)
            call_context = {"guild_id": guild_id}
            if name != OPT_OUT_ABOUT_ME:
                ref = args.get("ref")
                if isinstance(ref, bool) or not isinstance(ref, int):
                    return {"success": False, "result": ABOUT_BAD_REF, "code": "failed"}
                projection_id = self.cache.about_ref(ref)  # 回合內 member_recall 查本人時登記的編號
                if projection_id is None:
                    return {
                        "success": False, "code": "failed",
                        "result": ABOUT_BAD_REF if self.cache.has_about_refs() else ABOUT_NO_REF,
                    }
                call_context["projection_id"] = projection_id
        if name == MEMBER_RECALL:
            guild_id = params.get("guild_id")
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
                name, args, actor_id=actor_id if isinstance(actor_id, str) else None, audience=audience,
                session_id=session_id if isinstance(session_id, str) else None, context=call_context,
            )
        except Exception as exc:  # noqa: BLE001 - 單一工具失敗不拖垮整輪；只記例外類別
            logger.warning("記憶工具執行失敗：%s %s", name, type(exc).__name__)
            return {"success": False, "result": "tool failed", "code": "failed"}
        return self._finish(name, outcome)

    def _finish(self, name: str, outcome: dict[str, Any]) -> dict[str, Any]:
        outcome = dict(outcome)
        ids = outcome.pop("shared_ids", None)
        about_refs = outcome.pop("about_refs", None)
        text = str(outcome.get("result", ""))
        visible = text  # 旁證編號只認完整的行；被截斷的半行不登記
        if len(text) > self._max_chars:
            outcome["result"] = text[: self._max_chars] + "…"
            visible = "\n".join(text[: self._max_chars].split("\n")[:-1])
            ids = None  # 結果被截斷：模型看到的投影不保證對得上 id，寧缺勿濫
        if name == MEMBER_RECALL and isinstance(about_refs, Mapping) and outcome.get("success"):
            self.cache.put_about_refs({
                int(n): str(pid) for n, pid in about_refs.items() if f"〔旁證{n}〕" in visible
            })
        if name == MEMBER_RECALL and ids and outcome.get("success"):
            outcome["shared_ids"] = list(dict.fromkeys(ids))  # 去重保序；由 stage 併回 turn context
        return outcome
