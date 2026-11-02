"""EchoMemoryAdapter 的跨使用者分享 API 透傳（假 engine）。"""

from __future__ import annotations

from dataclasses import dataclass, field

from uep.stream.adapters.memory_echo import EchoMemoryAdapter, SharedRetrieval

from .test_memory_echo import _Ctx, _Engine


@dataclass
class _Item:
    projection_id: str


@dataclass
class _Shared:
    items: list = field(default_factory=list)
    scope_id: str = "scope-g1"
    degraded: bool = False
    degraded_reason: str = ""

    @property
    def ids(self):
        return [i.projection_id for i in self.items]

    def to_text(self, display_name=None):
        if not self.items:
            return ""
        name = display_name("u1") if display_name else "成員 u1"
        return f"{name}:" + ",".join(self.ids)


@dataclass
class _Unit:
    id: str
    source_type: str


class _Evidence:
    def __init__(self, units, fail=False):
        self.units, self.fail = units, fail

    def get_by_episode(self, episode_id):
        if self.fail:
            raise RuntimeError("爆了")
        return self.units


class _Layer:
    def __init__(self):
        self.calls = []

    def revoke_owner(self, scope, owner_uid, *, reason_code="member_left", block=True):
        self.calls.append((scope, owner_uid, reason_code, block))
        return "RR"

    def restore_owner(self, scope, owner_uid):
        self.calls.append(("restore", scope, owner_uid))
        return True


class _ShareEngine(_Engine):
    def __init__(self):
        super().__init__(_Ctx())
        self.calls2: dict[str, list] = {}
        self.fail = False
        self.shared = _Shared([_Item("p1"), _Item("p2")])
        self.share_layer = _Layer()
        self.evidence = _Evidence([_Unit("u_user", "user"), _Unit("u_agent", "agent")])
        self.store_kw: dict = {}

    def _rec(self, name, *a, **kw):
        if self.fail:
            raise RuntimeError("爆了")
        self.calls2.setdefault(name, []).append((a, kw))

    def retrieve_shared(self, query, **kw):
        self._rec("retrieve_shared", query, **kw)
        return self.shared

    def restrict_unit(self, unit_id, **kw):
        self._rec("restrict_unit", unit_id, **kw)
        return {"unit_id": unit_id, "restricted": True}

    def scan_privacy_intent(self, unit_ids, **kw):
        self._rec("scan_privacy_intent", unit_ids, **kw)
        return {"scanned": len(unit_ids)}

    def store_episode(self, **kw):
        self.store_kw = kw
        return "ep1"


async def test_retrieve_shared_透傳並回文字與ids():
    eng = _ShareEngine()
    out = await EchoMemoryAdapter(engine=eng).retrieve_shared(
        "問題", scope_id="scope-g1", viewer_uid="v", present_uids=["a", "b"],
        display_name=lambda uid: "小明",
    )
    assert isinstance(out, SharedRetrieval)
    assert out.text == "小明:p1,p2" and out.ids == ["p1", "p2"] and not out.degraded
    assert eng.calls2["retrieve_shared"] == [
        (("問題",), {"scope_id": "scope-g1", "viewer_uid": "v", "present_uids": ("a", "b")})
    ]


async def test_retrieve_shared_空結果文字為空字串與degraded透出():
    eng = _ShareEngine()
    eng.shared = _Shared([], degraded=True, degraded_reason="index_missing")
    out = await EchoMemoryAdapter(engine=eng).retrieve_shared(
        "q", scope_id="s", viewer_uid="v")
    assert out.text == "" and out.ids == []
    assert out.degraded and out.degraded_reason == "index_missing"


async def test_restrict_unit_與_scan_透傳():
    eng = _ShareEngine()
    a = EchoMemoryAdapter(engine=eng)
    assert (await a.restrict_unit("u1"))["restricted"] is True
    await a.restrict_unit("u2", reason_code="x", triggered_by="slash")
    assert eng.calls2["restrict_unit"] == [
        (("u1",), {"reason_code": "user_restrict", "triggered_by": "conversation"}),
        (("u2",), {"reason_code": "x", "triggered_by": "slash"}),
    ]
    assert await a.scan_privacy_intent(["u1"], force=True) == {"scanned": 1}
    assert eng.calls2["scan_privacy_intent"] == [((["u1"],), {"timeout": 20.0, "force": True})]
    assert await a.scan_privacy_intent([]) is None


async def test_revoke_owner_透傳share_layer():
    eng = _ShareEngine()
    out = await EchoMemoryAdapter(engine=eng).revoke_owner(
        "scope", "u9", reason_code="left", block=False)
    assert out == "RR"
    assert eng.share_layer.calls == [("scope", "u9", "left", False)]


async def test_restore_owner_透傳share_layer():
    eng = _ShareEngine()
    assert await EchoMemoryAdapter(engine=eng).restore_owner("scope", "u9") is True
    assert eng.share_layer.calls == [("restore", "scope", "u9")]
    assert await EchoMemoryAdapter(engine=_Engine(_Ctx())).restore_owner("s", "u") is None
    eng.share_layer.restore_owner = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    assert await EchoMemoryAdapter(engine=eng).restore_owner("s", "u") is None


async def test_引擎缺方法回None():
    a = EchoMemoryAdapter(engine=_Engine(_Ctx()))
    assert await a.retrieve_shared("q", scope_id="s", viewer_uid="v") is None
    assert await a.restrict_unit("u") is None
    assert await a.scan_privacy_intent(["u"]) is None
    assert await a.revoke_owner("s", "u") is None


async def test_例外降級回None():
    eng = _ShareEngine()
    eng.fail = True
    eng.share_layer.revoke_owner = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    a = EchoMemoryAdapter(engine=eng)
    assert await a.retrieve_shared("q", scope_id="s", viewer_uid="v") is None
    assert await a.restrict_unit("u") is None
    assert await a.scan_privacy_intent(["u"]) is None
    assert await a.revoke_owner("s", "u") is None


async def test_store_透傳shared欄位並補unit_ids():
    eng = _ShareEngine()
    adapter = EchoMemoryAdapter(engine=eng)
    adapter.assess_mode = "off"
    out = await adapter.store(
        "你好", "嗨", shared_context_ids=["p1"], shared_scope_id="scope-g1",
    )
    assert eng.store_kw["shared_context_ids"] == ["p1"]
    assert eng.store_kw["shared_scope_id"] == "scope-g1"
    assert out["episode_id"] == "ep1"
    assert out["unit_ids"] == ["u_user"]


async def test_store_evidence_scope_與attrs轉成EvidenceInput():
    import os

    import pytest

    snapshot = dict(os.environ)  # import echo_memory 會載入 .env，事後還原避免污染
    try:
        pytest.importorskip("echo_memory.schemas.evidence")
        from echo_memory.schemas.evidence import EvidenceInput
    finally:
        os.environ.clear()
        os.environ.update(snapshot)

    class _EvEngine(_ShareEngine):
        def store_episode(self, user_input, agent_response, session_id, *, evidence=None, **kw):
            self.store_kw = {"evidence": evidence, **kw}
            return "ep1"

    eng = _EvEngine()
    adapter = EchoMemoryAdapter(engine=eng)
    adapter.assess_mode = "off"
    await adapter.store(
        "你好", "嗨", "s1",
        evidence=[{"source_type": "user", "speaker_id": "42", "content": "你好",
                   "scope": "scope-g1", "attrs": {"channel_id": "c1"}}],
    )
    (ev,) = eng.store_kw["evidence"]
    assert isinstance(ev, EvidenceInput)
    assert ev.scope == "scope-g1" and ev.attrs == {"channel_id": "c1"}
