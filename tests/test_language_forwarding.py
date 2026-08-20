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
        self.profile_calls = []
        self._init_timings = {}

    def load_character(self, path, verbose=True):
        return True

    def synthesize(self, **kwargs):
        self.synthesize_calls.append(kwargs)
        Path(kwargs["output_path"]).write_bytes(b"RIFF")
        return True

    def synthesize_with_profile(self, **kwargs):
        self.profile_calls.append(kwargs)
        Path(kwargs["output_path"]).write_bytes(b"RIFF")
        return True, {}


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


def test_profile_generate_forwards_language(tmp_path):
    """profile_generate 必須與 generate 同樣轉發 language。

    引擎 synthesize_with_profile 預設 'en'——不轉發時中文 profiling 會吃
    錯誤 lang embedding + 英文分段器，分段行為與 shell 實際狀況脫節。
    """
    root = Path(__file__).resolve().parents[1]
    paths = default_generation_paths(root)

    captured = {}

    def factory(**kwargs):
        engine = _StubEngine(**kwargs)
        captured["engine"] = engine
        return engine

    backend = IndexTTS2Backend(paths, engine_factory=factory)
    backend.profile_generate("今天的天氣不錯，測試 profiling 語言轉發。", tmp_path / "p.wav")
    assert captured["engine"].profile_calls[0]["language"] == "zh"

    backend.profile_generate("Hello world", tmp_path / "p2.wav", language="ja")
    assert captured["engine"].profile_calls[1]["language"] == "ja"


def test_cli_detect_language_japanese():
    """Shell 層語言偵測：假名優先於 CJK 比例，日文不再被判成 other/zh。

    2.5 原生支援日語——shell 偵測到 ja 應直通合成，而非機器翻譯。
    """
    from echo_tts.cli.interactive import _detect_language, SUPPORTED_LANGUAGES

    assert "ja" in SUPPORTED_LANGUAGES
    # 假名為主的句子
    assert _detect_language("ほら、やっぱり成功したじゃない！") == "ja"
    # 漢字+假名混合（假名優先，不被 CJK 比例搶走）
    assert _detect_language("信じてくれなかったくせに、もう！") == "ja"
    # 漢字密集、假名極少（舊 10% 門檻會漏判成 zh）
    assert _detect_language("本日午後三時、新宿駅集合の予定。") == "ja"
    assert _detect_language("東京大学入学試験問題集を購入。") == "ja"
    # 長文中假名被標點/數字稀釋（比例式門檻的另一個漏洞）
    assert _detect_language("「二〇二六年八月二十日、午前十時三十分——第三會議室。」と書かれた。") == "ja"
    # 純中文與英文不受影響
    assert _detect_language("今天的天氣真的很不錯。") == "zh"
    assert _detect_language("The weather is nice today.") == "en"
    # 韓文仍為 other（引擎不支援）
    assert _detect_language("안녕하세요 오늘 날씨가 좋네요") == "other"
