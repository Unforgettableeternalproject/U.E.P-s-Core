"""語調 A/B 前置：把選定的 zh 參考音以 WSOLA 保音高加速 1.2 倍（同 zh_neutral_x1.2.wav 做法：16 kHz 單聲道）。
在 .venv-voxcpm 執行（有 audiotsm，CPU）。輸出 outputs/prosody_ab/refs/<name>_x1.2.wav。"""
import os
import sys

import librosa
import numpy as np
import soundfile as sf
from audiotsm import wsola
from audiotsm.io.array import ArrayReader, ArrayWriter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "outputs", "prosody_ab", "refs")
EMO = os.path.join(os.path.dirname(ROOT), "TTS-wt-vox-emotion", "echo_tts", "assets", "vox", "emotion")
SRC = {
    "happy057": os.path.join(EMO, "zh_happy_057.wav"),
    "surprised073": os.path.join(ROOT, "outputs", "emotion_v1", "refs", "candidates", "surprised_073_t{take}.wav"),
    "disgusted073": os.path.join(ROOT, "outputs", "emotion_v1", "refs", "candidates", "disgusted_073_t{take}.wav"),
}

if __name__ == "__main__":
    import json
    clips = json.load(open(os.path.join(ROOT, "outputs", "emotion_v1", "refs", "emotion_clips.json"), encoding="utf-8"))["clips"]
    os.makedirs(OUT, exist_ok=True)
    for name, path in SRC.items():
        if "{take}" in path:
            path = os.path.join(ROOT, "outputs", "emotion_v1", "refs", clips[name[:-3]]["073"]["file"])
        y, _ = librosa.load(path, sr=16000, mono=True)
        w = ArrayWriter(1)
        wsola(1, speed=1.2).run(ArrayReader(y.astype("float32")[None, :]), w)
        o = w.data[0].astype(np.float32)
        sf.write(os.path.join(OUT, f"{name}_x1.2.wav"), o, 16000)
        print(name, path, round(len(y) / 16000, 2), "->", round(len(o) / 16000, 2))
