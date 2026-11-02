"""Turn 邊界與插話判定的契約（設計文件 §7.5 的收斂結果）。

## 為什麼是兩個 Protocol 而不是一個

調查業界方案（LiveKit Agents / Pipecat / OpenAI Realtime / ElevenLabs）後的
關鍵發現：**turn-end 判定與 barge-in 判定共享同一個 VAD 事件源，但必須是
兩個獨立的消費者。**

原因是兩者的錯誤代價不對稱：

* turn-end 判成太慢 → 使用者乾等，TTFA 直接受害 → 要**靈敏**
* barge-in 判成太浮動 → 一聲咳嗽就把機器人打斷 → 要**保守**

合併成單一判定器就只能取一組閾值，必然犧牲其中一邊。LiveKit 把
``turn_handling``（endpointing）與 ``min_interruption_duration`` 分開配置，
Pipecat 把 Smart Turn 模型與 ``TurnAnalyzerUserTurnStopStrategy`` 分開，
是同一個結論的兩種實作。

## 為什麼 TurnDetector 不回傳 bool

純閾值靜音判定有個**調參數解不掉**的結構性矛盾：閾值短則思考停頓被誤判
為講完，閾值長則反應遲鈍。業界的解法一致是「動態延展等待視窗」——
LiveKit 的 ``min/max_endpointing_delay``、OpenAI 的 ``semantic_vad
eagerness``、Vapi 的 transcription-based smart endpointing 都是這個模式。

所以 :meth:`TurnDetector.evaluate` 回傳 :class:`TurnDecision`
（結束機率 + 建議再等多久），Phase 0 用固定閾值實作（永遠回同一延展時間），
Phase 1-2 換成語意模型時**介面不需要動**。

## 升級路徑

1. **Phase 0**：:class:`~uep.stream.turn_detector.SilenceTurnDetector`
   固定 700ms 靜音門檻
2. **Phase 1-2**：接 Pipecat Smart Turn v3（8MB 量化模型、CPU 推論 12ms、
   MIT 授權，本來就設計成搭配 Silero VAD 用，與既有雙 VAD 架構相容度最高）
3. **Phase 3+**：視實測 false-interruption 率決定要不要換 LiveKit 的
   135M transformer（精度更高但依賴重得多，<500MB RAM）
4. **Phase 5 Discord**：加 wake-word / 名字偵測 + pyannote 語者識別做
   :class:`AddressingClassifier` 的務實起點

## ⚠️ Phase 5 前置風險：回音消除（AEC）

barge-in 的必要前提是 STT 不會把 TTS 自己的聲音聽成使用者插話
（"echo hallucination"）。本機場景可靠 headset 物理隔離規避，但
**Discord bot 走原始 PCM 接收通常沒有內建 AEC**。這件事沒解決，
barge-in 在 Discord 場景會整個失效。動工 Phase 5 前必須單獨驗證。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from .types import Utterance


class VoiceState(str, Enum):
    """VAD 的原始判定。turn 與 barge-in 兩個判定器都消費這個訊號。"""

    SPEECH = "speech"
    SILENCE = "silence"


@dataclass(slots=True)
class VoiceEvent:
    """一次 VAD 判定結果。"""

    state: VoiceState
    timestamp: float
    """perf_counter 時刻。"""

    duration_s: float
    """這個狀態已持續多久。barge-in 的時長門檻直接看這個值。"""

    energy: float = 0.0
    speaker_id: str | None = None
    partial_text: str = ""
    """截至此刻的部分轉錄。語意型 TurnDetector 要用；純 VAD 版忽略。"""
    is_voiced: bool = False
    """VAD 對此 chunk 的判定（狀態為 SPEECH）。多聲道宿主據此累積說話秒數。"""

    chunk_s: float = 0.0
    """這個事件涵蓋的音訊秒數（**增量**，與 ``duration_s`` 的「狀態已持續多久」不同）。
    累積式 barge-in 加總的是這個值；0 = 未提供（策略退回用時間戳差推算）。"""


@dataclass(slots=True)
class TurnDecision:
    """turn-end 判定結果。

    刻意不是 bool——見模組 docstring。
    """

    is_end_of_turn: bool
    confidence: float = 1.0
    """0.0-1.0。Phase 0 的固定閾值版永遠回 1.0。"""

    suggested_wait_s: float = 0.0
    """建議再等多久才確定結束。

    ``is_end_of_turn=False`` 時代表「還沒講完，至少再等這麼久」；
    語意模型判斷使用者可能在思考時會拉長這個值（對應 LiveKit 的
    ``max_endpointing_delay=1.2s``）。
    """

    reason: str = ""
    """判定成因，用於調參時分析分布。"""


@dataclass(slots=True)
class InterruptionDecision:
    """barge-in 判定結果。"""

    should_interrupt: bool
    confidence: float = 1.0
    reason: str = ""


@runtime_checkable
class TurnDetector(Protocol):
    """判定「使用者這一輪講完了嗎」。靈敏優先。"""

    name: str

    def evaluate(self, event: VoiceEvent) -> TurnDecision:
        """對一次 VAD 事件做判定。必須是**同步且快速**的
        （Phase 1 的語意模型 CPU 推論僅 12ms，仍在可同步呼叫的範圍）。"""
        ...

    def reset(self) -> None:
        """turn 結束後重置內部狀態。"""
        ...


@runtime_checkable
class InterruptionDetector(Protocol):
    """判定「機器人正在講話時，使用者這個聲音算不算插話」。保守優先。

    要過濾掉的是 backchannel（「嗯」「對」「哦」這類附和聲）與環境噪音。
    業界的三種可組合策略：

    * **時長門檻** — 連續語音 ≥ N ms 才算（LiveKit 預設 500ms、
      Vapi 可調 50-500ms）。短促附和聲撐不到門檻。
    * **字數門檻** — 轉錄出 ≥ N 字才算（LiveKit ``min_interruption_words``）。
      ⚠️ LiveKit issue #3515 指出這個參數會**覆蓋**時長門檻而非疊加，
      我們自己實作時要明確定義兩者是 AND 還是 OR，別重蹈覆轍。
    * **信心分數** — 語意分類（Telnyx 建議起始閾值 0.4）。
    """

    name: str

    def evaluate(self, event: VoiceEvent) -> InterruptionDecision: ...

    def reset(self) -> None: ...


@runtime_checkable
class AddressingClassifier(Protocol):
    """判定「這句話是不是在對機器人講」。

    **Phase 0-4 永遠回傳 True**（單人本機測試，沒有第二個人可以講話）。

    留這個介面是因為 Discord 多人場景是既定終局，而學術上 addressee
    detection 至今仍是開放問題——沒有現成的開箱方案，商用系統普遍退回
    wake-word 這種折衷。既然註定要自己做，介面位置現在就要留好，
    否則 Phase 5 會被迫推翻 :class:`~uep.stream.contracts.stages.ThinkStage`
    的入口契約。
    """

    name: str

    def is_addressed_to_bot(self, utterance: Utterance) -> bool: ...


@dataclass(slots=True)
class TurnPolicy:
    """turn 判定的可調參數。

    預設值取自業界交叉比對，偏保守以求 Phase 0 穩定。
    生產環境的 turn-taking gap 標準是 200-400ms，但那是有語意模型撐著的
    數字；純 VAD 版硬調到 300ms 會讓思考停頓被大量誤判。
    """

    silence_threshold_s: float = 0.7
    """判定 turn 結束的靜音時長。

    OpenAI Realtime ``server_vad`` 預設 500ms，ElevenLabs 建議 5-10s
    （隨性對話）。700ms 是「單人測試、沒有多人干擾」場景下
    穩定性與反應速度的折衷。"""

    max_wait_s: float = 8.0
    """單一 turn 的最長等待。超過就強制結束，避免無限等待。"""

    min_utterance_s: float = 0.3
    """短於此的語音不成一個 turn（咳嗽、雜訊）。"""

    interruption_min_speech_s: float = 0.4
    """barge-in 的連續語音時長門檻。

    LiveKit 預設 0.5s、Vapi 可調 50-500ms。0.4s 是折衷起點，
    要用真實聽感測——太短會被咳嗽打斷，太長會讓插話反應遲鈍。"""

    interruption_min_words: int = 0
    """barge-in 的字數門檻。0 表示停用（Phase 0 沒有即時轉錄可用）。

    啟用時與時長門檻的關係是 **AND**（兩個都要滿足）——
    這是刻意與 LiveKit 的行為區隔，見 :class:`InterruptionDetector` 的說明。"""

    allow_interruptions: bool = True
    """全域開關。關掉可用來測「無插話」的基準延遲。"""

    interruption_backoff_s: float = 0.0
    """被插話後，機器人隔多久才能再開口。

    Vapi 的 ``backoffSeconds``：快節奏場景 0、健康照護建議 2.0。
    這是**設計選擇而非技術限制**，所以做成參數。"""

    metadata: dict = field(default_factory=dict)
