"""SIM 計算（resemblyzer 對 R1）。共用環境沒有 resemblyzer，改用 .venv-voxcpm 的 CPU 行程，只做評分、不載入任何 TTS 模型。

  .venv-voxcpm/Scripts/python.exe experiments/emotion_sim.py <list.json> <out.json>
list.json: {"key": "絕對路徑", ...}；out.json: {"key": sim}
"""
import json
import sys

import numpy as np

R1 = r"C:\Users\Bernie\source\repos\Unforgettableeternalproject\TTS-wt-model-eval\echo_tts\assets\reference\uep_reference_v25.wav"


def main(src, dst):
    from resemblyzer import VoiceEncoder, preprocess_wav
    enc = VoiceEncoder("cpu")
    emb = lambda p: enc.embed_utterance(preprocess_wav(p))  # noqa: E731
    r1 = emb(R1)
    items = json.load(open(src, encoding="utf-8"))
    out = {}
    for k, p in items.items():
        e = emb(p)
        out[k] = round(float(np.dot(r1, e) / (np.linalg.norm(r1) * np.linalg.norm(e))), 4)
    json.dump(out, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("sim done", len(out))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
