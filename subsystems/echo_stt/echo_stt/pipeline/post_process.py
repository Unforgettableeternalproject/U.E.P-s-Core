"""
STT 後處理管線 — Hallucination 過濾 + 繁簡轉換

在 STTWorker 辨識完成後、產生 TranscriptEntry 前執行。
每個處理步驟可獨立啟停。
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Optional

logger = logging.getLogger(__name__)


class HallucinationFilter:
    """偵測並過濾 Whisper 的 hallucination（幻覺輸出）。

    常見 hallucination 模式：
    1. 短語無限重複（如「什麼 什麼 什麼...」「the the the...」）
    2. 極低信心度 + 大量文字
    3. compression ratio 異常高

    Usage:
        filt = HallucinationFilter(max_repeat_ratio=0.4, min_confidence=0.1)
        clean_text = filt.filter(text, confidence=0.85)
        # 回傳 None 代表整段應丟棄
    """

    def __init__(
        self,
        max_repeat_ratio: float = 0.4,
        max_repeat_count: int = 5,
        min_confidence: float = 0.05,
        min_text_length: int = 1,
    ):
        """
        Args:
            max_repeat_ratio: 最常見 token 佔總 token 數的最大比例（超過即判定重複）
            max_repeat_count: 同一 token 連續出現的最大次數
            min_confidence: 低於此信心度直接丟棄
            min_text_length: 低於此字數直接丟棄
        """
        self.max_repeat_ratio = max_repeat_ratio
        self.max_repeat_count = max_repeat_count
        self.min_confidence = min_confidence
        self.min_text_length = min_text_length

        self._filtered_count = 0

    def filter(
        self,
        text: str,
        confidence: float = 1.0,
    ) -> Optional[str]:
        """過濾 hallucination。

        Args:
            text: 辨識文字
            confidence: 信心度

        Returns:
            清理後的文字，或 None（整段應丟棄）
        """
        if not text or len(text.strip()) < self.min_text_length:
            return None

        # 1. 極低信心度
        if confidence < self.min_confidence:
            self._filtered_count += 1
            logger.debug("[HalluFilter] 低信心度 (%.3f): %s", confidence, text[:50])
            return None

        # 2. 連續重複偵測
        if self._has_consecutive_repeats(text):
            self._filtered_count += 1
            logger.debug("[HalluFilter] 連續重複: %s", text[:80])
            return None

        # 3. 整體重複比例偵測
        if self._has_high_repeat_ratio(text):
            self._filtered_count += 1
            logger.debug("[HalluFilter] 高重複比例: %s", text[:80])
            return None

        return text

    def _has_consecutive_repeats(self, text: str) -> bool:
        """偵測連續重複 pattern。

        例如：「什麼 什麼 什麼 什麼 什麼」、「the the the the the」
        """
        # 按空格或標點分 token
        tokens = re.split(r'[\s,，。！？!?、;；]+', text)
        tokens = [t for t in tokens if t]

        # 檢查 token 級連續重複（有空格的情況）
        if len(tokens) >= self.max_repeat_count:
            consecutive = 1
            for i in range(1, len(tokens)):
                if tokens[i] == tokens[i - 1]:
                    consecutive += 1
                    if consecutive >= self.max_repeat_count:
                        return True
                else:
                    consecutive = 1

        # 字元級偵測——中文可能沒空格，看連續的相同字符組
        # 例如「什麼什麼什麼什麼」
        for length in (1, 2, 3, 4):
            if len(text) < length * self.max_repeat_count:
                continue
            chunks = [text[i:i+length] for i in range(0, len(text) - length + 1, length)]
            if len(chunks) >= self.max_repeat_count:
                consecutive = 1
                for i in range(1, len(chunks)):
                    if chunks[i] == chunks[i - 1]:
                        consecutive += 1
                        if consecutive >= self.max_repeat_count:
                            return True
                    else:
                        consecutive = 1

        return False

    def _has_high_repeat_ratio(self, text: str) -> bool:
        """偵測整體重複比例過高。"""
        tokens = re.split(r'[\s,，。！？!?、;；]+', text)
        tokens = [t for t in tokens if t]

        if len(tokens) < 4:
            return False

        counter = Counter(tokens)
        most_common_count = counter.most_common(1)[0][1]
        ratio = most_common_count / len(tokens)

        return ratio > self.max_repeat_ratio

    @property
    def filtered_count(self) -> int:
        return self._filtered_count


class ChineseConverter:
    """繁簡轉換（基於 OpenCC）。

    Usage:
        conv = ChineseConverter(conversion="s2twp")
        result = conv.convert("这个很好")  # → "這個很好"
    """

    def __init__(self, conversion: str = "s2twp"):
        """
        Args:
            conversion: OpenCC 轉換方案
                - "s2twp": 簡體 → 繁體（台灣用詞）— 推薦
                - "s2t": 簡體 → 繁體（標準）
                - "t2s": 繁體 → 簡體
                - "s2hk": 簡體 → 香港繁體
        """
        self.conversion = conversion
        self._converter = None
        self._available = False
        self._init()

    def _init(self):
        """初始化 OpenCC（若不可用則 graceful fallback）。"""
        try:
            from opencc import OpenCC
            self._converter = OpenCC(self.conversion)
            self._available = True
            logger.info("[ChineseConv] OpenCC loaded (conversion=%s)", self.conversion)
        except ImportError:
            logger.warning("[ChineseConv] opencc 套件未安裝，繁簡轉換停用")
            self._available = False
        except Exception as e:
            logger.warning("[ChineseConv] OpenCC 初始化失敗: %s", e)
            self._available = False

    def convert(self, text: str) -> str:
        """轉換文字。不可用時原樣回傳。"""
        if not self._available or not text:
            return text
        try:
            return self._converter.convert(text)
        except Exception:
            return text

    @property
    def is_available(self) -> bool:
        return self._available


class PostProcessor:
    """STT 後處理管線——組合所有後處理步驟。

    Usage:
        pp = PostProcessor(
            hallucination_filter=True,
            chinese_convert="s2twp",
        )
        result = pp.process("这个很好", confidence=0.9)
    """

    def __init__(
        self,
        hallucination_filter: bool = True,
        max_repeat_ratio: float = 0.4,
        max_repeat_count: int = 5,
        min_confidence: float = 0.05,
        chinese_convert: Optional[str] = None,
    ):
        """
        Args:
            hallucination_filter: 是否啟用 hallucination 過濾
            max_repeat_ratio: HallucinationFilter 的最大重複比例
            max_repeat_count: HallucinationFilter 的最大連續重複次數
            min_confidence: HallucinationFilter 的最低信心度
            chinese_convert: OpenCC 轉換方案（None = 不轉換）
        """
        self._hallu_filter: Optional[HallucinationFilter] = None
        self._chinese_conv: Optional[ChineseConverter] = None

        if hallucination_filter:
            self._hallu_filter = HallucinationFilter(
                max_repeat_ratio=max_repeat_ratio,
                max_repeat_count=max_repeat_count,
                min_confidence=min_confidence,
            )

        if chinese_convert:
            self._chinese_conv = ChineseConverter(conversion=chinese_convert)

    def process(self, text: str, confidence: float = 1.0) -> Optional[str]:
        """執行後處理管線。

        Returns:
            處理後的文字，或 None（應丟棄）
        """
        if not text:
            return None

        # Step 1: Hallucination filter
        if self._hallu_filter is not None:
            text = self._hallu_filter.filter(text, confidence=confidence)
            if text is None:
                return None

        # Step 2: 繁簡轉換
        if self._chinese_conv is not None:
            text = self._chinese_conv.convert(text)

        return text

    @property
    def hallucination_filtered_count(self) -> int:
        if self._hallu_filter is not None:
            return self._hallu_filter.filtered_count
        return 0

    @property
    def chinese_converter_available(self) -> bool:
        if self._chinese_conv is not None:
            return self._chinese_conv.is_available
        return False

    @classmethod
    def from_config(cls, config: dict) -> "PostProcessor":
        """從 config dict 建立 PostProcessor。

        讀取 post_processing 區段：
            post_processing:
              hallucination_filter: true
              max_repeat_ratio: 0.4
              max_repeat_count: 5
              min_confidence: 0.05
              chinese_convert: "s2twp"  # null = 不轉換
        """
        pp_cfg = config.get("post_processing", {})
        return cls(
            hallucination_filter=pp_cfg.get("hallucination_filter", True),
            max_repeat_ratio=pp_cfg.get("max_repeat_ratio", 0.4),
            max_repeat_count=pp_cfg.get("max_repeat_count", 5),
            min_confidence=pp_cfg.get("min_confidence", 0.05),
            chinese_convert=pp_cfg.get("chinese_convert", None),
        )
