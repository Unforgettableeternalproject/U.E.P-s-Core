"""LLMThinkStage 的 voice-memory 擴充：per-scope 記憶隔離、重生成去重、模型自生墊話。

不傳新參數時的舊行為由 test_think_llm.py 守住；這裡只測新增的部分。
"""

from __future__ import annotations

import asyncio

import pytest

from uep.stream.adapters.think_llm import (
    DEFAULT_TOOL_FILLER,
    LEAD_IN_VIOLATION_RE,
    LLMThinkStage,
)
from uep.stream.contracts.cancellation import CancellationToken
from uep.stream.contracts.types import Utterance

from .test_think_llm import (
    FakeBackend,
    FakeMemory,
    ToolBackend,
    _d,
    _executor,
    _last_user_content,
    collect,
)

# --- per-scope 記憶隔離與重生成去重（批 A1）---


class ScopedMemory(FakeMemory):
    """retrieve 回 ``mem-of-{owner}``，記錄查詢。"""

    def __init__(self, owner: str):
        super().__init__(result=f"mem-of-{owner}")


async def _run_turn(stage: LLMThinkStage, text: str, speaker: str | None, **kw):
    utt = Utterance(text=text, speaker_id=speaker, **kw)
    return [s async for s in stage.stream(utt, CancellationToken()) if s.text]


async def test_scope_交錯_A的記憶不進B():
    backend = FakeBackend(delay_s=0.001)
    mems = {"A": ScopedMemory("A"), "B": ScopedMemory("B")}
    stage = LLMThinkStage(backend, memory_resolver=lambda scope: mems.get(scope))

    await _run_turn(stage, "A 說話", "A")
    await _run_turn(stage, "B 說話", "B")
    # B 的第一輪：本輪不注入（下輪才生效），更不能吃到 A 的
    assert "mem-of" not in _last_user_content(backend)

    await _run_turn(stage, "A 又說", "A")
    content = _last_user_content(backend)
    assert "mem-of-A" in content and "mem-of-B" not in content
    await _run_turn(stage, "B 又說", "B")
    content = _last_user_content(backend)
    assert "mem-of-B" in content and "mem-of-A" not in content


async def test_resolver_回None_該scope不查記憶():
    backend = FakeBackend(delay_s=0.001)
    mem = ScopedMemory("A")
    stage = LLMThinkStage(backend, memory_resolver=lambda scope: mem if scope == "A" else None)
    await _run_turn(stage, "嗨", "X")
    await _run_turn(stage, "嗨", "X")
    assert mem.queries == []
    assert "mem-of" not in _last_user_content(backend)


async def test_active_scope_只在stream期間有值():
    seen: list[str | None] = []

    class SpyBackend(FakeBackend):
        async def stream_query(self, messages, system_instruction=None, **kw):
            seen.append(stage.active_scope)
            async for ch in super().stream_query(messages, system_instruction, **kw):
                yield ch

    stage = LLMThinkStage(SpyBackend())
    assert stage.active_scope is None
    await _run_turn(stage, "嗨", "uid-1")
    assert seen == ["uid-1"]
    assert stage.active_scope is None


async def test_沒設resolver時_speaker_id不影響記憶槽():
    # Web 版 STT 可能每輪給不同 speaker_id；沒設 resolver 就必須維持單槽舊行為
    backend = FakeBackend(delay_s=0.001)
    stage = LLMThinkStage(backend, memory=FakeMemory())
    await _run_turn(stage, "我家的貓", "spk1")
    await _run_turn(stage, "牠很可愛", "spk2")
    assert "毛球" in _last_user_content(backend)


async def test_memory_query_優先於_text():
    mem = FakeMemory()
    stage = LLMThinkStage(FakeBackend(delay_s=0.001), memory=mem)
    await _run_turn(stage, "多人轉錄 標頭 A: 嗨 B: 你好", None, metadata={"memory_query": "嗨"})
    assert mem.queries == ["嗨"]
    await _run_turn(stage, "沒有 metadata", None)
    assert mem.queries == ["嗨", "沒有 metadata"]


async def test_memory_retrieve_kwargs_透傳給記憶retrieve():
    seen: list[dict] = []

    class KwMemory(FakeMemory):
        async def retrieve(self, text, **kw):
            seen.append(kw)
            return await super().retrieve(text)

    stage = LLMThinkStage(FakeBackend(delay_s=0.001), memory=KwMemory())
    await _run_turn(stage, "嗨", None, metadata={"memory_retrieve_kwargs": {"shared_turn": True}})
    await _run_turn(stage, "再嗨", None)
    assert seen == [{"shared_turn": True}, {}]


async def test_同turn_id重生成_沿用注入且不重複retrieve():
    backend = FakeBackend(delay_s=0.001)
    mem = FakeMemory()
    stage = LLMThinkStage(backend, memory=mem)
    await _run_turn(stage, "第一輪", None)  # 寫入槽，下一輪注入
    utt = Utterance(text="第二輪")
    for _ in range(2):
        [s async for s in stage.stream(utt, CancellationToken())]
        assert "毛球" in _last_user_content(backend), "regen 也要拿到同一份注入"
    # 第二輪的 retrieve 完成後，regen 同 turn_id 同 query 不重跑
    assert mem.queries == ["第一輪", "第二輪"]


async def test_重生成_query變了就重跑():
    slow = FakeMemory()
    stage = LLMThinkStage(FakeBackend(delay_s=0.001), memory=slow)
    utt = Utterance(text="舊", metadata={"memory_query": "舊"})
    [s async for s in stage.stream(utt, CancellationToken())]
    utt.metadata["memory_query"] = "新"
    [s async for s in stage.stream(utt, CancellationToken())]
    assert slow.queries == ["舊", "新"]


async def test_retrieve被取消不算完成_regen會補跑():
    class SlowMemory(FakeMemory):
        block = True

        async def retrieve(self, text):
            if self.block:
                await asyncio.sleep(10)  # 第一次：生成結束時被取消
            return await super().retrieve(text)

    mem = SlowMemory()
    stage = LLMThinkStage(FakeBackend(), memory=mem)
    utt = Utterance(text="問題")
    [s async for s in stage.stream(utt, CancellationToken())]
    assert stage._retrieved is None, "被取消的 retrieve 不得記為完成"

    mem.block = False
    stage._backend = FakeBackend(delay_s=0.001)  # 給 retrieve task 跑完的機會
    [s async for s in stage.stream(utt, CancellationToken())]
    assert mem.queries == ["問題"], "上次被取消，regen 必須補跑"
    assert stage._retrieved is not None


# --- 墊話（批 A2）---

_TOOLS = [{"type": "function", "function": {"name": "deep_recall"}}]


def _lead_stage(mode: str, backend, *, filler: str | None = DEFAULT_TOOL_FILLER, **kw):
    return LLMThinkStage(
        backend, tools=_TOOLS, tool_executor=_executor([]),
        tool_filler=filler, tool_lead_in=mode, **kw,
    )


async def _texts(stage: LLMThinkStage) -> list[str]:
    return [s.text for s in await collect(stage, "我的貓叫什麼")]


async def test_預設模式是fixed_行為不變且不附墊話提示():
    backend = ToolBackend()
    stage = _lead_stage("fixed", backend)
    assert LLMThinkStage(FakeBackend()).tool_lead_in == "fixed"
    texts = await _texts(stage)
    assert texts[0] == DEFAULT_TOOL_FILLER
    assert "查詢前的開場" not in (backend.requests[0]["system_instruction"] or "")
    assert stage.last_tool_calls[0]["lead_in_source"] == "fixed"


async def test_不認得的模式直接報錯():
    with pytest.raises(ValueError):
        LLMThinkStage(FakeBackend(), tool_lead_in="bogus")


@pytest.mark.parametrize("mode", ["model_then_fixed", "model"])
async def test_模型自生墊話_先於工具結果念出_進assistant_content(mode):
    backend = ToolBackend(preface="我想想。")
    stage = _lead_stage(mode, backend)
    texts = await _texts(stage)
    assert texts[0] == "我想想。", "FLUSH 讓短墊話立刻成句，而不是被扣到續寫之後"
    assert "".join(texts[1:]) == "你上次說你養了一隻叫小黑的貓。"
    assert "讓我想一下" not in "".join(texts), "模型已開口就不補固定 filler"
    assert _d(backend.requests[1]["messages"][-2])["content"] == "我想想。"
    call = stage.last_tool_calls[0]
    assert call["lead_in_source"] == "model" and call["lead_in"] == "我想想。"
    assert "lead_in_violation" not in call


@pytest.mark.parametrize(
    ("mode", "expect_filler"),
    [("model_then_fixed", True), ("fixed", True), ("model", False), ("off", False)],
)
async def test_模型沒墊話時的退路(mode, expect_filler):
    backend = ToolBackend()
    stage = _lead_stage(mode, backend)
    texts = await _texts(stage)
    if expect_filler:
        assert texts[0] == DEFAULT_TOOL_FILLER
        assert stage.last_tool_calls[0]["lead_in_source"] == "fixed"
    else:
        assert "讓我想一下" not in "".join(texts)
        assert stage.last_tool_calls[0]["lead_in_source"] == "none"
        assert _d(backend.requests[1]["messages"][-2])["content"] == ""
    assert "".join(texts).endswith("你上次說你養了一隻叫小黑的貓。")


async def test_off模式即使模型先開口也不插filler():
    backend = ToolBackend(preface="好的，")
    stage = _lead_stage("off", backend)
    texts = "".join(await _texts(stage))
    assert texts.startswith("好的，") and "讓我想一下" not in texts
    assert stage.last_tool_calls[0]["lead_in_source"] == "model"


@pytest.mark.parametrize("mode", ["model_then_fixed", "model"])
async def test_model系模式_system_prompt_附墊話提示與extra(mode):
    backend = ToolBackend()
    stage = _lead_stage(mode, backend, tool_lead_in_extra="開場也要帶 @名字:")
    await _texts(stage)
    system = backend.requests[0]["system_instruction"]
    assert system.index("## 記憶工具") < system.index("## 查詢前的開場")
    assert system.rstrip().endswith("開場也要帶 @名字:")


@pytest.mark.parametrize("mode", ["fixed", "off"])
async def test_非model模式不附墊話提示(mode):
    backend = ToolBackend()
    await _texts(_lead_stage(mode, backend, tool_lead_in_extra="不該出現"))
    system = backend.requests[0]["system_instruction"]
    assert "查詢前的開場" not in system and "不該出現" not in system


async def test_沒有工具時不附墊話提示():
    backend = FakeBackend()
    stage = LLMThinkStage(backend, system_prompt="人設", tool_lead_in="model_then_fixed")
    await collect(stage)
    assert backend.last_system == "人設"


async def test_墊話斷言違規只觀測不攔截():
    backend = ToolBackend(preface="我記得這件事。")
    stage = _lead_stage("model_then_fixed", backend)
    texts = await _texts(stage)
    assert texts[0] == "我記得這件事。", "不攔截，照常念出"
    assert stage.last_tool_calls[0]["lead_in_violation"] is True


def test_固定filler不含記憶斷言():
    assert not LEAD_IN_VIOLATION_RE.search(DEFAULT_TOOL_FILLER)


# --- scope 所有權與工具執行（審查修正）---


async def test_scope_收尾只清自己設的_交錯不誤清():
    gate = asyncio.Event()

    class GatedBackend(FakeBackend):
        async def stream_query(self, messages, system_instruction=None, **kw):
            await gate.wait()
            async for ch in super().stream_query(messages, system_instruction, **kw):
                yield ch

    stage = LLMThinkStage(GatedBackend())
    token = CancellationToken()
    gen_a = stage.stream(Utterance(text="甲", speaker_id="A"), token)
    gen_b = stage.stream(Utterance(text="乙", speaker_id="B"), token)
    task_a = asyncio.ensure_future(gen_a.__anext__())
    await asyncio.sleep(0.01)
    assert stage.active_scope == "A"
    task_b = asyncio.ensure_future(gen_b.__anext__())
    await asyncio.sleep(0.01)
    assert stage.active_scope == "B"

    # A 先收尾：B 仍在跑，scope 不能被 A 清成 None
    task_a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task_a
    await gen_a.aclose()
    assert stage.active_scope == "B"

    gate.set()
    await task_b
    await gen_b.aclose()
    assert stage.active_scope is None


async def test_工具執行用本次stream的scope_不讀被改掉的值():
    seen: list[str | None] = []

    class InterleavedBackend(ToolBackend):
        async def stream_query(self, messages, system_instruction=None, **kw):
            # 模擬另一個 stream 在這輪進行中把 active_scope 改掉
            stage.active_scope = "OTHER"
            async for piece in super().stream_query(messages, system_instruction, **kw):
                yield piece

    async def executor(name: str, args: dict) -> dict:
        seen.append(stage.active_scope)
        return {"success": True, "result": "ok"}

    stage = LLMThinkStage(
        InterleavedBackend(),
        tools=[{"type": "function", "function": {"name": "deep_recall"}}],
        tool_executor=executor,
    )
    await _run_turn(stage, "我的貓", "A")
    assert seen == ["A"]
    assert stage.active_scope is None
