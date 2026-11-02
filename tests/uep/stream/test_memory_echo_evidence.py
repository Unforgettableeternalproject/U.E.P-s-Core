"""EchoMemoryAdapter 的 evidence 透傳與檢索降級旗標（假 engine，不碰真資料）。"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from uep.stream.adapters.memory_echo import EchoMemoryAdapter

from .test_memory_echo import _Ctx, _Ep, _StoreEngine, fake_affect  # noqa: F401

# import echo_memory 會在套件初始化時載入 .env 進 os.environ，
# 污染其他測試（例如 test_config 讀到真的 SYSTEM_PROMPT）——import 前後還原環境變數
_environ_snapshot = dict(os.environ)
try:
    pytest.importorskip("echo_memory.schemas.evidence")
    import echo_memory.schemas as _schemas  # noqa: E402
    import echo_memory.schemas.evidence as _evidence  # noqa: E402
finally:
    os.environ.clear()
    os.environ.update(_environ_snapshot)

EvidenceInput = _evidence.EvidenceInput
SourceRef = _evidence.SourceRef
TextSpan = _evidence.TextSpan

_AT = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


class _EvidenceEngine(_StoreEngine):
    """store_episode 有 evidence 參數的新版引擎。"""

    def store_episode(self, user_input, agent_response, session_id, *, evidence=None, **kw):
        self.stored.append(
            {"user_input": user_input, "agent_response": agent_response,
             "session_id": session_id, "evidence": evidence, **kw}
        )
        return "ep_test"


def _units():
    return [
        {
            "source_type": "user", "speaker_id": "42", "content": "我在學薩克斯風",
            "occurred_at": _AT,
            "source_ref": {"kind": "discord_voice", "channel": "voice", "ref_ids": ["t1"]},
        },
        {
            "source_type": "agent", "speaker_id": "agent", "content": "好棒。",
            "delivery_state": "partial", "delivery_unit": "sentence",
            "delivered_units": 1, "total_units": 2,
            "text_span": {"start": 0, "end": 3, "total": 8},
        },
    ]


async def test_dict_evidence_轉成_EvidenceInput_傳給引擎():
    engine = _EvidenceEngine()
    adapter = EchoMemoryAdapter(engine=engine)
    adapter.assess_mode = "off"
    out = await adapter.store("我在學薩克斯風", "好棒。", "s1", origin="voice", evidence=_units())
    kw = engine.stored[0]
    assert out["episode_id"] == "ep_test" and kw["origin"] == "voice"
    user, agent = kw["evidence"]
    assert isinstance(user, EvidenceInput) and isinstance(agent, EvidenceInput)
    assert user.source_ref == SourceRef(kind="discord_voice", channel="voice", ref_ids=("t1",))
    assert agent.text_span == TextSpan(0, 3, 8) and agent.delivery_state == "partial"
    assert agent.delivered_units == 1


async def test_EvidenceInput_物件原樣傳遞():
    engine = _EvidenceEngine()
    adapter = EchoMemoryAdapter(engine=engine)
    adapter.assess_mode = "off"
    unit = EvidenceInput(source_type="user", content="問")
    await adapter.store("問", "答", evidence=[unit])
    assert engine.stored[0]["evidence"] == [unit]


async def test_舊版引擎沒有_evidence_參數就不傳():
    engine = _StoreEngine()   # store_episode(**kw)：沒有明列 evidence
    adapter = EchoMemoryAdapter(engine=engine)
    adapter.assess_mode = "off"
    out = await adapter.store("問", "答", origin="voice", evidence=_units())
    kw = engine.stored[0]
    assert out["episode_id"] == "ep_test"
    assert "evidence" not in kw and kw["origin"] == "voice"


async def test_沒帶_evidence_時不多傳參數():
    engine = _EvidenceEngine()
    adapter = EchoMemoryAdapter(engine=engine)
    adapter.assess_mode = "off"
    await adapter.store("問", "答")
    assert engine.stored[0]["evidence"] is None


async def test_evidence_無法解析退回不帶來源_仍然存檔():
    engine = _EvidenceEngine()
    adapter = EchoMemoryAdapter(engine=engine)
    adapter.assess_mode = "off"
    out = await adapter.store("問", "答", evidence=[{"content": "缺 source_type"}])
    assert out["episode_id"] == "ep_test"
    assert engine.stored[0]["evidence"] is None


async def test_引擎拒絕不一致的_evidence_store_回None():
    class _Strict(_EvidenceEngine):
        def store_episode(self, *a, **kw):
            raise ValueError("user_input 與 evidence 中 user unit 的內容不一致")

    adapter = EchoMemoryAdapter(engine=_Strict())
    adapter.assess_mode = "off"
    assert await adapter.store("問", "答", evidence=_units()) is None


async def test_evidence_不影響_salience_評估(fake_affect, monkeypatch):  # noqa: F811
    # fake_affect 會把 echo_memory 換成假模組；把真正的 schemas 放回去讓轉換仍可 import
    monkeypatch.setitem(sys.modules, "echo_memory.schemas", _schemas)
    monkeypatch.setitem(sys.modules, "echo_memory.schemas.evidence", _evidence)
    engine = _EvidenceEngine()
    adapter = EchoMemoryAdapter(engine=engine, llm_fn=lambda p: "{}")
    adapter.assess_mode = "llm"
    out = await adapter.store("我在學薩克斯風", "好棒。", "s1", evidence=_units())
    kw = engine.stored[0]
    assert (kw["salience"], kw["da_weight"], kw["ht_weight"]) == (0.82, 0.6, 0.4)
    assert kw["assessment"] == {"novelty": 0.9}
    assert len(kw["evidence"]) == 2 and out["salience"] == 0.82
    assert fake_affect["neurochem_updates"] == 1


# ── 檢索降級旗標 ──


@dataclass
class _DegradedCtx(_Ctx):
    degraded: bool = False
    degraded_reason: str = ""


class _RetrieveEngine:
    def __init__(self, ctx):
        self._ctx = ctx

    def retrieve(self, text, **kw):
        return self._ctx


async def test_retrieve_記錄引擎的降級旗標():
    ctx = _DegradedCtx(degraded=True, degraded_reason="embedding 逾時")
    adapter = EchoMemoryAdapter(engine=_RetrieveEngine(ctx))
    assert adapter.last_retrieve_degraded is False
    assert await adapter.retrieve("問題") is None   # 降級的空結果行為不變
    assert adapter.last_retrieve_degraded is True
    assert adapter.last_degraded_reason == "embedding 逾時"


async def test_retrieve_下一次正常會清掉旗標():
    first = _DegradedCtx(degraded=True, degraded_reason="x")
    adapter = EchoMemoryAdapter(engine=_RetrieveEngine(first))
    await adapter.retrieve("a")
    adapter.engine._ctx = _DegradedCtx(
        episodes=[_Ep("a", "攀岩")], similarities={"a": 0.9},
    )
    adapter.min_match = 0.45
    assert await adapter.retrieve("b") == "攀岩:0.90"
    assert adapter.last_retrieve_degraded is False and adapter.last_degraded_reason == ""


async def test_retrieve_舊版_context_沒有降級欄位視為正常():
    adapter = EchoMemoryAdapter(engine=_RetrieveEngine(_Ctx()))
    await adapter.retrieve("問題")
    assert adapter.last_retrieve_degraded is False


class _IdemEngine(_StoreEngine):
    """store_episode 明確宣告 idempotency_key 的新版引擎。"""

    def store_episode(self, user_input, agent_response, session_id, *,
                      idempotency_key=None, **kw):
        self.stored.append({"idempotency_key": idempotency_key, **kw})
        return "ep_test"


class _OldEngine(_StoreEngine):
    """舊版引擎：簽名沒有 idempotency_key（也沒有 **kwargs）。"""

    def store_episode(self, user_input, agent_response, session_id):
        self.stored.append({"user_input": user_input})
        return "ep_old"


async def test_idempotency_key_有參數時透傳():
    engine = _IdemEngine()
    adapter = EchoMemoryAdapter(engine=engine)
    adapter.assess_mode = "off"
    await adapter.store("問", "答", "s1", idempotency_key="k1")
    assert engine.stored[0]["idempotency_key"] == "k1"


async def test_idempotency_key_為_None_不傳():
    engine = _StoreEngine()
    adapter = EchoMemoryAdapter(engine=engine)
    adapter.assess_mode = "off"
    await adapter.store("問", "答", "s1")
    assert "idempotency_key" not in engine.stored[0]


async def test_idempotency_key_舊版引擎不傳():
    engine = _OldEngine()
    adapter = EchoMemoryAdapter(engine=engine)
    adapter.assess_mode = "off"
    out = await adapter.store("問", "答", "s1", idempotency_key="k1")
    assert out["episode_id"] == "ep_old"


async def test_activity_profile_正常回傳():
    engine = _StoreEngine()
    rows = [{"name": "Hades", "play_count": 2, "total_minutes": 90.0, "last_played": "x"}]
    engine.activity_profile = lambda top_n=3: rows[:top_n]
    adapter = EchoMemoryAdapter(engine=engine)
    assert await adapter.activity_profile(top_n=1) == rows


async def test_activity_profile_引擎無此方法回空():
    adapter = EchoMemoryAdapter(engine=_StoreEngine())
    assert await adapter.activity_profile() == []


async def test_activity_profile_例外回空():
    engine = _StoreEngine()

    def boom(top_n=3):
        raise RuntimeError("壞了")

    engine.activity_profile = boom
    adapter = EchoMemoryAdapter(engine=engine)
    assert await adapter.activity_profile() == []


# ── warmup：embedding 暖機入口 ───────────────────────────────────


class _WarmupEngine:
    """有公開 warmup 的引擎。"""

    def __init__(self) -> None:
        self.warmed = 0

    def warmup(self) -> None:
        self.warmed += 1


class _EpisodicOnlyEngine:
    """只有 episodic 屬性（存取即觸發背景暖機）的舊版引擎。"""

    def __init__(self) -> None:
        self.episodic_loads = 0

    @property
    def episodic(self) -> object:
        self.episodic_loads += 1
        return object()


@pytest.mark.asyncio
async def test_warmup_uses_engine_warmup_when_present():
    engine = _WarmupEngine()
    adapter = EchoMemoryAdapter(engine=engine)
    assert await adapter.warmup() is True
    assert engine.warmed == 1
    await adapter.aclose()


@pytest.mark.asyncio
async def test_warmup_falls_back_to_episodic_access():
    engine = _EpisodicOnlyEngine()
    adapter = EchoMemoryAdapter(engine=engine)
    assert await adapter.warmup() is True
    assert engine.episodic_loads == 1
    await adapter.aclose()


@pytest.mark.asyncio
async def test_warmup_returns_false_when_nothing_to_warm():
    adapter = EchoMemoryAdapter(engine=object())
    assert await adapter.warmup() is False
    await adapter.aclose()


@pytest.mark.asyncio
async def test_warmup_swallows_exception(caplog):
    class _Boom:
        def warmup(self) -> None:
            raise RuntimeError("embedding 後端不可用")

    adapter = EchoMemoryAdapter(engine=_Boom())
    assert await adapter.warmup() is False
    assert "暖機失敗" in caplog.text
    await adapter.aclose()


async def test_retrieve_記錄引擎分段耗時():
    ctx = _DegradedCtx(degraded=True, degraded_reason="x")
    ctx.timings_ms = {"embed_ms": 2001.5, "search_ms": 3.0}
    adapter = EchoMemoryAdapter(engine=_RetrieveEngine(ctx))
    assert adapter.last_retrieve_timings_ms == {}
    await adapter.retrieve("問題")
    assert adapter.last_retrieve_timings_ms == {"embed_ms": 2001.5, "search_ms": 3.0}
    # 舊版 context 沒有 timings_ms：清成空 dict，不殘留上一次的值
    adapter.engine._ctx = _Ctx()
    await adapter.retrieve("問題")
    assert adapter.last_retrieve_timings_ms == {}
