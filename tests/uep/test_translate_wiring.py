"""Boundary Translator（fake MT 後端，不載入 torch／transformers、不需資料庫）。"""
from __future__ import annotations

import ast
import os
import pathlib
import threading
import time

import pytest

from uep.config import UepSettings
from uep.echo import translate_wiring as tw
from uep.echo.translate_wiring import (
    BoundaryTranslator,
    cjk_dominant,
    create_boundary_translator,
    is_degenerate,
    mt_model_dir,
)


class FakeBackend:
    def __init__(self, fn=None, delay: float = 0.0):
        self.calls: list[str] = []
        self.fn = fn or (lambda t: f"EN({t})")
        self.delay = delay

    def translate(self, text):
        self.calls.append(text)
        if self.delay:
            time.sleep(self.delay)
        return self.fn(text)


def _make(backend=None, **kw) -> tuple[BoundaryTranslator, FakeBackend]:
    backend = backend or FakeBackend()
    tr = BoundaryTranslator(backend_factory=lambda device: backend, device="cpu", **kw)
    tr.start_warmup()
    assert tr.wait_ready(5)
    return tr, backend


def test_alias_replaced_before_translation_and_warmup_runs_inference():
    tr, backend = _make()
    assert backend.calls[:3] == list(tw.WARMUP_SENTENCES)  # warmup 內有假推論
    res = tr.translate("小U，今天會下雨嗎？")
    assert res.status == "ok" and res.text_en == "EN(Yuki，今天會下雨嗎？)"
    assert backend.calls[-1] == "Yuki，今天會下雨嗎？" and "小U" not in backend.calls[-1]
    res2 = tr.translate("嘿 uep 你好嗎")
    assert "Yuki" in backend.calls[-1] and res2.text_en is not None


def test_custom_placeholder_and_aliases():
    tr, backend = _make(aliases=("阿福",), placeholder="Mira")
    tr.translate("阿福你在嗎")
    assert backend.calls[-1] == "Mira你在嗎"


def test_english_and_non_cjk_skipped_but_alias_normalised():
    tr, backend = _make()
    n = len(backend.calls)
    res = tr.translate("hey UEP, what is the weather?")
    assert res.status == "skipped" and res.text_en == "hey Yuki, what is the weather?"
    assert tr.translate("12345 !!!").status == "skipped"
    assert len(backend.calls) == n  # 沒呼叫 MT
    assert cjk_dominant("幫我寄 email") and not cjk_dominant("please 幫 me read this long english sentence")


def test_degenerate_output_is_unavailable():
    assert is_degenerate("the the the the the the the") and is_degenerate("   ")
    assert not is_degenerate("what is the weather like today") and not is_degenerate("no no no")
    bad = FakeBackend(lambda t: "ha ha ha ha ha ha ha ha" if "壞" in t else "ok fine")
    tr, _ = _make(bad)
    res = tr.translate("這句話壞掉了")
    assert res.text_en is None and res.status == "unavailable" and res.reason == "degenerate"


def test_timeout_returns_unavailable_and_later_call_recovers():
    slow = FakeBackend(lambda t: "slow answer here", delay=0.0)
    tr, _ = _make(slow, timeout_ms=50)
    slow.delay = 0.4
    res = tr.translate("你好嗎今天")
    assert res.text_en is None and res.reason == "timeout" and res.latency_ms < 300
    slow.delay = 0.0
    time.sleep(0.5)
    assert tr.translate("再試一次吧").text_en == "slow answer here"


def test_load_failure_is_unavailable_without_raising():
    def boom(device):
        raise RuntimeError("no weights")

    tr = BoundaryTranslator(backend_factory=boom, device="cpu")
    assert tr.translate("你好").text_en is None  # lazy：第一次呼叫觸發背景載入
    assert tr.wait_ready(5) and not tr.available
    res = tr.translate("你好嗎")
    assert res.text_en is None and res.reason == "load_failed"


def test_translate_before_ready_is_unavailable_warming():
    gate = threading.Event()

    def slow_factory(device):
        assert gate.wait(5)
        return FakeBackend()

    tr = BoundaryTranslator(backend_factory=slow_factory, device="cpu")
    tr.start_warmup()
    assert tr.translate("你好").reason == "warming"
    gate.set()
    assert tr.wait_ready(5) and tr.translate("你好").status == "ok"


def test_oom_on_load_falls_back_to_cpu():
    devices: list[str] = []

    def factory(device):
        devices.append(device)
        if device == "cuda":
            raise RuntimeError("CUDA out of memory")
        return FakeBackend()

    tr = BoundaryTranslator(backend_factory=factory, device="cuda", cpu_fallback=True)
    tr.start_warmup()
    assert tr.wait_ready(5) and tr.available and devices == ["cuda", "cpu"]
    off = BoundaryTranslator(backend_factory=factory, device="cuda", cpu_fallback=False)
    off.start_warmup()
    assert off.wait_ready(5) and not off.available


def test_cache_and_empty_input():
    tr, backend = _make()
    tr.translate("今天天氣好嗎？")
    n = len(backend.calls)
    assert tr.translate("今天天氣好嗎？").reason == "cache" and len(backend.calls) == n
    assert tr.translate("   ").text_en is None


def test_factory_reads_settings_and_default_dir(tmp_path):
    st = UepSettings(
        _env_file=None, UEP_TRANSLATE_TIMEOUT_MS="90", UEP_TRANSLATE_ALIASES="阿福", UEP_TRANSLATE_PLACEHOLDER="Mira",
        UEP_TRANSLATE_MODEL_DIR=str(tmp_path),
    )
    backend = FakeBackend()
    tr = create_boundary_translator(st, extra_aliases=("小艾",), backend_factory=lambda d: backend)
    assert tr.placeholder == "Mira" and tr.replace_aliases("阿福和小艾") == "Mira和Mira"
    assert tr.replace_aliases("小U") == "小U"  # 設定了別名清單就不用內建清單
    assert mt_model_dir(str(tmp_path)) == tmp_path
    assert mt_model_dir().parts[-4:] == (".uep", "models", "mt", "opus-mt-zh-en")
    assert UepSettings(_env_file=None).decision_translate is False  # 預設關閉


def test_module_top_level_does_not_import_inference_stack():
    tree = ast.parse(pathlib.Path(tw.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    assert not names & {"torch", "transformers"}


@pytest.mark.skipif(not os.environ.get("UEP_TEST_REAL_MT"), reason="真模型測試：設 UEP_TEST_REAL_MT=1 才跑")
def test_real_marian_model():
    tr = create_boundary_translator(UepSettings(_env_file=None))
    tr.start_warmup()
    assert tr.wait_ready(120) and tr.available
    res = tr.translate("小U，明天台北會下雨嗎？", timeout_ms=3000)
    assert res.status == "ok" and res.text_en and "Yuki" in res.text_en


def test_non_runtime_load_error_does_not_fall_back_to_cpu():
    devices: list[str] = []

    def factory(device):
        devices.append(device)
        raise ImportError("broken install")

    tr = BoundaryTranslator(backend_factory=factory, device="cuda", cpu_fallback=True)
    tr.start_warmup()
    assert tr.wait_ready(5) and not tr.available and devices == ["cuda"]
