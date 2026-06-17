"""語音活動偵測 — 基於能量分析的 VAD + 自適應串流 VAD"""

from echo_stt.vad.energy_vad import VoiceActivityDetection
from echo_stt.vad.advanced_vad import AdaptiveTrailingSilenceConfig

__all__ = ["VoiceActivityDetection", "AdaptiveTrailingSilenceConfig"]
