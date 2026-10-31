"""BlueMagpie-TTS 台灣腔對照片段（接續 tw_accent_check.py 的 8 句，同一個輸出資料夾並列）。

全部階段都在 .venv-bluemagpie 執行（輸出 outputs/bluemagpie_tw/）：
  gen    BlueMagpie 各版本生成（固定 seed = SEED+句序，每版 1 次），量測 RTF 與串流首包延遲
  build  修頭尾靜音、-23 LUFS、複製到 DEST/sNN/、Whisper 簡體字數、SIM 對 R1，並在 DEST/README.md 末尾追加說明

版本：
  BM-CENTROID  checkpoints/speaker_centroids.pt 內的 female_voice centroid（模型自帶預設聲音）
  BM-R1        以 R1（uep_reference_v25.wav）抽 speaker embedding
  BM-L085      以 v25_emotion_refs/neutral.wav 抽 speaker embedding
  BM-SPKB      checkpoints/speaker_b_embedding.pt（卡片的 hosted Speaker B 向量）

公開程式碼的 generate() 沒有 stop_threshold／stop_consecutive 參數，停止判斷固定為 stop head argmax。
"""
import argparse
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from tw_accent_check import DEST, EMO_DIR, SENTENCES, SEED, TARGET_LUFS, PAD_HEAD_S, PAD_TAIL_S, neutral_ref  # noqa: E402

ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "outputs", "bluemagpie_tw")
RAW = os.path.join(OUT, "raw")
FINAL = os.path.join(OUT, "final")
MODEL_DIR = os.path.join(ROOT, "models_cache", "BlueMagpie-TTS")
R1 = os.path.join(ROOT, "echo_tts", "assets", "reference", "uep_reference_v25.wav")
NEUTRAL = os.path.join(EMO_DIR, "neutral.wav")
MARK = "## BlueMagpie-TTS 各版本"

VERSIONS = [
    ("BM-CENTROID", "BlueMagpie-TTS 僅用模型自帶 centroid（speaker_centroids.pt 的 female_voice），看模型本身的台灣腔上限"),
    ("BM-R1", "BlueMagpie-TTS speaker-reference-audio-only，R1（uep_reference_v25.wav）抽 ECAPA 向量，無逐字稿"),
    ("BM-L085", "BlueMagpie-TTS speaker-reference-audio-only，v25_emotion_refs/neutral.wav（2.5+LoRA 0.85 生成）抽 ECAPA 向量"),
    ("BM-SPKB", "BlueMagpie-TTS 卡片的 hosted Speaker B 向量（speaker_b_embedding.pt），centroid 模式的另一個聲音預設"),
]


def jdump(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def cmd_gen(args):
    import soundfile as sf
    import torch
    sys.path.insert(0, os.path.join(ROOT, "models_cache", "BlueMagpie-TTS-src", "src"))
    from bluemagpie import BlueMagpieModel, extract_speaker_centroid
    os.makedirs(RAW, exist_ok=True)
    t0 = time.time()
    # tokenizer_config 的 extra_special_tokens 是 list，transformers 4.57 的 AutoTokenizer 讀不了，
    # from_local 會靜默吞掉例外而不掛 tokenizer；改直接用 tokenizer.json 建 fast tokenizer 傳入。
    from transformers import PreTrainedTokenizerFast
    tok = PreTrainedTokenizerFast(tokenizer_file=os.path.join(MODEL_DIR, "tokenizer.json"))
    model = BlueMagpieModel.from_local(MODEL_DIR, tokenizer=tok, training=False, device="cuda")
    print("load", round(time.time() - t0, 1), "s", flush=True)
    sr = model.sample_rate

    table = torch.load(os.path.join(MODEL_DIR, "checkpoints", "speaker_centroids.pt"), map_location="cpu", weights_only=True)
    print("centroid ids", table["speaker_ids"], flush=True)
    spk = {
        "BM-CENTROID": table["centroids"][table["speaker_ids"].index("female_voice")].float(),
        "BM-R1": extract_speaker_centroid(R1, device="cuda"),
        "BM-L085": extract_speaker_centroid(NEUTRAL, device="cuda"),
    }
    b = torch.load(os.path.join(MODEL_DIR, "checkpoints", "speaker_b_embedding.pt"), map_location="cpu", weights_only=True)
    spk["BM-SPKB"] = torch.nn.functional.normalize(b["embedding"].float(), dim=0)
    cs = lambda a, c: round(float(torch.nn.functional.cosine_similarity(a, c, dim=0)), 3)  # noqa: E731
    print("embedding cos:", {k: cs(spk["BM-CENTROID"], v) for k, v in spk.items()}, flush=True)

    kw = dict(cfg_value=2.0, inference_timesteps=10, retry_badcase=False)

    def run(text, seed, v):
        torch.manual_seed(seed)
        torch.cuda.synchronize()
        t = time.time()
        wav = model.generate(target_text=text, speaker_centroid=spk[v], **kw)
        torch.cuda.synchronize()
        return wav.detach().float().cpu().numpy().reshape(-1), time.time() - t

    def first_chunk(text, seed, v):
        torch.manual_seed(seed)
        torch.cuda.synchronize()
        t = time.time()
        first, n = None, 0
        for c in model.generate_streaming(target_text=text, speaker_centroid=spk[v], **kw):
            if first is None:
                torch.cuda.synchronize()
                first = time.time() - t
            n += c.numel()
        torch.cuda.synchronize()
        return first, time.time() - t, n / sr

    run("你好。", 1, "BM-R1")  # 暖機
    first_chunk("你好。", 1, "BM-R1")
    log = {"sample_rate": sr, "items": {}}
    for i, (sid, text, _, _) in enumerate(SENTENCES):
        for ver, _ in VERSIONS:
            wav, dt = run(text, SEED + i, ver)
            sf.write(os.path.join(RAW, f"{sid}__{ver}.wav"), wav, sr)
            dur = len(wav) / sr
            ttfa, st_total, st_dur = first_chunk(text, SEED + i, ver)
            log["items"][f"{sid}__{ver}"] = {"dur": round(dur, 3), "gen_s": round(dt, 3), "rtf": round(dt / dur, 3),
                                            "ttfa_s": round(ttfa, 3), "stream_total_s": round(st_total, 3),
                                            "stream_dur": round(st_dur, 3)}
            print(sid, ver, log["items"][f"{sid}__{ver}"], flush=True)
    jdump(os.path.join(OUT, "gen.json"), log)


def cmd_build(args):
    from math import gcd

    import librosa
    import numpy as np
    import pyloudnorm as pyln
    import soundfile as sf
    from faster_whisper import WhisperModel
    from opencc import OpenCC
    from resemblyzer import VoiceEncoder, preprocess_wav
    from scipy.signal import resample_poly

    gen = json.load(open(os.path.join(OUT, "gen.json"), encoding="utf-8"))["items"]
    sr_t = 44100
    meter = pyln.Meter(sr_t)
    os.makedirs(FINAL, exist_ok=True)
    finals = {}
    for sid, _, _, _ in SENTENCES:
        os.makedirs(os.path.join(FINAL, sid), exist_ok=True)
        for ver, _ in VERSIONS:
            y, sr = sf.read(os.path.join(RAW, f"{sid}__{ver}.wav"), dtype="float64")
            if y.ndim > 1:
                y = y.mean(axis=1)
            if sr != sr_t:
                g = gcd(sr, sr_t)
                y = resample_poly(y, sr_t // g, sr // g)
            yt, _ = librosa.effects.trim(y, top_db=40, frame_length=1024, hop_length=256)
            y = np.concatenate([np.zeros(int(sr_t * PAD_HEAD_S)), yt, np.zeros(int(sr_t * PAD_TAIL_S))])
            y = pyln.normalize.loudness(y, meter.integrated_loudness(y), TARGET_LUFS)
            pk = float(np.max(np.abs(y)))
            if pk > 0.98:
                y *= 0.98 / pk
            p = os.path.join(FINAL, sid, f"{sid}__{ver}.wav")
            sf.write(p, y, sr_t, subtype="PCM_16")
            finals[f"{sid}__{ver}"] = (p, round(len(y) / sr_t, 2))

    enc = VoiceEncoder("cuda")
    ref = enc.embed_utterance(preprocess_wav(R1))

    def sim(p):
        e = enc.embed_utterance(preprocess_wav(p))
        return float(np.dot(ref, e) / (np.linalg.norm(ref) * np.linalg.norm(e)))

    wm = WhisperModel("medium", device="cuda", compute_type="float16")
    s2t = OpenCC("s2t")
    asr, sims, simp = {}, {}, {}
    for key, (p, _) in finals.items():
        y, _ = librosa.load(p, sr=16000, mono=True)
        segs, _ = wm.transcribe(y, language="zh", beam_size=5)
        asr[key] = "".join(s.text for s in segs).strip()
        sims[key] = sim(p)
        simp[key] = sum(1 for c in asr[key] if "一" <= c <= "鿿" and s2t.convert(c) != c)
        print(key, round(sims[key], 3), simp[key], asr[key], flush=True)

    # 上一輪 6 版也算 SIM 與簡體字數作對照（同一套量測）
    prev = {}
    for sid, _, _, _ in SENTENCES:
        for ver in ("V25-L085", "V25-L100", "V25-L000", "VOX-R1", "VOX-ULT-L085", "VOX-ULT-L100"):
            p = os.path.join(DEST, sid, f"{sid}__{ver}.wav")
            if os.path.exists(p):
                prev.setdefault(ver, []).append(sim(p))
    jdump(os.path.join(OUT, "metrics.json"), {"asr": asr, "sim": sims, "simp": simp, "gen": gen,
                                              "prev_sim": {k: round(float(np.mean(v)), 3) for k, v in prev.items()}})

    from tw_accent_check import retroflex_report
    L = ["", MARK + "（追加）", "",
         "同一組 8 句、同一個 seed 規則（SEED+句序），每版只生成 1 次；全部 44.1kHz、-23 LUFS，頭尾靜音已修齊。",
         "模型：OpenFormosa/BlueMagpie-TTS（卡片寫 research and evaluation，僅個人評估，輸出不得公開散布）。"
         "cfg 2.0 / steps 10 / retry_badcase=False。",
         "【降級路徑】公開推論碼（GitHub OpenFormosa/BlueMagpie-TTS main，與 HF Space voidful/BlueMagpie-TTS-Demo 的 bluemagpie/ 逐字相同，Space 150 個 commit 抽查也都沒有新版）"
         "比 HF checkpoint 舊：載入時 checkpoint 的 spk_vox_gate、spk_dit_gate、stop_context_gate、stop_context_proj、duration_head 等權重沒有對應模組（unexpected keys），"
         "config 的 speaker_step_injection、continuation adapter 也沒被使用。所以以下是缺這些模組的降級推論，可能低估 BlueMagpie。"
         "另外公開碼的 generate() 沒有卡片所述的 stop_threshold／stop_consecutive，也沒有 speaker_embedding_from_wav；"
         "改用 `extract_speaker_centroid`（ECAPA，window_s=6.0 預設），停止判斷為 stop head argmax。找不到可用的完整版推論碼，故沒有 -FULL 版本。", "",
         "### 版本", ""]
    L += [f"- `{v}`：{d}" for v, d in VERSIONS]
    L += ["", "### 指標（8 句平均）", "",
          "RTF = 生成秒數 / 音訊秒數（RTX 5070 Ti、bf16、無 torch.compile）；首包延遲 = generate_streaming 第一個 chunk 到手的秒數（已暖機）。", "",
          "| 版本 | SIM 對 R1 | RTF | 首包延遲(s) | 簡體字數(8 句) | 捲舌->平舌 | 平舌->捲舌 |", "|---|---|---|---|---|---|---|"]
    for ver, _ in VERSIONS:
        ks = [f"{sid}__{ver}" for sid, *_ in SENTENCES]
        a = [0, 0, 0, 0]
        for (sid, text, _, _), k in zip(SENTENCES, ks):
            r = retroflex_report(text, asr[k])
            if r:
                a[0] += r["ret_n"]; a[1] += r["ret_to_flat"]; a[2] += r["flat_n"]; a[3] += r["flat_to_ret"]
        L.append(f"| {ver} | {np.mean([sims[k] for k in ks]):.3f} | {np.mean([gen[k]['rtf'] for k in ks]):.2f} | "
                 f"{np.mean([gen[k]['ttfa_s'] for k in ks]):.2f} | {sum(simp[k] for k in ks)} | {a[1]}/{a[0]} | {a[3]}/{a[2]} |")
    L += ["", "對照（同量測，上一輪各版 SIM 平均 / 簡體字數見上方彙總表）：",
          "- 上一輪 SIM 對 R1 平均：" + "、".join(f"{k} {np.mean(v):.3f}" for k, v in prev.items()), ""]
    L += ["### Whisper 轉寫（BlueMagpie 各版）", "", "| 句 | 版本 | 轉寫 | 簡體字 | SIM |", "|---|---|---|---|---|"]
    for sid, *_ in SENTENCES:
        for ver, _ in VERSIONS:
            k = f"{sid}__{ver}"
            L.append(f"| {sid} | {ver} | {asr[k]} | {simp[k]} | {sims[k]:.3f} |")
    L += ["", "### 音長 (秒，含頭尾靜音)", ""] + [f"- {k}: {d}" for k, (_, d) in finals.items()]
    text = "\n".join(L) + "\n"

    readme = os.path.join(DEST, "README.md")
    old = open(readme, encoding="utf-8").read()
    if MARK in old:
        old = old[: old.index("\n" + MARK)].rstrip("\n") + "\n"
    open(readme, "w", encoding="utf-8", newline="\n").write(old + text)
    shutil.copytree(FINAL, DEST, dirs_exist_ok=True)
    print("done ->", DEST)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["gen", "build"])
    a = ap.parse_args()
    {"gen": cmd_gen, "build": cmd_build}[a.stage](a)
