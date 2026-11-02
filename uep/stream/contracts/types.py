"""管線流通的資料型別。

三個 Stage 之間只傳這裡定義的東西，不傳各子系統的原生型別
（torch.Tensor、faster-whisper 的 Segment、Gemini 的 Response 等）——
那些一律由 adapter 在邊界轉換。這是「不變成第六個孤島」的具體防線：
Pipeline Core 不 import 任何子系統。

音訊一律用 **int16 little-endian PCM bytes**，不用 numpy/torch。
理由：Phase 0 骨架保持零第三方依賴，且 bytes 是所有前端
（sounddevice / Discord / WAV 檔）的共同分母。真模組的 tensor
在 adapter 裡轉一次即可。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from .style import SpeechStyle

# --- 取樣率常數（§7.6：Pipeline 內部用各模組原生取樣率，重採樣放 Frontend Adapter）---

STT_SAMPLE_RATE = 16_000
"""faster-whisper 的原生輸入取樣率。"""

TTS_SAMPLE_RATE = 22_050
"""IndexTTS2 的原生輸出取樣率。"""

DISCORD_SAMPLE_RATE = 48_000
"""Discord 語音要求，stereo。由 Frontend Adapter 負責重採樣。"""


def new_turn_id() -> str:
    """產生 turn 識別碼。短一點，因為會大量出現在 trace log 裡。"""
    return uuid.uuid4().hex[:12]


@dataclass(slots=True)
class Utterance:
    """使用者的一個完整輸入（InputStage 的產出）。

    「完整」的定義由 TurnDetector 決定，見 :mod:`uep.stream.contracts.turn`。
    """

    text: str
    turn_id: str = field(default_factory=new_turn_id)
    speaker_id: str | None = None
    """語者識別結果。單人場景為 None；Discord 場景用來做 addressing 判定。"""

    is_final: bool = True
    """False 表示這是部分轉錄（interim result）。

    Phase 0 只產出 final；保留欄位是因為 partial 轉錄可讓 ThinkStage
    提早做 Memory retrieve prefetch（§7.4 平行化的延伸應用）。
    """

    confidence: float = 1.0
    language: str | None = None
    started_at: float = field(default_factory=time.perf_counter)
    """使用者**開始**說話的時刻（perf_counter）。"""

    ended_at: float = field(default_factory=time.perf_counter)
    """使用者**說完**的時刻。TTFA 從這裡起算——這是使用者的主觀等待起點。"""

    metadata: dict = field(default_factory=dict)


@dataclass(slots=True)
class Sentence:
    """可送去合成的一句話（ThinkStage 的產出、SpeakStage 的輸入）。

    「句」不等於語言學上的句子——是 SentenceSplitter 判定的**合成單元**，
    可能因為長度上限而在非句末處切開。
    """

    text: str
    turn_id: str
    index: int
    """在這個 turn 中的序號，從 0 開始。播放順序依此排列。"""

    is_first: bool = False
    """是否為首句。首句切得短（TTFA 優先），標記讓 TTS adapter 可以走快路徑。"""

    is_last: bool = False
    """是否為 turn 的最後一句。用來收尾（flush、寫回對話歷史）。"""

    split_reason: str = ""
    """切點成因：``terminal`` / ``secondary`` / ``max_length`` / ``flush``。
    調參時要看這個分布——``max_length`` 佔比過高代表 LLM 在生成長句，
    首句閾值需要調整。"""

    style: SpeechStyle | None = None
    """這一句的語音風格。``None`` 表示沿用 SpeakStage 的預設。

    掛在句子上而不是整個 turn 上，是為了支援 phrase-level 的情緒切換
    （「我本來以為沒問題，**[遲疑]** 但好像有哪裡怪怪的…」）——
    切更細的句子 + 各自的 style 就能表達，不需要額外的 markup 結構。
    """

    created_at: float = field(default_factory=time.perf_counter)
    metadata: dict = field(default_factory=dict)


@dataclass(slots=True)
class AudioChunk:
    """一段音訊（SpeakStage 的產出）。

    對應 IndexTTS2 的 ``on_segment_audio`` 一次 callback 的產物。
    """

    pcm: bytes
    """int16 little-endian PCM。"""

    sample_rate: int
    turn_id: str
    sentence_index: int
    """來源句子的序號。用於對齊字幕、以及 barge-in 時判斷「講到哪一句」。"""

    chunk_index: int = 0
    """同一句可能被切成多個 chunk（長句 TTS 分段）。"""

    channels: int = 1
    is_last: bool = False
    created_at: float = field(default_factory=time.perf_counter)

    @property
    def duration_s(self) -> float:
        """音訊時長（秒）。用於 barge-in 時計算「實際播出了多少」。"""
        frame_bytes = 2 * self.channels  # int16
        if self.sample_rate <= 0 or frame_bytes == 0:
            return 0.0
        return len(self.pcm) / frame_bytes / self.sample_rate


class TurnPhase(str, Enum):
    """turn 的生命週期。LatencyTracer 依此打點。"""

    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    DONE = "done"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SentenceSpan:
    """一句話在實播時間軸上的區間（秒）。"""

    index: int
    text: str
    start_s: float
    end_s: float


@dataclass(slots=True)
class TurnResult:
    """一個 turn 結束後的摘要。

    ``spoken_text`` 是 barge-in 善後的關鍵：被插話中止時，
    LLM 生成了 10 句但只播出 3 句，寫回對話歷史的必須是那 3 句，
    否則 LLM 會以為自己講完了整段，下一輪的指代會全錯。
    """

    turn_id: str
    phase: TurnPhase
    utterance_text: str = ""
    generated_text: str = ""
    """LLM 實際生成的全部文字（可能多於播出的）。"""

    spoken_text: str = ""
    """實際播出去的文字。被取消時 < generated_text。"""

    spoken_duration_s: float = 0.0
    cancel_reason: str | None = None
    error: str | None = None
    metadata: dict = field(default_factory=dict)

    # --- 輸出端協調（T-07b）追加欄位：全是加法，預設值不影響既有呼叫 ---

    skipped_reason: str | None = None
    """這一輪沒出聲的原因（pass / no_spoken_text / tts_not_ready ...）。None = 有進入播放。"""

    cut_short: bool = False
    """播放被中途截斷（spoken 少於 generated）。"""

    audio_started: bool = False
    """是否已有真音訊交給 sink。"""

    first_audio_at: float | None = None
    """首個真音訊 frame 的 perf_counter 時刻；取不到為 None。"""

    sentence_spans: list[SentenceSpan] = field(default_factory=list)
    """各句在實播時間軸上的區間，供記憶層自行計算已播範圍。"""
