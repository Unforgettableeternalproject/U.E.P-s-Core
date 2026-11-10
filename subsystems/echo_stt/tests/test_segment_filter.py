"""逐段過濾與解碼參數測試——純函式＋假 faster-whisper 模型，無 GPU、不載模型。

規則與預設值來源：Echo-Stream ``stt_discord.py``（commit 56381f6 起）。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from echo_stt.pipeline.segment_filter import (
    SegmentFilterConfig,
    clean_whisper_garbage,
    credit_hallucination,
    english_guard_reason,
    filter_segments,
    hotword_leak_reason,
    is_hallucination,
    outro_hallucination,
    prompt_leak_reason,
    segment_reasons,
)

# 借用 test_engine 的假硬體 fixture
from tests.test_engine import engine_factory, mock_pyaudio  # noqa: F401


def seg(text="你好", start=0.0, end=2.0, nsp=0.1, lp=-0.3, cr=1.2):
    return SimpleNamespace(
        text=text, start=start, end=end, no_speech_prob=nsp, avg_logprob=lp, compression_ratio=cr
    )


class TestDefaults:
    def test_預設值與_bot_一致(self):
        c = SegmentFilterConfig()
        assert c.min_rms == 0.005
        assert c.max_no_speech_prob == 0.7
        assert c.min_avg_logprob == -1.2
        assert c.max_compression_ratio == 2.4
        assert c.outro_max_logprob == -0.6
        assert c.outro_max_duration_s == 1.5
        assert c.english_guard is False

    def test_from_mapping_覆寫與缺值(self):
        c = SegmentFilterConfig.from_mapping({"min_rms": 0.01, "filter_outro": False})
        assert c.min_rms == 0.01 and c.filter_outro is False
        assert c.max_no_speech_prob == 0.7


class TestOutro:
    """三條件同時成立才擋：短段、avg_logprob <= -0.6、文字精確等於套語。"""

    def test_三條件成立_擋下(self):
        assert outro_hallucination("The end.", -0.8, 1.0)
        assert outro_hallucination("See you later!", -0.7, 0.5)

    def test_反例_非短段放行(self):
        assert outro_hallucination("The end", -0.8, 1.6) is None

    def test_反例_logprob_高於門檻放行(self):
        assert outro_hallucination("The end", -0.5, 1.0) is None

    def test_反例_文字不精確等於放行(self):
        assert outro_hallucination("The end of the world", -0.8, 1.0) is None
        assert outro_hallucination("我們下次見 the end", -0.8, 1.0) is None

    def test_logprob_邊界_負0點6_算成立(self):
        assert outro_hallucination("The end", -0.6, 1.0)
        assert outro_hallucination("The end", -0.5999, 1.0) is None

    def test_時長邊界_1點5秒_算成立(self):
        assert outro_hallucination("The end", -0.8, 1.5)
        assert outro_hallucination("The end", -0.8, 1.5001) is None

    def test_logprob_缺值保守放行(self):
        assert outro_hallucination("The end", None, 1.0) is None

    def test_整合進_segment_reasons(self):
        cfg = SegmentFilterConfig()
        assert "outro" in segment_reasons(seg("Have a good day", 0, 1.0, lp=-0.9), cfg)
        assert "outro" not in segment_reasons(seg("Have a good day", 0, 1.0, lp=-0.4), cfg)
        off = SegmentFilterConfig(filter_outro=False)
        assert "outro" not in segment_reasons(seg("Have a good day", 0, 1.0, lp=-0.9), off)


class TestSegmentThresholds:
    def test_no_speech(self):
        cfg = SegmentFilterConfig()
        assert "no_speech" in segment_reasons(seg(nsp=0.71), cfg)
        assert "no_speech" not in segment_reasons(seg(nsp=0.7), cfg)

    def test_avg_logprob(self):
        cfg = SegmentFilterConfig()
        assert "avg_logprob" in segment_reasons(seg(lp=-1.21), cfg)
        assert "avg_logprob" not in segment_reasons(seg(lp=-1.2), cfg)

    def test_compression_ratio(self):
        cfg = SegmentFilterConfig()
        assert "compression_ratio" in segment_reasons(seg(cr=2.41), cfg)
        assert "compression_ratio" not in segment_reasons(seg(cr=2.4), cfg)


class TestCredit:
    @pytest.mark.parametrize("t", ["字幕由 小明 提供", "詞曲 李宗盛", "作詞 作曲 周杰倫", "志願者字幕組"])
    def test_署名格式擋下(self, t):
        assert credit_hallucination(t)

    def test_真實句子放行(self):
        assert credit_hallucination("我很喜歡李宗盛的歌") is None

    def test_整合進_segment_reasons(self):
        assert "credit" in segment_reasons(seg("字幕由 小明 提供"), SegmentFilterConfig())


class TestPromptHotwordLeak:
    def test_prompt_短句精確外洩(self):
        assert prompt_leak_reason("小U", "你好，小U，請幫我", 0.5)
        assert prompt_leak_reason("小U", "你好，小U，請幫我", 2.0) is None

    def test_prompt_覆蓋率外洩(self):
        assert prompt_leak_reason("以下是繁體中文的對話內容", "以下是繁體中文的對話內容", 3.0)
        assert prompt_leak_reason("今天天氣很好我想出門", "以下是繁體中文的對話內容", 3.0) is None

    def test_prompt_空值放行(self):
        assert prompt_leak_reason("任何文字", "", 1.0) is None

    def test_hotword_清單被念出擋下(self):
        words = ["UEP", "U.E.P", "小U", "艾斯維爾"]
        assert hotword_leak_reason("UEP 小U 艾斯維爾", words)

    def test_hotword_少於三個不同詞放行(self):
        assert hotword_leak_reason("UEP 小U", ["UEP", "U.E.P", "小U"]) is None

    def test_hotword_夾雜真實句子放行(self):
        words = ["UEP", "小U", "艾斯維爾"]
        assert hotword_leak_reason("UEP 小U 艾斯維爾 請幫我查一下今天台北的天氣怎麼樣", words) is None

    def test_整合進_segment_reasons(self):
        cfg = SegmentFilterConfig()
        r = segment_reasons(seg("UEP 小U 艾斯維爾"), cfg, hotwords=["UEP", "小U", "艾斯維爾"])
        assert "hotword_leak" in r
        r = segment_reasons(seg("小U", 0, 0.5), cfg, prompt="你好，小U，請幫我")
        assert "prompt_leak" in r


class TestHallucinationAndGarbage:
    @pytest.mark.parametrize("t", ["謝謝觀看", "Thank you.", "請訂閱我的頻道", "Thanks for listening!"])
    def test_黑名單擋下(self, t):
        assert is_hallucination(t)

    def test_真實句子放行(self):
        assert is_hallucination("謝謝你幫我") is None

    def test_garbage_清理(self):
        assert clean_whisper_garbage("你好Ｂ世界") == "你好，世界"
        assert clean_whisper_garbage("a﹑b�中") == "a、b中"
        assert clean_whisper_garbage("Ｂ only english") == "Ｂ only english"  # 不含中文不動


class TestEnglishGuard:
    def test_條件全成立擋下(self):
        assert english_guard_reason("Hello there", 0.3, 0.01, 2, 0.5, 0.02)

    @pytest.mark.parametrize(
        "text,conf,rms",
        [("Hello there friend", 0.3, 0.01), ("Hello there", 0.6, 0.01), ("Hello there", 0.3, 0.05),
         ("你好 hello", 0.3, 0.01)],
    )
    def test_任一條件不成立放行(self, text, conf, rms):
        assert english_guard_reason(text, conf, rms, 2, 0.5, 0.02) is None


# ──────────────────────────────────────────────
#  引擎整合：假 faster-whisper 模型
# ──────────────────────────────────────────────

def _fw_engine(engine_factory, segs, **overrides):
    engine = engine_factory(**overrides)
    model = MagicMock()
    model.transcribe.return_value = (iter(segs), SimpleNamespace(language="zh", language_probability=0.9))
    engine._fw_model = model
    return engine, model


def loud(n=16000):
    return (np.random.default_rng(0).standard_normal(n) * 0.1).astype(np.float32)


class TestEngineIntegration:
    def test_解碼參數_bot_預設(self, engine_factory):
        # fixture 的 recognition 帶舊值，這裡以 yaml 實際預設覆蓋後驗證傳進模型的參數
        from echo_stt.core.config import load_config

        rec = load_config()["recognition"]
        engine, model = _fw_engine(engine_factory, [seg()], recognition=rec)
        engine._transcribe_faster_whisper(loud())
        kw = model.transcribe.call_args.kwargs
        assert kw["condition_on_previous_text"] is False
        assert kw["repetition_penalty"] == 1.0
        assert kw["temperature"] == [0.0, 0.2]
        assert kw["no_speech_threshold"] == 0.6
        assert kw["compression_ratio_threshold"] == 2.4
        assert kw["vad_parameters"]["speech_pad_ms"] == 200
        assert kw["vad_parameters"]["threshold"] == 0.5

    def test_yaml_預設熱詞與_compute_type(self):
        from echo_stt.core.config import load_config

        cfg = load_config()
        assert cfg["whisper"]["compute_type"] == "int8_float16"
        assert cfg["recognition"]["hotwords_max"] == 30
        assert {"UEP", "U.E.P", "小U"} <= set(cfg["recognition"]["hotwords"])
        assert SegmentFilterConfig.from_mapping(cfg["segment_filter"]) == SegmentFilterConfig()

    def test_熱詞與上限(self, engine_factory):
        engine, model = _fw_engine(
            engine_factory, [seg()],
            recognition={"hotwords": ["UEP", "U.E.P", "小U", "uep"], "hotwords_max": 30},
        )
        engine._transcribe_faster_whisper(loud())
        assert model.transcribe.call_args.kwargs["hotwords"] == "UEP, U.E.P, 小U"  # 不分大小寫去重
        engine.set_hotwords([f"w{i}" for i in range(40)])
        assert len(engine._hotwords) == 30

    def test_低能量不送模型(self, engine_factory):
        engine, model = _fw_engine(engine_factory, [seg()])
        out = engine._transcribe_faster_whisper(np.zeros(16000, dtype=np.float32))
        assert out["text"] == ""
        model.transcribe.assert_not_called()
        assert engine.filter_stats["low_energy"] == 1

    def test_rms_門檻邊界(self, engine_factory):
        engine, model = _fw_engine(engine_factory, [seg()])
        engine._transcribe_faster_whisper(np.full(16000, 0.0049, dtype=np.float32))
        model.transcribe.assert_not_called()
        engine._transcribe_faster_whisper(np.full(16000, 0.0051, dtype=np.float32))
        model.transcribe.assert_called_once()

    def test_結尾套語段被丟_其餘保留並計數(self, engine_factory):
        segs = [seg("今天天氣很好", 0, 2.0, lp=-0.3), seg("The end", 2.0, 3.0, lp=-0.9)]
        engine, _ = _fw_engine(engine_factory, segs)
        out = engine._transcribe_faster_whisper(loud())
        assert out["text"] == "今天天氣很好"
        assert engine.filter_stats["outro"] == 1
        assert out["avg_logprob"] == -0.3  # 保留段落中最差的值

    def test_真實道別_高_logprob_放行(self, engine_factory):
        engine, _ = _fw_engine(engine_factory, [seg("See you later", 0, 1.0, lp=-0.4)])
        assert engine._transcribe_faster_whisper(loud())["text"] == "See you later"

    def test_整段幻覺黑名單(self, engine_factory):
        engine, _ = _fw_engine(engine_factory, [seg("謝謝觀看", 0, 2.0, lp=-0.3)])
        assert engine._transcribe_faster_whisper(loud())["text"] == ""
        assert engine.filter_stats["hallucination"] == 1

    def test_全部段落被過濾回空(self, engine_factory):
        engine, _ = _fw_engine(engine_factory, [seg(nsp=0.9)])
        out = engine._transcribe_faster_whisper(loud())
        assert out["text"] == "" and out["confidence"] == 0.0

    def test_filter_segments_累計原因(self):
        stats: dict = {}
        kept = filter_segments([seg(), seg(nsp=0.9), seg(lp=-2.0)], SegmentFilterConfig(), stats=stats)
        assert len(kept) == 1
        assert stats == {"no_speech": 1, "avg_logprob": 1}

    def test_transformers_溫度回退兩階(self, engine_factory):
        engine = engine_factory(whisper={"backend": "transformers"})
        engine.pipe = MagicMock(return_value={"text": "hi", "chunks": []})
        engine._transcribe_transformers(loud())
        gk = engine.pipe.call_args.kwargs["generate_kwargs"]
        assert gk["temperature"] == (0.0, 0.2)
