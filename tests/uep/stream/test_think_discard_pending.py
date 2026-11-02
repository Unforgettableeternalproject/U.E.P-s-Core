"""LLMThinkStage.discard_pending：成員離場時丟棄已渲染的待注入記憶（隱私）。"""

from __future__ import annotations

import asyncio

from uep.stream.adapters.think_llm import LLMThinkStage
from uep.stream.contracts.cancellation import CancellationToken
from uep.stream.contracts.types import Utterance

from .test_think_llm import FakeBackend, FakeMemory, _last_user_content


class OwnerMemory(FakeMemory):
    def __init__(self, owner: str):
        super().__init__(result=f"mem-of-{owner}")


async def _run_turn(stage: LLMThinkStage, text: str, speaker: str | None, **kw):
    utt = Utterance(text=text, speaker_id=speaker, **kw)
    return [s async for s in stage.stream(utt, CancellationToken()) if s.text]


def _stage(backend, mems):
    return LLMThinkStage(backend, memory_resolver=lambda scope: mems.get(scope))


async def test_清單一scope_不影響其他():
    stage = _stage(FakeBackend(delay_s=0.001), {})
    stage.seed_pending("A", "甲")
    stage.seed_pending("B", "乙")
    assert stage.discard_pending("A") == 1
    assert stage.discard_pending("A") == 0
    assert stage._pending_by_scope == {"B": "乙"}


async def test_all_scopes_清全部():
    stage = _stage(FakeBackend(delay_s=0.001), {})
    stage.seed_pending("A", "甲")
    stage.seed_pending("B", "乙")
    assert stage.discard_pending(all_scopes=True) == 2
    assert stage._pending_by_scope == {}


async def test_無resolver時任意scope對應共用槽():
    stage = LLMThinkStage(FakeBackend(delay_s=0.001), memory=FakeMemory())
    stage.seed_pending(None, "共用")
    assert stage.discard_pending("任意") == 1
    assert stage._pending_by_scope == {}


async def test_清除後下一輪不注入():
    backend = FakeBackend(delay_s=0.001)
    stage = _stage(backend, {"A": OwnerMemory("A")})
    await _run_turn(stage, "A 說話", "A")  # retrieve 寫入槽
    assert stage.discard_pending("A") == 1
    await _run_turn(stage, "A 又說", "A")
    assert "mem-of" not in _last_user_content(backend)


async def test_regen沿用路徑不帶回():
    backend = FakeBackend(delay_s=0.001)
    stage = _stage(backend, {"A": OwnerMemory("A")})
    stage.seed_pending("A", "共享投影")
    await _run_turn(stage, "嗨", "A", turn_id="t1")
    assert "共享投影" in _last_user_content(backend)
    stage.discard_pending("A")
    await _run_turn(stage, "嗨", "A", turn_id="t1")
    assert "共享投影" not in _last_user_content(backend)
    assert stage.last_injected_memory is None


async def test_只清相符槽的last_build():
    backend = FakeBackend(delay_s=0.001)
    stage = _stage(backend, {"A": OwnerMemory("A")})
    stage.seed_pending("A", "甲的投影")
    await _run_turn(stage, "嗨", "A", turn_id="t1")
    stage.discard_pending("B")  # 不相符，A 的 regen 沿用不受影響
    await _run_turn(stage, "嗨", "A", turn_id="t1")
    assert "甲的投影" in _last_user_content(backend)


async def test_清除前發起的retrieve晚到不寫回():
    gate = asyncio.Event()

    class SlowMemory(FakeMemory):
        async def retrieve(self, text):
            await gate.wait()
            return "含離場者的投影"

    # 生成必須比下方 0.05s 等待更久：Windows 計時器約 15ms 一跳，delay 0.001 實際會變慢，Linux 則會在 discard 前就跑完而取消 retrieve
    stage = _stage(FakeBackend(delay_s=0.02), {"A": SlowMemory()})
    task = asyncio.ensure_future(_run_turn(stage, "嗨", "A", turn_id="t1"))
    await asyncio.sleep(0.05)  # retrieve 已起跑、卡在 gate
    stage.discard_pending("A")
    gate.set()
    await task
    await asyncio.sleep(0.01)
    assert stage._pending_by_scope == {}
    # 去重記錄仍在：同 turn_id 重生成不重跑 retrieve
    assert stage._retrieved is not None


async def test_清除後新發起的retrieve正常寫回():
    stage = _stage(FakeBackend(delay_s=0.001), {"A": OwnerMemory("A")})
    stage.discard_pending(all_scopes=True)
    await _run_turn(stage, "嗨", "A", turn_id="t1")
    await asyncio.sleep(0.01)
    assert stage._pending_by_scope == {"A": "mem-of-A"}
