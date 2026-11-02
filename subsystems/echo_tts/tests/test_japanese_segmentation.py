"""日文分段行為的契約測試。

背景：2.5 加入日文支援後，`lite_engine` 的預切分分派是二元的
（zh → jieba，其餘 → pySBD），日文因此掉進英文句子切分器。pySBD 靠
「. ! ? + 空格」判句尾，套在無空格的日文上會把每個「。」都當切點，
實測 112 token 的日文被切成 5 段而同長度中文維持 1 段。

修法是日文跳過句子級預切分，直接交給 token 預算分段——其
`_SENTENCE_BOUNDARY` 已含日文標點（。、！？），只在超出預算時才切。
"""
import pytest

from echo_tts.modules.tts_module.tts_utils.text_v25 import split_text_by_token_budget

# 以字元數近似 token 數：2.5 的 tiktoken 對 CJK 大致 1 字 1 token，
# 這裡驗的是「切或不切」的決策，不需要真實 tokenizer（會拉進模型目錄）。
CHAR_LEN = len

CAPACITY = 1815
LANG_PREFIX = "<|ja|> "

JA_SHORT = "今日はいい天気ですね。机の上の仕事を全部片付けました。次は何をしましょうか？"
JA_MEDIUM = (
    "今日はいい天気ですね。机の上の仕事を全部片付けました。次は何をしましょうか？"
    "そういえば、昨日話していた新しいプロジェクトの件ですが、"
    "もう少し詳しく聞かせてもらえませんか。"
)


def segment(text: str, max_tokens: int) -> list[str]:
    return split_text_by_token_budget(text, CHAR_LEN, max_tokens, CAPACITY, LANG_PREFIX)


def test_short_japanese_stays_one_segment():
    """預算內的日文不該被切開——每個句號都切會讓韻律斷裂。"""
    assert CHAR_LEN(JA_SHORT) < 120
    assert segment(JA_SHORT, 120) == [JA_SHORT]


def test_medium_japanese_stays_whole_when_within_budget():
    budget = CHAR_LEN(JA_MEDIUM) + 20
    assert segment(JA_MEDIUM, budget) == [JA_MEDIUM]


def test_long_japanese_splits_only_when_over_budget():
    long_text = JA_MEDIUM * 3
    segments = segment(long_text, 120)
    assert len(segments) > 1
    # 沒有內容遺失
    assert "".join(segments) == long_text


def test_japanese_splits_at_punctuation_not_mid_word():
    """超出預算時，切點應落在日文標點之後，而不是切在詞中間。"""
    segments = segment(JA_MEDIUM * 3, 120)
    # 最後一段可能沒有結尾標點（文本結束），其餘每段都該以標點收尾
    for seg in segments[:-1]:
        assert seg[-1] in "。、！？，；：!?;:,.\n", f"切在非標點處: ...{seg[-12:]}"


def test_every_segment_respects_budget():
    budget = 120
    prefix_cost = CHAR_LEN(LANG_PREFIX)
    for seg in segment(JA_MEDIUM * 3, budget):
        assert CHAR_LEN(seg) <= budget - prefix_cost + 1


@pytest.mark.parametrize("text", [
    "あ...ごめんなさい……大事なデータを失くしちゃった……",
    "「わかった」と彼女は言った。それから、ゆっくりと立ち上がった。",
    "ねえねえ、聞いて！新しいモデルのテスト、一発で全部合格したんだよ！",
])
def test_japanese_punctuation_variants_stay_whole(text):
    """省略號、引號、感嘆號等變體在預算內都不該被切。"""
    assert segment(text, 120) == [text]


def test_sentence_boundary_covers_japanese_punctuation():
    """回歸測試：日文讀點與句點必須在邊界字元集裡。"""
    from echo_tts.modules.tts_module.tts_utils.text_v25 import _SENTENCE_BOUNDARY

    for mark in "。、！？":
        assert _SENTENCE_BOUNDARY.split(f"あ{mark}い") == ["あ" + mark, "い"], \
            f"邊界正則不認得 {mark}"


# ── 語言分割正則（目前停用，但保留防呆）──

def _load_cjk_regex():
    """從原始碼取出 _CJK_LANG_SPLIT_RE，避免 import lite_engine 拉進 torch。"""
    import re
    from pathlib import Path

    src_path = (Path(__file__).resolve().parent.parent / "echo_tts" / "modules"
                / "tts_module" / "lite_engine.py")
    src = src_path.read_text(encoding="utf-8")
    start = src.index("_CJK_LANG_SPLIT_RE = _re.compile(")
    end = src.index(")", src.index("ｦ-ﾟ]")) + 1
    ns = {"_re": re}
    exec(src[start:end], ns)
    return ns["_CJK_LANG_SPLIT_RE"]


@pytest.mark.parametrize("text,expected", [
    ("今日", True),           # 漢字
    ("はいい", True),          # 平假名
    ("テスト", True),          # 片假名
    ("ﾃｽﾄ", True),           # 半形片假名
    ("今天天氣", True),        # 中文
    ("hello", False),
    ("123", False),
])
def test_cjk_regex_covers_kana(text, expected):
    """假名必須算 CJK。

    否則語言分割一旦重新啟用，「今日はいい天気」只有漢字被認成 cjk、
    假名掉進 neutral，日文會在假名處被切碎。
    """
    assert bool(_load_cjk_regex().search(text)) is expected


def test_japanese_sentence_fully_covered_by_cjk_regex():
    regex = _load_cjk_regex()
    missed = [c for c in "今日はいい天気ですね" if not regex.search(c)]
    assert not missed, f"未被認出的字元: {missed}"
