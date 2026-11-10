"""LLMThinkStage 長對話壓縮（ContextManager 四層）的測試。

用假的串流 backend 與假的壓縮 backend——不打真 API。ContextManager 與其壓縮管線是真的，
所以 60／80／92% 的分層、冷卻與反抖動都照真實邏輯跑。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from uep.stream.adapters.think_compress import CompressConfig, is_context_overflow
from uep.stream.adapters.think_llm import LLMThinkStage
from uep.stream.contracts.cancellation import CancellationToken
from uep.stream.contracts.types import Utterance

PERSONA = "你是諾瓦，SACRED-PERSONA-XYZ：永遠用繁體中文、語氣直接。"
FIRST_FACT = "我家的貓叫小墨"


class StreamBackend:
    """回固定文字；可在前 N 次呼叫丟出指定例外（模擬 413）。"""

    def __init__(self, fail_times: int = 0, error: Exception | None = None) -> None:
        self.fail_times = fail_times
        self.error = error
        self.calls: list[list[Any]] = []
        self.systems: list[str | None] = []

    async def stream_query(
        self, messages: Sequence[Any], system_instruction: str | None = None, **kwargs
    ) -> AsyncIterator[str]:
        self.calls.append(list(messages))
        self.systems.append(system_instruction)
        if len(self.calls) <= self.fail_times:
            raise self.error  # type: ignore[misc]
        for ch in "好的，我知道了。":
            yield ch


class CompressBackend:
    """假的壓縮 backend：摘要 = 各則訊息前 40 字串起來（第 1 輪的事實因此留在摘要裡）。"""

    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self.gate = gate
        self.summarize_calls = 0
        self.compact_calls = 0
        self.compact_sacred: list[Any] = []
        self.started = asyncio.Event()

    async def _wait(self) -> None:
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()

    @staticmethod
    def _digest(messages: Sequence[Any]) -> str:
        return "；".join(f"{m.role}:{m.content[:40]}" for m in messages)

    async def summarize(self, messages: Sequence[Any], instructions: str | None = None) -> str:
        self.summarize_calls += 1
        await self._wait()
        return self._digest(messages)

    async def compact(self, messages: Sequence[Any], sacred: Any, instructions: str | None = None) -> str:
        self.compact_calls += 1
        self.compact_sacred.append(sacred)
        await self._wait()
        return self._digest(messages)


class RecordingList(list):
    """記錄切片賦值（壓縮的原子替換）次數。"""

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.slice_assignments = 0

    def __setitem__(self, key: Any, value: Any) -> None:
        if isinstance(key, slice):
            self.slice_assignments += 1
        super().__setitem__(key, value)


def seed_history(count: int = 12) -> list[dict[str, str]]:
    """count 則 user/assistant 交替的歷史；第 0 則帶 FIRST_FACT。"""
    items = []
    for i in range(count):
        role = "user" if i % 2 == 0 else "assistant"
        body = FIRST_FACT if i == 0 else f"第{i}則的閒聊內容，聊到天氣與午餐"
        items.append({"role": role, "content": f"{body}。" + "補充說明。" * 6})
    return items


def make_stage(
    *,
    ratio: float,
    backend: StreamBackend | None = None,
    compress_backend: CompressBackend | None = None,
    history: list[dict[str, str]] | None = None,
    **cfg: Any,
) -> LLMThinkStage:
    """建一個壓縮用量剛好約 ``ratio``（相對 context_window）的 stage。"""
    cfg.setdefault("cooldown_seconds", 0.0)
    stage = LLMThinkStage(
        backend or StreamBackend(),
        system_prompt=PERSONA,
        compress=CompressConfig(context_window=1_000, **cfg),
        compress_backend=compress_backend or CompressBackend(),
    )
    items = history if history is not None else seed_history()
    stage.history = RecordingList(items)
    comp = stage._compressor
    assert comp is not None
    manager = comp._load(items)  # 量出實際 token 數（含 system）
    window = int(manager.budget.total_tokens / ratio)
    comp.config = CompressConfig(context_window=window, **cfg)
    comp._manager.config.context_window_size = window
    comp._manager.budget.context_window = window
    comp._manager._engine.budget.context_window = window
    return stage


async def run_turn(stage: LLMThinkStage, text: str, turn_id: str) -> list[str]:
    sentences = [s.text async for s in stage.stream(Utterance(text=text, turn_id=turn_id), CancellationToken())]
    await stage.commit(turn_id, "".join(sentences), "".join(sentences))
    return sentences


# --- 分層 ---


async def test_below_60_percent_does_nothing() -> None:
    cb = CompressBackend()
    stage = make_stage(ratio=0.5, compress_backend=cb)
    before = list(stage.history)
    await run_turn(stage, "你好", "t1")
    await stage.wait_compress()
    assert cb.summarize_calls == cb.compact_calls == 0
    assert list(stage.history)[: len(before)] == before


async def test_tier2_summarizes_oldest_and_keeps_first_turn_fact() -> None:
    cb = CompressBackend()
    stage = make_stage(ratio=0.85, compress_backend=cb)
    original = list(stage.history)
    await run_turn(stage, "繼續聊", "t1")
    await stage.wait_compress()

    assert cb.summarize_calls == 1 and cb.compact_calls == 0
    first = stage.history[0]
    assert first["role"] == "system" and FIRST_FACT in first["content"]
    # 最舊 10 則被摘要掉，其餘原物件（身分不變）仍在
    assert all(any(h is o for h in stage.history) for o in original[10:])
    assert not any(h is original[0] for h in stage.history)


async def test_tier3_full_compaction_keeps_recent_tail_verbatim() -> None:
    cb = CompressBackend()
    stage = make_stage(ratio=0.95, compress_backend=cb, keep_recent=4)
    original = list(stage.history)
    await run_turn(stage, "繼續聊", "t1")
    await stage.wait_compress()

    assert cb.compact_calls == 1
    assert FIRST_FACT in stage.history[0]["content"]
    committed = stage.history[-2:]  # 剛 commit 的 user / assistant
    assert [h["role"] for h in committed] == ["user", "assistant"]
    # 最近的原文在（來自快照的尾端，加上壓縮期間新增的本輪）
    assert any(h is original[-1] for h in stage.history)
    assert len(stage.history) < len(original)


async def test_first_turn_survives_more_than_20_turns() -> None:
    """跨 20 輪（>history_limit）後第 1 輪的資訊仍在送給 LLM 的 messages 裡。"""
    cb = CompressBackend()
    backend = StreamBackend()
    stage = LLMThinkStage(
        backend,
        system_prompt=PERSONA,
        compress=CompressConfig(context_window=1, cooldown_seconds=0.0),
        compress_backend=cb,
    )
    comp = stage._compressor
    assert comp is not None
    padding = "今天的閒聊內容，聊到天氣與午餐。" * 4
    await run_turn(stage, f"{FIRST_FACT}。{padding}", "t0")
    # 用量預算設為「約 18 則訊息裝滿」，讓壓縮在 20 輪的過程中自然觸發
    window = int(comp._load(stage.history).history.total_tokens * 9)
    comp.config = CompressConfig(context_window=window, cooldown_seconds=0.0)
    comp._manager._engine.budget.context_window = window
    comp._manager.config.context_window_size = window
    for n in range(1, 26):
        await run_turn(stage, f"第{n}輪。{padding}", f"t{n}")
        await stage.wait_compress()

    assert cb.summarize_calls + cb.compact_calls >= 1
    sent = "\n".join(m.content for m in backend.calls[-1])
    assert FIRST_FACT in sent
    assert len(stage.history) < 2 * 26  # 歷史確實被壓過，沒有無限增長
    assert backend.systems[-1] is not None and "SACRED-PERSONA-XYZ" in backend.systems[-1]


# --- 背景執行、原子替換 ---


async def test_compression_runs_in_background_and_swaps_atomically() -> None:
    gate = asyncio.Event()
    cb = CompressBackend(gate=gate)
    stage = make_stage(ratio=0.85, compress_backend=cb)
    history = stage.history
    assert isinstance(history, RecordingList)
    original = list(history)

    await run_turn(stage, "繼續聊", "t1")  # commit 排了背景壓縮，回合本身不被擋
    await asyncio.wait_for(cb.started.wait(), 5)
    assert stage._compress_task is not None and not stage._compress_task.done()
    assert history.slice_assignments == 0  # 壓縮還沒回來：歷史原封不動
    assert list(history[: len(original)]) == original

    await run_turn(stage, "壓縮中的新一輪", "t2")  # 壓縮期間回合照常進行、照常 commit
    during = history[-2:]
    assert history.slice_assignments == 0

    gate.set()
    await stage.wait_compress()
    assert stage.history is history
    assert history.slice_assignments == 1  # 單一切片賦值 = 原子替換
    assert history[0]["role"] == "system"
    # 壓縮期間新增的項目接在後面、身分不變
    assert history[-1] is during[-1] and history[-2] is during[-2]


async def test_items_retracted_during_compression_are_dropped() -> None:
    gate = asyncio.Event()
    cb = CompressBackend(gate=gate)
    stage = make_stage(ratio=0.85, compress_backend=cb)
    await run_turn(stage, "繼續聊", "t1")
    await asyncio.wait_for(cb.started.wait(), 5)
    assert await stage.retract("t1")  # 壓縮進行中撤回 t1
    gate.set()
    await stage.wait_compress()
    assert not any(h["content"] == "繼續聊" for h in stage.history)


async def test_result_discarded_when_context_reset_during_compression() -> None:
    gate = asyncio.Event()
    cb = CompressBackend(gate=gate)
    stage = make_stage(ratio=0.85, compress_backend=cb)
    await run_turn(stage, "繼續聊", "t1")
    await asyncio.wait_for(cb.started.wait(), 5)
    stage.reset_context([{"role": "user", "content": "新聽眾"}])
    gate.set()
    await stage.wait_compress()
    assert stage.history == [{"role": "user", "content": "新聽眾"}]


async def test_failed_compression_keeps_history_and_backs_off() -> None:
    class Boom(CompressBackend):
        async def summarize(self, messages, instructions=None):  # type: ignore[override]
            self.summarize_calls += 1
            raise RuntimeError("api down")

    cb = Boom()
    stage = make_stage(ratio=0.85, compress_backend=cb, failure_backoff_seconds=3600.0)
    await run_turn(stage, "繼續聊", "t1")
    await stage.wait_compress()
    assert all(h["role"] != "system" for h in stage.history)  # 沒有摘要，原歷史還在
    assert any(FIRST_FACT in h["content"] for h in stage.history)
    calls = cb.summarize_calls
    await run_turn(stage, "再聊", "t2")
    await stage.wait_compress()
    assert cb.summarize_calls == calls  # 退避期間不重打


# --- Sacred 人設 ---


async def test_sacred_persona_never_enters_or_leaves_history() -> None:
    cb = CompressBackend()
    backend = StreamBackend()
    stage = make_stage(ratio=0.95, backend=backend, compress_backend=cb)
    await run_turn(stage, "繼續聊", "t1")
    await stage.wait_compress()

    assert cb.compact_calls == 1
    # 第三層摘要提示逐字帶著人設
    assert "SACRED-PERSONA-XYZ" in cb.compact_sacred[0].total_content
    # 人設仍完整在 system，沒被壓縮；歷史裡也沒有被改寫過的人設副本
    assert PERSONA in (stage._system_instruction() or "")
    await run_turn(stage, "壓縮後的一輪", "t2")
    assert PERSONA in (backend.systems[-1] or "")
    assert not any("SACRED-PERSONA-XYZ" in h["content"] for h in stage.history if h["role"] != "system")


# --- 第四層：413 ---


def _overflow_error() -> Exception:
    class APIStatusError(Exception):
        status_code = 413

    return APIStatusError("Request too large")


async def test_413_triggers_emergency_truncation_and_retries_once() -> None:
    backend = StreamBackend(fail_times=1, error=_overflow_error())
    stage = make_stage(ratio=0.3, backend=backend, keep_recent=4)  # 用量不高：只靠 413 路徑
    history = stage.history
    original_len = len(history)

    sentences = await run_turn(stage, "最新的問題", "t1")

    assert "".join(sentences) == "好的，我知道了。"
    assert len(backend.calls) == 2
    first, second = backend.calls
    assert len(first) == original_len + 1
    assert len(second) <= 4 + 1 and len(second) < len(first)  # 只留最近幾則 + 本輪使用者訊息
    assert second[-1].content == "最新的問題"  # 本輪使用者訊息保留
    assert FIRST_FACT not in "".join(m.content for m in second)
    # 人設不受影響：兩次請求都帶完整 system，歷史也沒被塞入緊急還原的人設副本
    assert all(PERSONA in (s or "") for s in backend.systems)
    assert not any("Emergency Recovery" in h["content"] for h in stage.history)
    assert stage.history[0]["role"] == "user"  # 不以孤兒 assistant 開頭


async def test_413_is_retried_only_once() -> None:
    backend = StreamBackend(fail_times=5, error=_overflow_error())
    stage = make_stage(ratio=0.3, backend=backend)
    with pytest.raises(Exception, match="Request too large"):
        await run_turn(stage, "最新的問題", "t1")
    assert len(backend.calls) == 2


async def test_non_overflow_error_is_not_swallowed() -> None:
    backend = StreamBackend(fail_times=1, error=RuntimeError("boom"))
    stage = make_stage(ratio=0.3, backend=backend)
    with pytest.raises(RuntimeError, match="boom"):
        await run_turn(stage, "你好", "t1")
    assert len(backend.calls) == 1


async def test_413_without_compression_just_raises() -> None:
    backend = StreamBackend(fail_times=1, error=_overflow_error())
    stage = LLMThinkStage(backend, system_prompt=PERSONA)  # 未啟用壓縮：行為與以往相同
    stage.history[:] = seed_history()
    with pytest.raises(Exception, match="Request too large"):
        await run_turn(stage, "你好", "t1")
    assert len(backend.calls) == 1


def test_is_context_overflow_shapes() -> None:
    class WithCode(Exception):
        code = "context_length_exceeded"

    assert is_context_overflow(_overflow_error())
    assert is_context_overflow(WithCode("x"))
    assert is_context_overflow(RuntimeError("This model's maximum context length is 128000 tokens"))
    assert not is_context_overflow(RuntimeError("rate limited"))


async def test_compression_disabled_keeps_history_limit_window() -> None:
    stage = LLMThinkStage(StreamBackend(), system_prompt=PERSONA, history_limit=6)
    stage.history[:] = seed_history(12)
    assert stage._compressor is None
    assert len(stage._history_window()) <= 6
