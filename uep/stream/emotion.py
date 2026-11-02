"""逐句情緒控制——LLM 情緒標記 → SpeechStyle（Speech Planner 起步版）。

## 這一層在解什麼問題

現況是每句都用角色檔的預設情緒向量，聽起來像機器人。基礎設施早就在：
:attr:`~uep.stream.contracts.types.Sentence.style` → adapter 的
``_apply_style`` 翻譯成後端原生控制。缺的是**產生** style 的那一端。

起步做法：讓 LLM 在回應裡夾帶輕量標記（``[開心]``），ThinkStage 逐句
解析成 :class:`~uep.stream.contracts.style.SpeechStyle`，標記本身從文字
剔除。完整的 Speech Planner 設計見 docs/design/tts-improvement-reference.md。

## preset 表從哪裡來（2026-08-23）

內建的 :data:`EMOTION_PRESETS` 只是**沒有 TTS 子系統時的退路**。真正的來源是
echo_tts 的 ``emotion_presets.yaml``——adapter 啟動時讀進來、經
:func:`register_presets` 注入（core 不 import 子系統，但可以接受塞進來的 dict）。
一份定義兩邊共用，艾斯維爾在 yaml 改的東西這裡跟著變。

preset 向量只定義**配方**（維度間比例），絕對強度由 TTS 那邊的區間決定
（0.65±0.08，見 [PM] TestSeparateTTSSystem）——所以這裡的值多大沒意義，
adapter 的 ``_apply_style`` 會等比縮放。

標記名同時接受中文與英文（``[開心]`` / ``[happy]``），大小寫不分。
英日文對話時模型很自然會寫英文標記，只認中文等於那兩種語言的標記全部無效。

## 為什麼是「剝掉所有短標記」而不是只剝認得的

``sanitize_for_tts`` 會剔除方括號**符號**但保留內容——LLM 若寫了不在
表裡的 ``[大笑]``，不剝掉就會被唸成「大笑」。所以策略是：

* 認得的標記 → 轉成 style，剝掉
* 不認得的短標記 → 靜默剝掉，退回預設 style（**不炸管線**）

正常語音內容不該出現方括號（sanitize 本來就會清掉符號），
誤殺合法內容的風險可忽略。
"""

from __future__ import annotations

import re
from dataclasses import replace

from .contracts.style import SpeechStyle

BUILTIN_PRESETS: dict[str, dict[str, float]] = {
    "happy": {"happy": 1.0},
    "excited": {"happy": 0.7, "surprised": 0.3},
    "sad": {"sad": 1.0},
    "angry": {"angry": 1.0},
    "afraid": {"afraid": 1.0},
    "disgusted": {"disgusted": 1.0},
    "melancholic": {"melancholic": 1.0},
    "surprised": {"surprised": 1.0},
    "calm": {"calm": 1.0},
    "gentle": {"calm": 0.6, "happy": 0.4},
}
"""沒有 echo_tts 時的退路。key 用英文（與 yaml 對齊），值只是比例。"""

BUILTIN_ALIASES: dict[str, str] = {
    "開心": "happy",
    "興奮": "excited",
    "難過": "sad",
    "悲傷": "sad",
    "生氣": "angry",
    "憤怒": "angry",
    "害怕": "afraid",
    "恐懼": "afraid",
    "厭惡": "disgusted",
    "憂鬱": "melancholic",
    "驚訝": "surprised",
    "平靜": "calm",
    "冷靜": "calm",
    "溫柔": "gentle",
    "嚴厲": "stern",
    "惆悵": "wistful",
    "慌張": "flustered",
    "傲嬌": "tsundere",
    "期待": "anticipated",
    "中性": "neutral",
}
"""中文標記名 → preset key。yaml 裡的 preset 有英文 key 就自動可用，
這張表讓中文也能對上；沒對應 preset 的別名會在 prompt 裡被略過。"""

EMOTION_PRESETS: dict[str, dict[str, float]] = dict(BUILTIN_PRESETS)
"""**目前生效**的 preset 表（key → 八維分佈）。:func:`register_presets` 會整個換掉。
Web 面板的 preset 下拉也吃這張表。"""

_aliases: dict[str, str] = dict(BUILTIN_ALIASES)
_lookup: dict[str, str] = {}
"""小寫標記名（含別名）→ preset key。由 :func:`_rebuild_lookup` 維護。"""


def _rebuild_lookup() -> None:
    _lookup.clear()
    for key in EMOTION_PRESETS:
        _lookup[key.lower()] = key
    for alias, key in _aliases.items():
        if key in EMOTION_PRESETS:
            _lookup[alias.lower()] = key


def register_presets(
    presets: dict[str, dict[str, float]], aliases: dict[str, str] | None = None
) -> None:
    """換掉生效的 preset 表（adapter 從 echo_tts 的 yaml 讀進來後呼叫）。

    ``neutral`` 這種全零配方會被保留但不進 prompt——模型標 ``[neutral]``
    等於「回到預設」，解析時視為認得、style 為 None。
    """
    cleaned = {
        str(k): {d: float(v) for d, v in vec.items() if float(v) > 0.0}
        for k, vec in presets.items()
    }
    EMOTION_PRESETS.clear()
    EMOTION_PRESETS.update(cleaned)
    _aliases.clear()
    _aliases.update(BUILTIN_ALIASES)
    if aliases:
        _aliases.update(aliases)
    _rebuild_lookup()


def reset_presets() -> None:
    """回到內建表（測試用）。"""
    register_presets(BUILTIN_PRESETS)


def resolve_marker(name: str) -> str | None:
    """標記名（中/英、大小寫不分）→ preset key；認不得回 None。"""
    return _lookup.get(name.strip().lower())


def preset_labels() -> list[str]:
    """給 prompt 用的清單：``開心(happy)`` 這種中英並列，沒中文別名就只列英文。"""
    zh_by_key: dict[str, str] = {}
    for alias, key in _aliases.items():
        zh_by_key.setdefault(key, alias)
    labels = []
    for key, vec in EMOTION_PRESETS.items():
        if not any(v > 0 for v in vec.values()):
            continue  # neutral 不列
        zh = zh_by_key.get(key)
        labels.append(f"{zh}({key})" if zh else key)
    return labels


_rebuild_lookup()


def marker_prompt() -> str:
    """接在人設之後的 system prompt 補充。人設是基底，這是本功能的附加指示。

    動態組——preset 表可能被 :func:`register_presets` 換過。

    **不強制標**（艾斯維爾 2026-08-23）：沒標的句子用的就是我們自己設定的預設
    情緒，那是正常狀態；標記只在情緒明顯轉折時用。之前一度改成「第一句一定標」，
    被打回——能標就好。
    """
    return (
        "【語音情緒標記】\n"
        "你的回覆會被轉成語音。當某句話的情緒有明顯轉折時，可以在那一句開頭加上"
        "情緒標記，格式是方括號包住標記名，例如「[開心]太好了！」；"
        "沒有標記的句子會用預設語氣，所以平鋪直敘不必標。"
        "講英文或日文時也可以用同樣的標記。\n"
        f"可用標記（中文或英文名皆可）：{'、'.join(preset_labels())}。\n"
        "除了情緒標記之外，回覆中不要使用方括號。\n"
        "回覆是口語對話：不要使用 markdown 格式（列表符號、標題、粗體），"
        "列舉事情時用自然的語句串接。"
    )


MARKER_PROMPT = marker_prompt()
"""內建 preset 表下的 prompt（相容舊 import；實際使用請呼叫 :func:`marker_prompt`）。"""

_MARKER_RE = re.compile(r"\[([^\[\]\s]{1,12})\]")
"""短標記：方括號包 1-12 個無空白字元（英文名如 ``melancholic`` 要塞得下）。
放寬到任意長度會開始誤殺正常內容。"""


def extract_style(
    text: str, base: SpeechStyle | None = None
) -> tuple[str, SpeechStyle | None]:
    """剝掉文字裡的情緒標記，回傳（乾淨文字, style 或 None）。

    ``base`` 是 turn 級的預設 style（例如設定面板的覆寫）——標記只換掉
    **情緒分佈**，語速、強度沿用 base，兩層控制才不會互相打架。

    一句裡有多個標記時取第一個認得的；解析不出任何認得的標記就回 None，
    由呼叫端退回預設。**這個函式不拋例外**——標記格式錯誤不能炸管線。
    """
    style: SpeechStyle | None = None

    def _consume(match: re.Match[str]) -> str:
        nonlocal style
        key = resolve_marker(match.group(1))
        preset = EMOTION_PRESETS.get(key) if key is not None else None
        if preset and style is None:  # 空配方（neutral）= 認得但回預設
            # style.style 記下 preset 名——前端 / log 靠它顯示「這句標了什麼」
            style = (
                replace(base, emotion=dict(preset), style=key)
                if base is not None
                else SpeechStyle(emotion=dict(preset), style=key)
            )
        return ""  # 認不認得都剝掉——見模組 docstring

    cleaned = _MARKER_RE.sub(_consume, text).strip()
    return cleaned, style


__all__ = [
    "EMOTION_PRESETS",
    "BUILTIN_PRESETS",
    "BUILTIN_ALIASES",
    "MARKER_PROMPT",
    "marker_prompt",
    "register_presets",
    "reset_presets",
    "resolve_marker",
    "preset_labels",
    "extract_style",
]
