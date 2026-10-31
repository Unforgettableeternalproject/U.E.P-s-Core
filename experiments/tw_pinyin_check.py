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
    only = set(args.only.split(",")) if args.only else None
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
    sp = os.path.join(OUT, "sent.json")
    if only and os.path.exists(sp):
        sent_log = json.load(open(sp, encoding="utf-8"))
    for i, (sid, text, _, _) in enumerate(T.SENTENCES):
        if only and sid not in only:
            continue
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


SPD_SIDS = ("s01", "s04", "s08")
SPD_MARK = "## 語速調整對照（SPD 版）"


def _stretch_wsola(y, sr, rate):
    """保音高時間伸縮（WSOLA，audiotsm）；rate>1 加快。系統無 rubberband，故未用 pyrubberband。"""
    import numpy as np
    from audiotsm import wsola
    from audiotsm.io.array import ArrayReader, ArrayWriter
    r = ArrayReader(y.astype("float32")[None, :])
    w = ArrayWriter(1)
    wsola(1, speed=rate).run(r, w)
    return w.data[0].astype(np.float64)


def _post(y, sr_t, top_db=40):
    """與 build 相同的修靜音、補頭尾、-23 LUFS；回傳 (音訊, 語音段秒數)。"""
    import librosa
    import numpy as np
    import pyloudnorm as pyln
    yt, _ = librosa.effects.trim(y, top_db=top_db, frame_length=1024, hop_length=256)
    speech_s = len(yt) / sr_t
    y = np.concatenate([np.zeros(int(sr_t * T.PAD_HEAD_S)), yt, np.zeros(int(sr_t * T.PAD_TAIL_S))])
    y = pyln.normalize.loudness(y, pyln.Meter(sr_t).integrated_loudness(y), T.TARGET_LUFS)
    pk = float(np.max(np.abs(y)))
    if pk > 0.98:
        y *= 0.98 / pk
    return y, speech_s


def cmd_spd(args):
    from math import gcd

    import librosa
    import numpy as np
    import soundfile as sf
    import torch
    from scipy.signal import resample_poly
    from voxcpm import VoxCPM
    sr_t = 44100
    rawd = os.path.join(OUT, "spd_raw")
    os.makedirs(rawd, exist_ok=True)
    sent = json.load(open(os.path.join(OUT, "sent.json"), encoding="utf-8"))
    n_path, n_text = T.neutral_ref()
    # a. 參考音加速（保音高），逐字稿不變
    y16, _ = librosa.load(n_path, sr=16000, mono=True)
    refs = {}
    for tag, rate in (("ref110", 1.1), ("ref120", 1.2)):
        p = os.path.join(rawd, f"neutral_{tag}.wav")
        sf.write(p, _stretch_wsola(y16, 16000, rate), 16000)
        refs[tag] = p
    model = VoxCPM.from_pretrained(T.MODEL_DIR, load_denoiser=False, optimize=False, local_files_only=True)
    sr = model.tts_model.sample_rate

    def run(text, seed, ref):
        torch.manual_seed(seed)
        ch = list(model.generate_streaming(text=text, cfg_value=2.0, inference_timesteps=10, normalize=False,
                                           reference_wav_path=ref, prompt_wav_path=ref, prompt_text=n_text))
        return np.concatenate(ch)

    run("你好。", 1, n_path)
    stats, out = {}, {}
    for i, (sid, *_r) in enumerate(T.SENTENCES):
        if sid not in SPD_SIDS:
            continue
        s = sent[sid]["sent"]
        nchar = sum("一" <= c <= "鿿" for c in sent[sid]["original"])
        seed = T.SEED + i
        base, _ = sf.read(os.path.join(RAW, f"{sid}__VOX-ULT-TWN.wav"), dtype="float64")
        base_sr = sr
        vers = {"ref110": run(s, seed, refs["ref110"]), "ref120": run(s, seed, refs["ref120"]),
                "hint": run("(語速稍快)" + s, seed, n_path), "hintEn": run("(slightly faster)" + s, seed, n_path)}
        for tag, rate in (("post110", 1.1), ("post120", 1.2)):
            vers[tag] = _stretch_wsola(base if base.ndim == 1 else base.mean(axis=1), sr, rate)
        vers["base"] = base
        for tag, w in vers.items():
            w = np.asarray(w, dtype="float64")
            g = gcd(sr, sr_t)
            w44 = resample_poly(w, sr_t // g, sr // g)
            fin, speech_s = _post(w44, sr_t)
            stats[f"{sid}__{tag}"] = {"speech_s": round(speech_s, 2), "s_per_char": round(speech_s / nchar, 3), "chars": nchar}
            if tag != "base":
                sf.write(os.path.join(DEST, sid, f"{sid}__VOX-ULT-TWN-SPD-{tag}.wav"), fin, sr_t, subtype="PCM_16")
        print(sid, {k.split("__")[1]: v["s_per_char"] for k, v in stats.items() if k.startswith(sid)}, flush=True)
    T.jdump(os.path.join(OUT, "spd_stats.json"), stats)
    L = ["", SPD_MARK, "",
         "以 s01、s04、s08 的 `VOX-ULT-TWN` 為基礎，同 seed。檔名 `sNN__VOX-ULT-TWN-SPD-<方法><倍率>.wav`：", "",
         "- `ref110` / `ref120`：參考音 neutral.wav 先以 WSOLA 保音高加速 1.1x / 1.2x，再做 ULT 克隆（prompt 與 reference 皆用加速版，逐字稿不變）",
         "- `post110` / `post120`：直接對 VOX-ULT-TWN 輸出做 WSOLA 保音高加速（audiotsm；環境沒有 rubberband 執行檔，故未用 pyrubberband）",
         "- `hint`：文字前加 `(語速稍快)`；`hintEn`：加 `(slightly faster)`（ULT 模式下實際嘗試，非官方保證有效）", "",
         "### 實測語速（語音段秒數 / 中文字數，越小越快；已排除頭尾靜音）", "",
         "| 句 | base(TWN) | ref110 | ref120 | post110 | post120 | hint | hintEn |", "|---|---|---|---|---|---|---|---|"]
    for sid in SPD_SIDS:
        L.append(f"| {sid} | " + " | ".join(str(stats[f"{sid}__{t}"]["s_per_char"]) for t in
                 ("base", "ref110", "ref120", "post110", "post120", "hint", "hintEn")) + " |")
    p = os.path.join(DEST, "README.md")
    txt = open(p, encoding="utf-8").read()
    if SPD_MARK in txt:
        txt = txt[:txt.index(SPD_MARK)].rstrip(chr(10)) + chr(10)
    with open(p, "w", encoding="utf-8", newline=chr(10)) as f:
        f.write(txt + chr(10).join(L) + chr(10))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["gen", "build", "spd"])
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    {"gen": cmd_gen, "build": cmd_build, "spd": cmd_spd}[a.stage](a)
