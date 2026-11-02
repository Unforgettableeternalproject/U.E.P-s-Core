"""LLM 輸出防線：過濾已知的中文污染 token。

gpt-5.6-luna 偶爾會在句尾吐出 OpenAI 中文分詞器的已知垃圾 token
（例如「天天彩票app」），語音路徑會把它唸出來、文字路徑會顯示給使用者。
這是模型本身的污染，不是 prompt 或程式碼的問題，所以在輸出端擋。

處理點在句子層級（切句器之後、送下游之前）：污染詞多半落在句尾，
句子已是完整單位，不會有「半個詞先送出」的問題。
本模組只做字串處理，不 import 任何子系統。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

logger = logging.getLogger(__name__)

# 預設污染詞（繁簡皆收）。只收「整詞」——單字「彩」「快」或單獨的「平台」「福利」
# 會誤殺正常用語，一律不收。
DEFAULT_BLOCKLIST: tuple[str, ...] = (
    # 彩票類
    "天天中彩票", "天天彩票", "天天彩", "中彩票", "彩票app", "彩票APP", "彩票",
    "足球彩票", "福利彩票", "体彩", "體彩", "六合彩", "彩经", "彩經",
    "彩神争霸", "彩神爭霸", "时时彩", "時時彩", "重庆时时彩", "重慶時時彩",
    "北京赛车", "北京賽車", "幸运飞艇", "幸運飛艇", "极速赛车", "極速賽車",
    "快三", "大发快三", "大發快三", "pk拾", "PK拾", "分分彩", "五分彩",
    "澳门博彩", "澳門博彩", "百家乐", "百家樂",
    # 娛樂平台類
    "娱乐平台", "娛樂平台", "娱乐注册", "娛樂註冊", "娱乐城", "娛樂城",
    "菲律宾申博", "菲律賓申博", "金沙娱乐", "金沙娛樂", "皇冠体育", "皇冠體育",
)

# 緊接在污染詞後的 ASCII 尾巴（app、APP、www.xxx.com 之類，只吃一個 token），可帶一個前導空白
_ASCII_TAIL = r"(?:[ \t]?[A-Za-z0-9][A-Za-z0-9._\-/:]*)?"

# 剪完後若沒有「真正的內容字元」就視為空句：只認漢字、全形假名、ASCII 字母數字。
# 顏文字常用希臘字母（ω）、半形片假名（ﾉ）等，\w 會把它們當內容，所以不用 \w。
_CONTENT_RE = re.compile(r"[A-Za-z0-9぀-ヿ㐀-䶿一-鿿]")


class OutputGuard:
    """句子層級的污染詞過濾器。"""

    def __init__(
        self,
        *,
        enabled: bool = True,
        extra: Iterable[str] = (),
        blocklist: Iterable[str] | None = None,
    ) -> None:
        self.enabled = enabled
        words = list(DEFAULT_BLOCKLIST if blocklist is None else blocklist)
        words.extend(extra)
        # 長詞優先，避免「天天彩票」被較短的「彩票」先吃掉而留下「天天」
        unique = sorted({w for w in words if w}, key=len, reverse=True)
        self.words: tuple[str, ...] = tuple(unique)
        self._pattern = (
            re.compile("(?:" + "|".join(re.escape(w) for w in unique) + ")" + _ASCII_TAIL,
                       re.IGNORECASE)
            if unique
            else None
        )

    def clean(self, text: str) -> tuple[str, list[str]]:
        """回傳 ``(清理後文字, 命中詞列表)``。

        命中處連同其後緊接的 ASCII 尾巴一起剪掉，再修剪多餘空白；
        剪完若只剩標點／顏文字／空白，回傳空字串（呼叫端整句丟棄）。
        沒命中、或本來就沒內容的句子原樣返回。
        """
        if not self.enabled or self._pattern is None or not text:
            return text, []
        hits: list[str] = []

        def _sub(m: re.Match[str]) -> str:
            hits.append(m.group(0))
            return ""

        cleaned = self._pattern.sub(_sub, text)
        if not hits:
            return text, []
        for hit in hits:
            logger.warning("輸出過濾：命中已知污染詞 %r（原句：%r）", hit, text)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
        if not _CONTENT_RE.search(cleaned):
            return "", hits
        return cleaned, hits


__all__ = ["DEFAULT_BLOCKLIST", "OutputGuard"]
