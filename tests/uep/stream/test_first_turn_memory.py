"""首輪記憶預填、預熱用量觀測與 prompt_cache_key 的測試（假 backend，不打真 API）。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from uep.stream.adapters.think_llm import LLMThinkStage


class _Backend:
    """沒 tools 參數時只吐文字；絕不主動回工具呼叫（避免無限往返）。"""

    def __init__(self, api: str | None = None, usage: dict[str, int] | None = None) -> None:
        self.api = api
        self.requests: list[dict[str, Any]] = []
        self._usage = usage if usage is not None else {}

    async def stream_query(
        self, messages: Sequence[Any], system_instruction: str | None = None, **kwargs
    ) -> AsyncIterator[str]:
        self.requests.append({"system_instruction": system_instruction, **kwargs})
        yield "好"

    def get_last_stream_usage(self) -> dict[str, int]:
        return dict(self._usage)


def test_seed_首輪注入並被_pop():
    stage = LLMThinkStage(_Backend(), prompt_cache_key="")
    assert stage.seed_pending(None, "艾斯維爾喜歡咖啡") is True
    msgs = stage._build_messages("你好", None, "t1")
    assert "艾斯維爾喜歡咖啡" in msgs[-1]["content"]
    assert stage.last_injected_memory == "艾斯維爾喜歡咖啡"
    assert stage._pending_by_scope == {}, "注入後槽被 pop"
    msgs2 = stage._build_messages("再問", None, "t2")
    assert msgs2[-1]["content"] == "再問"


def test_seed_槽已有值不覆蓋():
    stage = LLMThinkStage(_Backend(), prompt_cache_key="")
    stage._pending_by_scope[None] = "真實檢索"
    assert stage.seed_pending(None, "預取") is False
    assert stage._pending_by_scope[None] == "真實檢索"
    assert stage.seed_pending(None, "  ") is False


def test_seed_有_resolver_時依_scope_分槽():
    stage = LLMThinkStage(_Backend(), memory_resolver=lambda s: object(), prompt_cache_key="")
    assert stage.seed_pending("u1", "甲的記憶") is True
    assert stage.seed_pending("u2", "乙的記憶") is True
    assert stage.seed_pending("u1", "覆蓋?") is False
    assert "甲的記憶" in stage._build_messages("hi", "u1", "t1")[-1]["content"]
    assert stage._build_messages("hi", "u1", "t2")[-1]["content"] == "hi"
    assert "乙的記憶" in stage._build_messages("hi", "u2", "t3")[-1]["content"]


async def test_prewarm_記下_usage_且不自帶_reasoning_參數():
    usage = {"input_tokens": 5000, "cached_tokens": 4096, "output_tokens": 2}
    for api in ("responses", "chat"):
        b = _Backend(api=api, usage=usage)
        stage = LLMThinkStage(b, system_prompt="人設", prompt_cache_key="")
        assert stage.last_prewarm_usage is None
        assert await stage.prewarm() is True
        assert stage.last_prewarm_usage == usage
        # effort 屬 prompt cache key：預熱不得覆寫，交給 backend 與正常輪同一套邏輯
        assert "reasoning" not in b.requests[0]
        assert "reasoning_effort" not in b.requests[0]


async def test_prewarm_沒回_usage_時為_None():
    stage = LLMThinkStage(_Backend(api="responses"), system_prompt="人設", prompt_cache_key="")
    assert await stage.prewarm() is True
    assert stage.last_prewarm_usage is None


async def test_prompt_cache_key_正常輪與預熱都帶():
    b = _Backend()
    stage = LLMThinkStage(b, system_prompt="人設", prompt_cache_key="bot-voice")
    await stage.prewarm()
    assert b.requests[0]["prompt_cache_key"] == "bot-voice"

    from uep.stream.contracts.cancellation import CancellationToken
    from uep.stream.contracts.types import Utterance

    async for _ in stage.stream(Utterance(text="嗨"), CancellationToken()):
        pass
    assert b.requests[1]["prompt_cache_key"] == "bot-voice"


async def test_prompt_cache_key_空值不帶_且可由_env_讀取(monkeypatch):
    b = _Backend()
    stage = LLMThinkStage(b, system_prompt="人設", prompt_cache_key="")
    await stage.prewarm()
    assert "prompt_cache_key" not in b.requests[0]

    monkeypatch.setenv("ECHO_STREAM_LLM_PROMPT_CACHE_KEY", " from-env ")
    assert LLMThinkStage(_Backend()).prompt_cache_key == "from-env"
    monkeypatch.setenv("ECHO_STREAM_LLM_PROMPT_CACHE_KEY", "")
    assert LLMThinkStage(_Backend()).prompt_cache_key is None
