"""retract_about_me／opt_out_about_me／correct_about_me：假引擎驗包裝、ref 轉換與結果文字。

actor／scope 只來自宿主 context；旁證編號由 member_recall 登記到 context["about_refs"]。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from uep.stream.adapters.memory_echo import (
    ABOUT_BAD_REF,
    ABOUT_ME_TOOLS,
    ABOUT_NO_CONTEXT,
    ABOUT_NO_REF,
    ABOUT_UNSUPPORTED,
    DEFAULT_TOOLS,
    EchoMemoryAdapter,
)
from uep.stream.adapters.think_llm import TOOLS_PROMPT, build_tools_prompt

from .test_memory_echo import _Ctx, _Engine
from .test_memory_echo_sharing import _Item


@dataclass
class _RefShared:
    """帶 about_refs() 的 SharedContext 替身；渲染成「- 〔旁證N〕…」一項一行。"""

    items: list = field(default_factory=list)
    refs: dict = field(default_factory=dict)
    scope_id: str = "scope-g1"
    degraded: bool = False
    degraded_reason: str = ""

    @property
    def ids(self):
        return [i.projection_id for i in self.items]

    def about_refs(self):
        return dict(self.refs)

    def to_text(self, display_name=None):
        return "\n".join(
            f"- 〔旁證{n}〕{'字' * 10}" for n in sorted(self.refs)
        ) or ""


@dataclass
class _NoRefShared:
    """沒有 about_refs 的舊版 SharedContext。"""

    items: list = field(default_factory=lambda: [_Item("p1")])
    scope_id: str = "scope-g1"
    degraded: bool = False
    degraded_reason: str = ""

    @property
    def ids(self):
        return [i.projection_id for i in self.items]

    def to_text(self, display_name=None):
        return "- 一行"


class _AboutEngine(_Engine):
    def __init__(self, shared=None):
        super().__init__(_Ctx())
        self.shared = shared or _RefShared(
            [_Item("p1"), _Item("p2")], {1: "proj-a", 2: "proj-b"})
        self.calls3: list[tuple] = []
        self.retract_result = {"ok": True, "reason": "ok", "projection_id": "proj-a"}
        self.opt_result = {"ok": True, "reason": "ok", "enabled": True}
        self.correct_result = {"ok": True, "reason": "ok", "projection_id": "proj-a",
                               "correction_stored": True, "episode_id": "e1"}

    def retrieve_shared(self, query, *, scope_id, viewer_uid, present_uids=(), owner_uid=None):
        return self.shared

    def retract_about_me(self, scope_id, actor_uid, projection_id):
        self.calls3.append(("retract", scope_id, actor_uid, projection_id))
        return self.retract_result

    def opt_out_about_me(self, scope_id, actor_uid, enabled):
        self.calls3.append(("opt_out", scope_id, actor_uid, enabled))
        return self.opt_result

    def correct_about_me(self, scope_id, actor_uid, projection_id, correction_text, *,
                         evidence=None, session_id=None):
        self.calls3.append(("correct", scope_id, actor_uid, projection_id, correction_text,
                            evidence, session_id))
        return self.correct_result


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("ECHO_STREAM_MEMORY_TOOLS", raising=False)


def _adapter(engine, tools=("member_recall",) + ABOUT_ME_TOOLS):
    adapter = EchoMemoryAdapter(engine=engine)
    adapter.enabled_tools = list(tools)
    adapter.set_member_resolver(lambda name: "actor1" if name == "本人" else "u_other")
    return adapter


def _mctx():
    """member_recall 脈絡 + 明確的 about_me（actor 判定只看 about_me）。"""
    return {"member_recall": {"scope_id": "scope-g1", "viewer_uid": "actor1"},
            "about_me": {"scope_id": "scope-g1", "actor_uid": "actor1"}}


def _ctx(refs=None, **about):
    cfg = {"scope_id": "scope-g1", "actor_uid": "actor1", "session_id": "sess1"}
    cfg.update(about)
    ctx = {"about_me": cfg}
    if refs is not None:
        ctx["about_refs"] = refs
    return ctx


# --- 預設關閉與 schema ---

def test_預設工具清單不含關於我工具():
    assert not set(DEFAULT_TOOLS.split(",")) & set(ABOUT_ME_TOOLS)
    names = [d["function"]["name"] for d in EchoMemoryAdapter(_AboutEngine()).tool_definitions()]
    assert not set(names) & set(ABOUT_ME_TOOLS)


def test_明列後曝光_參數沒有subject或uid(monkeypatch):
    monkeypatch.setenv("ECHO_STREAM_MEMORY_TOOLS", ",".join(ABOUT_ME_TOOLS))
    defs = EchoMemoryAdapter(_AboutEngine()).tool_definitions()
    assert [d["function"]["name"] for d in defs] == list(ABOUT_ME_TOOLS)
    for d in defs:
        props = d["function"]["parameters"]["properties"]
        assert not {"subject", "uid", "actor_uid", "user_id", "projection_id"} & set(props)
    by = {d["function"]["name"]: d["function"]["parameters"] for d in defs}
    assert by["retract_about_me"]["required"] == ["ref"]
    assert by["opt_out_about_me"]["required"] == ["enabled"]
    assert by["correct_about_me"]["required"] == ["ref", "correction_text"]


async def test_未啟用時execute_tool擋掉():
    eng = _AboutEngine()
    out = await _adapter(eng, tools=("deep_recall",)).execute_tool(
        "retract_about_me", {"ref": 1}, context=_ctx({1: "proj-a"}))
    assert out["success"] is False and eng.calls3 == []


def test_prompt規則只在曝光時加入():
    assert build_tools_prompt(["deep_recall"]) == TOOLS_PROMPT
    assert "retract_about_me" in build_tools_prompt(["correct_about_me"])


# --- ref 轉換與成功 ---

async def test_撤回_ref轉projection_id_actor來自context():
    eng = _AboutEngine()
    out = await _adapter(eng).execute_tool(
        "retract_about_me", {"ref": 2}, context=_ctx({1: "proj-a", 2: "proj-b"}))
    assert eng.calls3 == [("retract", "scope-g1", "actor1", "proj-b")]
    assert out["success"] is True and "已撤回" in out["result"]


async def test_撤回_重複撤回already_hidden():
    eng = _AboutEngine()
    eng.retract_result = {"ok": True, "reason": "already_hidden", "projection_id": "proj-a"}
    out = await _adapter(eng).execute_tool(
        "retract_about_me", {"ref": 1}, context=_ctx({1: "proj-a"}))
    assert out["success"] is True and "已經處理過" in out["result"]


async def test_退出與恢復_不需ref():
    eng = _AboutEngine()
    adapter = _adapter(eng)
    out = await adapter.execute_tool("opt_out_about_me", {"enabled": True}, context=_ctx())
    assert eng.calls3 == [("opt_out", "scope-g1", "actor1", True)]
    assert out["success"] is True and "最近在玩" in out["result"]
    eng.opt_result = {"ok": True, "reason": "ok", "enabled": False}
    out = await adapter.execute_tool("opt_out_about_me", {"enabled": False}, context=_ctx())
    assert "已恢復" in out["result"]


async def test_退出_enabled型別不對_不呼叫引擎():
    eng = _AboutEngine()
    out = await _adapter(eng).execute_tool(
        "opt_out_about_me", {"enabled": "yes"}, context=_ctx())
    assert out["success"] is False and eng.calls3 == []


async def test_糾正_帶session_id與更正文字():
    eng = _AboutEngine()
    out = await _adapter(eng).execute_tool(
        "correct_about_me", {"ref": 1, "correction_text": " 我其實不玩那個 "},
        context=_ctx({1: "proj-a"}))
    assert eng.calls3 == [("correct", "scope-g1", "actor1", "proj-a", "我其實不玩那個",
                           None, "sess1")]
    assert out["success"] is True and "已保存" in out["result"]


async def test_糾正_correction_stored為False_明說未保存需重送():
    eng = _AboutEngine()
    eng.correct_result = {"ok": True, "reason": "ok", "projection_id": "proj-a",
                          "correction_stored": False, "episode_id": None}
    out = await _adapter(eng).execute_tool(
        "correct_about_me", {"ref": 1, "correction_text": "更正"}, context=_ctx({1: "proj-a"}))
    assert out["success"] is True
    assert "未保存" in out["result"] and "重送" in out["result"]


async def test_糾正_空文字不呼叫引擎():
    eng = _AboutEngine()
    out = await _adapter(eng).execute_tool(
        "correct_about_me", {"ref": 1, "correction_text": "  "}, context=_ctx({1: "proj-a"}))
    assert out["success"] is False and eng.calls3 == []


# --- 被拒絕、不支援、脈絡缺漏 ---

@pytest.mark.parametrize("tool,args,attr", [
    ("retract_about_me", {"ref": 1}, "retract_result"),
    ("opt_out_about_me", {"enabled": True}, "opt_result"),
    ("correct_about_me", {"ref": 1, "correction_text": "x"}, "correct_result"),
])
async def test_被拒絕_保留reason(tool, args, attr):
    eng = _AboutEngine()
    setattr(eng, attr, {"ok": False, "reason": "not_owner"})
    out = await _adapter(eng).execute_tool(tool, args, context=_ctx({1: "proj-a"}))
    assert out["success"] is False
    assert "not_owner" in out["result"] and out["result"] != ABOUT_UNSUPPORTED


async def test_糾正_no_actor_engine_reason原樣回報():
    eng = _AboutEngine()
    eng.correct_result = {"ok": False, "reason": "no_actor_engine"}
    out = await _adapter(eng).execute_tool(
        "correct_about_me", {"ref": 1, "correction_text": "x"}, context=_ctx({1: "proj-a"}))
    assert "no_actor_engine" in out["result"]


@pytest.mark.parametrize("tool,args", [
    ("retract_about_me", {"ref": 1}),
    ("opt_out_about_me", {"enabled": True}),
    ("correct_about_me", {"ref": 1, "correction_text": "x"}),
])
async def test_引擎沒這個方法_回不支援(tool, args):
    eng = _Engine(_Ctx())  # 沒有三個方法
    out = await _adapter(eng).execute_tool(tool, args, context=_ctx({1: "proj-a"}))
    assert out == {"success": False, "result": ABOUT_UNSUPPORTED}


async def test_引擎丟例外_回失敗而非不支援():
    eng = _AboutEngine()

    def boom(*a, **k):
        raise RuntimeError("x")

    eng.retract_about_me = boom
    out = await _adapter(eng).execute_tool(
        "retract_about_me", {"ref": 1}, context=_ctx({1: "proj-a"}))
    assert out["success"] is False and "engine_error" in out["result"]


@pytest.mark.parametrize("context", [None, {}, {"about_me": {"scope_id": "s"}},
                                      {"about_me": {"actor_uid": "a"}}])
async def test_脈絡缺漏_不碰引擎(context):
    eng = _AboutEngine()
    out = await _adapter(eng).execute_tool("opt_out_about_me", {"enabled": True}, context=context)
    assert out == {"success": False, "result": ABOUT_NO_CONTEXT} and eng.calls3 == []


async def test_沒有about_me_不退回member_recall設定_fail_closed():
    eng = _AboutEngine()
    ctx = {"member_recall": {"scope_id": "scope-g9", "viewer_uid": "v9"},
           "about_refs": {1: "proj-a"}}
    adapter = _adapter(eng)
    for tool, args in (("opt_out_about_me", {"enabled": True}),
                       ("retract_about_me", {"ref": 1}),
                       ("correct_about_me", {"ref": 1, "correction_text": "x"})):
        out = await adapter.execute_tool(tool, args, context=ctx)
        assert out == {"success": False, "result": ABOUT_NO_CONTEXT}
    assert eng.calls3 == []


async def test_member_recall沒有about_me_不登記refs():
    ctx = {"member_recall": {"scope_id": "scope-g1", "viewer_uid": "actor1"}}
    await _adapter(_AboutEngine()).execute_tool("member_recall", {"member": "本人"}, context=ctx)
    assert "about_refs" not in ctx


async def test_adapter缺省用self():
    eng = _AboutEngine()
    await _adapter(eng).execute_tool("opt_out_about_me", {"enabled": True}, context=_ctx())
    assert eng.calls3 == [("opt_out", "scope-g1", "actor1", True)]


async def test_工具參數塞uid無效_actor仍取自context():
    eng = _AboutEngine()
    await _adapter(eng).execute_tool(
        "opt_out_about_me", {"enabled": True, "actor_uid": "evil", "subject": "evil"},
        context=_ctx())
    assert eng.calls3[0][2] == "actor1"


# --- ref 缺失 ---

async def test_ref不存在_拒絕():
    eng = _AboutEngine()
    out = await _adapter(eng).execute_tool(
        "retract_about_me", {"ref": 9}, context=_ctx({1: "proj-a"}))
    assert out == {"success": False, "result": ABOUT_BAD_REF} and eng.calls3 == []


@pytest.mark.parametrize("ref", [None, "abc", ""])
async def test_ref不是整數_拒絕(ref):
    eng = _AboutEngine()
    out = await _adapter(eng).execute_tool(
        "retract_about_me", {"ref": ref}, context=_ctx({1: "proj-a"}))
    assert out["success"] is False and eng.calls3 == []


@pytest.mark.parametrize("refs", [None, {}])
async def test_about_refs缺失_回無法指定對象_不猜id(refs):
    eng = _AboutEngine()
    adapter = _adapter(eng)
    for tool, args in (("retract_about_me", {"ref": 1}),
                       ("correct_about_me", {"ref": 1, "correction_text": "x"})):
        out = await adapter.execute_tool(tool, args, context=_ctx(refs))
        assert out == {"success": False, "result": ABOUT_NO_REF}
    assert eng.calls3 == []


# --- about_refs 的來源：retrieve_shared／member_recall ---

async def test_retrieve_shared帶回about_refs():
    res = await _adapter(_AboutEngine()).retrieve_shared(
        "q", scope_id="scope-g1", viewer_uid="v1")
    assert res.about_refs == {1: "proj-a", 2: "proj-b"}


async def test_舊版SharedContext沒有about_refs_得空dict():
    res = await _adapter(_AboutEngine(_NoRefShared())).retrieve_shared(
        "q", scope_id="scope-g1", viewer_uid="v1")
    assert res.about_refs == {}


async def test_member_recall登記about_refs到context_再撤回():
    eng = _AboutEngine()
    adapter = _adapter(eng)
    ctx = _mctx()
    out = await adapter.execute_tool("member_recall", {"member": "本人"}, context=ctx)
    assert "〔旁證1〕" in out["result"]
    assert ctx["about_refs"] == {1: "proj-a", 2: "proj-b"}
    out = await adapter.execute_tool("retract_about_me", {"ref": 1}, context=ctx)
    assert out["success"] is True
    assert eng.calls3 == [("retract", "scope-g1", "actor1", "proj-a")]


async def test_member_recall舊SharedContext_不登記refs_撤回被拒():
    eng = _AboutEngine(_NoRefShared())
    adapter = _adapter(eng)
    ctx = _mctx()
    await adapter.execute_tool("member_recall", {"member": "本人"}, context=ctx)
    assert ctx["about_refs"] == {}
    out = await adapter.execute_tool("retract_about_me", {"ref": 1}, context=ctx)
    assert out == {"success": False, "result": ABOUT_NO_REF}


async def test_截斷時只登記LLM看得到的旁證編號_ids與行數不變():
    items = [_Item(f"p{i}") for i in range(1, 7)]
    refs = {i: f"proj-{i}" for i in range(1, 7)}
    eng = _AboutEngine(_RefShared(items, refs))
    adapter = _adapter(eng)
    adapter.tool_max_chars = 120
    ctx = _mctx()
    out = await adapter.execute_tool("member_recall", {"member": "本人"}, context=ctx)
    complete = out["result"].split(chr(10))[:-1]
    shown = [n for n in refs if any(f"〔旁證{n}〕" in ln for ln in complete)]
    assert 0 < len(shown) < 6
    assert set(ctx["about_refs"]) == set(shown)
    # 「- 」開頭的完整行數 = 登記的 ids 數：新格式不破壞既有截斷對位
    assert ctx["member_recall_ids"] == [f"p{n}" for n in shown]


async def test_先查本人再查別人_refs保持本人那份():
    eng = _AboutEngine()
    adapter = _adapter(eng)
    ctx = _mctx()
    await adapter.execute_tool("member_recall", {"member": "本人"}, context=ctx)
    mine = dict(ctx["about_refs"])
    assert mine == {1: "proj-a", 2: "proj-b"}
    eng.shared = _RefShared([_Item("q1")], {1: "other-x"})
    await adapter.execute_tool("member_recall", {"member": "別人"}, context=ctx)
    assert ctx["about_refs"] == mine


async def test_查本人兩次_後者整份覆蓋前者():
    eng = _AboutEngine()
    adapter = _adapter(eng)
    ctx = _mctx()
    await adapter.execute_tool("member_recall", {"member": "本人"}, context=ctx)
    eng.shared = _RefShared([_Item("q1")], {1: "proj-new"})
    await adapter.execute_tool("member_recall", {"member": "本人"}, context=ctx)
    assert ctx["about_refs"] == {1: "proj-new"}


async def test_只查別人_不建立about_refs():
    ctx = _mctx()
    await _adapter(_AboutEngine()).execute_tool(
        "member_recall", {"member": "別人"}, context=ctx)
    assert "about_refs" not in ctx


async def test_糾正_evidence取自host_context_不來自LLM參數():
    eng = _AboutEngine()
    ev = [{"source_type": "user_statement", "channel_kind": "guild", "audience_size": 5}]
    import uep.stream.adapters.memory_echo as m

    seen = []
    orig = m._coerce_evidence
    m._coerce_evidence = lambda item: (seen.append(item), item)[1]
    try:
        await _adapter(eng).execute_tool(
            "correct_about_me",
            {"ref": 1, "correction_text": "x", "evidence": [{"evil": 1}]},
            context=_ctx({1: "proj-a"}, evidence=ev))
    finally:
        m._coerce_evidence = orig
    assert seen == ev
    assert eng.calls3[0][5] == ev


async def test_糾正_沒有evidence_傳None():
    eng = _AboutEngine()
    await _adapter(eng).execute_tool(
        "correct_about_me", {"ref": 1, "correction_text": "x", "evidence": [1]},
        context=_ctx({1: "proj-a"}))
    assert eng.calls3[0][5] is None


async def test_糾正_already_hidden且correction_stored為False_仍要求重送():
    eng = _AboutEngine()
    eng.correct_result = {"ok": True, "reason": "already_hidden", "projection_id": "proj-a",
                          "correction_stored": False, "episode_id": None}
    out = await _adapter(eng).execute_tool(
        "correct_about_me", {"ref": 1, "correction_text": "x"}, context=_ctx({1: "proj-a"}))
    assert "未保存" in out["result"] and "重送" in out["result"]


@pytest.mark.parametrize("ref,expected", [
    (1, "proj-a"), (1.0, "proj-a"), ("2", "proj-b"), (" 2 ", "proj-b"),
    (True, None), (False, None), (1.9, None), ("1.0", None), ("-1", None),
    ("一", None), ("１", None), ([1], None), ({}, None),
])
async def test_ref解析(ref, expected):
    eng = _AboutEngine()
    out = await _adapter(eng).execute_tool(
        "retract_about_me", {"ref": ref}, context=_ctx({1: "proj-a", 2: "proj-b"}))
    if expected is None:
        assert out == {"success": False, "result": ABOUT_BAD_REF} and eng.calls3 == []
    else:
        assert eng.calls3[0][3] == expected


async def test_糾正_宿主沒給session_id_傳None_不回退adapter的session():
    eng = _AboutEngine()
    adapter = _adapter(eng)
    adapter._session_id = "scope-adapter-session"
    ctx = {"about_me": {"scope_id": "scope-g1", "actor_uid": "actor1"}, "about_refs": {1: "proj-a"}}
    await adapter.execute_tool("correct_about_me", {"ref": 1, "correction_text": "x"}, context=ctx)
    assert eng.calls3[0][6] is None
