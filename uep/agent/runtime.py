"""``ModelPortRuntime``：``ModelPort`` -> ``Runtime`` 協定（Agent 架構提案 §7.2，切片 C3）。

- 把非同步的 ``ModelPort.stream`` 轉成同步的 ``RuntimeEvent`` 迭代器：整個 attempt 在**呼叫 ``stream`` 的執行緒**
  （worker 執行緒，``AgentCore.run_step`` 所在）內用私有 event loop 驅動，呼叫端不需要 asyncio。
- 工具呼叫完整後（``ModelEvent.tool_intent``，或由 ``ToolCallAssembler`` 累積 ``tool_call_delta`` 產生）轉成
  ``tool_intent``；SDK 的 run 在每個工具呼叫處結束，``submit_tool_result`` 之後以**新的一輪**帶 Core 持有的 history
  加工具結果續跑（D61 B 路徑模擬 A 路徑）。history 由 ``messages_for`` 提供（ContextManager 擁有），Runtime 不保存跨 attempt 的歷史。
- ``thought_delta`` 只寫 log（Q-B），不進 ``RuntimeEvent``。
- 取消：``cancel`` 同時設旗標並呼叫 ``ModelPort.cancel``（冪等）；之後 ``stream`` 只回 ``CANCELLED`` 錯誤並結束。
- D60 啟動斷言未過（``CapabilityMissingError``）：yield 該 ``CAPABILITY_MISSING`` 事件並結束，不送請求。

``ModelPortBackend`` 是同一個 Runtime 的 ``StreamingBackend`` 形狀轉接器：讓宿主既有的對話迴圈（``LLMThinkStage``）
把「一次模型呼叫」交給 ``ModelPort``，工具呼叫以 tool-call piece 回給它，由它的 executor（ActionIntent 橋）執行。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from uep.adapters.runtime import (
    BUDGET_HINT_CONVERGE,
    CancelAck,
    RuntimeEvent,
    RuntimeRequest,
    RuntimeStatus,
)
from uep.ids import new_id
from uep.models.port import (
    CapabilityMissingError,
    ChatMessage,
    ModelEvent,
    ModelPort,
    ModelRequest,
    ToolCallAssembler,
)

logger = logging.getLogger(__name__)

CONVERGE_NOTE = "預算已接近上限：請盡快收束，優先用已取得的資訊回覆，避免不必要的工具呼叫。"
BACKEND_CLOSE_WAIT_S = 5.0  # 消費者取消後，等在途的 next() 收尾的上限
MessagesFor = Callable[[RuntimeRequest], Sequence[ChatMessage]]


@dataclass
class _Attempt:
    request: RuntimeRequest
    messages: list[ChatMessage]
    cancelled: bool = False
    state: str = "running"
    seq: int = 0
    pending_calls: list[str] = field(default_factory=list)  # 已送出 tool_intent、尚未收到結果的 call_id（依序）
    results: dict[str, str] = field(default_factory=dict)  # call_id -> 回給模型的文字
    usage: dict[str, int] = field(default_factory=dict)


def _result_text(result: Mapping[str, Any]) -> str:
    """D61 視圖 -> 回給模型的工具結果文字：ok 取各 item 的 content；其餘只給狀態短碼（不編造內容）。"""
    status = str(result.get("status", "unknown"))
    if status == "ok":
        parts = [str(item["content"]) for item in result.get("items", ()) if isinstance(item, Mapping) and "content" in item]
        return "\n".join(parts) if parts else "(no result)"
    reason = result.get("reason_code")
    return f"tool {status}" + (f": {reason}" if isinstance(reason, str) else "")


result_text = _result_text  # 供 AgentLoop 以同一份規則組 child history（與 Runtime 內部 history 逐則相等）


class ModelPortRuntime:
    def __init__(
        self, port: ModelPort, messages_for: MessagesFor, *, max_output_tokens: int = 256,
        reasoning_effort: str | None = None, max_rounds: int = 4, continue_after_tools: bool = True,
    ) -> None:
        self._port = port
        self._messages_for = messages_for
        self._max_output_tokens = max_output_tokens
        self._reasoning_effort = reasoning_effort
        self._max_rounds = max_rounds
        self._continue = continue_after_tools
        self._attempts: dict[str, _Attempt] = {}
        self._lock = threading.Lock()

    # ---- Runtime 協定 ----
    def start(self, request: RuntimeRequest) -> str:
        attempt = _Attempt(request=request, messages=list(self._messages_for(request)))
        if request.budget_hint == BUDGET_HINT_CONVERGE:
            attempt.messages.append(ChatMessage("system", CONVERGE_NOTE))  # 只影響這個 attempt 的請求，不進 Core 的 history
        with self._lock:
            self._attempts[request.attempt_id] = attempt
        return request.attempt_id

    def resume(self, request: RuntimeRequest) -> str:
        raise NotImplementedError("checkpoint resume 屬切片 D／E；本 Runtime 只支援 start")

    def cancel(self, attempt_id: str, reason: str) -> CancelAck:
        attempt = self._attempts.get(attempt_id)
        if attempt is not None:
            attempt.cancelled = True
            attempt.state = "cancelled"
        self._port.cancel(attempt_id)  # 冪等；已結束的 attempt 也安全
        logger.debug("ModelPortRuntime 取消 attempt=%s reason=%s", attempt_id, reason)
        return "stopped"

    def end(self, attempt_id: str) -> None:
        """釋放 attempt 的記憶體狀態（呼叫端確定不再 stream／submit 之後）。"""
        with self._lock:
            self._attempts.pop(attempt_id, None)

    def status(self, attempt_id: str) -> RuntimeStatus:
        attempt = self._attempts.get(attempt_id)
        if attempt is None:
            return RuntimeStatus(attempt_id, "unknown")
        return RuntimeStatus(attempt_id, attempt.state, attempt.seq, {"usage": dict(attempt.usage)})  # type: ignore[arg-type]

    def submit_tool_result(self, attempt_id: str, action_id: str, result: Mapping[str, Any]) -> None:
        attempt = self._attempts.get(attempt_id)
        if attempt is None or not attempt.pending_calls:
            logger.warning("忽略沒有對應呼叫的工具結果：attempt=%s action=%s", attempt_id, action_id)
            return
        call_id = attempt.pending_calls.pop(0)  # AgentCore 依 intent 順序交付
        attempt.results[call_id] = _result_text(result)

    def stream(self, attempt_id: str) -> Iterator[RuntimeEvent]:
        attempt = self._attempts.get(attempt_id)
        if attempt is None:
            yield self._event(_Attempt(self._dummy_request(attempt_id), []), "error", error={
                "code": "CANCELLED", "retryable": False, "detail": "未知的 attempt"})
            return
        loop = asyncio.new_event_loop()
        try:
            yield from self._run(attempt, loop)
        finally:
            loop.close()

    # ---- 內部 ----
    @staticmethod
    def _dummy_request(attempt_id: str) -> RuntimeRequest:
        return RuntimeRequest(
            run_id="", attempt_id=attempt_id, snapshot_ref="", capability_set={}, cancel_scope={},
            deadline=datetime.now(UTC) + timedelta(seconds=1), mode="direct_chat",
        )

    @staticmethod
    def _event(attempt: _Attempt, kind: str, **fields: Any) -> RuntimeEvent:
        attempt.seq += 1
        return RuntimeEvent(attempt_id=attempt.request.attempt_id, seq=attempt.seq, kind=kind, **fields)  # type: ignore[arg-type]

    def _run(self, attempt: _Attempt, loop: asyncio.AbstractEventLoop) -> Iterator[RuntimeEvent]:
        attempt_id = attempt.request.attempt_id
        for _round in range(self._max_rounds):
            if attempt.cancelled:
                yield self._cancelled(attempt)
                return
            request = ModelRequest(
                attempt_id=attempt_id, messages=tuple(attempt.messages),
                capability_set=attempt.request.capability_set, max_output_tokens=self._max_output_tokens,
                reasoning_effort=self._reasoning_effort,
            )
            assembler = ToolCallAssembler()
            intents: list[ModelEvent] = []
            texts: list[str] = []
            finish: str | None = None
            agen = self._port.stream(request)
            try:
                while True:
                    try:
                        ev = loop.run_until_complete(agen.__anext__())
                    except StopAsyncIteration:
                        break
                    except CapabilityMissingError as exc:
                        attempt.state = "failed"
                        yield exc.event
                        return
                    if attempt.cancelled:
                        break
                    if ev.kind == "thought_delta":
                        logger.debug("thought_delta attempt=%s len=%d", attempt_id, len(ev.text))  # Q-B：只寫 log，不寫內容
                    elif ev.kind == "text_delta":
                        texts.append(ev.text)
                        yield self._event(attempt, "text_delta", text_delta=ev.text)
                    elif ev.kind == "tool_call_delta":
                        assembler.feed(ev)
                    elif ev.kind == "tool_intent":
                        intents.append(ev)
                    elif ev.kind == "usage" and ev.usage:
                        for key, value in ev.usage.items():
                            attempt.usage[key] = attempt.usage.get(key, 0) + int(value)
                    elif ev.kind == "finish":
                        finish = ev.finish_reason
                        break
            finally:
                self._close(loop, agen)
            if attempt.cancelled or finish == "cancelled":
                attempt.cancelled = True
                yield self._cancelled(attempt)
                return
            if finish == "error":
                attempt.state = "failed"
                yield self._event(attempt, "error", error={
                    "code": "PROVIDER_UNAVAILABLE", "retryable": True, "detail": "模型層串流失敗"})
                return
            if not intents:
                intents = assembler.intents(attempt_id)
            if not intents:
                attempt.state = "completed"
                yield self._event(attempt, "result", result={"status": "ok", "usage": dict(attempt.usage)})
                return
            calls = tuple({"id": i.call_id, "name": i.tool, "args": dict(i.args or {})} for i in intents)
            attempt.messages.append(ChatMessage("assistant", "".join(texts), tool_calls=calls))
            for i in intents:
                attempt.pending_calls.append(i.call_id)
                yield self._event(attempt, "tool_intent", tool_intent={
                    "tool": i.tool, "args": dict(i.args or {}), "args_complete": bool(i.args_complete),
                    "call_id": i.call_id, "index": i.index, "side_effect_hint": None,
                    "batch_size": len(intents),  # 同一則模型訊息內的呼叫數；AgentLoop 據此收齊整批再排程（§6.4-1）
                })
            if not self._continue:
                attempt.state = "completed"
                return
            for i in intents:  # 每個 call 都要有 tool 訊息（缺一個 provider 會拒絕整段）
                text = attempt.results.pop(i.call_id, None)
                if text is None:
                    text = "tool invalid_tool_args" if not i.args_complete else "tool unknown"
                    if i.call_id in attempt.pending_calls:
                        attempt.pending_calls.remove(i.call_id)
                attempt.messages.append(ChatMessage("tool", text, tool_call_id=i.call_id))
        attempt.state = "completed"
        yield self._event(attempt, "result", result={"status": "max_rounds", "usage": dict(attempt.usage)})

    @staticmethod
    def _close(loop: asyncio.AbstractEventLoop, agen: Any) -> None:
        """關閉模型串流；若目前執行緒已有執行中的 loop（stream 被在 loop 執行緒上回收），無法同步收尾，交給 GC。"""
        try:
            loop.run_until_complete(agen.aclose())
        except RuntimeError:
            logger.debug("模型串流在執行中的 loop 內回收，略過同步 aclose")

    def _cancelled(self, attempt: _Attempt) -> RuntimeEvent:
        attempt.state = "cancelled"
        return self._event(attempt, "error", error={"code": "CANCELLED", "retryable": False, "detail": "已取消"})


# ---- StreamingBackend 轉接器 ----

def _close_iterator(iterator: Any) -> None:
    """在 worker 執行緒關閉同步串流（讓它的 finally 在沒有 loop 的執行緒收尾）；仍在執行中的就交給取消旗標。"""
    try:
        iterator.close()
    except ValueError:
        pass

@dataclass(frozen=True)
class ToolCallPiece:
    """``LLMThinkStage`` 認得的工具呼叫事件形狀（``id``／``name``／``arguments``）。"""

    id: str
    name: str
    arguments: str


def _chat_messages(messages: Sequence[Any], system_instruction: str | None) -> list[ChatMessage]:
    out: list[ChatMessage] = []
    if system_instruction:
        out.append(ChatMessage("system", system_instruction))
    for m in messages:
        get = m.get if isinstance(m, Mapping) else (lambda k, d=None, m=m: getattr(m, k, d))
        role = str(get("role", "user"))
        calls_raw = get("tool_calls", None)
        if not calls_raw:
            meta = get("metadata", None)
            calls_raw = meta.get("tool_calls") if isinstance(meta, Mapping) else None
        calls: list[dict[str, Any]] = []
        for c in calls_raw or ():
            fn = c.get("function", c)
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            calls.append({"id": c.get("id", ""), "name": fn.get("name", ""), "args": args if isinstance(args, dict) else {}})
        out.append(ChatMessage(
            role if role in ("system", "user", "assistant", "tool") else "user",  # type: ignore[arg-type]
            str(get("content", "") or ""), tool_calls=tuple(calls), tool_call_id=get("tool_call_id", None),
        ))
    return out


def _capability_from_rendered(tools: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """宿主已由註冊表 ``project`` + ``render_tools`` 得到的 tools -> 還原成 ``capability_set`` 形狀。
    ``ModelPort`` 的 D60 斷言會再把它 render 一次整份比對，所以不可能多給或改內容。"""
    return {"tools": [
        {"tool": t["function"]["name"], "description": t["function"]["description"],
         "input_schema": t["function"]["parameters"]} for t in tools
    ]}


class ModelPortBackend:
    """``StreamingBackend``：一次 ``stream_query`` = 一個 attempt（一輪模型呼叫）。"""

    def __init__(self, port: ModelPort, **runtime_kwargs: Any) -> None:
        self._snapshots: dict[str, list[ChatMessage]] = {}
        self.runtime = ModelPortRuntime(
            port, lambda req: self._snapshots.pop(req.snapshot_ref, []), continue_after_tools=False, **runtime_kwargs,
        )

    async def stream_query(
        self, messages: Sequence[Any], system_instruction: str | None = None, **kwargs: Any
    ) -> AsyncIterator[Any]:
        attempt_id = new_id("att")
        ref = f"snap_{attempt_id}"
        self._snapshots[ref] = _chat_messages(messages, system_instruction)
        tools = kwargs.get("tools") or []
        request = RuntimeRequest(
            run_id="", attempt_id=attempt_id, snapshot_ref=ref, capability_set=_capability_from_rendered(tools),
            cancel_scope={}, deadline=datetime.now(UTC) + timedelta(minutes=5), mode="direct_chat",
        )
        self.runtime.start(request)
        iterator = iter(self.runtime.stream(attempt_id))
        end = object()
        loop = asyncio.get_running_loop()
        pending: asyncio.Future[Any] | None = None  # 目前在 worker 執行緒上進行中的 next()
        try:
            while True:
                pending = loop.run_in_executor(None, next, iterator, end)
                event = await asyncio.shield(pending)
                pending = None
                if event is end:
                    return
                if event.kind == "text_delta":
                    yield event.text_delta
                elif event.kind == "tool_intent":
                    intent = event.tool_intent or {}
                    if intent.get("args_complete"):
                        yield ToolCallPiece(
                            str(intent.get("call_id") or f"{attempt_id}:{intent.get('index', 0)}"),
                            str(intent["tool"]), json.dumps(intent.get("args") or {}, ensure_ascii=False),
                        )
                elif event.kind == "error":
                    logger.warning("ModelPortBackend 結束於錯誤：%s", (event.error or {}).get("code"))
                    return
        finally:
            # 消費者取消（插話、停止）或正常結束都走這裡；cancel 冪等，會一路傳到 ModelPort.cancel
            if self.runtime.status(attempt_id).state == "running":
                self.runtime.cancel(attempt_id, "backend_closed")
            self.runtime.end(attempt_id)
            if pending is not None and not pending.done():
                # next() 還在 worker 執行緒上跑（消費者在等待中被取消）：cancel 已讓模型串流收尾，等它回來再 close，
                # 不與仍在執行的 generator 撞在一起；等不到就不 close（交給 GC，stream 內的 loop 清理有防護）
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait({pending}, timeout=BACKEND_CLOSE_WAIT_S)
            if pending is None or pending.done():
                await asyncio.shield(loop.run_in_executor(None, _close_iterator, iterator))
