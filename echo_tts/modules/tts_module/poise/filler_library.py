"""
FillerLibrary — 銜接詞素材庫

提供各語言、各強度層級的 filler 文字，
附帶 avoid_recent 機制防止連續出現相同 filler。
"""

import random
from collections import deque
from typing import Dict, List, Optional

from .level import PoiseLevel


# ── 中文 Filler 庫 ──
ZH_FILLERS: Dict[PoiseLevel, List[str]] = {
    PoiseLevel.BREATH: [
        "......",
        "............",
        "——......",
    ],
    PoiseLevel.FILLER_LIGHT: [
        "嗯...",
        "唔...",
        "那個...",
        "也就是說...",
        "所以啊...",
        "然後呢...",
        "對...",
        "嗯嗯...",
    ],
    PoiseLevel.FILLER_HEAVY: [
        "等一下喔...",
        "讓我想想...",
        "啊對了...",
        "等我整理一下...",
        "嗯，怎麼說呢...",
    ],
    PoiseLevel.ACTION: [
        "抱歉，讓我喝口水...",
        "稍微停頓一下...",
        "嗯，等我一下...",
    ],
}

# ── 英文 Filler 庫 ──
EN_FILLERS: Dict[PoiseLevel, List[str]] = {
    PoiseLevel.BREATH: [
        "......",
        "............",
        "—......",
    ],
    PoiseLevel.FILLER_LIGHT: [
        "Umm...",
        "Uh...",
        "So...",
        "Well...",
        "I mean...",
        "You know...",
        "Right...",
        "Hmm...",
    ],
    PoiseLevel.FILLER_HEAVY: [
        "Hold on a moment...",
        "Let me think...",
        "Oh right...",
        "Let me gather my thoughts...",
        "Hmm, how should I put this...",
    ],
    PoiseLevel.ACTION: [
        "Sorry, let me take a sip of water...",
        "Just a moment, please...",
        "Hmm, give me a second...",
    ],
}

# ── 日文 Filler 庫 ──
JA_FILLERS: Dict[PoiseLevel, List[str]] = {
    PoiseLevel.BREATH: [
        "......",
        "............",
        "——......",
    ],
    PoiseLevel.FILLER_LIGHT: [
        "えっと...",
        "うーん...",
        "あの...",
        "そうね...",
        "んん...",
        "まあ...",
        "それで...",
    ],
    PoiseLevel.FILLER_HEAVY: [
        "ちょっと待って...",
        "考えさせて...",
        "あ、そうだ...",
        "えっと、どう言えばいいかな...",
    ],
    PoiseLevel.ACTION: [
        "ごめん、ちょっと一息つかせて...",
        "少し待ってね...",
        "んん、ちょっとだけ...",
    ],
}

# Maximum number of recent fillers to track for dedup
_DEFAULT_HISTORY_SIZE = 5


class FillerLibrary:
    """
    Language-aware filler text provider with deduplication.

    Tracks recently used fillers to avoid repetition within a synthesis run.
    """

    def __init__(
        self,
        history_size: int = _DEFAULT_HISTORY_SIZE,
        seed: Optional[int] = None,
        zh_fillers: Optional[Dict[PoiseLevel, List[str]]] = None,
        en_fillers: Optional[Dict[PoiseLevel, List[str]]] = None,
        ja_fillers: Optional[Dict[PoiseLevel, List[str]]] = None,
    ):
        self._rng = random.Random(seed)
        self._history: deque = deque(maxlen=history_size)
        self._zh = zh_fillers or ZH_FILLERS
        self._en = en_fillers or EN_FILLERS
        self._ja = ja_fillers or JA_FILLERS

    def get_filler(
        self,
        level: PoiseLevel,
        language: str = "zh",
    ) -> Optional[str]:
        """
        Get a filler text for the given level and language.

        Returns None only for SILENCE level. BREATH level now uses
        TTS-synthesized ellipsis (e.g., "......") instead of external SFX.

        Args:
            level: The poise level (BREATH, FILLER_LIGHT, FILLER_HEAVY, or ACTION).
            language: Language code ("zh", "en", or "ja").

        Returns:
            Filler text string, or None.
        """
        if level == PoiseLevel.SILENCE:
            return None

        pool = {"zh": self._zh, "ja": self._ja}.get(language, self._en)
        candidates = pool.get(level, [])

        if not candidates:
            return None

        # Filter out recently used fillers
        available = [f for f in candidates if f not in self._history]

        # If all have been used recently, allow any
        if not available:
            available = candidates

        choice = self._rng.choice(available)
        self._history.append(choice)
        return choice

    def reset(self, seed: Optional[int] = None) -> None:
        """Reset history for a new synthesis run."""
        self._history.clear()
        if seed is not None:
            self._rng = random.Random(seed)

    @property
    def supported_levels(self) -> List[PoiseLevel]:
        """Levels that have filler content available."""
        return [PoiseLevel.BREATH, PoiseLevel.FILLER_LIGHT, PoiseLevel.FILLER_HEAVY, PoiseLevel.ACTION]
