"""SpeechStyle——backend 無關的語音風格描述。

## 為什麼要這一層

五套 TTS 的控制方式完全不同：

| 後端 | 控制方式 |
|------|---------|
| IndexTTS 2.5 | 八維 emotion vector + ``emo_alpha`` + ``duration_factor`` |
| Chatterbox V3 | ``exaggeration`` + ``cfg_weight`` |
| Qwen3-TTS / CosyVoice 3 | 自然語言 instruction（「輕柔、有點疲憊但仍友善」）|
| Fish Audio S2 | 行內 tag（``[whisper]`` ``[excited]``）|
| VoxCPM2 | Voice Design + Style Guidance |

把其中任何一套的參數直接當成管線契約，就等於把管線綁死在那個後端上。
所以這裡定義一個**共同的描述**，由各 adapter 翻譯成自己後端的控制方式。

## 三個維度是刻意分開的

情緒、強度、表現力是三件事：

* ``emotion={"sad": 0.8}`` + ``expressiveness=0.2`` → 很悲傷，但幾乎沒表現出來
* ``emotion={"happy": 0.4}`` + ``expressiveness=0.9`` → 只有一點高興，但表現得很誇張

這個區分對角色表演很重要，而多數後端只給一個「情緒強度」旋鈕。

## phrase-level 控制怎麼表達

Fish S2 那種句中切換情緒（「我本來以為沒問題，[hesitant]但好像有哪裡怪怪的…」），
在這條管線裡**不需要額外的結構**——:class:`~uep.stream.contracts.types.Sentence`
本身就是切分單元，phrase-level 等於「切更細的 Sentence，每個帶自己的 style」。

這樣契約保持最小，而且天然接上既有的 SentenceSplitter：
要做到 phrase-level，只要讓 splitter 在情緒轉折處也切一刀即可。

## 這一層不決定情緒是什麼

**「這句話該用什麼情緒」是 ThinkStage / LLM 的判斷，不是管線的。**
這裡只定義怎麼把那個判斷傳下去。契約層一旦開始猜情緒，
就違反了「不實作任何推理邏輯」的範圍限制。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

EMOTION_DIMENSIONS: tuple[str, ...] = (
    "happy",
    "angry",
    "sad",
    "afraid",
    "disgusted",
    "melancholic",
    "surprised",
    "calm",
)
"""八個情緒維度。

順序取自 IndexTTS 2.5 的 emotion vector——那是既有資產，沿用它的順序
可以讓 adapter 的轉換是零成本的。但這組維度本身語意通用，
其他後端也能對應（或忽略）。
"""


@dataclass(frozen=True, slots=True)
class SpeechStyle:
    """一段語音的風格描述。不可變——同一個 style 會被多個句子共用。"""

    emotion: dict[str, float] = field(default_factory=dict)
    """情緒分佈。key 取自 :data:`EMOTION_DIMENSIONS`，值 0.0-1.0。
    未列出的維度視為 0。空 dict 代表「用後端的預設」。"""

    intensity: float = 0.5
    """情緒強度，0~1 的**相對**刻度，由 adapter 映射到後端的可用區間——
    0.5 = 後端定案區間的中點（IndexTTS2 的區間由 echo_tts ``user_to_strength`` 決定）。
    不是絕對值：contracts 不知道任何後端的數字。"""

    expressiveness: float | None = None
    """表現力——「這個人講話有多戲劇化」。對應 Chatterbox 的 ``exaggeration``。

    與 :attr:`intensity` 是不同的軸：情緒有多強 vs 表現得有多明顯。
    ``None`` 表示不指定，由後端決定。"""

    speed: float = 1.0
    """語速倍率。1.0 = 後端預設，>1 較快。

    刻意用**倍率**而不是各後端的原生參數（IndexTTS 是 -1~1 的偏移、
    有些是 ``duration_factor``），adapter 負責換算。"""

    volume: float = 1.0
    """音量倍率。多數後端不支援，會被忽略。"""

    style: str = ""
    """自然語言的風格描述（「輕柔、有點疲憊，但仍然保持友善」）。

    給 Qwen3-TTS / CosyVoice 3 / VoxCPM2 這類吃 instruction 的後端。
    IndexTTS 這種吃向量的後端會忽略它——**兩者並存是刻意的**，
    因為同一個 SpeechStyle 要能餵給不同後端，各取所需。"""

    def normalized_emotion(self) -> dict[str, float]:
        """回傳補齊八個維度、值夾在 0-1 的情緒分佈。"""
        return {
            dim: max(0.0, min(1.0, float(self.emotion.get(dim, 0.0))))
            for dim in EMOTION_DIMENSIONS
        }

    def emotion_vector(self) -> list[float]:
        """轉成 :data:`EMOTION_DIMENSIONS` 順序的八維向量。"""
        normalized = self.normalized_emotion()
        return [normalized[dim] for dim in EMOTION_DIMENSIONS]

    @property
    def is_neutral(self) -> bool:
        """是否沒有指定任何情緒——adapter 可據此走「不動後端狀態」的快路徑。"""
        return not any(v > 0.0 for v in self.emotion.values())

    def dominant(self) -> tuple[str, float] | None:
        """最強的情緒維度。給只支援單一情緒標籤的後端用。"""
        normalized = self.normalized_emotion()
        best = max(normalized.items(), key=lambda kv: kv[1])
        return best if best[1] > 0.0 else None

    def merged(self, other: SpeechStyle | None) -> SpeechStyle:
        """用 ``other`` 覆寫本身有指定的欄位。

        用於「turn 級預設 + 句級覆寫」：整段回應設一個基調，
        某幾句再局部調整。
        """
        if other is None:
            return self
        return replace(
            self,
            emotion={**self.emotion, **other.emotion} if other.emotion else self.emotion,
            intensity=other.intensity,
            expressiveness=(
                other.expressiveness if other.expressiveness is not None else self.expressiveness
            ),
            speed=other.speed,
            volume=other.volume,
            style=other.style or self.style,
        )


NEUTRAL = SpeechStyle()
"""中性風格。adapter 遇到這個應該完全不動後端狀態。"""


__all__ = ["EMOTION_DIMENSIONS", "SpeechStyle", "NEUTRAL"]
