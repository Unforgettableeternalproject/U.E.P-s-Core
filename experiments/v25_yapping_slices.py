"""從 uep_yapping.wav（5 分鐘原始音訊）切出 15 秒候選窗並各合成試聽句。

背景：上游 infer_v2_5 只取參考音前 15 秒（_load_and_cut_audio(spk, 15)），
整檔餵入等於只用開頭。本腳本每 60 秒取一個 15 秒窗（跳過前 2 秒避免開頭雜訊），
每窗合成同一句中性文字供艾斯維爾挑選最佳參考段。

用法：
    python experiments/v25_yapping_slices.py
"""
import os
import sys
import time

UPSTREAM_REPO = r"C:\Users\Bernie\source\repos\Other\index-tts\index-tts"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CKPT_DIR = os.path.join(PROJECT_ROOT, "echo_tts", "modules", "tts_module", "checkpoints_v25")
SOURCE_WAV = r"C:\Users\Bernie\source\repos\Unforgettableeternalproject\U.E.P-s-Core\outputs\tts\uep_yapping.wav"
SLICE_DIR = os.path.join(PROJECT_ROOT, "outputs", "yapping_slices")
OUT_DIR = os.path.join(PROJECT_ROOT, "outputs", "v25_timbre_yapping")

sys.path.insert(0, UPSTREAM_REPO)

import transformers.pytorch_utils as _pu

if not hasattr(_pu, "is_torch_greater_or_equal_than_1_13"):
    _pu.is_torch_greater_or_equal_than_1_13 = True

from opencc import OpenCC

_T2S = OpenCC("t2s")

NEUTRAL_TEXT = _T2S.convert("今天的天氣不錯，我把桌面上的工作都整理完了，接下來要做什麼呢？")

SLICE_SECONDS = 15.0
HOP_SECONDS = 60.0
SKIP_HEAD_SECONDS = 2.0


def make_slices() -> list[str]:
    import librosa
    import soundfile as sf

    os.makedirs(SLICE_DIR, exist_ok=True)
    audio, sr = librosa.load(SOURCE_WAV, sr=None, mono=True)
    total_s = len(audio) / sr
    print(f">> 來源長度 {total_s:.1f}s @ {sr}Hz", flush=True)

    paths = []
    start = SKIP_HEAD_SECONDS
    idx = 0
    while start + SLICE_SECONDS <= total_s:
        seg = audio[int(start * sr): int((start + SLICE_SECONDS) * sr)]
        path = os.path.join(SLICE_DIR, f"slice_{idx:02d}_at{int(start):03d}s.wav")
        sf.write(path, seg, sr)
        paths.append(path)
        idx += 1
        start += HOP_SECONDS
    print(f">> 切出 {len(paths)} 個 15 秒候選窗", flush=True)
    return paths


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    slices = make_slices()

    from indextts.infer_v2_5 import IndexTTS2

    tts = IndexTTS2(
        cfg_path=os.path.join(CKPT_DIR, "config.yaml"),
        model_dir=CKPT_DIR,
        use_bf16=True,
    )
    print(">> 模型就緒", flush=True)

    for slice_path in slices:
        tag = os.path.splitext(os.path.basename(slice_path))[0]
        out_path = os.path.join(OUT_DIR, f"{tag}.wav")
        t0 = time.perf_counter()
        tts.infer(
            spk_audio_prompt=slice_path,
            text=NEUTRAL_TEXT,
            lang="ZH",
            output_path=out_path,
        )
        print(f">> {tag}: {time.perf_counter() - t0:.2f}s", flush=True)

    print(f"\n=== 完成，試聽輸出：{OUT_DIR}（候選窗原音：{SLICE_DIR}）===")


if __name__ == "__main__":
    main()
