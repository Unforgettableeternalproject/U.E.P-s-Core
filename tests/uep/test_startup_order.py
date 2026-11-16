"""啟動順序：VOX（speak）、think、STT 都 prepare 完，Laya／翻譯／NLP／記憶 embedding 的載入才開始（不需資料庫、不載任何模型）。"""
from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from uep.console import real_rig
from uep.echo import host as host_mod
from uep.echo import laya_wiring
from uep.echo.host import EchoPipelineHost
from uep.turn.decision_config import DecisionConfig
from uep.turn.models import SessionContext
from uep.turn.proposer import RespondProposer

CTX = SessionContext(conversation_id="c1", segment_id="s1", audience_scope="aud", session_ref="sess")


class _Stage:
    """記錄 prepare 事件的假 stage；``fail`` 時 prepare 拋例外（模擬沒有被守衛降級的失敗）。"""

    def __init__(self, name: str, events: list[str], fail: bool = False) -> None:
        self.name, self.events, self.fail = name, events, fail

    async def prepare(self) -> None:
        self.events.append(f"{self.name}.prepare.start")
        await asyncio.sleep(0)
        if self.fail:
            self.events.append(f"{self.name}.prepare.failed")
            raise RuntimeError("prepare 失敗")
        self.events.append(f"{self.name}.prepare.end")


class _Loadable:
    """帶 start_warmup 的假載入單元（像 LayaProposer／NlpLegacyProposer）。"""

    def __init__(self, name: str, events: list[str]) -> None:
        self.name, self.events = name, events

    def start_warmup(self) -> None:
        self.events.append(f"{self.name}.load")


class _FakePrimary(RespondProposer):
    """像 LayaRulesProposer：內層 Laya 在 ``.laya``。"""

    proposer_id = "laya.primary"

    def __init__(self, laya: _Loadable) -> None:
        self.laya = laya


def _make_host(monkeypatch, events: list[str], *, fail_speak: bool = False, shadows: tuple[str, ...] = ()):
    created: list[tuple[str, dict]] = []
    loadables = {"laya.primary": _Loadable("laya", events), "nlp.legacy": _Loadable("nlp", events)}

    def fake_create(pid: str, **kw):
        created.append((pid, kw))
        if pid == "laya.primary":
            return _FakePrimary(loadables[pid])
        if pid == "nlp.legacy":
            return loadables[pid]
        return RespondProposer()

    monkeypatch.setattr(host_mod, "create_proposer", fake_create)
    cfg = replace(DecisionConfig(), proposer_id="laya.primary", shadow_proposer_ids=shadows)
    host = EchoPipelineHost(
        publisher=None, think=_Stage("think", events), speak=_Stage("speak", events, fail_speak),  # type: ignore[arg-type]
        sink_factory=lambda *a: "x", session=CTX, decision=cfg, input_stage=_Stage("stt", events),
    )
    return host, created


def test_laya_與_nlp_建構時延後載入且在_stt_prepare_之後才啟動(monkeypatch):
    events: list[str] = []
    host, created = _make_host(monkeypatch, events, shadows=("nlp.legacy",))
    try:
        assert created and all(kw.get("defer_warmup") is True for _pid, kw in created)
        assert events == []  # 建構期不載任何東西
        host.add_post_start_hook(lambda: events.append("embedding.warm"))
        asyncio.run(host._startup())
        assert events == [
            "speak.prepare.start", "speak.prepare.end",  # VOX 先單獨完成
            "think.prepare.start", "think.prepare.end",
            "stt.prepare.start", "stt.prepare.end",  # STT 載入也不與延後的載入重疊
            "laya.load", "nlp.load", "embedding.warm",  # 整段 prepare 之後才開始其他載入
        ]
    finally:
        host.orchestrator.close()


def test_speak_prepare_失敗時其他元件仍會啟動且例外照常上拋(monkeypatch):
    events: list[str] = []
    host, _ = _make_host(monkeypatch, events, fail_speak=True)
    try:
        host.add_post_start_hook(lambda: events.append("embedding.warm"))
        with pytest.raises(RuntimeError, match="prepare 失敗"):
            asyncio.run(host._startup())
        assert events == ["speak.prepare.start", "speak.prepare.failed", "laya.load", "embedding.warm"]
    finally:
        host.orchestrator.close()


def test_延後啟動的例外只記_log_不擋其他元件(monkeypatch):
    events: list[str] = []
    host, _ = _make_host(monkeypatch, events)
    try:
        def boom() -> None:
            raise RuntimeError("hook 失敗")

        host.add_post_start_hook(boom)
        host.add_post_start_hook(lambda: events.append("embedding.warm"))
        asyncio.run(host._startup())
        assert events[-2:] == ["laya.load", "embedding.warm"] and "stt.prepare.end" in events
    finally:
        host.orchestrator.close()


def test_呼叫端明確傳入的_proposer_不被延後(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(host_mod, "create_proposer", lambda pid, **kw: RespondProposer())
    host = EchoPipelineHost(
        publisher=None, think=_Stage("think", events), speak=_Stage("speak", events),  # type: ignore[arg-type]
        sink_factory=lambda *a: "x", session=CTX, proposer=RespondProposer(), shadow_proposers=(),
    )
    try:
        assert host._deferred_warmups == []
    finally:
        host.orchestrator.close()


def test_laya_延後載入時不開背景執行緒_start_warmup_才載入(monkeypatch):
    monkeypatch.setattr(laya_wiring, "laya_available", lambda: True)
    started: list[int] = []
    monkeypatch.setattr(laya_wiring.LayaProposer, "_load", lambda self: started.append(1) or self._ready.set())
    proposer = laya_wiring.LayaProposer(autoload=False)
    assert started == [] and proposer._thread is None and not proposer.loaded
    proposer.start_warmup()
    assert proposer.wait_ready(5) and started == [1]
    proposer.start_warmup()  # 冪等
    assert started == [1]


def test_registry_工廠把_defer_warmup_傳給_laya(monkeypatch):
    seen: list[bool] = []

    class FakeLaya:
        pass

    def fake_create(cfg=None, settings=None, defer_warmup=False, **_):
        seen.append(defer_warmup)
        return FakeLaya()

    monkeypatch.setattr(laya_wiring, "create_laya_proposer", fake_create)
    monkeypatch.setattr(laya_wiring, "LayaRulesProposer", lambda laya, rules: (laya, rules))
    laya_wiring.create_laya_primary_proposer(defer_warmup=True)
    laya_wiring.create_laya_primary_proposer()
    assert seen == [True, False]


# ---- 記憶 embedding ----

def test_warm_engine_factory_延後模式建_engine_不暖機_start_warm_才在背景暖機(monkeypatch):
    calls: list[tuple[str, str]] = []
    done = threading.Event()

    def fake_warm(model: str, device: str) -> None:
        calls.append((model, device))
        done.set()

    monkeypatch.setattr("uep.echo.memory_wiring.warmup_embedding", fake_warm, raising=False)
    warmups: dict[str, float] = {}
    factory = real_rig.WarmEngineFactory(
        lambda ns, root: object(), warmups, model="BAAI/bge-m3", device="cpu", backend="sentence-transformers", defer=True,
    )
    factory("ns1", Path("x"))
    assert calls == [] and warmups == {}  # 建 engine（memory.start）時不載 bge-m3
    factory.start_warm()
    assert done.wait(5) and calls == [("BAAI/bge-m3", "cpu")]


def test_embedding_暖機失敗只降級不拋出(monkeypatch):
    def boom(model: str, device: str) -> None:
        raise RuntimeError("載入失敗")

    monkeypatch.setattr("uep.echo.memory_wiring.warmup_embedding", boom, raising=False)
    warmups: dict[str, float] = {}
    factory = real_rig.WarmEngineFactory(lambda ns, root: object(), warmups, model="m", device="cpu", backend=None, defer=True)
    factory.warm()  # 不拋
    assert "memory_embedding" in warmups


def test_echo_engine_factory_延後模式關掉_engine_自動暖機(monkeypatch, tmp_path):
    import echo_memory

    from uep.echo.memory_wiring import EchoEngineFactory

    seen: list[dict] = []
    monkeypatch.setattr(echo_memory, "MemoryEngine", lambda **kw: seen.append(kw) or object())
    EchoEngineFactory(defer_embedding_warmup=True)("ns", tmp_path)
    EchoEngineFactory()("ns", tmp_path)
    assert seen[0]["warmup_embedding"] is False and "warmup_embedding" not in seen[1]


def test_think_prepare_失敗時延後的載入仍會啟動(monkeypatch):
    events: list[str] = []
    host, _ = _make_host(monkeypatch, events)
    try:
        host.think.fail = True
        host.add_post_start_hook(lambda: events.append("embedding.warm"))
        with pytest.raises(RuntimeError, match="prepare 失敗"):
            asyncio.run(host._startup())
        assert events == [
            "speak.prepare.start", "speak.prepare.end", "think.prepare.start", "think.prepare.failed",
            "laya.load", "embedding.warm",
        ]
    finally:
        host.orchestrator.close()
