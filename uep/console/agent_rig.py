"""Console 的 agent 工具路徑（``UEP_TOOL_PATH=agent``）組裝零件：真實 ``ModelPort``、AgentThinkStage 工廠、
fake 記憶埠，以及 ``/loop`` 一次性把下一句導向 ``route=agent_loop`` 的開關。

只在 ``tool_path=agent`` 時由 ``rig.build_runtime`` 使用；legacy 與 fake 組裝不載入本模組。
"""
from __future__ import annotations

import dataclasses
import threading
from collections.abc import Callable, Mapping
from typing import Any

from uep.memory.protocol import RetrieveResult, StoreResult
from uep.stream.tracer import LatencyTracer
from uep.turn.models import ReplyMode

LOOP_ROUTE = "agent_loop"
_DIALECTS = {"chat": "openai_chat", "responses": "openai_responses"}


def build_model_port(config: Any) -> Any:
    """以 ``ThinkConfig`` 建 PydanticAI 模型埠（openai 後端；``api`` chat／responses 對映 dialect；ollama 照 provider）。

    缺金鑰拋 ``ThinkConfigError``（呼叫端轉成 ``ConsoleError``）。不送任何請求。
    """
    from uep.models.pydantic_ai_port import PydanticAIModelPort

    config.require_credentials()
    if config.provider == "ollama":
        return PydanticAIModelPort("ollama", config.model, base_url=config.base_url)
    dialect = _DIALECTS.get(config.api, "openai_chat")
    return PydanticAIModelPort(
        dialect, config.model, api_key=config.api_key, project=config.project,  # type: ignore[arg-type]
        organization=config.organization, base_url=config.base_url,
    )


def agent_think_factory(model_port: Any, settings: Any, config: Any) -> Callable[[LatencyTracer], Any]:
    """``AgentThinkStage(LLMThinkStage(ModelPortBackend(model_port)))`` 的工廠；其餘參數與 ``real_think_factory`` 一致。"""
    from uep.agent.runtime import ModelPortBackend
    from uep.stream.adapters.think_agent import AgentThinkStage
    from uep.stream.adapters.think_compress import compression_kwargs
    from uep.stream.adapters.think_llm import LLMThinkStage

    backend = ModelPortBackend(model_port)
    compress = compression_kwargs(settings.compress_config(), config)

    def factory(tracer: LatencyTracer) -> Any:
        return AgentThinkStage(LLMThinkStage(
            backend, system_prompt=settings.infer_system_prompt, tracer=tracer, config=config, **compress,
        ))

    return factory


class ConsoleFakeMemory:
    """agent 路徑在 ``--memory fake`` 下的 ``MemoryPort``：行程內記憶，只供驗工具鏈（不做語意檢索）。

    ``force_remember`` 存字串，``deep_recall`` 以關鍵字子字串比對；其餘工具回 failed。
    """

    def __init__(self) -> None:
        self.facts: list[str] = []

    def retrieve(self, scope: str, query: str, audience: str, **kwargs: Any) -> RetrieveResult:
        return RetrieveResult(text="")

    def store(self, *, actor_id: str | None, user_text: str, spoken_text: str, session_id: str | None = None,
              idempotency_key: str | None = None, **metadata: Any) -> StoreResult:
        return StoreResult(status="stored")

    def profile(self, *, actor_id: str | None, audience: str, max_chars: int = 800) -> str:
        return ""

    def set_session(self, session_id: str | None) -> None:
        return None

    def tool_definitions(self) -> list[dict[str, Any]]:
        return []

    def execute_tool(self, name: str, args: Mapping[str, Any], *, actor_id: str | None, audience: str,
                     session_id: str | None = None, context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if name == "force_remember":
            self.facts.append(str(args.get("content", "")))
            return {"success": True, "result": "已記住（fake 記憶，僅本次執行有效）。"}
        if name == "deep_recall":
            query = str(args.get("query", ""))
            hits = [f for f in self.facts if query and (query in f or f in query)]
            return {"success": True, "result": "\n".join(hits) if hits else "（fake 記憶沒有相關資料）"}
        return {"success": False, "result": "fake 記憶不支援此工具", "code": "failed"}


class LoopRouteSwitch:
    """``/loop`` 的一次性開關：arm 之後，下一個 ``reply_mode=respond`` 的提議改走 ``route=agent_loop``。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._armed = False

    def arm(self) -> None:
        with self._lock:
            self._armed = True

    def disarm(self) -> None:
        with self._lock:
            self._armed = False

    def take(self) -> bool:
        with self._lock:
            armed, self._armed = self._armed, False
            return armed


def install_loop_route(host: Any, switch: LoopRouteSwitch) -> None:
    """包住 primary proposer 的 ``propose``（同 ``rig._record_submissions`` 的做法）：armed 且提議為 respond 時改 route。"""
    proposer = host.orchestrator.proposer
    original = proposer.propose

    def propose(candidate: Any) -> Any:
        proposal = original(candidate)
        if proposal.reply_mode is ReplyMode.RESPOND and switch.take():
            return dataclasses.replace(proposal, route=LOOP_ROUTE)
        return proposal

    proposer.propose = propose
