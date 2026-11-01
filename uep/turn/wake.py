"""喚醒詞偵測（搬自 bot ``echo_decision.gate.wake_detector``）。

精確匹配基底 0.95；模糊匹配（Levenshtein，只比對與名字等長的視窗）：名字字元數 < ``fuzzy_min_name_length``
不做模糊，容許距離 = min(2, 字元數 // fuzzy_chars_per_edit)，距離 <= 1 為 0.70、等於 2 為 0.50；
再加位置權重（句首 +0.05、句尾 -0.05），clamp 到 [0, 1]。純函式、不依賴時鐘。
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from enum import StrEnum

from uep.turn.decision_config import BotIdentityConfig, GateConfig


class AddressConfidence(StrEnum):
    CERTAIN = "certain"
    TENTATIVE = "tentative"
    NONE = "none"


@dataclass(frozen=True)
class GateResult:
    confidence_level: AddressConfidence
    confidence_score: float
    source: str  # wake_word／none
    matched_name: str | None = None


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if len(a) > len(b):
        a, b = b, a
    prev = list(range(len(a) + 1))
    for j, char_b in enumerate(b, start=1):
        curr = [j]
        for i, char_a in enumerate(a, start=1):
            curr.append(prev[i - 1] if char_a == char_b else 1 + min(prev[i - 1], prev[i], curr[i - 1]))
        prev = curr
    return prev[-1]


def _relative_weight(index: int, text_len: int) -> float:
    if text_len == 0:
        return 0.0
    relative = index / text_len
    if relative <= 0.3:
        return 0.05
    if relative >= 0.7:
        return -0.05
    return 0.0


def _best_window(text: str, name: str) -> tuple[int, int]:
    """與 name 等長的視窗中編輯距離最小者：回傳 (距離, 起始位置)。文字比名字短時距離為 len(name)。"""
    n = len(name)
    if len(text) < n:
        return n, 0
    best_dist, best_idx = n, 0
    for i in range(len(text) - n + 1):
        dist = levenshtein(text[i:i + n], name)
        if dist < best_dist:
            best_dist, best_idx = dist, i
        if best_dist == 0:
            break
    return best_dist, best_idx


class WakeDetector:
    _CACHE_MAX = 256

    def __init__(self, config: GateConfig | None = None, identity: BotIdentityConfig | None = None) -> None:
        self._config = config or GateConfig()
        self._identity = identity or BotIdentityConfig()
        self._cache: OrderedDict[str, GateResult] = OrderedDict()
        self._cache_lock = threading.Lock()

    def detect(self, text: str) -> GateResult:
        """同一段文字重複判斷（aggregator 每次 tick 都問、candidate 再問一次）時走快取；模糊比對是 O(n*m)。"""
        with self._cache_lock:
            hit = self._cache.get(text)
            if hit is not None:
                self._cache.move_to_end(text)
                return hit
        result = self._detect(text)
        with self._cache_lock:
            self._cache[text] = result
            while len(self._cache) > self._CACHE_MAX:
                self._cache.popitem(last=False)
        return result

    def _detect(self, text: str) -> GateResult:
        lowered = text.lower()
        best_score = 0.0
        best_name: str | None = None
        for name in self._identity.all_names:
            score = self._score_name(lowered, name.lower())
            if score > best_score:
                best_score, best_name = score, name
        best_score = max(0.0, min(1.0, best_score))
        found = best_name is not None and best_score > 0.0
        return GateResult(
            self._level(best_score), best_score, "wake_word" if found else "none", best_name if found else None
        )

    def is_called(self, text: str) -> bool:
        """這段文字是否已確定在叫人設（CERTAIN）；aggregator 用來縮短等待、orchestrator 用來算 addressed_hint。"""
        return self.detect(text).confidence_level is AddressConfidence.CERTAIN

    def _score_name(self, text: str, name: str) -> float:
        if not name:
            return 0.0
        idx = text.find(name)
        if idx != -1:
            return 0.95 + _relative_weight(idx, len(text))
        # 名字太短時不做模糊匹配：容許距離 2 等於只要有一個字相同就命中
        if len(name) < self._config.fuzzy_min_name_length:
            return 0.0
        max_dist = min(2, len(name) // max(1, self._config.fuzzy_chars_per_edit))
        if max_dist < 1:
            return 0.0
        dist, best_idx = _best_window(text, name)
        if dist > max_dist:
            return 0.0
        return (0.70 if dist <= 1 else 0.50) + _relative_weight(best_idx, len(text))

    def _level(self, score: float) -> AddressConfidence:
        if score >= self._config.certain_threshold:
            return AddressConfidence.CERTAIN
        if score >= self._config.tentative_threshold:
            return AddressConfidence.TENTATIVE
        return AddressConfidence.NONE
