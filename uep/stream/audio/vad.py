"""能量式 VAD——Phase 3 的固定閾值實作。

刻意做得很笨（RMS 能量 + 遲滯 + hangover），理由與
:mod:`uep.stream.turn_detector` 相同：介面已經設計成
語意 VAD（Silero / Smart Turn 的前端）可以無痛換入的形狀，
Phase 3 要驗證的是「麥克風 → 管線」的接線，不是 VAD 精度。

## 為什麼不用 echo_stt 的 AdvancedVAD

echo_stt 的 VAD 與它的 segment 產出狀態機（muzzle）耦合，產物是
``AudioSegment``；而 Echo Stream 的 turn / barge-in 判定消費的是
:class:`~uep.stream.contracts.turn.VoiceEvent`。轉接的成本比這個
百行實作還高，而且會讓 turn 邊界的控制權跑到子系統手上——
turn 判定是管線的職責（§7.5），不能外包。

## 時間軸用音訊樣本數，不用牆鐘

duration 一律從「已餵入的樣本數 / 取樣率」推算。理由：測試與檔案來源
推送音訊比即時快得多，用 ``perf_counter`` 算 duration 會全錯；
而真麥克風的音訊時鐘與牆鐘本來就近似同步，不損失什麼。
``VoiceEvent.timestamp`` 仍依契約填 ``perf_counter``。
"""

from __future__ import annotations

import math
import time

from ..contracts.turn import VoiceEvent, VoiceState
from ..contracts.types import STT_SAMPLE_RATE


class EnergyVad:
    """RMS 能量閾值 VAD，帶遲滯與 hangover。

    * **遲滯**：進入 SPEECH 用 ``threshold``，離開用
      ``threshold × hysteresis``——避免能量在閾值附近抖動時狀態亂跳。
    * **hangover**：能量掉下去後要**持續**低於離開閾值 ``hangover_s``
      才真的切回 SILENCE——字與字之間的微小停頓不該被判成靜音，
      否則 turn 判定的靜音計時會一直被重置又重啟。

    切回 SILENCE 時，狀態起點回溯到能量掉下去的那一刻——
    靜音時長不含 hangover，否則 turn 判定的實效門檻會變成
    ``silence_threshold + hangover``。
    """

    def __init__(
        self,
        sample_rate: int = STT_SAMPLE_RATE,
        threshold: float = 0.015,
        hysteresis: float = 0.6,
        hangover_s: float = 0.15,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate 必須為正")
        self.sample_rate = sample_rate
        self.threshold = threshold
        """進入 SPEECH 的正規化 RMS 門檻（int16 滿刻度 = 1.0）。"""

        self.hysteresis = hysteresis
        self.hangover_s = hangover_s

        self._clock_s = 0.0
        """音訊時間軸：已處理的樣本數換算的秒數。"""

        self._state = VoiceState.SILENCE
        self._state_since_s = 0.0
        self._below_since_s: float | None = None
        """SPEECH 期間能量開始低於離開閾值的時刻（hangover 計時起點）。"""

    @property
    def state(self) -> VoiceState:
        return self._state

    @property
    def clock_s(self) -> float:
        return self._clock_s

    def process(self, pcm: bytes) -> VoiceEvent:
        """吃一個 int16 PCM chunk，回傳當前的 VAD 判定。

        chunk 大小不拘（麥克風常見 20-100ms），狀態跨 chunk 累積。
        """
        samples = memoryview(pcm).cast("h")
        n = len(samples)
        chunk_s = n / self.sample_rate if n else 0.0
        self._clock_s += chunk_s
        now = self._clock_s

        energy = self._rms(samples)

        if self._state is VoiceState.SILENCE:
            if energy >= self.threshold:
                self._state = VoiceState.SPEECH
                # 起點算在 chunk 開頭——這整個 chunk 已經是語音了
                self._state_since_s = now - chunk_s
                self._below_since_s = None
        else:
            leave = self.threshold * self.hysteresis
            if energy >= leave:
                self._below_since_s = None
            else:
                if self._below_since_s is None:
                    self._below_since_s = now - chunk_s
                if now - self._below_since_s >= self.hangover_s:
                    self._state = VoiceState.SILENCE
                    # 回溯到能量掉下去的那一刻，靜音時長不含 hangover
                    self._state_since_s = self._below_since_s
                    self._below_since_s = None

        return VoiceEvent(
            state=self._state,
            timestamp=time.perf_counter(),
            duration_s=max(0.0, now - self._state_since_s),
            energy=energy,
        )

    def reset(self) -> None:
        """回到初始狀態。時鐘**不**歸零——音訊時間軸是連續的。"""
        self._state = VoiceState.SILENCE
        self._state_since_s = self._clock_s
        self._below_since_s = None

    @staticmethod
    def _rms(samples: memoryview) -> float:
        n = len(samples)
        if n == 0:
            return 0.0
        acc = 0
        for value in samples:
            acc += value * value
        return math.sqrt(acc / n) / 32768.0


__all__ = ["EnergyVad"]
