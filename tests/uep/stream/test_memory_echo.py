"""EchoMemoryAdapter：不碰真 echo_memory，用假 engine 驗 adapter 自己的責任。"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field

import pytest

from uep.stream.adapters.memory_echo import EchoMemoryAdapter


@dataclass
class _Ep:
    id: str
    user_input: str
    is_dreamed: bool = False
    salience_score: float = 0.5


@dataclass
class _Ctx:
    episodes: list = field(default_factory=list)
    similarities: dict = field(default_factory=dict)
    concepts: list = field(default_factory=list)
    entities: list = field(default_factory=list)
    procedures: list = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.episodes or self.concepts or self.entities or self.procedures)

    def to_text(self) -> str:
        return "|".join(f"{e.user_input}:{self.similarities[e.id]:.2f}" for e in self.episodes)


class _Episodic:
    def __init__(self, eps):
        self._eps = eps

    def count(self):
        return len(self._eps)

    def list_episodes(self, limit=50, offset=0, **_):
        return self._eps[offset : offset + min(limit, 200)]


class _Engine:
    def __init__(self, ctx, eps=()):
        self._ctx = ctx
        self.calls: list[dict] = []
        self.episodic = _Episodic(list(eps))

    def retrieve(self, text, **kw):
        self.calls.append(kw)
        return self._ctx


async def test_retrieve_丟棄圖擴散漏過門檻的episode():
    ctx = _Ctx(
        episodes=[_Ep("a", "攀岩"), _Ep("b", "深藍色"), _Ep("c", "無分數")],
        similarities={"a": 0.62, "b": 0.31},  # c 沒有分數 = 圖擴散漏出來的
    )
    adapter = EchoMemoryAdapter(engine=_Engine(ctx), top_k=3)
    adapter.min_match = 0.45
    assert await adapter.retrieve("問題") == "攀岩:0.62"


async def test_retrieve_透傳引擎參數_舊引擎略過不認得的():
    ctx = _Ctx(episodes=[_Ep("a", "攀岩")], similarities={"a": 0.9})
    eng = _Engine(ctx)
    await EchoMemoryAdapter(engine=eng).retrieve("問題", shared_turn=True)
    assert eng.calls[-1]["shared_turn"] is True

    class _OldEngine(_Engine):
        def retrieve(self, text, *, top_k=None, min_similarity=None, exclude_session_id=None):
            self.calls.append({"top_k": top_k})
            return self._ctx

    old = _OldEngine(ctx)
    assert await EchoMemoryAdapter(engine=old).retrieve("問題", shared_turn=True) == "攀岩:0.90"


async def test_retrieve_全部低於門檻回None():
    ctx = _Ctx(episodes=[_Ep("b", "深藍色")], similarities={"b": 0.31})
    adapter = EchoMemoryAdapter(engine=_Engine(ctx))
    adapter.min_match = 0.45
    assert await adapter.retrieve("問題") is None


async def test_retrieve_把門檻傳給引擎():
    engine = _Engine(_Ctx())
    adapter = EchoMemoryAdapter(engine=engine)
    adapter.min_match = 0.5
    adapter.set_session("s1")
    await adapter.retrieve("問題")
    assert engine.calls[0]["min_similarity"] == 0.5
    assert engine.calls[0]["exclude_session_id"] == "s1"


async def test_pending_dream_count_只算未dreamed且過顯著性():
    eps = [
        _Ep("1", "x", is_dreamed=False, salience_score=0.5),
        _Ep("2", "x", is_dreamed=True, salience_score=0.9),
        _Ep("3", "x", is_dreamed=False, salience_score=0.3),
        _Ep("4", "x", is_dreamed=False, salience_score=0.7),
    ]
    adapter = EchoMemoryAdapter(engine=_Engine(_Ctx(), eps))
    assert await adapter.pending_dream_count() == 2


async def test_pending_dream_count_跨頁():
    eps = [_Ep(str(i), "x") for i in range(450)]
    adapter = EchoMemoryAdapter(engine=_Engine(_Ctx(), eps))
    assert await adapter.pending_dream_count() == 450


# --- salience（Phase 4b）---
# echo_memory 不在測試環境；用假模組塞進 sys.modules，只驗 adapter 的串接責任。



class _StoreEngine(_Engine):
    def __init__(self, ctx=None):
        super().__init__(ctx or _Ctx())
        self.neurochem = object()
        self.stored: list[dict] = []

    def store_episode(self, **kw):
        self.stored.append(kw)
        return "ep_test"


@pytest.fixture
def fake_affect(monkeypatch):
    """假的 echo_memory.affect：assess 回固定五維、evaluate 回固定三值。"""
    calls: dict = {"assess": [], "neurochem_updates": 0}

    class Assessment:
        def __init__(self, novelty=0.9):
            self.novelty = novelty

        def to_dict(self):
            return {"novelty": self.novelty}

    class SubconsciousAssessor:
        def __init__(self, llm_fn=None, structured_llm_fn=None):
            self.llm_fn = llm_fn

        def assess(self, user_input, memory_ctx=None, agent_response=""):
            calls["assess"].append((user_input, memory_ctx, self.llm_fn))
            if user_input == "炸":
                raise RuntimeError("assess boom")
            return Assessment()

    class SalienceSignals:
        @classmethod
        def from_subconscious(cls, assessment, **_):
            return ("signals", assessment)

    class SalienceEvaluator:
        def __init__(self, neurochem):
            self.neurochem = neurochem

        def evaluate(self, signals, user_input="", agent_response=""):
            return (0.82, 0.6, 0.4)

        def update_neurochem(self, signals):
            calls["neurochem_updates"] += 1

    mods = {
        "echo_memory": types.ModuleType("echo_memory"),
        "echo_memory.affect": types.ModuleType("echo_memory.affect"),
        "echo_memory.affect.subconscious": types.ModuleType("x"),
        "echo_memory.affect.signals": types.ModuleType("x"),
        "echo_memory.affect.salience": types.ModuleType("x"),
    }
    mods["echo_memory.affect.subconscious"].SubconsciousAssessor = SubconsciousAssessor
    mods["echo_memory.affect.signals"].SalienceSignals = SalienceSignals
    mods["echo_memory.affect.salience"].SalienceEvaluator = SalienceEvaluator
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return calls


async def test_store_帶三值與評估維度(fake_affect):
    engine = _StoreEngine()
    adapter = EchoMemoryAdapter(engine=engine, llm_fn=lambda p: "{}")
    adapter.assess_mode = "llm"
    out = await adapter.store("我在學薩克斯風", "好棒", origin="voice")
    kw = engine.stored[0]
    assert (kw["salience"], kw["da_weight"], kw["ht_weight"]) == (0.82, 0.6, 0.4)
    assert kw["assessment"] == {"novelty": 0.9}
    assert kw["origin"] == "voice"
    assert out["episode_id"] == "ep_test" and out["salience"] == 0.82
    assert fake_affect["neurochem_updates"] == 1


async def test_store_評估失敗退回預設照存(fake_affect):
    engine = _StoreEngine()
    adapter = EchoMemoryAdapter(engine=engine, llm_fn=lambda p: "{}")
    out = await adapter.store("炸", "回覆")
    kw = engine.stored[0]
    assert "salience" not in kw, "評估炸了就交給 store_episode 的預設 0.5"
    assert out["episode_id"] == "ep_test"


async def test_store_off模式不評估(fake_affect):
    engine = _StoreEngine()
    adapter = EchoMemoryAdapter(engine=engine)
    adapter.assess_mode = "off"
    await adapter.store("問", "答")
    assert fake_affect["assess"] == []
    assert "salience" not in engine.stored[0]


async def test_store_用同一輪retrieve的context評估(fake_affect):
    ctx = _Ctx(episodes=[_Ep("a", "舊事")], similarities={"a": 0.7})
    engine = _StoreEngine(ctx)
    adapter = EchoMemoryAdapter(engine=engine, llm_fn=lambda p: "{}")
    await adapter.retrieve("第一輪")
    await adapter.retrieve("第二輪")
    # 第一輪的 store 比第二輪的 retrieve 晚到——仍要配到第一輪的 context
    await adapter.store("第一輪", "答")
    _, used_ctx, _ = fake_affect["assess"][0]
    assert used_ctx is ctx
    assert "第一輪" not in adapter._contexts, "用過就釋放"


async def test_assessor_用評估專用的llm_fn(fake_affect):
    adapter = EchoMemoryAdapter(engine=_StoreEngine())
    cheap = lambda p: "cheap"  # noqa: E731
    adapter.set_llm_fn(lambda p: "full", assess_llm_fn=cheap)
    await adapter.store("問", "答")
    assert fake_affect["assess"][0][2] is cheap


async def test_heuristic模式不給llm(fake_affect):
    adapter = EchoMemoryAdapter(engine=_StoreEngine(), llm_fn=lambda p: "full")
    adapter.assess_mode = "heuristic"
    await adapter.store("問", "答")
    assert fake_affect["assess"][0][2] is None


async def test_judge_微調salience並帶標籤(fake_affect, monkeypatch):
    class Decision:
        salience_delta = 0.3
        metadata_tags = ["correction"]
        force_remember = True
        entity_triggers = False
        reasoning = "使用者要求不要再糾正"
        source = "llm"

    class StorageJudge:
        def __init__(self, llm_fn=None, structured_llm_fn=None):
            pass

        def storage_decision(self, **kw):
            assert kw["baseline_salience"] == 0.82
            return Decision()

    mod = types.ModuleType("x")
    mod.StorageJudge = StorageJudge
    monkeypatch.setitem(sys.modules, "echo_memory.affect.judge", mod)
    monkeypatch.setenv("ECHO_STREAM_MEMORY_JUDGE", "on")

    engine = _StoreEngine()
    adapter = EchoMemoryAdapter(engine=engine, llm_fn=lambda p: "{}")
    out = await adapter.store("別再糾正我", "好")
    kw = engine.stored[0]
    assert kw["salience"] == 1.0  # 0.82 + 0.3 夾到 1
    assert kw["force_remember"] is True
    assert kw["judge"]["tags"] == ["correction"]
    assert out["judge"]["source"] == "llm"


async def test_judge_關閉時不呼叫(fake_affect, monkeypatch):
    monkeypatch.setenv("ECHO_STREAM_MEMORY_JUDGE", "off")
    monkeypatch.setitem(sys.modules, "echo_memory.affect.judge", None)  # import 會炸
    engine = _StoreEngine()
    adapter = EchoMemoryAdapter(engine=engine, llm_fn=lambda p: "{}")
    await adapter.store("問", "答")
    assert engine.stored[0]["salience"] == 0.82 and "judge" not in engine.stored[0]


# --- 工具呼叫（C 段）---


@pytest.fixture
def fake_tools(monkeypatch):
    """假的 echo_memory llm_integration.tools：三個 schema + 記錄呼叫的 executor。"""
    calls: list = []
    decls = [
        {"name": "deep_recall", "description": "d", "parameters": {"type": "object"}},
        {"name": "force_remember", "description": "f", "parameters": {"type": "object"}},
        {"name": "trigger_dream", "description": "t", "parameters": {"type": "object"}},
    ]

    class ToolExecutor:
        def __init__(self, engine, session_id, dream_cooldown_turns=5):
            self.engine = engine
            self.session_id = session_id

        def execute(self, name, args):
            calls.append((self.session_id, name, args))
            return {"success": True, "result": "R" * 3000}

    mods = {
        "llm_integration": types.ModuleType("llm_integration"),
        "llm_integration.tools": types.ModuleType("llm_integration.tools"),
        "llm_integration.tools.definitions": types.ModuleType("x"),
        "llm_integration.tools.executor": types.ModuleType("x"),
    }
    mods["llm_integration.tools.definitions"].TOOL_DECLARATIONS = decls
    mods["llm_integration.tools.executor"].ToolExecutor = ToolExecutor
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return calls


def test_tool_definitions_預設只開兩個且包成openai格式(fake_tools, monkeypatch):
    monkeypatch.delenv("ECHO_STREAM_MEMORY_TOOLS", raising=False)
    adapter = EchoMemoryAdapter(_Engine(_Ctx()))
    defs = adapter.tool_definitions()
    assert [d["function"]["name"] for d in defs] == ["deep_recall", "force_remember"]
    assert all(d["type"] == "function" for d in defs)


def test_tool_definitions_可用設定關閉或擴充(fake_tools, monkeypatch):
    monkeypatch.setenv("ECHO_STREAM_MEMORY_TOOLS", "off")
    assert EchoMemoryAdapter(_Engine(_Ctx())).tool_definitions() == []
    monkeypatch.setenv("ECHO_STREAM_MEMORY_TOOLS", "deep_recall, trigger_dream, nope")
    names = [d["function"]["name"] for d in EchoMemoryAdapter(_Engine(_Ctx())).tool_definitions()]
    assert names == ["deep_recall", "trigger_dream"]


async def test_execute_tool_綁session_擋白名單_截結果(fake_tools, monkeypatch):
    monkeypatch.delenv("ECHO_STREAM_MEMORY_TOOLS", raising=False)
    monkeypatch.setenv("ECHO_STREAM_MEMORY_TOOL_MAX_CHARS", "100")
    adapter = EchoMemoryAdapter(_Engine(_Ctx()))
    adapter.set_session("s1")

    out = await adapter.execute_tool("deep_recall", {"query": "貓"})
    assert out["success"] and len(out["result"]) == 101
    assert fake_tools == [("s1", "deep_recall", {"query": "貓"})]

    denied = await adapter.execute_tool("trigger_dream", {})
    assert denied["success"] is False and len(fake_tools) == 1

    adapter.set_session("s2")
    await adapter.execute_tool("force_remember", {"content": "x"})
    assert fake_tools[-1][0] == "s2", "換會話後 executor 重建"


async def test_aclose_等排隊中的寫入跑完才關閉executor():
    import asyncio
    import threading

    adapter = EchoMemoryAdapter(engine=_Engine(_Ctx()))
    release = threading.Event()
    done: list[str] = []

    def _slow_store():
        release.wait(timeout=5)
        done.append("store")

    loop = asyncio.get_running_loop()
    pending = loop.run_in_executor(adapter._executor, _slow_store)
    closing = asyncio.create_task(adapter.aclose())
    await asyncio.sleep(0.05)
    assert not closing.done()  # 寫入還沒完成，不能先關
    release.set()
    await closing
    await pending
    assert done == ["store"]
    with pytest.raises(RuntimeError):
        adapter._executor.submit(lambda: None)
    await adapter.aclose()  # 重複呼叫安全


async def test_store_salience評估期間不持鎖_另一呼叫能取得鎖(fake_affect, monkeypatch):
    import asyncio
    import threading
    import time

    lock = threading.Lock()
    in_assess = threading.Event()
    release = threading.Event()

    class SlowAssessor:
        def __init__(self, llm_fn=None, structured_llm_fn=None):
            pass

        def assess(self, user_input, memory_ctx=None, agent_response=""):
            in_assess.set()
            assert release.wait(5), "測試沒放行"
            return sys.modules["echo_memory.affect.subconscious"]._Assessment()

    class _Assessment:
        def to_dict(self):
            return {}

    sub = sys.modules["echo_memory.affect.subconscious"]
    sub._Assessment = _Assessment
    sub.SubconsciousAssessor = SlowAssessor

    engine = _StoreEngine()
    adapter = EchoMemoryAdapter(engine=engine, llm_fn=lambda p: "{}", engine_lock=lock)
    task = asyncio.ensure_future(adapter.store("問", "答"))
    deadline = time.monotonic() + 5
    while not in_assess.is_set() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert in_assess.is_set()

    # assess 還在跑：鎖必須是空的，另一個 engine 呼叫（profile）要能立刻完成
    assert lock.acquire(blocking=False), "salience 評估期間不該持有 engine 鎖"
    lock.release()
    engine.get_profile = lambda max_chars=800: "profile"
    assert await asyncio.wait_for(adapter.profile(), 2) == "profile"

    release.set()
    out = await asyncio.wait_for(task, 5)
    assert out["episode_id"] == "ep_test"
    assert engine.stored[0]["salience"] == 0.82


def test_force_remember描述限定明確要求才呼叫(fake_tools, monkeypatch):
    monkeypatch.delenv("ECHO_STREAM_MEMORY_TOOLS", raising=False)
    defs = EchoMemoryAdapter(_Engine(_Ctx())).tool_definitions()
    by_name = {d["function"]["name"]: d["function"] for d in defs}
    desc = by_name["force_remember"]["description"]
    assert "明確要求" in desc and "自動寫入" in desc
    assert by_name["deep_recall"]["description"] == "d", "只覆寫 force_remember"
