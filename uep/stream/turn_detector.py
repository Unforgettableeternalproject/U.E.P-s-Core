"""Phase 0 的 turn / barge-in 判定實作——固定閾值版。

刻意做得很笨：固定靜音門檻、固定時長門檻，沒有任何語意判斷。

這**不是**因為偷懶，而是因為介面（:mod:`uep.stream.contracts.turn`）已經
設計成語意模型可以無痛換入的形狀。Phase 0 要驗證的是管線骨架，不是判定精度。

已知限制（單人本機測試可接受，Discord 多人場景不可）：

* 純閾值靜音判定有調參數解不掉的矛盾——閾值短則思考停頓被誤判為講完，
  閾值長則反應遲鈍。Phase 1-2 接 Pipecat Smart Turn v3（8MB 量化模型、
  CPU 推論 12ms、MIT 授權）解決。
* 沒有 backchannel 過濾。「嗯」「對」這類附和聲只靠時長門檻擋，
  擋不掉稍長的附和。
"""

from __future__ import annotations

from .contracts.turn import (
    InterruptionDecision,
    InterruptionDetector,
    TurnDecision,
    TurnDetector,
    TurnPolicy,
    VoiceEvent,
    VoiceState,
)
from .contracts.types import Utterance


class SilenceTurnDetector(TurnDetector):
    """靜音時長達標就判定 turn 結束。

    另有 :attr:`TurnPolicy.max_wait_s` 的保險——使用者一直講不停時強制切斷，
    避免單一 turn 無限成長把 context 撐爆。
    """

    name = "silence"

    def __init__(self, policy: TurnPolicy | None = None) -> None:
        self.policy = policy or TurnPolicy()
        self._speech_accum_s = 0.0
        self._turn_elapsed_s = 0.0

    def evaluate(self, event: VoiceEvent) -> TurnDecision:
        self._turn_elapsed_s = max(self._turn_elapsed_s, event.duration_s)

        if event.state is VoiceState.SPEECH:
            # 取整個 turn 中最長的一次 SPEECH burst：多個零碎短 burst 不會互相洗掉，
            # 結尾的短語氣詞（嗯）也不會把前面的長語音判成太短
            self._speech_accum_s = max(self._speech_accum_s, event.duration_s)
            return TurnDecision(
                is_end_of_turn=False,
                confidence=1.0,
                suggested_wait_s=self.policy.silence_threshold_s,
                reason="speaking",
            )

        # 太短的語音不成一個 turn（咳嗽、關門聲、鍵盤敲擊）
        if self._speech_accum_s < self.policy.min_utterance_s:
            return TurnDecision(
                is_end_of_turn=False,
                confidence=1.0,
                suggested_wait_s=self.policy.silence_threshold_s,
                reason="too_short",
            )

        if event.duration_s >= self.policy.silence_threshold_s:
            return TurnDecision(
                is_end_of_turn=True, confidence=1.0, reason="silence_threshold"
            )

        return TurnDecision(
            is_end_of_turn=False,
            confidence=1.0,
            suggested_wait_s=self.policy.silence_threshold_s - event.duration_s,
            reason="silence_pending",
        )

    def reset(self) -> None:
        self._speech_accum_s = 0.0
        self._turn_elapsed_s = 0.0


class DurationInterruptionDetector(InterruptionDetector):
    """連續語音時長達標就算插話。

    與 :class:`SilenceTurnDetector` 刻意分開（見
    :mod:`uep.stream.contracts.turn` 的說明）：barge-in 要保守、
    turn-end 要靈敏，合併就只能取一組閾值。

    字數門檻與時長門檻的關係是 **AND**（兩者都要滿足）。
    LiveKit 的實作是覆蓋關係（issue #3515），會讓使用者以為設了兩層保護
    但實際只有一層生效——我們明確定義成 AND 避免同樣的坑。
    """

    name = "duration"

    def __init__(self, policy: TurnPolicy | None = None) -> None:
        self.policy = policy or TurnPolicy()

    def evaluate(self, event: VoiceEvent) -> InterruptionDecision:
        if not self.policy.allow_interruptions:
            return InterruptionDecision(False, reason="disabled")

        if event.state is not VoiceState.SPEECH:
            return InterruptionDecision(False, reason="not_speech")

        if event.duration_s < self.policy.interruption_min_speech_s:
            return InterruptionDecision(
                False, confidence=0.0, reason="below_duration_threshold"
            )

        min_words = self.policy.interruption_min_words
        if min_words > 0:
            if len(event.partial_text.split()) < min_words:
                return InterruptionDecision(
                    False, confidence=0.0, reason="below_word_threshold"
                )

        return InterruptionDecision(True, confidence=1.0, reason="sustained_speech")

    def reset(self) -> None:  # 無狀態
        return


class AlwaysAddressed:
    """Phase 0-4 的 addressing 判定：永遠是在跟機器人講話。

    單人本機測試沒有第二個人可以講話，所以這是正確的行為，不是暫時的 stub。

    Phase 5 Discord 多人場景才需要真的判定。屆時的務實起點是
    wake-word / 名字偵測 + pyannote 語者識別——學術意義上的 addressee
    detection 至今仍是開放問題（沒有現成方案，商用系統普遍退回 wake-word），
    所以別期待接一個 library 就解決。
    """

    name = "always"

    def is_addressed_to_bot(self, utterance: Utterance) -> bool:  # noqa: ARG002
        return True
