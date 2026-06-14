"""
Emotion Dynamics — 情緒驅動的推論參數動態調控

根據 8D 情緒向量動態調整 TTS 推論參數，讓不同情緒有不同的語音特徵：
- temperature：控制 GPT 採樣隨機性（高情緒→高溫度→更豐富的語調變化）
- interval_silence_ms：段間靜音（悲傷→長停頓, 憤怒→短促）
- s2mel_stretch_ratio：語速控制（憤怒→快, 悲傷→慢）
- speed_variation：段落間語速擺幅（驚訝→大起伏, 憤怒→穩定快）
- cfm_inference_cfg_rate：style conditioning 強度（強情緒→高）

三層情緒系統中的「第三層」。

使用方式：
    dynamics = EmotionDynamics()
    overrides = dynamics.compute(emotion_vector)
    # overrides.segment_stretch(seg_idx) → 帶段落變化的 stretch_ratio
"""

import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# 情緒維度名稱索引
EMO_HAPPY = 0
EMO_ANGRY = 1
EMO_SAD = 2
EMO_AFRAID = 3
EMO_DISGUSTED = 4
EMO_MELANCHOLIC = 5
EMO_SURPRISED = 6
EMO_CALM = 7

EMO_NAMES = ["happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm"]


@dataclass
class EmotionProfile:
    """單一情緒維度對推論參數的影響配置"""
    temperature_offset: float = 0.0       # 相對於 base temperature 的偏移
    silence_ms_offset: float = 0.0        # 相對於 base interval_silence_ms 的偏移
    stretch_ratio_offset: float = 0.0     # 相對於 base s2mel_stretch_ratio 的偏移
    speed_variation: float = 0.0          # 段落間語速擺幅（±stretch_ratio 最大偏移量）
    cfg_rate_offset: float = 0.0          # 相對於 base cfm_inference_cfg_rate 的偏移


@dataclass
class DynamicsOverrides:
    """情緒動態調控的推論參數覆寫"""
    temperature: Optional[float] = None
    interval_silence_ms: Optional[float] = None
    s2mel_stretch_ratio: Optional[float] = None
    speed_variation: float = 0.0          # 加權後的段落語速擺幅
    cfm_inference_cfg_rate: Optional[float] = None
    dominant_emotion: str = "neutral"
    emotion_intensity: float = 0.0

    # ── 安全範圍（由 EmotionDynamics 設定）──
    _stretch_range: tuple = (1.2, 2.2)

    def segment_stretch(self, seg_idx: int) -> float:
        """取得帶段落變化的 stretch_ratio。

        每個段落根據 seg_idx 和 speed_variation 產生穩定的隨機偏移，
        讓同一次合成的重複呼叫得到一致結果。

        Args:
            seg_idx: 段落索引（用作 seed 的一部分）

        Returns:
            該段落的 stretch_ratio（已 clamp 到安全範圍）
        """
        base = self.s2mel_stretch_ratio if self.s2mel_stretch_ratio is not None else 1.7
        if self.speed_variation <= 0.001:
            return base

        # 用 seg_idx 產生穩定但有變化的偏移
        rng = random.Random(hash(("seg_stretch", seg_idx)))
        offset = rng.gauss(0, self.speed_variation * 0.5)  # 68% 在 ±0.5*variation 內
        result = base + offset
        return max(self._stretch_range[0], min(self._stretch_range[1], result))

    def to_dict(self) -> Dict[str, float]:
        """轉為 dict，只包含非 None 的值"""
        d = {}
        if self.temperature is not None:
            d["temperature"] = self.temperature
        if self.interval_silence_ms is not None:
            d["interval_silence_ms"] = self.interval_silence_ms
        if self.s2mel_stretch_ratio is not None:
            d["s2mel_stretch_ratio"] = self.s2mel_stretch_ratio
        if self.speed_variation > 0:
            d["speed_variation"] = self.speed_variation
        if self.cfm_inference_cfg_rate is not None:
            d["cfm_inference_cfg_rate"] = self.cfm_inference_cfg_rate
        return d


# ── 每種情緒的推論參數影響 ──
# 設計原則：
#   - 高能量情緒（angry, surprised）→ 高 temperature, 短靜音, 快語速
#   - 低能量情緒（sad, melancholic）→ 低 temperature, 長靜音, 慢語速
#   - 平靜類（calm, happy）→ 中等 temperature, 正常靜音
#   - cfg_rate 在強情緒時提高（加強 style conditioning）
#   - speed_variation：段落間語速擺幅
#     · 低 variation（0.01~0.03）= 穩定節奏（angry, calm）
#     · 中 variation（0.04~0.06）= 自然起伏（happy, sad）
#     · 高 variation（0.07~0.10）= 不穩定節奏（surprised, afraid）
DEFAULT_PROFILES: Dict[int, EmotionProfile] = {
    EMO_HAPPY: EmotionProfile(
        temperature_offset=+0.05,     # 稍微活潑
        silence_ms_offset=-20,        # 稍短停頓
        stretch_ratio_offset=-0.05,   # 稍快
        speed_variation=0.03,         # 輕微起伏，自然愉悅
        cfg_rate_offset=+0.05,
    ),
    EMO_ANGRY: EmotionProfile(
        temperature_offset=+0.10,     # 更多變化
        silence_ms_offset=-80,        # 短促急迫
        stretch_ratio_offset=-0.20,   # 明顯加快
        speed_variation=0.02,         # 穩定的快——怒氣不動搖
        cfg_rate_offset=+0.10,
    ),
    EMO_SAD: EmotionProfile(
        temperature_offset=-0.10,     # 更壓抑
        silence_ms_offset=+100,       # 長停頓
        stretch_ratio_offset=+0.25,   # 明顯變慢
        speed_variation=0.04,         # 中等起伏——偶爾欲言又止
        cfg_rate_offset=+0.05,
    ),
    EMO_AFRAID: EmotionProfile(
        temperature_offset=+0.08,     # 顫抖感
        silence_ms_offset=-30,        # 略短（緊張）
        stretch_ratio_offset=-0.10,   # 略快（急促）
        speed_variation=0.08,         # 高起伏——緊張顫抖，語速不穩
        cfg_rate_offset=+0.08,
    ),
    EMO_DISGUSTED: EmotionProfile(
        temperature_offset=+0.03,
        silence_ms_offset=+20,
        stretch_ratio_offset=+0.05,   # 略慢（不耐煩）
        speed_variation=0.02,         # 穩定——冷淡且一致
        cfg_rate_offset=+0.05,
    ),
    EMO_MELANCHOLIC: EmotionProfile(
        temperature_offset=-0.15,     # 最壓抑
        silence_ms_offset=+120,       # 最長停頓
        stretch_ratio_offset=+0.30,   # 最慢
        speed_variation=0.05,         # 中等——時快時慢的遊離感
        cfg_rate_offset=+0.03,
    ),
    EMO_SURPRISED: EmotionProfile(
        temperature_offset=+0.15,     # 最多變化
        silence_ms_offset=-60,        # 短促
        stretch_ratio_offset=-0.15,   # 快
        speed_variation=0.10,         # 最高——驚訝時語速很不穩定
        cfg_rate_offset=+0.10,
    ),
    EMO_CALM: EmotionProfile(
        temperature_offset=-0.05,     # 穩定
        silence_ms_offset=+30,        # 自然停頓
        stretch_ratio_offset=+0.08,   # 略慢
        speed_variation=0.01,         # 最低——平穩如水
        cfg_rate_offset=-0.05,        # 降低 style conditioning（更自然）
    ),
}


class EmotionDynamics:
    """
    情緒驅動的推論參數動態調控器。

    根據 8D 情緒向量計算加權的參數偏移，覆寫 engine 的 config 預設值。
    只在情緒強度超過閾值時才產生覆寫（避免中性情緒造成不必要的偏移）。
    """

    def __init__(
        self,
        enabled: bool = True,
        activation_threshold: float = 0.05,
        intensity_scale: float = 1.0,
        profiles: Optional[Dict[int, EmotionProfile]] = None,
        # 參數安全邊界
        temperature_range: tuple = (0.5, 1.2),
        silence_ms_range: tuple = (50, 500),
        stretch_ratio_range: tuple = (1.2, 2.2),
        cfg_rate_range: tuple = (0.3, 0.95),
    ):
        """
        Args:
            enabled: 是否啟用動態調控
            activation_threshold: 情緒強度低於此值時不產生覆寫
            intensity_scale: 全局強度縮放（>1 加強效果, <1 減弱）
            profiles: 每種情緒的參數影響配置（None = 使用預設）
            temperature_range: temperature 安全範圍 (min, max)
            silence_ms_range: interval_silence_ms 安全範圍
            stretch_ratio_range: s2mel_stretch_ratio 安全範圍
            cfg_rate_range: cfm_inference_cfg_rate 安全範圍
        """
        self.enabled = enabled
        self.activation_threshold = activation_threshold
        self.intensity_scale = intensity_scale
        self.profiles = profiles or DEFAULT_PROFILES
        self.temperature_range = temperature_range
        self.silence_ms_range = silence_ms_range
        self.stretch_ratio_range = stretch_ratio_range
        self.cfg_rate_range = cfg_rate_range

    def compute(
        self,
        emotion_vector: List[float],
        base_temperature: float = 0.8,
        base_silence_ms: float = 200,
        base_stretch_ratio: float = 1.72,
        base_cfg_rate: float = 0.7,
    ) -> DynamicsOverrides:
        """
        根據情緒向量計算推論參數覆寫。

        Args:
            emotion_vector: 8D 情緒向量 (emo_bias 套用後、歸一化後)
            base_*: 各參數的 config 基準值

        Returns:
            DynamicsOverrides，包含計算後的參數值
        """
        if not self.enabled or len(emotion_vector) != 8:
            return DynamicsOverrides()

        total_intensity = sum(emotion_vector)
        if total_intensity < self.activation_threshold:
            return DynamicsOverrides(dominant_emotion="neutral", emotion_intensity=0.0)

        # 找出主導情緒
        max_val = max(emotion_vector)
        dominant_idx = emotion_vector.index(max_val)
        dominant_name = EMO_NAMES[dominant_idx] if dominant_idx < len(EMO_NAMES) else "unknown"

        # 加權計算偏移
        temp_offset = 0.0
        silence_offset = 0.0
        stretch_offset = 0.0
        variation_sum = 0.0
        cfg_offset = 0.0

        for idx, weight in enumerate(emotion_vector):
            if weight <= 0 or idx not in self.profiles:
                continue
            profile = self.profiles[idx]
            scaled_weight = weight * self.intensity_scale
            temp_offset += profile.temperature_offset * scaled_weight
            silence_offset += profile.silence_ms_offset * scaled_weight
            stretch_offset += profile.stretch_ratio_offset * scaled_weight
            variation_sum += profile.speed_variation * scaled_weight
            cfg_offset += profile.cfg_rate_offset * scaled_weight

        # 套用偏移並 clamp 到安全範圍
        temperature = self._clamp(
            base_temperature + temp_offset,
            self.temperature_range[0], self.temperature_range[1]
        )
        silence_ms = self._clamp(
            base_silence_ms + silence_offset,
            self.silence_ms_range[0], self.silence_ms_range[1]
        )
        stretch_ratio = self._clamp(
            base_stretch_ratio + stretch_offset,
            self.stretch_ratio_range[0], self.stretch_ratio_range[1]
        )
        cfg_rate = self._clamp(
            base_cfg_rate + cfg_offset,
            self.cfg_rate_range[0], self.cfg_rate_range[1]
        )

        return DynamicsOverrides(
            temperature=temperature,
            interval_silence_ms=silence_ms,
            s2mel_stretch_ratio=stretch_ratio,
            speed_variation=variation_sum,
            cfm_inference_cfg_rate=cfg_rate,
            dominant_emotion=dominant_name,
            emotion_intensity=total_intensity,
            _stretch_range=self.stretch_ratio_range,
        )

    @staticmethod
    def _clamp(value: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, value))


def create_emotion_dynamics(config: dict) -> EmotionDynamics:
    """
    從 config dict 建立 EmotionDynamics 實例。

    config.yaml 預期格式：
        emotion_dynamics:
          enabled: true
          activation_threshold: 0.05
          intensity_scale: 1.0
    """
    cfg = config.get('emotion_dynamics', {})
    return EmotionDynamics(
        enabled=cfg.get('enabled', True),
        activation_threshold=float(cfg.get('activation_threshold', 0.05)),
        intensity_scale=float(cfg.get('intensity_scale', 1.0)),
    )
