"""重生 s03 的 V25-L085（core env）：參數同 tw_accent_check.cmd_gen25（LoRA scale 0.85、無情緒向量、seed = SEED+句序），
並比照 cmd_build 後處理（修靜音 top_db=40、補頭尾、-23 LUFS）覆蓋 accent_check/s03/s03__V25-L085.wav。"""
import os
import sys
from math import gcd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tw_accent_check as T  # noqa: E402


def main():
    sys.path.insert(0, T.ROOT)
    import librosa
    import numpy as np
    import pyloudnorm as pyln
    import soundfile as sf
    import torch
    from scipy.signal import resample_poly

    from echo_tts.runtime.indextts_backend import IndexTTS2Backend
    from echo_tts.runtime.indextts_generate import default_generation_paths
    idx = [s[0] for s in T.SENTENCES].index("s03")
    text = T.SENTENCES[idx][1]
    backend = IndexTTS2Backend(paths=default_generation_paths(T.ROOT), device="cuda", use_fp16=True, use_cuda_kernel=True, use_accel=False)
    backend.load()
    backend.load_lora(scale=0.85)
    backend.emotion_vector = None
    backend.set_lora_scale(0.85)
    os.makedirs(T.RAW, exist_ok=True)
    raw = os.path.join(T.RAW, "s03__V25-L085.wav")
    torch.manual_seed(T.SEED + idx)
    backend.generate(text, raw, language="zh")
    y, sr = sf.read(raw, dtype="float64")
    y = y if y.ndim == 1 else y.mean(axis=1)
    if sr != 44100:
        g = gcd(sr, 44100)
        y = resample_poly(y, 44100 // g, sr // g)
    yt, _ = librosa.effects.trim(y, top_db=40, frame_length=1024, hop_length=256)
    y = np.concatenate([np.zeros(int(44100 * T.PAD_HEAD_S)), yt, np.zeros(int(44100 * T.PAD_TAIL_S))])
    y = pyln.normalize.loudness(y, pyln.Meter(44100).integrated_loudness(y), T.TARGET_LUFS)
    pk = float(np.max(np.abs(y)))
    if pk > 0.98:
        y *= 0.98 / pk
    sf.write(os.path.join(T.DEST, "s03", "s03__V25-L085.wav"), y, 44100, subtype="PCM_16")
    print("done", text, round(len(y) / 44100, 2), "seed", T.SEED + idx)


if __name__ == "__main__":
    main()
