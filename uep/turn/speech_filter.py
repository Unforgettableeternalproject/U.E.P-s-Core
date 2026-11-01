"""生成中 PASS 偵測（P3接入方案 §4.4 的 ``SpeechTextFilter``）：LLM 以 ``[PASS]`` 表示整輪不開口。

句級、內容級，不可能是 ticket 級：結構上符合 ES 的 ``SpeechTextFilter``（``passed`` 屬性與 ``feed``），
但本檔不依賴 echo_stream。還沒唸出任何字之前，句子以 ``[PASS]`` 開頭 -> ``passed``，ES 只關閉該輪語音
（think 照常生成到完）；其餘位置出現的 ``[PASS]`` 標記一律剝掉，不唸出來。
Discord 的 ``@名字:`` 標記協定留在 bot 實作（不搬進 Core）。
"""
from __future__ import annotations

import re

_PASS_RE = re.compile(r"\[\s*PASS\s*\]", re.IGNORECASE)


class PassMarkerFilter:
    def __init__(self) -> None:
        self.passed = False
        self._spoken = False

    def feed(self, sentence_text: str) -> str | None:
        """回傳要唸的文字；None = 這句不唸。"""
        if self.passed or not sentence_text:
            return None
        body = sentence_text.lstrip()
        if not self._spoken and _PASS_RE.match(body):
            self.passed = True
            return None
        out = _PASS_RE.sub("", sentence_text).strip()
        if out:
            self._spoken = True
            return out
        return None
