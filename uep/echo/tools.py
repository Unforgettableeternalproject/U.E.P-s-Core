"""ES 內建記憶工具的 Host 白名單執行器（P3接入方案 §2.2 CC-6、D79；Part S S3 之後改走 ActionIntent）。

ES 的 ``LLMThinkStage`` 會在 loop 上 ``await tool_executor(name, args, context=tool_context)``。這裡取代 ES 自帶的
executor，讓工具呼叫一律經 Core 的 Access Layer（audience／ACL 過濾）：

- 白名單：``deep_recall``（internal_read）、``force_remember``（reversible_local）、``member_recall``
  （internal_read）；不在 ``TOOL_CLASSES`` 內一律拒絕。
- 身分與 audience 只讀 Core 每輪新建的 ``tool_context``（``actor_id``、``audience``、``segment_id``、``ticket_ref``、
  ``guild_id``、``present_uids``）；LLM 給的 ``args`` 不能指定範圍。缺漏一律拒絕（fail-closed）。
- ``member_recall`` 的名稱解析用 ``GuildRoster``（顯示名與別名由平台轉接器提供）；解析不到或不只一位符合，
  不碰 engine、請模型先向使用者確認。LLM 實際看到的投影 id 寫回 ``tool_context["member_recall_ids"]``，
  回覆後由宿主併入 ``shared_context_ids``。
- 阻塞的 Access Layer 呼叫丟到 executor 執行，不卡 ES 的 loop。
- 執行後交給 ``on_action(turn_id, name, tool_class, status)`` 事後補記 ``action.*`` 事件（payload 只有分類與結果碼，
  不放引數與結果原文）；被拒絕與解析不到成員的呼叫不補記（沒有任何動作發生），只計數。
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Callable, Mapping
from concurrent.futures import Executor
from typing import Any

from uep.echo.memory import GuildRoster
from uep.memory.access import MEMBER_RECALL, TOOL_CLASSES
from uep.memory.protocol import MemoryPort

logger = logging.getLogger("uep.echo.tools")

DEFAULT_TOOL_MAX_CHARS = 1500
MEMBER_NOT_FOUND = "無法確認是哪位成員，請先向使用者確認名字。"
_NAME_MAX = 80
ActionRecorder = Callable[[str, str, str, str], None]


def _denied(name: str) -> dict[str, Any]:
    return {"success": False, "result": f"Tool {name!r} is not available.", "code": "denied"}


class CoreToolExecutor:
    """滿足 ES 的 ``ToolExecutor``（``async (name, args, *, context=None) -> {"success", "result"}``）。"""

    def __init__(
        self, memory: MemoryPort, roster: GuildRoster, *, on_action: ActionRecorder | None = None,
        executor: Executor | None = None, max_chars: int = DEFAULT_TOOL_MAX_CHARS,
        metrics: Counter[str] | None = None,
    ) -> None:
        self._memory = memory
        self._roster = roster
        self._on_action = on_action
        self._executor = executor
        self._max_chars = max_chars
        self.metrics: Counter[str] = metrics if metrics is not None else Counter()

    async def __call__(
        self, name: str, args: Mapping[str, Any], *, context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        outcome = await self._run(name, args, context)
        code = outcome.pop("code", None) if isinstance(outcome, dict) else None
        status = self._status(outcome, code)
        if status in ("denied", "not_found"):
            self.metrics[f"echo.tool.{status}"] += 1  # 沒有任何動作發生：只計數、不補記 action 事件
        else:
            self.metrics[f"echo.tool.{status}"] += 1
            turn_id = context.get("ticket_ref") if isinstance(context, dict) else None
            if self._on_action is not None and isinstance(turn_id, str):
                try:
                    self._on_action(turn_id, name, TOOL_CLASSES[name], status)
                except Exception:
                    logger.exception("補記工具 action 事件失敗")
        return outcome

    @staticmethod
    def _status(outcome: Mapping[str, Any], code: object) -> str:
        if code in ("denied", "not_found"):
            return str(code)
        if code == "timeout":
            return "unknown"  # 逾時後副作用（例如已寫入）不確定，只能記 unknown
        if code == "failed" or not outcome.get("success"):
            return "failed"
        return "executed"

    async def _run(
        self, name: str, args: Mapping[str, Any], context: dict[str, Any] | None
    ) -> dict[str, Any]:
        if name not in TOOL_CLASSES or not isinstance(args, Mapping) or not isinstance(context, dict):
            return _denied(name)
        actor_id, audience = context.get("actor_id"), context.get("audience")
        if not isinstance(audience, str) or not audience:
            return _denied(name)
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
                "guild_id": guild_id, "member_uid": uid, "present_uids": list(context.get("present_uids") or ()),
            }
        loop = asyncio.get_running_loop()
        try:
            outcome: dict[str, Any] = await loop.run_in_executor(
                self._executor,
                lambda: self._memory.execute_tool(
                    name, dict(args), actor_id=actor_id if isinstance(actor_id, str) else None, audience=audience,
                    session_id=session_id if isinstance(session_id, str) else None, context=call_context,
                ),
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
