"""語者識別 — pyannote.audio 嵌入 + 多重距離計算"""

from echo_stt.speaker.identification import SpeakerIdentification
from echo_stt.speaker.context_handler import SpeakerContextHandler

__all__ = ["SpeakerIdentification", "SpeakerContextHandler"]
