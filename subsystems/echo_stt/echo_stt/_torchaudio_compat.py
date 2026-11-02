"""torchaudio 2.9+ 相容層。

torchaudio 2.9 起移除 ``AudioMetaData``、``list_audio_backends``、
``set_audio_backend``（2.11 已全數移除），而 pyannote.audio 3.x 與
speechbrain 1.0.x 在 import 時仍會存取這些屬性。這裡只在屬性缺少時補上
最小替身，讓第三方套件可以 import；舊版 torchaudio（例如 2.7）下完全不動。

注意：``torchaudio.info`` / ``load`` / ``save`` 不在補丁範圍內，
本專案不經由 pyannote 讀檔（改傳記憶體中的 waveform），音檔讀寫用 soundfile。
"""

import collections
import sys

_PATCHED = False


def ensure_torchaudio_compat() -> bool:
    """補齊缺少的 torchaudio 屬性。回傳是否有實際補上任何屬性。

    torchaudio 未載入時不主動 import（避免輕量使用者被迫載入 torch）；
    需要時由實際 import pyannote 的模組在 import 前再呼叫一次。
    """
    global _PATCHED
    torchaudio = sys.modules.get("torchaudio")
    if torchaudio is None:
        return False

    patched = False
    if not hasattr(torchaudio, "AudioMetaData"):
        torchaudio.AudioMetaData = collections.namedtuple(  # type: ignore[attr-defined]
            "AudioMetaData",
            "sample_rate num_frames num_channels bits_per_sample encoding",
        )
        patched = True
    if not hasattr(torchaudio, "list_audio_backends"):
        torchaudio.list_audio_backends = lambda: ["soundfile"]  # type: ignore[attr-defined]
        patched = True
    if not hasattr(torchaudio, "set_audio_backend"):
        torchaudio.set_audio_backend = lambda *args, **kwargs: None  # type: ignore[attr-defined]
        patched = True

    _PATCHED = _PATCHED or patched
    return patched


# 若 torchaudio 已被載入，import 本模組時即補上
ensure_torchaudio_compat()
