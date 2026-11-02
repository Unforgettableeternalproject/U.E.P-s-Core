"""sanitize_for_tts 的顏文字過濾：正例要清乾淨、反例不可誤刪。"""

from __future__ import annotations

import pytest

from uep.stream.adapters.speak_indextts import IndexTTS2SpeakStage, sanitize_for_tts
from uep.stream.contracts.cancellation import CancellationToken
from tests.uep.stream.conftest import FakeTTSBackend, sentence_stream


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("測試成功。 ￣▽￣ノ", "測試成功。"),
        ("都正常。 ￣▽￣b", "都正常。"),
        ("好喔(・ω・)", "好喔"),
        ("太棒了 (ﾉ◕ヮ◕)ﾉ*:・ﾟ✧", "太棒了"),
        ("加油╰(*°▽°*)╯", "加油"),
        ("衝啊 (ง •̀ω•́)ง", "衝啊"),
        ("你好ヾ(≧▽≦*)o 今天", "你好 今天"),
        ("哈哈ゞ 好", "哈哈 好"),
        ("謝謝 (^_^)", "謝謝"),
        ("唉 (T_T)", "唉"),
        ("好 >_< 啦", "好 啦"),
        ("真的假的 ^^", "真的假的"),
        ("開心 ヽ(´▽`)ノ", "開心"),
        ("晚安 ★彡 ♪", "晚安 彡"),
        ("哈哈 😀👍", "哈哈"),
        ("（╯°□°）╯︵ ┻━┻ 氣死", "氣死"),
    ],
)
def test_顏文字被清掉(raw, expected):
    assert sanitize_for_tts(raw) == expected


@pytest.mark.parametrize(
    "text",
    [
        "OK b 計畫",
        "我喜歡 C++ 和 Rust",
        "A/B 測試結果不錯",
        "在 2024 年 (Q3) 上線",
        "他說（笑）然後走了",
        "see (Figure 1) and (e.g. this)",
        "今天 30°C 很熱",
        "this is a b c test",
        "(user id) 是欄位",
        "x ^ 2 的結果",
        "well-known game",
    ],
)
def test_正常文字不被誤刪(text):
    # 括號與引號本來就會被 sanitize 剝掉，所以拿「剝括號後」的結果比對：內容必須完整保留
    stripped = text.replace("(", "").replace(")", "").replace("（", "").replace("）", "")
    assert sanitize_for_tts(text) == " ".join(stripped.split())


async def test_只剩顏文字的句子不送去合成():
    backend = FakeTTSBackend(segments_per_sentence=1)
    stage = IndexTTS2SpeakStage(backend=backend)
    chunks = [
        c
        async for c in stage.stream(
            sentence_stream(["￣▽￣ノ。", "(・ω・)", "。！", "你好。"]), CancellationToken()
        )
    ]
    assert backend.completed == ["你好。"]
    assert len(chunks) == 1
