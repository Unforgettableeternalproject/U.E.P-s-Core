"""SentenceSplitter 的測試。

切分是整個管線最大的槓桿，切錯的後果分兩種：切太細語調破碎、
切太粗 TTFA 拉長。這裡測的是「切點判定正確」，聽感要用耳朵測。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from uep.stream.contracts.cancellation import CancellationToken, CancelReason
from uep.stream.splitter import (
    SentenceSplitter,
    SplitPolicy,
    char_weight,
    text_weight,
)


async def chars(text: str) -> AsyncIterator[str]:
    """逐字元模擬 token 流。"""
    for ch in text:
        yield ch


async def collect(text: str, policy: SplitPolicy | None = None) -> list[str]:
    splitter = SentenceSplitter(policy)
    return [
        s.text async for s in splitter.split(chars(text), "t") if s.text
    ]


async def collect_full(text: str, policy: SplitPolicy | None = None):
    splitter = SentenceSplitter(policy)
    return [s async for s in splitter.split(chars(text), "t")]


# --- 權重 ---


def test_中文字權重高於英文字母():
    """閾值代表的是發音時長，不是字元數——不換算會讓英文被切得極短。"""
    assert char_weight("好") == 1.0
    assert char_weight("a") == 0.4
    assert char_weight("，") == 0.0


def test_日文假名與中文同權重():
    assert char_weight("あ") == 1.0
    assert char_weight("カ") == 1.0


# --- 基本切分 ---


async def test_全形句末標點立即切():
    result = await collect(
        "你好。今天天氣真好。", SplitPolicy(first_min_weight=1.0, min_weight=1.0)
    )
    assert result == ["你好。", "今天天氣真好。"]


async def test_全形問號驚嘆號():
    result = await collect("真的嗎？太好了！", SplitPolicy(first_min_weight=1.0, min_weight=1.0))
    assert result == ["真的嗎？", "太好了！"]


async def test_結尾引號跟著句子走():
    result = await collect(
        '他說「好啊."後面繼續講一些別的內容。', SplitPolicy(first_min_weight=1.0, min_weight=1.0)
    )
    assert result[0].endswith('"') or "」" in result[0] or result[0].endswith("好啊.")


# --- 半形標點的歧義處理（串流中最容易錯的地方）---


async def test_小數點不切():
    """3.14 的點不是句末——這是串流切分最典型的誤判。

    刻意放寬長度上限：這裡驗的是「標點判定」，不能讓長度強制切分
    混進來當變因。
    """
    loose = SplitPolicy(first_max_weight=999.0, max_weight=999.0)
    result = await collect("圓周率大約是3.14這個數字很常用喔。", loose)
    assert len(result) == 1
    assert "3.14" in result[0]


async def test_省略號不切():
    result = await collect(
        "我想想...應該可以吧。", SplitPolicy(first_min_weight=1.0, min_weight=1.0)
    )
    assert all("." not in r or r.count(".") >= 2 for r in result[:1])
    assert len(result) <= 2


async def test_半形句號在後面有字時才切():
    result = await collect(
        "Hello world. This is fine.", SplitPolicy(first_min_weight=1.0, min_weight=1.0)
    )
    assert len(result) >= 1
    assert result[0].startswith("Hello")


# --- 首句優先 ---


async def test_首句用較低閾值_較早切出():
    policy = SplitPolicy(first_min_weight=6.0, min_weight=20.0)
    result = await collect_full(
        "我覺得這個想法不錯，可以再多想一下細節，然後我們就開始做。", policy
    )
    texts = [s.text for s in result if s.text]
    assert texts[0] == "我覺得這個想法不錯，"
    assert result[0].is_first
    assert result[0].split_reason == "secondary"


async def test_首句標記只有第一句有():
    result = await collect_full("一句。二句。三句。")
    firsts = [s.is_first for s in result if s.text]
    assert firsts[0] is True
    assert not any(firsts[1:])


# --- 長度上限 ---


async def test_超長句被強制切開():
    """一個 80 字的長句在 RTF 0.3 下要合成 6 秒，後面全部延遲。"""
    # 寬限設 0——這裡驗的是「上限必定切開」，寬限行為另有專屬測試
    policy = SplitPolicy(
        first_max_weight=10.0, max_weight=15.0,
        allow_secondary_split=False, hard_cut_grace=0.0,
    )
    long_text = "這" * 60 + "。"
    result = await collect_full(long_text, policy)
    assert len(result) > 3
    assert any(s.split_reason == "max_length" for s in result)


async def test_強制切分優先回溯到空白處():
    """在自然停頓處切，銜接假影遠小於在強母音中間硬切。"""
    policy = SplitPolicy(first_max_weight=4.0, max_weight=4.0, allow_secondary_split=False)
    result = await collect("hello world foo bar baz qux", policy)
    assert any(r.endswith(" ") or " " in r for r in result)


# --- 收尾 ---


async def test_沒有句末標點也會_flush():
    result = await collect_full("這句話沒有標點")
    texts = [s.text for s in result if s.text]
    assert texts == ["這句話沒有標點"]
    assert result[-1].is_last


async def test_最後一句標記_is_last():
    result = await collect_full("一句。二句。")
    assert result[-1].is_last
    assert not any(s.is_last for s in result[:-1])


async def test_序號連續遞增():
    result = await collect_full("一。二。三。四。")
    assert [s.index for s in result] == list(range(len(result)))


# --- 取消 ---


async def test_取消後不_flush_殘句():
    """使用者已經在講話了，補送殘句只會讓機器人多講半句廢話。

    取消是以例外形式冒出來的（消費端必須知道這輪沒講完），
    重點是「第二句還沒」那段殘餘**不會**被 flush 出來。
    """
    from uep.stream.contracts.cancellation import CancelledError

    token = CancellationToken()
    splitter = SentenceSplitter()
    out = []

    async def slow_tokens():
        for ch in "第一句話講完了。第二句還沒":
            yield ch

    with pytest.raises(CancelledError):
        async for sentence in splitter.split(slow_tokens(), "t", token):
            out.append(sentence.text)
            token.cancel(CancelReason.BARGE_IN)

    assert out == ["第一句話講完了。"]


async def test_取消時拋出():
    token = CancellationToken()
    token.cancel(CancelReason.BARGE_IN)
    splitter = SentenceSplitter()
    from uep.stream.contracts.cancellation import CancelledError

    with pytest.raises(CancelledError):
        async for _ in splitter.split(chars("你好。"), "t", token):
            pass


# --- 切點成因 ---


async def test_split_reason_可分類():
    """調參時要看這個分布：max_length 佔比過高代表閾值要調。"""
    result = await collect_full(
        "你好。這是一段比較長的句子，裡面有逗號分隔的內容。",
        SplitPolicy(first_min_weight=1.0, min_weight=1.0),
    )
    reasons = {s.split_reason for s in result}
    assert "terminal" in reasons


def test_text_weight_累加():
    assert text_weight("你好") == 2.0
    assert text_weight("，。") == 0.0


async def test_過短的句末不切_併入下一句():
    """"Of course!" 這種兩三字短句單獨合成會各吃一次 TTS 固定開銷，
    且語氣破碎——句末標點也要達最短長度，不夠就併入下一句。"""
    result = await collect(
        "好。那我們就從這裡開始講起吧。",
        SplitPolicy(first_min_weight=6.0, first_max_weight=999.0, min_weight=12.0),
    )
    assert len(result) == 1
    assert result[0].startswith("好。")


# --- 硬切寬限（2026-08-21 實測回饋） ---


async def test_超過上限先等標點再切_不切在詞中間():
    """「…遊戲吸/引，…」這種詞中硬切比句子長幾個字難聽得多。"""
    policy = SplitPolicy(
        first_min_weight=4.0, first_max_weight=10.0, hard_cut_grace=6.0
    )
    # 權重 10 時沒有標點，「，」在權重 14 處——寬限內，應該等到標點
    text = "一二三四五六七八九十十一十二，後面的內容。"
    result = await collect_full(text, policy)
    assert result[0].text.endswith("，")
    assert result[0].split_reason == "secondary"


async def test_寬限用完仍會硬切():
    policy = SplitPolicy(
        first_min_weight=4.0, first_max_weight=10.0, hard_cut_grace=4.0
    )
    text = "這" * 30 + "。"
    result = await collect_full(text, policy)
    assert result[0].split_reason == "max_length"
    assert text_weight(result[0].text) <= 14.0 + 1


# --- 括號內不切（2026-08-21 實測回饋） ---


async def test_書名號內的冒號不當切點():
    """《光與影：33號遠征隊》被切成「《光與影：」是實測看到的殘句。"""
    policy = SplitPolicy(
        first_min_weight=2.0, first_max_weight=99.0, min_weight=2.0, max_weight=99.0
    )
    text = "我想試試《光與影：33號遠征隊》這款，然後繼續講別的。"
    result = await collect(text, policy)
    assert result[0] == "我想試試《光與影：33號遠征隊》這款，"


async def test_括號外的次級標點照常切():
    policy = SplitPolicy(
        first_min_weight=2.0, first_max_weight=99.0, min_weight=2.0, max_weight=99.0
    )
    result = await collect("第一段夠長了，第二段也夠長，", policy)
    assert result == ["第一段夠長了，", "第二段也夠長，"]


async def test_未閉合括號不會永久卡死切分():
    """LLM 忘記閉合《 時，句末標點與硬切仍要能收尾。"""
    policy = SplitPolicy(
        first_min_weight=2.0, first_max_weight=99.0, min_weight=2.0, max_weight=99.0
    )
    result = await collect("他提到《某本書，內容還不錯。之後再說。", policy)
    assert len(result) == 2  # 兩個句號各切一次，沒有被括號深度擋住


async def test_FLUSH_訊號強制切句_不看長度下限():
    from uep.stream.splitter import FLUSH

    async def toks():
        for t in ["嗯，", "讓我想一下。", FLUSH, "你上次說你養了一隻叫小黑的貓。"]:
            yield t

    out = [s async for s in SentenceSplitter(SplitPolicy(first_min_weight=8.0)).split(toks(), "t")]
    assert [s.text for s in out][:2] == ["嗯，讓我想一下。", "你上次說你養了一隻叫小黑的貓。"]
    assert out[0].split_reason == "flush" and out[0].index == 0 and out[1].index == 1


async def test_FLUSH_空buffer不產生空句():
    from uep.stream.splitter import FLUSH

    async def toks():
        for t in [FLUSH, "好。"]:
            yield t

    out = [s async for s in SentenceSplitter(SplitPolicy()).split(toks(), "t")]
    assert [s.text for s in out if s.text] == ["好。"]


# --- 後續句絕對上限 hard_cap（2026-11-01） ---


def _cap_policy(**kw) -> SplitPolicy:
    base = dict(
        first_min_weight=2.0, first_max_weight=4.0, hard_cut_grace=0.0,
        min_weight=10.0, max_weight=20.0,
    )
    base.update(kw)
    return SplitPolicy(**base)


async def _skip_first(text: str, policy: SplitPolicy):
    result = await collect_full(text, policy)
    return result[1:]


async def test_hardcap_純中文在_max_之後遇到逗號就切在逗號():
    # 首句「好的。」；後續句 25 字無標點、再接逗號：越過 max 20、未到 cap 40
    text = "好的。" + "這" * 25 + "，" + "後" * 5 + "。"
    rest = await _skip_first(text, _cap_policy(allow_secondary_split=True))
    assert rest[0].text == "這" * 25 + "，"
    assert rest[0].split_reason == "secondary"


async def test_hardcap_到上限仍無標點才_max_length_硬切():
    text = "好的。" + "這" * 100 + "。"
    rest = await _skip_first(text, _cap_policy())
    assert rest[0].split_reason == "max_length"
    assert text_weight(rest[0].text) == 40.0  # 預設 2 x max


async def test_hardcap_可由欄位覆寫():
    text = "好的。" + "這" * 100 + "。"
    rest = await _skip_first(text, _cap_policy(hard_cap_weight=30.0))
    assert rest[0].split_reason == "max_length"
    assert text_weight(rest[0].text) == 30.0


async def test_hardcap_括號內的逗號在_max_之後可以切():
    text = "好的。" + "（" + "這" * 25 + "，" + "後" * 5 + "）。"
    rest = await _skip_first(text, _cap_policy())
    assert rest[0].text.endswith("，")
    assert rest[0].split_reason == "secondary"


async def test_hardcap_括號內的逗號在_max_之前仍不切():
    text = "好的。" + "（" + "這" * 12 + "，" + "後" * 5 + "）。"
    rest = await _skip_first(text, _cap_policy())
    assert rest[0].text.count("，") == 1 and rest[0].text.endswith("）。")


async def test_hardcap_首句行為不變_仍用_grace():
    policy = _cap_policy(hard_cut_grace=4.0)
    result = await collect_full("這" * 30 + "。", policy)
    assert result[0].split_reason == "max_length"
    assert text_weight(result[0].text) <= 8.0 + 1
