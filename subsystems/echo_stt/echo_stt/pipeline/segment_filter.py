"""逐 segment 的 Whisper 幻聽／雜訊過濾。

規則與預設值照搬 Echo-Stream ``echo_stream/adapters/stt_discord.py``
（commit 56381f6 起的版本；各函式註解內的 bot 行號為原 bot ``cogs/stt.py`` 的出處）。
純函式＋一個設定 dataclass，不 import 模型與 torch，可無 GPU 單元測試。

放在 echo_stt（Core 的 STT 路徑）而不是 Discord adapter：所有平台共用同一套過濾。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 幻覺黑名單：比對前先正規化（小寫、去標點與空白）。
HALLUCINATION_EXACT = {
    "thankyou", "thankyouforwatching", "thanksforwatching", "thanks", "bye", "byebye",
    "you", "謝謝", "謝謝觀看", "謝謝大家", "感謝觀看", "谢谢", "谢谢观看",
    "ご視聴ありがとうございました", "ありがとうございました",
}
HALLUCINATION_CONTAINS = [
    "ご視聴いただきありがとうございます", "ご視聴ありがとうございました", "チャンネル登録",
    "請不吝點贊訂閱", "請不吝點贊", "點贊訂閱轉發", "訂閱我的頻道", "訂閱頻道", "订阅我的频道",
    "字幕由", "字幕提供", "詞曲李宗盛", "詞曲 李宗盛", "可能夾雜英文詞彙", "以下是繁體中文的對話",
    "字幕志愿者", "字幕志願者", "amara.org", "明鏡與點點欄目",
    "thank you for watching", "thanks for listening", "thank you for listening",
    "please subscribe", "subtitles by", "subscribe to my channel",
    "感謝您的觀看", "感谢您的观看", "謝謝收看", "謝謝你的觀看",
]
# 結尾／短段英文套語。「See you later」這類使用者也會真的說，不能無條件擋：
# 必須同時「短段 + 低 avg_logprob」才算（outro_hallucination）。
HALLUCINATION_OUTRO = {
    "theend", "haveagoodday", "haveagoodone", "seeyoulater", "seeyoulate", "seeyousoon",
    "seeyounexttime", "seeyouinthenextvideo", "seeyouinthenextone",
}
_NORMALIZE_RE = re.compile(r"[\W_]+", re.UNICODE)
_CJK_RE = re.compile(r"[一-鿿]")
_LONE_FULLWIDTH_B_RE = re.compile(r"(?<![A-Za-z0-9Ａ-Ｚａ-ｚ])Ｂ(?![A-Za-z0-9Ａ-Ｚａ-ｚ])")

# 字幕／歌曲 credit 格式：「詞・作曲・編曲 李宗盛」「字幕由 XXX 提供」「XXX 字幕組」
_CREDIT_KW = (
    "作詞|作曲|編曲|填詞|詞曲|詞|曲|監製|製作|演唱|原唱|混音|母帶|字幕|翻譯|校對|時間軸|志願者|"
    "作词|编曲|填词|词曲|词|监制|制作|演唱|翻译|校对|时间轴|志愿者|字幕"
)
_CREDIT_SEP = r"[\s・·•/／:：,，、\-－]"
# 名字 token：<=8 字，且不含常見口語虛詞（避免「曲 這首歌很好聽」之類句子被誤傷）
_CREDIT_NAME = r"[^\s・·•/／:：,，、\-－的是了嗎呢吧啊不很我你他好難太也都就這那有會]{1,8}"
_CREDIT_RES = [
    re.compile(
        rf"^(?:{_CREDIT_KW})(?:{_CREDIT_SEP}*(?:{_CREDIT_KW}))*{_CREDIT_SEP}+{_CREDIT_NAME}$"
    ),
    re.compile(
        rf"^(?:{_CREDIT_KW})(?:{_CREDIT_SEP}*(?:{_CREDIT_KW}))+{_CREDIT_SEP}*{_CREDIT_NAME}$"
    ),
    re.compile(
        rf"^(?:字幕|翻譯|翻译)由{_CREDIT_SEP}*.{{1,16}}?{_CREDIT_SEP}*"
        r"(?:提供|製作|制作|翻譯|翻译)?$"
    ),
    re.compile(rf"^{_CREDIT_NAME}{_CREDIT_SEP}*(?:字幕組|字幕组|字幕志願者|字幕志愿者)$"),
]


def normalize_text(text: str) -> str:
    """小寫並移除標點、空白與底線。"""
    return _NORMALIZE_RE.sub("", text).lower()


def clean_whisper_garbage(text: str) -> str:
    """保底正規化 Whisper 的標點亂碼（byte-level token 被破壞）。僅限含中文的文字。

    獨立全形「Ｂ」→「，」、「﹑」→「、」、移除 U+FFFD。（bot :120）
    """
    if not _CJK_RE.search(text):
        return text
    text = _LONE_FULLWIDTH_B_RE.sub("，", text)
    return text.replace("﹑", "、").replace("�", "")


def credit_hallucination(text: str) -> Optional[str]:
    """整個 segment 只是「字幕／歌曲 credit + 名字」格式時回傳原因。（bot :237）

    只比對整段格式，不看人名本身：討論李宗盛的真實句子不會命中。
    """
    t = text.strip().rstrip(".。!！?？").strip()
    if not t:
        return None
    for i, r in enumerate(_CREDIT_RES):
        if r.match(t):
            return f"credit_format:{i}"
    return None


def is_hallucination(text: str) -> Optional[str]:
    """命中幻覺黑名單回傳原因，否則 None。（bot :509）"""
    norm = normalize_text(text)
    if not norm:
        return "empty"
    if norm in HALLUCINATION_EXACT:
        return f"exact:{norm}"
    for p in HALLUCINATION_CONTAINS:
        if normalize_text(p) in norm:
            return f"contains:{p}"
    return None


def outro_hallucination(
    text: str,
    avg_logprob: Optional[float],
    duration_s: float,
    max_logprob: float = -0.6,
    max_duration_s: float = 1.5,
) -> Optional[str]:
    """短段 + 低信心 + 文字精確等於結尾套語才回傳原因。

    avg_logprob 拿不到（None）時保守放行。邊界：avg_logprob <= -0.6、時長 <= 1.5s 算成立。
    """
    if avg_logprob is None or avg_logprob > max_logprob or duration_s > max_duration_s:
        return None
    norm = normalize_text(text)
    if norm in HALLUCINATION_OUTRO:
        return f"outro:{norm}"
    return None


def hotword_leak_reason(seg_text: str, hotwords: Optional[Iterable[str]]) -> Optional[str]:
    """hotwords 外洩：segment 幾乎只由詞表內的詞組成且含 >=3 個不同詞。（bot :248）"""
    words = list(hotwords or [])
    if not words:
        return None
    seg = normalize_text(seg_text)
    if not seg:
        return None
    hit: set[str] = set()
    covered = [False] * len(seg)
    for w in words:
        nw = normalize_text(str(w))
        if not nw:
            continue
        start = 0
        while True:
            i = seg.find(nw, start)
            if i < 0:
                break
            hit.add(nw)
            for t in range(i, i + len(nw)):
                covered[t] = True
            start = i + len(nw)
    ratio = sum(covered) / len(seg)
    if len(hit) >= 3 and ratio >= 0.6:
        return f"hotword_list:{len(hit)}words,{ratio:.2f}"
    return None


def prompt_leak_reason(seg_text: str, prompt: Optional[str], seg_dur: float) -> Optional[str]:
    """segment 是否為 initial_prompt 外洩（模型把提示詞念出來）。（bot :477）"""
    if not prompt:
        return None
    seg, pr = normalize_text(seg_text), normalize_text(prompt)
    if not seg or not pr:
        return None
    if 2 <= len(seg) <= 4 and seg in pr and seg_dur <= 1.0:
        return f"short_exact:{seg}"
    covered = [False] * len(seg)
    for i in range(len(seg)):
        longest = 0
        for j in range(len(pr)):
            k = 0
            while i + k < len(seg) and j + k < len(pr) and seg[i + k] == pr[j + k]:
                k += 1
            longest = max(longest, k)
        if longest >= 4:
            for t in range(i, i + longest):
                covered[t] = True
    ratio = sum(covered) / len(seg)
    if ratio >= 0.5:
        return f"coverage:{ratio:.2f}"
    return None


def _ascii_ratio(text: str) -> float:
    letters = [c for c in text if not c.isspace() and c not in ",.?!'\"-:;…"]
    return sum(c.isascii() for c in letters) / len(letters) if letters else 0.0


def english_guard_reason(
    text: str, conf: float, rms: float, max_words: int, max_conf: float, max_rms: float
) -> Optional[str]:
    """純英文、短、信心低、音量小 → 回傳原因（多半是中文被誤聽）。（bot :466）"""
    if _CJK_RE.search(text) or _ascii_ratio(text) < 0.95:
        return None
    words = len(text.split())
    if words <= max_words and conf < max_conf and rms < max_rms:
        return f"words={words},conf={conf:.2f},rms={rms:.4f}"
    return None


@dataclass
class SegmentFilterConfig:
    """逐段過濾設定。預設值 = Echo-Stream ``DiscordSttProfile``（= bot ``DEFAULT_STT_SETTINGS``）。"""

    min_rms: float = 0.005  # 16k float RMS，約 -46 dBFS
    max_no_speech_prob: float = 0.7
    min_avg_logprob: float = -1.2
    max_compression_ratio: float = 2.4
    min_text_chars: int = 1
    filter_credits: bool = True
    clean_garbage: bool = True
    filter_hallucination: bool = True
    filter_outro: bool = True
    outro_max_logprob: float = -0.6
    outro_max_duration_s: float = 1.5
    english_guard: bool = False
    english_guard_max_words: int = 2
    english_guard_max_conf: float = 0.5
    english_guard_max_rms: float = 0.02

    @classmethod
    def from_mapping(cls, data: Optional[dict[str, Any]]) -> "SegmentFilterConfig":
        """由 yaml 的 ``segment_filter`` 區段建立；缺的鍵用預設值。"""
        base = cls()
        data = data or {}
        kwargs = {}
        for name, default in base.__dict__.items():
            if name in data and data[name] is not None:
                kwargs[name] = type(default)(data[name])
        return cls(**kwargs)


def segment_reasons(
    seg: Any,
    cfg: SegmentFilterConfig,
    *,
    prompt: str = "",
    hotwords: Optional[Iterable[str]] = None,
) -> list[str]:
    """回傳該 segment 被擋下的原因清單；空清單 = 放行。

    ``seg`` 需有 text／start／end／no_speech_prob／avg_logprob／compression_ratio
    （faster-whisper 的 Segment 形狀）。
    """
    text = seg.text.strip()
    dur = seg.end - seg.start
    reasons: list[str] = []
    if seg.no_speech_prob > cfg.max_no_speech_prob:
        reasons.append("no_speech")
    if seg.avg_logprob < cfg.min_avg_logprob:
        reasons.append("avg_logprob")
    if seg.compression_ratio > cfg.max_compression_ratio:
        reasons.append("compression_ratio")
    if cfg.filter_credits and credit_hallucination(text):
        reasons.append("credit")
    if cfg.filter_outro and outro_hallucination(
        text, seg.avg_logprob, dur, cfg.outro_max_logprob, cfg.outro_max_duration_s
    ):
        reasons.append("outro")
    if prompt and prompt_leak_reason(text, prompt, dur):
        reasons.append("prompt_leak")
    if hotword_leak_reason(text, hotwords):
        reasons.append("hotword_leak")
    return reasons


def filter_segments(
    segments: Iterable[Any],
    cfg: SegmentFilterConfig,
    *,
    prompt: str = "",
    hotwords: Optional[Iterable[str]] = None,
    stats: Optional[dict[str, int]] = None,
) -> list[Any]:
    """逐段過濾，回傳保留的 segments。被擋下的段記 log，並累計進 ``stats[原因]``。"""
    hotwords = list(hotwords or [])
    kept = []
    for seg in segments:
        reasons = segment_reasons(seg, cfg, prompt=prompt, hotwords=hotwords)
        if seg.text.strip() and not reasons:
            kept.append(seg)
        elif reasons:
            logger.info("[STT] segment 被過濾 %s: %r", reasons, seg.text.strip())
            if stats is not None:
                for r in reasons:
                    stats[r] = stats.get(r, 0) + 1
    return kept
