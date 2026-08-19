"""2.5 語言 conditioning 轉發測試（Codex review pipe-d07fb3ab P1 回歸防護）。

2.5 的 language 不只是正規化選項——lang embedding 直接加進 text embedding，
故未指定語言時必須自動偵測，且必須正確轉發到 engine.synthesize。
"""
from pathlib import Path

from echo_tts.runtime.indextts_generate import default_generation_paths, detect_language
from echo_tts.runtime.indextts_backend import IndexTTS2Backend


def test_detect_language():
    assert detect_language("今天的天氣不錯，接下來要做什麼呢？") == "zh"
    assert detect_language("这个 module 的 refactor 完成了") == "zh"
    assert detect_language("ねえ、聞いて！すごくない？") == "ja"
    assert detect_language("The weather is nice today.") == "en"


class _StubEngine:
    def __init__(self, **kwargs):
        self.synthesize_calls = []

    def load_character(self, path, verbose=True):
        return True

    def synthesize(self, **kwargs):
        self.synthesize_calls.append(kwargs)
        Path(kwargs["output_path"]).write_bytes(b"RIFF")
        return True


def test_backend_auto_detects_chinese(tmp_path):
    root = Path(__file__).resolve().parents[1]
    paths = default_generation_paths(root)

    captured = {}

    def factory(**kwargs):
        engine = _StubEngine(**kwargs)
        captured["engine"] = engine
        return engine

    backend = IndexTTS2Backend(paths, engine_factory=factory)
    backend.generate("今天的天氣不錯，我們來測試語言轉發。", tmp_path / "out.wav")

    call = captured["engine"].synthesize_calls[0]
    assert call["language"] == "zh"


def test_backend_respects_explicit_language(tmp_path):
    root = Path(__file__).resolve().parents[1]
    paths = default_generation_paths(root)

    captured = {}

    def factory(**kwargs):
        engine = _StubEngine(**kwargs)
        captured["engine"] = engine
        return engine

    backend = IndexTTS2Backend(paths, engine_factory=factory)
    backend.generate("Hello world", tmp_path / "out.wav", language="ja")

    call = captured["engine"].synthesize_calls[0]
    assert call["language"] == "ja"
