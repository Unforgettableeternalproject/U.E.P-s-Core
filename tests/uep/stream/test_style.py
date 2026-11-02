"""SpeechStyle 契約與 adapter 翻譯的測試。"""

from __future__ import annotations

import pytest

from uep.stream.adapters.speak_indextts import (
    CONFIG_STRETCH_RATIO,
    speed_multiplier_to_offset,
)
from uep.stream.contracts.style import EMOTION_DIMENSIONS, NEUTRAL, SpeechStyle

# --- 情緒向量 ---


def test_八個維度的順序固定():
    """順序沿用 IndexTTS 2.5 的 emotion vector，adapter 的轉換才是零成本的。"""
    assert EMOTION_DIMENSIONS == (
        "happy", "angry", "sad", "afraid",
        "disgusted", "melancholic", "surprised", "calm",
    )


def test_轉成八維向量():
    style = SpeechStyle(emotion={"happy": 0.3, "calm": 0.7})
    assert style.emotion_vector() == [0.3, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.7]


def test_未指定的維度補零():
    assert SpeechStyle(emotion={"sad": 1.0}).emotion_vector()[2] == 1.0
    assert sum(SpeechStyle(emotion={"sad": 1.0}).emotion_vector()) == 1.0


def test_超出範圍的值被夾住():
    style = SpeechStyle(emotion={"happy": 5.0, "sad": -3.0})
    assert style.normalized_emotion()["happy"] == 1.0
    assert style.normalized_emotion()["sad"] == 0.0


def test_未知的維度被忽略():
    """LLM 可能吐出我們沒定義的情緒名稱，不該讓管線掛掉。"""
    style = SpeechStyle(emotion={"happy": 0.5, "怒氣沖沖": 0.9})
    assert style.emotion_vector()[0] == 0.5
    assert len(style.emotion_vector()) == 8


def test_中性判定():
    assert NEUTRAL.is_neutral
    assert SpeechStyle(emotion={"happy": 0.0}).is_neutral
    assert not SpeechStyle(emotion={"happy": 0.1}).is_neutral


def test_取最強情緒():
    """給只支援單一情緒標籤的後端用。"""
    style = SpeechStyle(emotion={"happy": 0.2, "sad": 0.8})
    assert style.dominant() == ("sad", 0.8)


def test_中性時沒有最強情緒():
    assert NEUTRAL.dominant() is None


# --- 情緒 vs 表現力是兩個軸 ---


def test_情緒與表現力可獨立設定():
    """「很悲傷但幾乎沒表現出來」與「只有一點高興但演得很誇張」
    是兩種不同的東西，多數後端只給一個旋鈕。"""
    suppressed = SpeechStyle(emotion={"sad": 0.8}, intensity=0.8, expressiveness=0.2)
    theatrical = SpeechStyle(emotion={"happy": 0.4}, intensity=0.4, expressiveness=0.9)

    assert suppressed.expressiveness < theatrical.expressiveness
    assert suppressed.emotion_vector()[2] > theatrical.emotion_vector()[0]


def test_表現力預設不指定():
    """None 表示交給後端決定，不是 0。"""
    assert NEUTRAL.expressiveness is None


# --- 合併 ---


def test_句級覆寫_turn級預設():
    base = SpeechStyle(emotion={"calm": 0.8}, speed=1.0, style="conversational")
    override = SpeechStyle(emotion={"surprised": 0.9}, speed=1.2)
    merged = base.merged(override)

    assert merged.emotion["calm"] == 0.8
    assert merged.emotion["surprised"] == 0.9
    assert merged.speed == 1.2
    assert merged.style == "conversational"


def test_合併_None_回傳自己():
    base = SpeechStyle(emotion={"calm": 0.5})
    assert base.merged(None) is base


def test_style_是不可變的():
    """同一個 style 會被多個句子共用，可變會出事。"""
    style = SpeechStyle()
    with pytest.raises(Exception):  # noqa: B017 - frozen dataclass
        style.speed = 2.0  # type: ignore[misc]


# --- 語速換算 ---


def test_倍率_1_對應偏移_0():
    assert speed_multiplier_to_offset(1.0) == 0.0


def test_變快是正偏移():
    """引擎裡 stretch = 1.72 - offset×0.5，stretch 越小唸得越快。"""
    assert speed_multiplier_to_offset(1.2) > 0


def test_變慢是負偏移():
    assert speed_multiplier_to_offset(0.8) < 0


def test_換算回去的_stretch_符合倍率():
    multiplier = 1.25
    offset = speed_multiplier_to_offset(multiplier)
    stretch = CONFIG_STRETCH_RATIO - offset * 0.5
    assert abs(stretch - CONFIG_STRETCH_RATIO / multiplier) < 1e-9


def test_偏移被夾在合法範圍():
    """語速設過頭該「頂到底」，不該讓整個 turn 掛掉。"""
    assert speed_multiplier_to_offset(100.0) == 1.0
    assert speed_multiplier_to_offset(0.01) == -1.0


def test_零或負倍率不爆炸():
    assert speed_multiplier_to_offset(0.0) == 0.0
    assert speed_multiplier_to_offset(-1.0) == 0.0


# --- adapter 套用 ---


async def test_句子帶_style_會套到_backend(monkeypatch):
    import sys

    from uep.stream.adapters.speak_indextts import IndexTTS2SpeakStage
    from uep.stream.contracts.cancellation import CancellationToken
    from uep.stream.contracts.types import Sentence

    pytest.importorskip("numpy")
    from tests.uep.stream.conftest import FakeTTSBackend as FakeBackend

    # 擋掉 echo_tts，驗的是退回路徑（向量 × 強度）；真換算見 test_speak_indextts
    monkeypatch.setitem(sys.modules, "echo_tts.config.emotion_presets", None)
    backend = FakeBackend(segments_per_sentence=1)
    stage = IndexTTS2SpeakStage(backend=backend)

    async def stream():
        yield Sentence(
            text="這句要很開心。",
            turn_id="t",
            index=0,
            is_first=True,
            is_last=True,
            style=SpeechStyle(emotion={"happy": 0.9}, intensity=1.0),
        )

    _ = [c async for c in stage.stream(stream(), CancellationToken())]
    # intensity（emo_alpha）的翻譯是向量 × 強度，這裡設 1.0 驗原值
    assert backend.emotion_vector[0] == pytest.approx(0.9)


async def test_沒有_style_時不動_backend():
    """預設音色是既有資產，adapter 不該無故覆寫。"""
    from uep.stream.adapters.speak_indextts import IndexTTS2SpeakStage
    from uep.stream.contracts.cancellation import CancellationToken
    from uep.stream.contracts.types import Sentence

    pytest.importorskip("numpy")
    from tests.uep.stream.conftest import FakeTTSBackend as FakeBackend

    backend = FakeBackend(segments_per_sentence=1)
    backend.emotion_vector = None
    stage = IndexTTS2SpeakStage(backend=backend)

    async def stream():
        yield Sentence(text="普通一句。", turn_id="t", index=0, is_first=True, is_last=True)

    _ = [c async for c in stage.stream(stream(), CancellationToken())]
    assert backend.emotion_vector is None
