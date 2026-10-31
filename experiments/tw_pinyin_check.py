"""步驟 1：台灣發音前處理對照（VOX-ULT-TWN / VOX-R1-TWN）。

沿用 tw_accent_check.py 的 8 句、seed（SEED+句序）、參考音與後處理；只在送入模型前套用
TaiwanPronunciationNormalizer。無前處理的版本沿用上一輪檔案
（VOX-ULT = VOX-ULT-L085，即 neutral.wav 參考；VOX-R1）。

  gen    [.venv-voxcpm] 生成 raw 兩版
  build  [.venv-voxcpm] 修靜音、-23 LUFS、44.1kHz，放進對照資料夾並追加 README 段落
"""
import argparse
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tw_accent_check as T  # noqa: E402
from taiwan_pronunciation import TaiwanPronunciationNormalizer, dictionary_markdown  # noqa: E402

OUT = os.path.join(ROOT, "outputs", "tw_pinyin_check")
RAW = os.path.join(OUT, "raw")
DEST = T.DEST
MARK = "## 步驟 1：台灣發音前處理（TWN 版）"


def cmd_gen(args):
    import numpy as np
    import soundfile as sf
    import torch
    from voxcpm import VoxCPM
    os.makedirs(RAW, exist_ok=True)
    norm = TaiwanPronunciationNormalizer()
    model = VoxCPM.from_pretrained(T.MODEL_DIR, load_denoiser=False, optimize=False, local_files_only=True)
    sr = model.tts_model.sample_rate
    n_path, n_text = T.neutral_ref()
    sent_log = {}

    def run(text, seed, **kw):
        torch.manual_seed(seed)
        chunks = list(model.generate_streaming(text=text, cfg_value=2.0, inference_timesteps=10,
                                               normalize=False, **kw))
        return np.concatenate(chunks)

    run("你好。", 1, reference_wav_path=T.R1_16K)  # 暖機（與上一輪相同）
    for i, (sid, text, _, _) in enumerate(T.SENTENCES):
        sent, log = norm.explain(text)
        sent_log[sid] = {"original": text, "sent": sent, "replaced": log}
        specs = {
            "VOX-R1-TWN": {"reference_wav_path": T.R1_16K},
            "VOX-ULT-TWN": {"reference_wav_path": n_path, "prompt_wav_path": n_path, "prompt_text": n_text},
        }
        for ver, kw in specs.items():
            wav = run(sent, T.SEED + i, **kw)
            sf.write(os.path.join(RAW, f"{sid}__{ver}.wav"), wav, sr)
            print(sid, ver, round(len(wav) / sr, 2), sent, flush=True)
    T.jdump(os.path.join(OUT, "sent.json"), sent_log)


def cmd_build(args):
    from math import gcd

    import librosa
    import numpy as np
    import pyloudnorm as pyln
    import soundfile as sf
    from scipy.signal import resample_poly
    sr_t = 44100
    meter = pyln.Meter(sr_t)
    for sid, _, _, _ in T.SENTENCES:
        for ver in ("VOX-R1-TWN", "VOX-ULT-TWN"):
            y, sr = sf.read(os.path.join(RAW, f"{sid}__{ver}.wav"), dtype="float64")
            if y.ndim > 1:
                y = y.mean(axis=1)
            if sr != sr_t:
                g = gcd(sr, sr_t)
                y = resample_poly(y, sr_t // g, sr // g)
            yt, _ = librosa.effects.trim(y, top_db=40, frame_length=1024, hop_length=256)
            y = np.concatenate([np.zeros(int(sr_t * T.PAD_HEAD_S)), yt, np.zeros(int(sr_t * T.PAD_TAIL_S))])
            y = pyln.normalize.loudness(y, meter.integrated_loudness(y), T.TARGET_LUFS)
            pk = float(np.max(np.abs(y)))
            if pk > 0.98:
                y *= 0.98 / pk
            d = os.path.join(DEST, sid)
            os.makedirs(d, exist_ok=True)
            sf.write(os.path.join(d, f"{sid}__{ver}.wav"), y, sr_t, subtype="PCM_16")
    sent = json.load(open(os.path.join(OUT, "sent.json"), encoding="utf-8"))
    L = ["", MARK, "",
         "每句新增 2 版：`VOX-ULT-TWN`（= VOX-ULT-L085 加前處理）、`VOX-R1-TWN`（= VOX-R1 加前處理）。",
         "不加前處理的對照版沿用上一輪檔案（VOX-ULT-L085、VOX-R1），seed、cfg 2.0、steps 10 完全相同，"
         "差別只有送入模型的文字（`normalize=False`，使用官方 `{pin1yin1}` 拼音輸入）。", "",
         "### 每句實際送進模型的文字", ""]
    for sid, v in sent.items():
        rep = "、".join(f"{r['word']}[{r['status']}]" for r in v["replaced"]) or "（無替換）"
        L += [f"- {sid}：`{v['sent']}`", f"  - 原句：{v['original']}", f"  - 替換：{rep}"]
    L += ["", "### 字典內容（初版）", "",
          "只替換需要修正的字；多音字以詞組比對（最長詞優先），不做單字替換。",
          "依據為教育部《國語辭典簡編本》，2026-10-31 逐詞查證；「待確認」代表簡編本查不到或無法確認義項。", "",
          dictionary_markdown(), "",
          "連詞「和」只在人稱代名詞前後觸發（我和你、他和她…），「和平／和氣／和諧」等詞排除。", ""]
    p = os.path.join(DEST, "README.md")
    txt = open(p, encoding="utf-8").read()
    if MARK in txt:
        txt = txt[:txt.index(MARK)].rstrip("\n") + "\n"
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(txt + "\n".join(L) + "\n")
    print("done", DEST)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["gen", "build"])
    a = ap.parse_args()
    {"gen": cmd_gen, "build": cmd_build}[a.stage](a)
