"""``ModelPort`` 的 PydanticAI 實作（Spike 1）：只用其模型層，不用它的執行／history（Agent 架構提案 §7.2）。

- 工具全部走 ``ExternalToolset``：SDK 只知道名稱與 schema，**定義只來自** ``render_tools(capability_set)``。
- 遇到工具呼叫即結束 run（``output_type=[str, DeferredToolRequests]``）；tool_intent 由 Core 執行。
- 串流 part 事件對映：ThinkingPartDelta -> thought_delta；TextPartDelta -> text_delta；
  ToolCallPart(Delta) -> tool_call_delta -> ToolCallAssembler -> tool_intent。
- D60 啟動斷言自寫（不信任 SDK）：將送出的 tools 與 ``render_tools(capability_set)`` 整份比對，內建工具必須為空。
- 取消：每個 attempt 一個 ``CancellationToken``；``cancel()`` 返回後不再 yield 事件。

SDK（``pydantic_ai``）的 import 只允許出現在 ``uep/models/``（tests/uep/test_models_boundary.py 強制）。
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from pydantic_ai import (
    Agent,
    CancellationToken,
    DeferredToolRequests,
    ExternalToolset,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    RunContext,
    SystemPromptPart,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolCallPartDelta,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai import (
    ModelRequest as SdkRequest,
)
from pydantic_ai import (
    ModelResponse as SdkResponse,
)
from pydantic_ai.exceptions import ModelRetry, RunCancelled, UnexpectedModelBehavior
from pydantic_ai.messages import FunctionToolResultEvent
from pydantic_ai.models import Model
from pydantic_ai.run import AgentRunResultEvent
from pydantic_ai.tools import ToolDefinition

from uep.adapters.runtime import RuntimeRequest, check_tools_match_capability
from uep.models.port import (
    CapabilityMissingError,
    ChatMessage,
    Dialect,
    ModelEvent,
    ModelRequest,
    ProviderCapabilities,
    ToolCallAssembler,
)
from uep.tools.render import render_tools

logger = logging.getLogger(__name__)


class _GuardedExternalToolset(ExternalToolset):
    """外部工具集：任何「SDK 想自己執行工具」的路徑都計數並拒絕（S1-1 的 mock executor）。"""

    def __init__(self, tool_defs: list[ToolDefinition], counter: list[int]) -> None:
        super().__init__(tool_defs)
        self._counter = counter

    async def call_tool(self, name: str, tool_args: dict[str, Any], ctx: RunContext[Any], tool: Any) -> Any:
        self._counter[0] += 1
        raise RuntimeError(f"SDK 不得執行工具：{name}")


class PydanticAIModelPort:
    """``dialect``：openai_chat｜openai_responses｜ollama。``model`` 可直接注入（離線測試用 FunctionModel／TestModel）。"""

    def __init__(
        self,
        dialect: Dialect,
        model_name: str = "",
        *,
        api_key: str | None = None,
        project: str | None = None,
        organization: str | None = None,
        base_url: str | None = None,
        timeout: float = 120.0,
        reasoning_summary: str | None = None,
        thinking: bool | None = None,
        model: Model | None = None,
    ) -> None:
        self.dialect: Dialect = dialect
        self.model_name = model_name
        self.reasoning_summary = reasoning_summary
        self.thinking = thinking
        self.caps = ProviderCapabilities(
            tools_with_reasoning=dialect != "openai_chat",
            emits_thinking=dialect != "openai_chat",
        )
        self._model = model if model is not None else self._build_model(
            dialect, model_name, api_key, project, organization, base_url, timeout,
        )
        # 工具重試設 0：模型叫了不在清單內的工具時，SDK 預設會自行再打一次模型（多一次請求），這裡不要；
        # 未知工具改由 ``stream`` 轉成 tool_intent 交給 Core 的 Gateway 拒絕（output 重試維持預設）
        self._agent: Agent[None, Any] = Agent(
            self._model, output_type=[str, DeferredToolRequests], retries={"tools": 0},
        )
        self._tokens: dict[str, CancellationToken] = {}
        self._cancelled: set[str] = set()
        self.sdk_tool_executions: list[int] = [0]  # S1-1：SDK 自行執行工具的次數（必須恆為 0）
        self.dropped_deltas: list[str] = []  # fail-closed 丟棄的 delta 原因（S1-2；正常情況應為空）
        self.audit: list[tuple[str, str]] = []  # (SDK part 歸屬, 對映出的事件 kind)；S1-2 結構判定用

    @staticmethod
    def _build_model(
        dialect: Dialect, model_name: str, api_key: str | None, project: str | None,
        organization: str | None, base_url: str | None, timeout: float,
    ) -> Model:
        from openai import AsyncOpenAI
        from pydantic_ai.models.ollama import OllamaModel
        from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
        from pydantic_ai.providers.ollama import OllamaProvider
        from pydantic_ai.providers.openai import OpenAIProvider

        if dialect == "ollama":
            return OllamaModel(model_name, provider=OllamaProvider(base_url=base_url or "http://localhost:11434/v1"))
        client = AsyncOpenAI(
            api_key=api_key, project=project, organization=organization, base_url=base_url, timeout=timeout,
        )
        provider = OpenAIProvider(openai_client=client)
        if dialect == "openai_responses":
            return OpenAIResponsesModel(model_name, provider=provider)
        return OpenAIChatModel(model_name, provider=provider)

    # ── D60 ──────────────────────────────────────────────────────────────

    def _tool_defs(self, req: ModelRequest) -> tuple[list[ToolDefinition], list[dict[str, Any]]]:
        """``(要交給 SDK 的 ToolDefinition, 預期的 render_tools 結果)``；定義只來自 render_tools。"""
        expected = render_tools(req.capability_set)
        defs = [
            ToolDefinition(
                name=t["function"]["name"], description=t["function"]["description"],
                parameters_json_schema=t["function"]["parameters"],
            )
            for t in expected
        ]
        return defs, expected

    def _check_d60(self, req: ModelRequest, defs: list[ToolDefinition], expected: list[dict[str, Any]]) -> None:
        """送出前比對：把「實際交給 SDK 的 ToolDefinition」再經 render_tools 還原成 chat 形狀，與預期整份比對。"""
        sent_cap = {"tools": [
            {"tool": d.name, "description": d.description or "", "input_schema": d.parameters_json_schema} for d in defs
        ]}
        rr = RuntimeRequest(
            run_id="model_port", attempt_id=req.attempt_id, snapshot_ref="", capability_set=req.capability_set,
            cancel_scope={}, deadline=datetime.now(UTC), mode="direct_chat",
        )
        problem = check_tools_match_capability(
            rr, render_tools(sent_cap), expected, builtin_tools=tuple(req.builtin_tools),
        )
        if problem is not None:
            raise CapabilityMissingError(problem)

    # ── history 轉換（Core 持有 history；SDK 不保存）─────────────────────

    @staticmethod
    def _history(messages: list[ChatMessage]) -> list[Any]:
        names: dict[str, str] = {}
        out: list[Any] = []
        for m in messages:
            if m.role == "system":
                out.append(SdkRequest(parts=[SystemPromptPart(m.content)]))
            elif m.role == "user":
                out.append(SdkRequest(parts=[UserPromptPart(m.content)]))
            elif m.role == "assistant":
                parts: list[Any] = [TextPart(m.content)] if m.content else []
                for c in m.tool_calls:
                    names[c["id"]] = c["name"]
                    parts.append(ToolCallPart(c["name"], c.get("args") or {}, c["id"]))
                out.append(SdkResponse(parts=parts))
            elif m.role == "tool":
                out.append(SdkRequest(parts=[ToolReturnPart(
                    names.get(m.tool_call_id or "", ""), m.content, tool_call_id=m.tool_call_id or "",
                )]))
        return out

    def _settings(self, req: ModelRequest, has_tools: bool) -> dict[str, Any]:
        settings: dict[str, Any] = {"max_tokens": req.max_output_tokens}
        effort = req.reasoning_effort
        if self.dialect == "openai_chat" and has_tools and effort not in (None, "none"):
            # 沿用現行做法：gpt-5.6-luna 在 chat completions 不接受 function tools + reasoning_effort
            logger.warning("chat completions 帶 tools 時 reasoning_effort=%r 不被接受，本次改為 none", effort)
            effort = "none"
        if self.dialect in ("openai_chat", "openai_responses") and effort:
            settings["openai_reasoning_effort"] = effort
        if self.dialect == "openai_responses" and self.reasoning_summary:
            settings["openai_reasoning_summary"] = self.reasoning_summary
        if self.thinking is not None:
            settings["thinking"] = self.thinking
        return settings

    # ── 串流 ─────────────────────────────────────────────────────────────

    async def stream(self, req: ModelRequest) -> AsyncIterator[ModelEvent]:
        defs, expected = self._tool_defs(req)
        self._check_d60(req, defs, expected)  # 未過就拋，尚未送出任何請求
        token = CancellationToken()
        self._tokens[req.attempt_id] = token
        self._cancelled.discard(req.attempt_id)
        assembler = ToolCallAssembler()
        router = _PartRouter(self)
        toolset = _GuardedExternalToolset(defs, self.sdk_tool_executions)
        history = self._history(list(req.messages))
        finish: str = "stop"
        called: set[str] = set()  # 模型這次呼叫過的工具名稱（判斷是否為「未知工具」造成的中止）
        known = {d.name for d in defs}
        try:
            async with self._agent.run_stream_events(
                None, message_history=history, toolsets=[toolset],
                model_settings=self._settings(req, bool(defs)), cancellation_token=token,
            ) as events:
                async for ev in events:
                    if req.attempt_id in self._cancelled:
                        finish = "cancelled"
                        break
                    for out in router.route(ev):
                        if out.kind == "tool_call_delta":
                            assembler.feed(out)
                            if out.tool:
                                called.add(out.tool)
                        if req.attempt_id in self._cancelled:
                            finish = "cancelled"
                            break
                        yield out
                    if finish == "cancelled":
                        break
                    if isinstance(ev, AgentRunResultEvent):
                        result = ev.result
                        last = next((m for m in reversed(result.all_messages()) if isinstance(m, SdkResponse)), None)
                        reason = getattr(last, "finish_reason", None)
                        if isinstance(result.output, DeferredToolRequests):
                            finish = "tool_calls"
                        elif reason == "length":
                            finish = "length"
                        u = result.usage
                        yield ModelEvent(kind="usage", usage={
                            "input_tokens": u.input_tokens or 0, "output_tokens": u.output_tokens or 0,
                        })
        except RunCancelled:
            finish = "cancelled"
        except UnexpectedModelBehavior as exc:
            if isinstance(exc.__cause__, ModelRetry) and called - known:
                # 模型呼叫了清單外的工具：SDK 因重試為 0 而中止。呼叫已完整收到，照常輸出 tool_intent，
                # 清單外者由 Gateway 以 capability_missing 拒絕（不會執行，也不多打一次模型）
                logger.warning("模型呼叫了清單外的工具（%d 個），交由 Gateway 拒絕", len(called - known))
                finish = "tool_calls"
            else:
                logger.exception("模型層串流失敗 attempt=%s", req.attempt_id)
                finish = "error"
        except Exception:
            logger.exception("模型層串流失敗 attempt=%s", req.attempt_id)
            finish = "error"
        finally:
            self._tokens.pop(req.attempt_id, None)
        if finish != "cancelled":
            for intent in assembler.intents(req.attempt_id):
                yield intent
        yield ModelEvent(kind="finish", finish_reason=finish)  # type: ignore[arg-type]

    def cancel(self, attempt_id: str) -> None:
        self._cancelled.add(attempt_id)
        token = self._tokens.get(attempt_id)
        if token is not None:
            token.cancel()


class _PartRouter:
    """SDK part 事件 -> ModelEvent。想／說分流**只看 SDK 的 part 歸屬**，不看內容。

    - PartStartEvent 登記 index -> part 種類；PartDeltaEvent 必須命中已登記的 index，且 delta 型別與 part 種類相符。
    - 無法確定歸屬（缺 part_start、index 跳號、型別與歸屬不符、未知 delta）一律 fail-closed：丟棄並記錄，
      絕不當成 text_delta（也不當成 thought_delta）。
    """

    def __init__(self, port: PydanticAIModelPort) -> None:
        self._port = port
        self._kinds: dict[int, str] = {}

    def _drop(self, reason: str) -> list[ModelEvent]:
        logger.warning("丟棄無法確定歸屬的 delta：%s", reason)
        self._port.dropped_deltas.append(reason)
        return []

    def _emit(self, source: str, event: ModelEvent) -> list[ModelEvent]:
        self._port.audit.append((source, event.kind))
        return [event]

    def route(self, ev: Any) -> list[ModelEvent]:
        if isinstance(ev, FunctionToolResultEvent):
            self._port.sdk_tool_executions[0] += 1  # 保險：SDK 若產生工具結果事件也算一次執行
            return []
        if isinstance(ev, PartStartEvent):
            return self._start(ev)
        if isinstance(ev, PartDeltaEvent):
            return self._delta(ev)
        if isinstance(ev, PartEndEvent) and isinstance(ev.part, ToolCallPart):
            return [ModelEvent(kind="tool_call_done", index=ev.index, call_id=ev.part.tool_call_id)]
        return []

    def _start(self, ev: PartStartEvent) -> list[ModelEvent]:
        part = ev.part
        if isinstance(part, ThinkingPart):
            self._kinds[ev.index] = "thinking"
            return self._emit("thinking", ModelEvent(kind="thought_delta", text=part.content)) if part.content else []
        if isinstance(part, TextPart):
            self._kinds[ev.index] = "text"
            return self._emit("text", ModelEvent(kind="text_delta", text=part.content)) if part.content else []
        if isinstance(part, ToolCallPart):
            self._kinds[ev.index] = "tool_call"
            return self._emit("tool_call", ModelEvent(
                kind="tool_call_delta", index=ev.index, call_id=part.tool_call_id, tool=part.tool_name,
                args_fragment=_frag(part.args),
            ))
        self._kinds[ev.index] = "other"  # 其他 part（如 builtin 結果）不對外；其 delta 一律不產生 text／thought
        return []

    def _delta(self, ev: PartDeltaEvent) -> list[ModelEvent]:
        d = ev.delta
        owner = self._kinds.get(ev.index)
        if owner is None:
            return self._drop(f"index={ev.index} 缺 part_start（{type(d).__name__}）")
        if isinstance(d, ThinkingPartDelta):
            if owner != "thinking":
                return self._drop(f"index={ev.index} 歸屬 {owner}，卻收到 ThinkingPartDelta")
            return self._emit("thinking", ModelEvent(kind="thought_delta", text=d.content_delta)) if d.content_delta else []
        if isinstance(d, TextPartDelta):
            if owner != "text":
                return self._drop(f"index={ev.index} 歸屬 {owner}，卻收到 TextPartDelta")
            return self._emit("text", ModelEvent(kind="text_delta", text=d.content_delta)) if d.content_delta else []
        if isinstance(d, ToolCallPartDelta):
            if owner != "tool_call":
                return self._drop(f"index={ev.index} 歸屬 {owner}，卻收到 ToolCallPartDelta")
            return self._emit("tool_call", ModelEvent(
                kind="tool_call_delta", index=ev.index, call_id=d.tool_call_id or "",
                tool=d.tool_name_delta or "", args_fragment=_frag(d.args_delta),
            ))
        return self._drop(f"index={ev.index} 未知的 delta 型別 {type(d).__name__}")


def _frag(args: Any) -> str:
    if args is None:
        return ""
    return args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
