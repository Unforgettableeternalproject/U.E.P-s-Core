"""``AgentThinkStage``：agent 工具路徑的 Think stage（Agent 架構提案 §9.1 C 列，切片 C3）。

組合持有一個 ``LLMThinkStage``（history、prompt、切句、commit／settle／retract 全部仍由它擁有，不複製協定面），
只換掉它的 ``tool_executor``：工具呼叫改交給注入的 ``ToolTurnPort``（Core 端以 ActionIntent 執行），並在回合結束時通知
Port 收尾回合 Task。

- 本檔不 import ``uep.agent``／``uep.tools``（``uep/stream`` 單向依賴）：Port 只是 Protocol，由 ``uep/echo`` 的 wiring 綁定。
- 宿主（``EchoPipelineHost``）仍會對 ``think.tool_executor`` 賦值舊的 ``CoreToolExecutor``；這裡的 ``tool_executor``
  屬性接住並**忽略**它（兩條路徑互斥：agent 路徑只走 Port）。其餘屬性（``tools``、``memory_resolver`` 等）轉給 inner。
- 結果裡的 ``shared_ids``（member_recall 的投影 id）由這裡套用回 turn 的 ``tool_context["member_recall_ids"]``，
  executor 本身不改共用狀態。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from concurrent.futures import Executor
from typing import Any, Protocol

from ..contracts.cancellation import CancellationToken
from ..contracts.types import Sentence, Utterance

logger = logging.getLogger(__name__)

_OWN = frozenset({"inner", "port", "_inner", "_port", "_executor", "_host_executor"})


class ToolTurnPort(Protocol):
    """Core 端的回合工具執行（阻塞呼叫；stage 會丟到 executor 執行，不卡 ES 的 loop）。"""

    def run_tool(self, name: str, args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        """執行一個工具呼叫，回 ``{"success": bool, "result": str[, "shared_ids": [...]]}``。永不拋（失敗以 success=False 表達）。"""
        ...

    def finish_turn(self, turn_id: str) -> None:
        """回合結束收尾（沒有任何工具呼叫的回合為 no-op）。"""
        ...


class AgentThinkStage:
    name = "llm"

    def __init__(self, inner: Any, port: ToolTurnPort | None = None, *, executor: Executor | None = None) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_port", port)
        object.__setattr__(self, "_executor", executor)
        object.__setattr__(self, "_host_executor", None)
        inner.tool_executor = self._execute

    # ---- 組合：屬性轉給 inner ----
    @property
    def inner(self) -> Any:
        return self._inner

    @property
    def port(self) -> ToolTurnPort | None:
        return self._port

    def bind_port(self, port: ToolTurnPort) -> None:
        object.__setattr__(self, "_port", port)

    @property
    def tool_executor(self) -> Any:
        """宿主賦值的舊 executor 只被記住、不使用（agent 路徑互斥）；未賦值前為 None，通過宿主的「自帶 executor」檢查。"""
        return self._host_executor

    @tool_executor.setter
    def tool_executor(self, value: Any) -> None:
        object.__setattr__(self, "_host_executor", value)

    def __getattr__(self, item: str) -> Any:
        if item in _OWN or item.startswith("__"):
            raise AttributeError(item)
        return getattr(self._inner, item)

    def __setattr__(self, key: str, value: Any) -> None:
        if key in _OWN or key == "tool_executor":
            if key == "tool_executor":
                type(self).tool_executor.fset(self, value)  # type: ignore[attr-defined]
            else:
                object.__setattr__(self, key, value)
        else:
            setattr(self._inner, key, value)

    # ---- ThinkStage：stream 外包回合收尾，其餘走 __getattr__ ----
    async def stream(self, utterance: Utterance, token: CancellationToken) -> AsyncIterator[Sentence]:
        turn_id = utterance.turn_id or ""
        try:
            async for sentence in self._inner.stream(utterance, token):
                yield sentence
        finally:
            await self._finish(turn_id)

    async def _finish(self, turn_id: str) -> None:
        port = self._port
        if port is None or not turn_id:
            return
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, port.finish_turn, turn_id)
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            # 取消中的收尾：讓它在背景完成，不吞掉取消
            future.add_done_callback(lambda f: f.exception() and logger.error("回合收尾失敗：%s", type(f.exception()).__name__))
            raise
        except Exception:
            logger.exception("回合收尾失敗：%s", turn_id)

    # ---- 給 inner 的 tool_executor ----
    async def _execute(
        self, name: str, args: Mapping[str, Any], *, context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        port = self._port
        if port is None or not isinstance(context, dict) or not isinstance(args, Mapping):
            return {"success": False, "result": f"Tool {name!r} is not available."}
        loop = asyncio.get_running_loop()
        try:
            outcome = await loop.run_in_executor(self._executor, port.run_tool, name, dict(args), context)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - Port 不該拋；保險起見不拖垮整輪
            logger.warning("工具 Port 失敗：%s %s", name, type(exc).__name__)
            return {"success": False, "result": "tool failed"}
        outcome = dict(outcome)
        ids = outcome.pop("shared_ids", None)
        if ids:
            bucket = context.setdefault("member_recall_ids", [])
            for pid in ids:
                if pid not in bucket:
                    bucket.append(pid)
        return outcome

