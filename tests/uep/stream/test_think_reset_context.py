"""LLMThinkStage.reset_context：聽眾切換時清乾淨對話狀態、保留實例。"""

from __future__ import annotations

import asyncio

import pytest

from uep.stream.adapters.think_llm import LLMThinkStage
from uep.stream.contracts.cancellation import CancellationToken
from uep.stream.contracts.types import Utterance

from .test_think_llm import FakeBackend, FakeMemory, _last_user_content


async def _run_turn(stage: LLMThinkStage, text: str, speaker: str | None, **kw):
    utt = Utterance(text=text, speaker_id=speaker, **kw)
    return [s async for s in stage.stream(utt, CancellationToken()) if s.text]


async def _committed_turn(stage: LLMThinkStage, text: str, speaker: str, turn_id: str):
    await _run_turn(stage, text, speaker, turn_id=turn_id)
    await stage.commit(turn_id, "我回答了", "我回答了")


def _stage(backend, mems=None, **kw) -> LLMThinkStage:
    mems = mems or {}
    return LLMThinkStage(backend, memory_resolver=lambda scope: mems.get(scope), **kw)


async def test_reset後history為空():
    stage = _stage(FakeBackend(delay_s=0.001))
    await _committed_turn(stage, "你好", "A", "t1")
    assert len(stage.history) == 2
    stage.reset_context()
    assert stage.history == []
    assert stage._assistant_by_turn == {}


async def test_給定history以複本取代():
    stage = _stage(FakeBackend(delay_s=0.001))
    await _committed_turn(stage, "你好", "A", "t1")
    given = [{"role": "user", "content": "舊話題"}]
    stage.reset_context(given)
    assert stage.history == given
    assert stage.history is not given  # 複本，呼叫端之後改動不影響
    given.append({"role": "assistant", "content": "x"})
    assert len(stage.history) == 1


async def test_reset後settle舊turn_id回False():
    stage = _stage(FakeBackend(delay_s=0.001))
    await _committed_turn(stage, "你好", "A", "t1")
    stage.reset_context([{"role": "assistant", "content": "我回答了"}])
    assert await stage.settle("t1", "改寫") is False
    assert stage.history[0]["content"] == "我回答了"


async def test_reset清掉待注入記憶且舊pending不可再丟棄():
    stage = _stage(FakeBackend(delay_s=0.001))
    stage.seed_pending("A", "甲的記憶")
    stage.seed_pending("B", "乙的記憶")
    stage.reset_context()
    assert stage._pending_by_scope == {}
    assert stage.discard_pending("A") == 0
    assert stage.discard_pending(all_scopes=True) == 0


async def test_reset後上一群聽眾的記憶不再注入():
    backend = FakeBackend(delay_s=0.001)
    stage = _stage(backend, {"A": FakeMemory(result="甲群的祕密")})
    stage.seed_pending("A", "甲群的祕密")
    await _run_turn(stage, "嗨", "A", turn_id="t1")
    assert "甲群的祕密" in _last_user_content(backend)
    await asyncio.sleep(0.01)  # 讓 retrieve 把下一輪的槽寫好
    assert stage._pending_by_scope.get("A")

    stage.reset_context()
    assert stage.last_injected_memory is None
    assert stage._last_build is None and stage._retrieved is None
    await _run_turn(stage, "嗨", "B", turn_id="t2")
    assert "甲群的祕密" not in _last_user_content(backend)


async def test_reset後同turn_id不沿用舊注入也會重新檢索():
    backend = FakeBackend(delay_s=0.001)
    memory = FakeMemory(result="新群的記憶")
    stage = _stage(backend, {"A": memory})
    stage.seed_pending("A", "舊群的記憶")
    await _run_turn(stage, "嗨", "A", turn_id="t1")
    stage.reset_context()
    await _run_turn(stage, "嗨", "A", turn_id="t1")
    assert "舊群的記憶" not in _last_user_content(backend)
    assert len(memory.queries) == 2  # _retrieved 去重已清，重新檢索


async def test_reset遞增世代使在途retrieve作廢():
    stage = _stage(FakeBackend(delay_s=0.001))
    gen_before = stage._discard_gen
    stage.reset_context()
    assert stage._discard_gen == gen_before + 1


async def test_stream進行中呼叫拋RuntimeError():
    stage = _stage(FakeBackend(delay_s=0.01))
    agen = stage.stream(Utterance(text="嗨", speaker_id="A"), CancellationToken())
    first = await agen.__anext__()
    assert first is not None
    try:
        with pytest.raises(RuntimeError):
            stage.reset_context()
        # 失敗時不得動到狀態
        assert stage.active_scope == "A"
    finally:
        await agen.aclose()
    stage.reset_context()  # stream 收尾後可以


async def test_reset保留設定與靜態前綴():
    tools = [{"type": "function", "function": {"name": "deep_recall"}}]
    stage = _stage(
        FakeBackend(delay_s=0.001), system_prompt="人設", tools=tools, prompt_cache_key="k1",
    )
    stage._prewarm_sent["h"] = 1.0
    await _committed_turn(stage, "你好", "A", "t1")
    stage.last_usage = {"input_tokens": 10}
    backend = stage.backend
    stage.reset_context()
    assert stage.backend is backend
    assert stage.system_prompt == "人設"
    assert stage.tools == tools
    assert stage.prompt_cache_key == "k1"
    assert stage._prewarm_sent == {"h": 1.0}
    assert stage.last_usage == {"input_tokens": 10}


async def test_reset清掉上一群的工具結果摘要與profile():
    stage = _stage(FakeBackend(delay_s=0.001))
    stage.last_tool_calls = [{"name": "deep_recall", "result_summary": "甲群的事"}]
    stage.profile = "甲群 profile"  # profile 隨聽眾而變，reset 後由宿主重設
    stage.reset_context()
    assert stage.last_tool_calls == []
    assert stage.profile == ""


async def test_reset後commit舊turn_id不得進新history():
    stage = _stage(FakeBackend(delay_s=0.001))
    await _run_turn(stage, "上一群的問題", "A", turn_id="t1")  # stream 已結束、尚未 commit
    stage.reset_context()
    await stage.commit("t1", "上一群的回答", "上一群的回答")
    assert stage.history == []
    assert stage._assistant_by_turn == {}
    assert await stage.settle("t1", "改寫") is False


async def test_reset後新一輪commit正常_舊輪commit仍被丟棄():
    stage = _stage(FakeBackend(delay_s=0.001))
    await _run_turn(stage, "舊問題", "A", turn_id="t1")
    stage.reset_context()
    await _run_turn(stage, "新問題", "B", turn_id="t2")
    await stage.commit("t1", "舊回答", "舊回答")  # 晚到的舊輪
    await stage.commit("t2", "新回答", "新回答")
    assert stage.history == [
        {"role": "user", "content": "新問題"},
        {"role": "assistant", "content": "新回答"},
    ]
