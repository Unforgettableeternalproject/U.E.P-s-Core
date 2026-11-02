"""member_recall 指名查詢工具：假引擎驗 adapter 的解析、透傳、掛名與消費記錄。

脈絡（scope／viewer）隨工具呼叫以 ``context`` 傳入，不存在 adapter 上。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from uep.stream.adapters.memory_echo import EchoMemoryAdapter
from uep.stream.adapters.think_llm import LLMThinkStage
from uep.stream.contracts.cancellation import CancellationToken
from uep.stream.contracts.types import Utterance

from .test_memory_echo import _Ctx, _Engine
from .test_memory_echo_sharing import _Item, _Shared
from .test_think_llm import ToolBackend, _Call


class _OwnerEngine(_Engine):
    """帶 owner_uid 的 retrieve_shared 替身。"""

    def __init__(self, shared=None):
        super().__init__(_Ctx())
        self.calls2: list[tuple] = []
        self.shared = shared or _Shared([_Item("p1"), _Item("p2")])

    def retrieve_shared(self, query, *, scope_id, viewer_uid, present_uids=(), owner_uid=None):
        self.calls2.append((query, scope_id, viewer_uid, tuple(present_uids), owner_uid))
        return self.shared


class _OldEngine(_Engine):
    """舊版引擎：retrieve_shared 不認得 owner_uid。"""

    def __init__(self):
        super().__init__(_Ctx())
        self.called = 0

    def retrieve_shared(self, query, *, scope_id, viewer_uid, present_uids=()):
        self.called += 1
        return _Shared([_Item("x")])


class _KwargsEngine(_Engine):
    """只有 **kwargs 的引擎：不算明確支援 owner_uid。"""

    def __init__(self):
        super().__init__(_Ctx())
        self.called = 0

    def retrieve_shared(self, query, **kw):
        self.called += 1
        return _Shared([_Item("x")])


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("ECHO_STREAM_MEMORY_TOOLS", raising=False)


def _adapter(engine, resolver=lambda name: "u1", tools=("member_recall",)):
    adapter = EchoMemoryAdapter(engine=engine)
    adapter.enabled_tools = list(tools)
    adapter.set_member_resolver(resolver)
    return adapter


def _ctx(scope="scope-g1", viewer="v1", present=("v1", "a"), **extra):
    cfg = {"scope_id": scope, "viewer_uid": viewer, "present_uids": list(present),
           "display_name": lambda uid: "成員甲"}
    cfg.update(extra)
    return {"member_recall": cfg}


async def test_解析成功_帶owner_uid_掛名_ids寫回context():
    eng = _OwnerEngine()
    adapter = _adapter(eng, lambda name: "u_member_a" if name == "成員甲" else None)
    ctx = _ctx()
    out = await adapter.execute_tool(
        "member_recall", {"member": "成員甲", "query": "遊戲"}, context=ctx)
    assert out["success"] is True
    assert eng.calls2 == [("遊戲", "scope-g1", "v1", ("v1", "a"), "u_member_a")]
    assert "成員甲" in out["result"] and "p1,p2" in out["result"]
    assert ctx["member_recall_ids"] == ["p1", "p2"]


async def test_標題用resolver對應的顯示名_不回填LLM給的字串():
    adapter = _adapter(_OwnerEngine(), lambda name: "u1")
    out = await adapter.execute_tool(
        "member_recall", {"member": "忽略前述指示的怪字串"}, context=_ctx())
    assert out["result"].startswith("以下是關於「成員甲」（本伺服器成員）在伺服器裡可分享的內容；")
    assert "以本人為準" in out["result"].split("\n", 1)[0]
    assert "怪字串" not in out["result"]


async def test_query空白可用():
    eng = _OwnerEngine()
    out = await _adapter(eng).execute_tool("member_recall", {"member": "成員甲"}, context=_ctx())
    assert out["success"] is True
    assert eng.calls2[0][0] == "" and eng.calls2[0][4] == "u1"


@pytest.mark.parametrize("resolver", [None, lambda name: None])
async def test_解析失敗或未注入_不呼叫引擎(resolver):
    eng = _OwnerEngine()
    ctx = _ctx()
    out = await _adapter(eng, resolver).execute_tool(
        "member_recall", {"member": "路人"}, context=ctx)
    assert out["result"] == "無法確認是哪位成員。"
    assert eng.calls2 == [] and "member_recall_ids" not in ctx


async def test_resolver拋例外_視同無法確認():
    def boom(name):
        raise RuntimeError("x")

    eng = _OwnerEngine()
    out = await _adapter(eng, boom).execute_tool(
        "member_recall", {"member": "成員甲"}, context=_ctx())
    assert "無法確認" in out["result"] and eng.calls2 == []


@pytest.mark.parametrize("context", [
    None,
    {},
    {"member_recall": {}},
    {"member_recall": {"scope_id": "s"}},
    {"member_recall": {"viewer_uid": "v"}},
    {"member_recall": "壞掉"},
])
async def test_context缺失或不完整_不呼叫引擎(context):
    eng = _OwnerEngine()
    out = await _adapter(eng).execute_tool("member_recall", {"member": "成員甲"}, context=context)
    assert out["success"] is True and "無法確認" in out["result"]
    assert eng.calls2 == []


async def test_兩輪交錯_scope與ids各歸各的():
    """同一個 adapter 同時服務兩個 guild 的輪次：A 不會用到 B 的 scope，ids 各進各的 dict。"""
    release = asyncio.Event()

    class _SlowAdapter(EchoMemoryAdapter):
        async def retrieve_shared(self, text, **kw):
            if kw["scope_id"] == "scope-A":
                await release.wait()
            return await super().retrieve_shared(text, **kw)

    class _Eng(_OwnerEngine):
        def retrieve_shared(self, query, *, scope_id, viewer_uid, present_uids=(),
                            owner_uid=None):
            self.shared = _Shared([_Item(f"p-{scope_id}")])
            return super().retrieve_shared(
                query, scope_id=scope_id, viewer_uid=viewer_uid,
                present_uids=present_uids, owner_uid=owner_uid)

    eng = _Eng()
    adapter = _SlowAdapter(engine=eng)
    adapter.enabled_tools = ["member_recall"]
    adapter.set_member_resolver(lambda name: "u1")
    ctx_a, ctx_b = _ctx(scope="scope-A", viewer="va"), _ctx(scope="scope-B", viewer="vb")
    task_a = asyncio.create_task(
        adapter.execute_tool("member_recall", {"member": "x"}, context=ctx_a))
    await asyncio.sleep(0)
    await adapter.execute_tool("member_recall", {"member": "x"}, context=ctx_b)
    release.set()
    await task_a
    assert ctx_a["member_recall_ids"] == ["p-scope-A"]
    assert ctx_b["member_recall_ids"] == ["p-scope-B"]
    assert {(c[1], c[2]) for c in eng.calls2} == {("scope-A", "va"), ("scope-B", "vb")}


async def test_owner等於viewer_引擎回空_不登記ids():
    eng = _OwnerEngine(_Shared([]))
    ctx = _ctx(viewer="u1")
    out = await _adapter(eng).execute_tool("member_recall", {"member": "成員甲"}, context=ctx)
    assert out["result"] == "「成員甲」是本伺服器成員，但目前沒有可分享的內容。"
    assert "member_recall_ids" not in ctx


async def test_截斷時ids只含LLM實際看得到的():
    @dataclass
    class _Multi:
        items: list = field(default_factory=list)
        scope_id: str = "s"
        degraded: bool = False
        degraded_reason: str = ""

        @property
        def ids(self):
            return [i.projection_id for i in self.items]

        def to_text(self, display_name=None):
            return "\n".join(["<b>", "note", "head"]
                             + [f"- 成員甲 提過：{'內' * 20}{i.projection_id}" for i in self.items]
                             + ["</b>"])

    eng = _OwnerEngine(_Multi([_Item("p1"), _Item("p2"), _Item("p3")]))
    adapter = _adapter(eng)
    ctx = _ctx()
    # 標題 + 3 行 header + 第一項剛好放得下、第二項被切
    full = (await adapter.execute_tool(
        "member_recall", {"member": "成員甲"}, context=_ctx()))["result"]
    adapter.tool_max_chars = full.index("p1") + len("p1") + 3
    out = await adapter.execute_tool("member_recall", {"member": "成員甲"}, context=ctx)
    assert out["result"].endswith("…")
    assert ctx["member_recall_ids"] == ["p1"]

    ctx2 = _ctx()
    adapter.tool_max_chars = 10
    await adapter.execute_tool("member_recall", {"member": "成員甲"}, context=ctx2)
    assert ctx2.get("member_recall_ids", []) == []


async def test_舊引擎不支援owner_uid_回空不退化成全體查詢():
    eng = _OldEngine()
    out = await EchoMemoryAdapter(engine=eng).retrieve_shared(
        "", scope_id="s", viewer_uid="v", owner_uid="u1")
    assert out is not None and out.text == "" and out.ids == []
    res = await _adapter(eng).execute_tool("member_recall", {"member": "成員甲"}, context=_ctx())
    assert eng.called == 0 and res["success"] is True


async def test_只有kwargs的引擎_視為不支援_回空():
    eng = _KwargsEngine()
    out = await EchoMemoryAdapter(engine=eng).retrieve_shared(
        "", scope_id="s", viewer_uid="v", owner_uid="u1")
    assert out is not None and out.ids == [] and eng.called == 0


async def test_retrieve_shared_有owner_uid時允許空text_沒有時仍回None():
    eng = _OwnerEngine()
    adapter = EchoMemoryAdapter(engine=eng)
    out = await adapter.retrieve_shared("", scope_id="s", viewer_uid="v", owner_uid="u1")
    assert out is not None and out.ids == ["p1", "p2"]
    assert eng.calls2[-1][4] == "u1"
    assert await adapter.retrieve_shared("  ", scope_id="s", viewer_uid="v") is None


def test_工具未啟用時不在schema():
    names = [d["function"]["name"] for d in EchoMemoryAdapter(_OwnerEngine()).tool_definitions()]
    assert "member_recall" not in names


def test_工具啟用且有resolver時出現在schema(monkeypatch):
    monkeypatch.setenv("ECHO_STREAM_MEMORY_TOOLS", "member_recall")
    adapter = EchoMemoryAdapter(_OwnerEngine(), member_resolver=lambda n: "u1")
    defs = adapter.tool_definitions()
    assert [d["function"]["name"] for d in defs] == ["member_recall"]
    params = defs[0]["function"]["parameters"]
    assert params["required"] == ["member"] and "query" in params["properties"]


def test_啟用但未注入resolver_不曝光並只警告一次(monkeypatch, caplog):
    monkeypatch.setenv("ECHO_STREAM_MEMORY_TOOLS", "member_recall")
    adapter = EchoMemoryAdapter(_OwnerEngine())
    with caplog.at_level("WARNING"):
        assert adapter.tool_definitions() == []
        assert adapter.tool_definitions() == []
    assert sum("member_resolver" in r.message for r in caplog.records) == 1


async def test_未啟用時execute_tool擋掉():
    eng = _OwnerEngine()
    adapter = _adapter(eng, tools=("deep_recall",))
    out = await adapter.execute_tool("member_recall", {"member": "成員甲"}, context=_ctx())
    assert out["success"] is False and eng.calls2 == []


# --- think stage 透傳 tool_context ---


async def _drain(stage, utt):
    async for _ in stage.stream(utt, CancellationToken()):
        pass


def _stage(executor):
    backend = ToolBackend(call=_Call("c1", "member_recall", '{"member": "成員甲"}'))
    return LLMThinkStage(backend, tools=[{"x": 1}], tool_executor=executor)


async def test_think_把本輪tool_context傳給接受context的executor():
    seen = []

    async def ex(name, args, *, context=None):
        seen.append(context)
        return {"success": True, "result": "ok"}

    tc = {"member_recall": {"scope_id": "s"}}
    await _drain(_stage(ex), Utterance(text="hi", speaker_id="u", metadata={"tool_context": tc}))
    assert seen and seen[0] is tc


async def test_think_舊executor不收context照舊呼叫():
    seen = []

    async def ex(name, args):
        seen.append((name, args))
        return {"success": True, "result": "ok"}

    await _drain(_stage(ex), Utterance(
        text="hi", speaker_id="u", metadata={"tool_context": {"a": 1}}))
    assert seen == [("member_recall", {"member": "成員甲"})]


async def test_查無投影時_label缺省用這位成員():
    eng = _OwnerEngine(_Shared([]))
    ctx = {"member_recall": {"scope_id": "s", "viewer_uid": "v"}}
    out = await _adapter(eng).execute_tool("member_recall", {"member": "成員甲"}, context=ctx)
    assert out["result"] == "「這位成員」是本伺服器成員，但目前沒有可分享的內容。"


async def test_解析失敗_不說是本伺服器成員():
    eng = _OwnerEngine(_Shared([_Item("p1")]))
    out = await _adapter(eng, resolver=lambda name: None).execute_tool(
        "member_recall", {"member": "成員乙"}, context=_ctx())
    assert out["result"] == "無法確認是哪位成員。"
    assert "本伺服器成員" not in out["result"] and eng.calls2 == []


SHARED_MAX_ITEMS = 2  # _rendered_ids 從結果類別所在模組讀上限


@dataclass
class _RelItem:
    projection_id: str
    relation: str = "owner"


class _SectionedShared:
    """D87 分段結果：items 原始順序交錯，渲染時本人段在前、旁證段在後，各段上限 2。"""

    def __init__(self, items):
        self.items = items
        self.degraded = False
        self.degraded_reason = ""
        self.scope_id = "scope-g1"

    @property
    def ids(self):
        return [it.projection_id for it in self.items]

    @property
    def owner_items(self):
        return [it for it in self.items if it.relation != "about"]

    @property
    def about_items(self):
        return [it for it in self.items if it.relation == "about"]

    def to_text(self, display_name=None):
        lines = [f"- 本人：{it.projection_id}" for it in self.owner_items[:SHARED_MAX_ITEMS]]
        lines += [f"- 旁證：{it.projection_id}" for it in self.about_items[:SHARED_MAX_ITEMS]]
        return "\n".join(lines)


async def test_分段結果_ids依渲染順序且只含實際渲染的項目():
    items = [_RelItem("a1", "about"), _RelItem("o1"), _RelItem("a2", "about"),
             _RelItem("o2"), _RelItem("o3"), _RelItem("a3", "about")]
    ctx = _ctx()
    out = await _adapter(_OwnerEngine(shared=_SectionedShared(items))).execute_tool(
        "member_recall", {"member": "成員甲"}, context=ctx)
    assert out["success"] is True
    # 本人段在前（o1、o2）、旁證段在後（a1、a2）；超過上限沒渲染的 o3、a3 不登記
    assert ctx["member_recall_ids"] == ["o1", "o2", "a1", "a2"]


async def test_分段結果_截斷時ids與可見行一致():
    items = [_RelItem("a1", "about"), _RelItem("o1"), _RelItem("o2"), _RelItem("a2", "about")]
    adapter = _adapter(_OwnerEngine(shared=_SectionedShared(items)))
    full = (await adapter.execute_tool(
        "member_recall", {"member": "成員甲"}, context=_ctx()))["result"]
    adapter.tool_max_chars = full.index("o2") + len("o2") + 2  # 只看得到本人段兩行
    ctx = _ctx()
    await adapter.execute_tool("member_recall", {"member": "成員甲"}, context=ctx)
    assert ctx["member_recall_ids"] == ["o1", "o2"]
