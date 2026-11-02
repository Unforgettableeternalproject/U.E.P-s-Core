"""一致性掃描修正的回歸測試（取消路徑、成對引號、常數來源、設定快取、整潔項）。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import sys
import types
from pathlib import Path

import pytest

from uep.stream.adapters.memory_echo import EchoMemoryAdapter
from uep.stream.adapters.speak_indextts import (
    CONFIG_STRETCH_RATIO,
    IndexTTS2SpeakStage,
    sanitize_for_tts,
    speed_multiplier_to_offset,
)
from uep.stream.adapters.think_llm import EchoBackend, LLMThinkStage
from uep.stream.contracts.cancellation import CancellationToken
from uep.stream.channel import ChannelClosed, StreamChannel
from tests.uep.stream.conftest import FakeTTSBackend, sentence_stream

np = pytest.importorskip("numpy", reason="adapter 的音訊轉換需要 numpy")


# --- B2：分支指引 ---


def test_不再指引切到_spike_分支():
    root = Path(__file__).resolve().parents[3]
    files = [
        root / "uep" / "stream" / "adapters" / "memory_echo.py",
    ]
    for f in files:
        assert "agent-memory-spike" not in f.read_text(encoding="utf-8"), f


# --- D1：取消路徑一律標記為中止 ---


@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: asyncio.CancelledError(),
        lambda: concurrent.futures.CancelledError(),
        lambda: ChannelClosed("通道已關閉"),
    ],
)
async def test_put失敗的取消類例外轉成_Aborted(monkeypatch, exc_factory):
    seen: list[BaseException] = []

    def boom(channel, item, loop):
        raise exc_factory()

    monkeypatch.setattr(StreamChannel, "put_threadsafe", boom)

    class Backend(FakeTTSBackend):
        def generate(self, text, output_path, on_segment_audio=None, **kwargs):
            try:
                on_segment_audio(np.zeros(128, dtype="float32"), 0, 1)
            except BaseException as exc:  # noqa: BLE001 - 檢查 backend 實際收到什麼
                seen.append(exc)
                raise

    stage = IndexTTS2SpeakStage(backend=Backend(), queue_size=1)
    chunks = [c async for c in stage.stream(sentence_stream(["一句。"]), CancellationToken())]
    assert chunks == []
    assert len(seen) == 1
    # backend（TTS lite_engine）只認 is_cancellation 屬性
    assert getattr(seen[0], "is_cancellation", False) is True


# --- D2：成對引號保留 ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("自稱為「神」的男人", "自稱為「神」的男人"),
        ("他說『你好』然後走了", "他說『你好』然後走了"),
        ("「甲」和「乙」", "「甲」和「乙」"),
        ("孤兒「引號沒關", "孤兒引號沒關"),
        ("只有關」", "只有關"),
        ("錯配「甲』", "錯配甲"),
        ("空的「」引號", "空的引號"),
        ("巢狀「外『內』外」", "巢狀外『內』外"),
        ("《書名》和（括號）", "書名和括號"),
    ],
)
def test_成對引號保留_其餘照刪(raw, expected):
    assert sanitize_for_tts(raw) == expected


def test_過長內文的引號不保留():
    inner = "甲" * 31
    assert sanitize_for_tts(f"「{inner}」") == inner
    ok = "甲" * 30
    assert sanitize_for_tts(f"「{ok}」") == f"「{ok}」"


# --- D5：常數改讀來源 ---


def test_語速換算優先用引擎回報的_config_stretch():
    assert speed_multiplier_to_offset(2.0, 1.0) == pytest.approx(1.0)  # 2×1.0×(1-0.5)
    assert speed_multiplier_to_offset(1.2) == speed_multiplier_to_offset(
        1.2, CONFIG_STRETCH_RATIO
    )


def test_stage_從引擎讀_config_stretch():
    class Backend(FakeTTSBackend):
        engine = object()

        def get_speed_info(self):
            return {"config_stretch_ratio": 1.5}

    assert IndexTTS2SpeakStage(backend=Backend())._engine_config_stretch_ratio() == 1.5
    # 引擎未載入（backend 回寫死的 1.7）不可信 → None 走常數
    Backend.engine = None
    assert IndexTTS2SpeakStage(backend=Backend())._engine_config_stretch_ratio() is None
    # 假 backend 沒有 get_speed_info → None
    assert IndexTTS2SpeakStage(backend=FakeTTSBackend())._engine_config_stretch_ratio() is None


def test_蒸餾門檻讀來源模組_失敗才fallback(monkeypatch):
    pkg = types.ModuleType("echo_memory")
    dream = types.ModuleType("echo_memory.dream")
    dist = types.ModuleType("echo_memory.dream.distillation")

    class KnowledgeDistillation:
        MIN_SALIENCE_THRESHOLD = 0.8

    dist.KnowledgeDistillation = KnowledgeDistillation
    monkeypatch.setitem(sys.modules, "echo_memory", pkg)
    monkeypatch.setitem(sys.modules, "echo_memory.dream", dream)
    monkeypatch.setitem(sys.modules, "echo_memory.dream.distillation", dist)
    assert EchoMemoryAdapter._distill_min_salience() == 0.8

    monkeypatch.setitem(sys.modules, "echo_memory.dream.distillation", None)  # import 失敗
    assert EchoMemoryAdapter._distill_min_salience() == EchoMemoryAdapter.DISTILL_MIN_SALIENCE


# --- D6：不改寫 STT 設定快取 ---


def test_STT_adapter不改寫load_config的全域快取(monkeypatch):
    shared = {"multi_channel": {"enabled": False}}
    received: list[dict] = []

    core = types.ModuleType("echo_stt.core")
    cfg_mod = types.ModuleType("echo_stt.core.config")
    cfg_mod.load_config = lambda: shared
    eng_mod = types.ModuleType("echo_stt.core.multi_channel_engine")

    class MultiChannelSTTEngine:
        def __init__(self, config):
            received.append(config)

        def initialize(self):
            return True

    eng_mod.MultiChannelSTTEngine = MultiChannelSTTEngine
    for name, mod in {
        "echo_stt": types.ModuleType("echo_stt"),
        "echo_stt.core": core,
        "echo_stt.core.config": cfg_mod,
        "echo_stt.core.multi_channel_engine": eng_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)

    from uep.stream.adapters.input_stt import _EchoSttBackend

    _EchoSttBackend()
    assert shared["multi_channel"]["enabled"] is False, "全域快取被改寫"
    assert received[0]["multi_channel"]["enabled"] is True


# --- 整潔：並行安全探針 ---


class _EngineBase:
    storage_lock = None


def test_並行安全探針看ensure_initialized而非warmup():
    class OnlyWarmup(_EngineBase):
        def warmup(self, background=True):
            return None

    class Modern(OnlyWarmup):
        def ensure_initialized(self):
            return None

    assert EchoMemoryAdapter(engine=OnlyWarmup())._is_fine_grained() is False
    assert EchoMemoryAdapter(engine=Modern())._is_fine_grained() is True


# --- 整潔：think stage aclose 關閉 backend 資源 ---


async def test_aclose關閉backend的async_client():
    closed: list[str] = []

    class Client:
        async def close(self):
            closed.append("async")

    class Backend(EchoBackend):
        _async_client = Client()

    await LLMThinkStage(backend=Backend()).aclose()
    assert closed == ["async"]


async def test_aclose優先用公開aclose且不碰同步client():
    calls: list[str] = []

    class SyncClient:
        def close(self):
            calls.append("sync")

    class Backend(EchoBackend):
        _client = SyncClient()

        async def aclose(self):
            calls.append("aclose")

    await LLMThinkStage(backend=Backend()).aclose()
    assert calls == ["aclose"]

    class Only(EchoBackend):
        _client = SyncClient()

    calls.clear()
    await LLMThinkStage(backend=Only()).aclose()
    assert calls == [], "同步 client 與記憶 llm_fn 共用，不得關閉"
