"""Echo-Stream 實際使用的台灣腔 ASR（Breeze2.5 ct2 fp16，faster_whisper 載入）。

參數對齊 Echo-Stream/echo_stream/adapters/stt_discord.py 的 DiscordSttProfile 預設：
language=zh、beam_size=5、temperature=(0.0, 0.2)、local_files_only。
模型：TestSeparateSTTSystem/models/stt/whisper/whisper-breeze25-ct2-fp16（該 repo 內唯一的 Breeze 模型；
Echo-Stream 的 model_path 由 bot 設定提供、不在 repo 內，故以此為準）。
"""
import os

REPOS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEL = os.path.join(REPOS, "TestSeparateSTTSystem", "models", "stt", "whisper", "whisper-breeze25-ct2-fp16")
_wm = None


def load():
    global _wm
    if _wm is None:
        from faster_whisper import WhisperModel
        _wm = WhisperModel(MODEL, device="cuda", compute_type="float16", local_files_only=True)
    return _wm


def transcribe(path_or_array, language="zh"):
    import librosa
    y = librosa.load(path_or_array, sr=16000, mono=True)[0] if isinstance(path_or_array, str) else path_or_array
    segs, _ = load().transcribe(y, language=language, beam_size=5, temperature=[0.0, 0.2])
    return "".join(s.text for s in segs).strip()
