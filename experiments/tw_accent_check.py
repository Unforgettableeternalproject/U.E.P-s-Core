"""台灣腔對照片段（非盲測，檔名直接標系統）。

8 句中性繁中句，集中放台灣審音、捲舌/平舌、兒化、輕聲與長句韻律，
每句生成 V25-L085 / V25-L100 / V25-L000 / VOX-R1 / VOX-ULT-L085 / VOX-ULT-L100。

階段（分環境執行，輸出 outputs/tw_accent_check/）：
  gen25   [core env]     2.5：LoRA 0.85 / 1.0 / 無 LoRA，並用 L100 生成 VOX-ULT-L100 的中性參考音
  genvox  [.venv-voxcpm] VoxCPM2：R1 參考、ult(neutral 參考 L085)、ult(L100 參考)
  asr     [.venv-voxcpm] Whisper 轉寫各版本（供拼音比對）
  build   [core env]     修靜音、-23 LUFS、複製到輸出資料夾、產生 README（含 pypinyin 聲母比對）

  core env = U.E.P-s-Core/env/Scripts/python.exe
"""
import argparse
import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "outputs", "tw_accent_check")
RAW = os.path.join(OUT, "raw")
FINAL = os.path.join(OUT, "final")
DEST = "E:/Materials/Voices/UEP/2026-10-31_tw_accent_check"
MODEL_DIR = os.path.join(ROOT, "models_cache", "VoxCPM2")
EMO_DIR = os.path.join(ROOT, "outputs", "v25_emotion_refs")
R1_16K = os.path.join(ROOT, "outputs", "voxcpm2_zeroshot", "refs", "R1.wav")
SEED = 1031
TARGET_LUFS = -23.0
PAD_HEAD_S, PAD_TAIL_S = 0.15, 0.25

# (編號, 句子, 要注意聽的字, 台灣預期讀法)
SENTENCES = [
    ("s01", "步驟很簡單，垃圾要分類，星期五我和你一起去。",
     "步驟、垃圾、星期、和", "步驟ㄅㄨˋ ㄗㄡˋ（非 zhòu）；垃圾ㄌㄜˋ ㄙㄜˋ（非 lā jī）；星期ㄒㄧㄥ ㄑㄧˊ（非 qī）；連詞「和」ㄏㄢˋ（非 hé）"),
    ("s02", "這家企業研究頭髮保養，也綜合分析了亞洲市場。",
     "企業、研究、頭髮、綜合、亞洲", "企ㄑㄧˋ業；研ㄐㄧㄡˋ究（非 yán）；頭髮ㄈㄚˇ（非 fà）；綜ㄗㄨㄥˋ合（非 zōng）；亞洲ㄧㄚˋ ㄓㄡ（依艾斯維爾提供的審音清單）"),
    ("s03", "這批貨的質量不錯，我們下星期再研究一次。",
     "質量、星期、研究", "質量ㄓˊ ㄌㄧㄤˋ（中國 zhì liàng）；星期ㄒㄧㄥ ㄑㄧˊ；研究ㄧㄢˊ ㄐㄧㄡˋ"),
    ("s04", "知識就是力量，吃飯之前先出去上廁所。",
     "知、識、吃、出、上、廁所", "捲舌音（ㄓㄔㄕ）台灣口語偏弱，接近ㄗㄘㄙ但不應完全變成平舌；中國腔捲舌明顯、舌尖上翹更重"),
    ("s05", "早上在村子裡，三個司機私下討論賽事。",
     "早、村、子、三、司、私、賽", "ㄗㄘㄙ平舌音密集；對照 s04，聽台灣腔下兩組的區別是否被收斂（捲舌弱化、平舌正常）"),
    ("s06", "你在這裡等一會兒，我去那邊看看好玩的。",
     "一會兒", "台灣說「一會兒」不兒化，讀ㄧˋ ㄏㄨㄟˇ（ㄦˊ 輕或省略）；中國腔會讀成 yíhuìr 的兒化尾（捲舌尾音）"),
    ("s07", "先生，你知道這些東西和衣服的意思嗎？",
     "先生、知道、東西、衣服、意思", "輕聲少：台灣常讀本調或弱輕聲，如東ㄉㄨㄥ 西ㄒㄧ（偏本調）、意ㄧˋ思ㄙ；中國腔輕聲明顯（dōngxi、yīfu、yìsi）"),
    ("s08", "週末我想去市場買點菜，順便繞去書店逛逛，晚上再回家煮一鍋熱湯給大家喝。",
     "整句的韻律、F0 走向、語尾", "日常長句：聽整體語調起伏、停頓與句尾下降方式，台灣腔語調平緩、句尾較軟；中國腔起伏較大、語調較明顯"),
]
VERSIONS = [
    ("V25-L085", "IndexTTS 2.5 + LoRA gpt_best 0.85（現行設定），uep_v25 角色，無情緒向量"),
    ("V25-L100", "IndexTTS 2.5 + LoRA 1.0（台灣腔最強）"),
    ("V25-L000", "IndexTTS 2.5 不掛 LoRA（中國腔基準，反例）"),
    ("VOX-R1", "VoxCPM2 只給參考音 R1（uep_reference_v25），cfg 2.0 / steps 10"),
    ("VOX-ULT-L085", "VoxCPM2 ultimate cloning，參考音 v25_emotion_refs/neutral.wav（2.5+LoRA 0.85 生成）加其逐字稿"),
    ("VOX-ULT-L100", "VoxCPM2 ultimate cloning，參考音為 2.5+LoRA 1.0 生成的中性中文（ref_L100_neutral.wav）加其逐字稿"),
]


def jdump(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def neutral_ref():
    d = json.load(open(os.path.join(EMO_DIR, "emotion_refs.json"), encoding="utf-8"))
    for it in d["items"]:
        if it.get("selected") and it["emotion"] == "neutral":
            return os.path.join(EMO_DIR, it["selected_file"]), it["text"]
    raise RuntimeError("找不到 neutral 參考音")


# ---------------------------------------------------------------- gen25
def cmd_gen25(args):
    sys.path.insert(0, ROOT)
    import soundfile as sf
    import torch
    from echo_tts.runtime.indextts_backend import IndexTTS2Backend
    from echo_tts.runtime.indextts_generate import default_generation_paths
    os.makedirs(RAW, exist_ok=True)
    backend = IndexTTS2Backend(paths=default_generation_paths(ROOT), device="cuda", use_fp16=True,
                               use_cuda_kernel=True, use_accel=False)
    backend.load()
    backend.load_lora(scale=0.85)
    backend.emotion_vector = None
    _, ref_text = neutral_ref()
    log = {}
    # 以 scale 切換：0.85 -> 1.0 -> 0.0。不可用 unload_lora：PEFT 已就地注入，卸載後 scale 仍殘留
    # （實測 unload 後輸出與 scale 1.0 位元相同），scale=0.0 才與未載入 LoRA 的輸出位元相同。
    for ver, scale in (("V25-L085", 0.85), ("V25-L100", 1.0), ("V25-L000", None)):
        backend.set_lora_scale(0.0 if scale is None else scale)
        print(ver, backend.get_lora_status(), flush=True)
        for i, (sid, text, _, _) in enumerate(SENTENCES):
            torch.manual_seed(SEED + i)
            p = os.path.join(RAW, f"{sid}__{ver}.wav")
            backend.generate(text, p, language="zh")
            log[f"{sid}__{ver}"] = round(sf.info(p).frames / sf.info(p).samplerate, 2)
        if ver == "V25-L100":
            torch.manual_seed(SEED + 100)
            backend.generate(ref_text, os.path.join(RAW, "ref_L100_neutral.wav"), language="zh")
            d = sf.info(os.path.join(RAW, "ref_L100_neutral.wav"))
            log["ref_L100_neutral"] = round(d.frames / d.samplerate, 2)
            print("ref_L100 dur", log["ref_L100_neutral"], flush=True)
    jdump(os.path.join(OUT, "gen25.json"), log)


# ---------------------------------------------------------------- genvox
def cmd_genvox(args):
    import numpy as np
    import soundfile as sf
    import torch
    from voxcpm import VoxCPM
    os.makedirs(RAW, exist_ok=True)
    model = VoxCPM.from_pretrained(MODEL_DIR, load_denoiser=False, optimize=False, local_files_only=True)
    sr = model.tts_model.sample_rate
    n_path, n_text = neutral_ref()
    l100_path = os.path.join(RAW, "ref_L100_neutral.wav")

    def run(text, seed, **kw):
        torch.manual_seed(seed)
        chunks = list(model.generate_streaming(text=text, cfg_value=2.0, inference_timesteps=10, **kw))
        return np.concatenate(chunks)

    run("你好。", 1, reference_wav_path=R1_16K)  # 暖機
    for i, (sid, text, _, _) in enumerate(SENTENCES):
        seed = SEED + i
        specs = {
            "VOX-R1": {"reference_wav_path": R1_16K},
            "VOX-ULT-L085": {"reference_wav_path": n_path, "prompt_wav_path": n_path, "prompt_text": n_text},
            "VOX-ULT-L100": {"reference_wav_path": l100_path, "prompt_wav_path": l100_path, "prompt_text": n_text},
        }
        for ver, kw in specs.items():
            wav = run(text, seed, **kw)
            sf.write(os.path.join(RAW, f"{sid}__{ver}.wav"), wav, sr)
            print(sid, ver, round(len(wav) / sr, 2), flush=True)


# ---------------------------------------------------------------- asr
def cmd_asr(args):
    import librosa
    from faster_whisper import WhisperModel
    wm = WhisperModel("medium", device="cuda", compute_type="float16")
    res = {}
    for sid, text, _, _ in SENTENCES:
        for ver, _ in VERSIONS:
            y, _ = librosa.load(os.path.join(RAW, f"{sid}__{ver}.wav"), sr=16000, mono=True)
            segs, _ = wm.transcribe(y, language="zh", beam_size=5)
            res[f"{sid}__{ver}"] = "".join(s.text for s in segs).strip()
            print(sid, ver, res[f"{sid}__{ver}"], flush=True)
    jdump(os.path.join(OUT, "asr.json"), res)


# ---------------------------------------------------------------- build
def initials(s):
    from pypinyin import Style, lazy_pinyin
    return lazy_pinyin(s, style=Style.INITIALS, strict=False)


def retroflex_report(ref, hyp):
    """逐字對齊（長度相同時）統計：原文捲舌聲母(zh/ch/sh)被辨成平舌(z/c/s)的比例。"""
    from opencc import OpenCC
    hyp = OpenCC("s2t").convert(hyp)
    strip = lambda t: "".join(c for c in t if "\u4e00" <= c <= "\u9fff")  # noqa: E731
    r, h = strip(ref), strip(hyp)
    if len(r) != len(h):
        return None
    ri, hi = initials(r), initials(h)
    ret = [(a, b) for a, b in zip(ri, hi) if a in ("zh", "ch", "sh")]
    flat = sum(b in ("z", "c", "s") for _, b in ret)
    ret2 = [(a, b) for a, b in zip(ri, hi) if a in ("z", "c", "s")]
    flat2 = sum(b in ("zh", "ch", "sh") for _, b in ret2)
    return {"ret_n": len(ret), "ret_to_flat": flat, "flat_n": len(ret2), "flat_to_ret": flat2,
            "char_diff": sum(a != b for a, b in zip(r, h))}


def cmd_build(args):
    from math import gcd

    import librosa
    import numpy as np
    import pyloudnorm as pyln
    import soundfile as sf
    from scipy.signal import resample_poly
    os.makedirs(FINAL, exist_ok=True)
    sr_t = 44100
    meter = pyln.Meter(sr_t)
    durs = {}
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
            sf.write(os.path.join(FINAL, sid, f"{sid}__{ver}.wav"), y, sr_t, subtype="PCM_16")
            durs[f"{sid}__{ver}"] = round(len(y) / sr_t, 2)
    asr_path = os.path.join(OUT, "asr.json")
    asr = json.load(open(asr_path, encoding="utf-8")) if os.path.exists(asr_path) else {}

    L = ["# 台灣腔對照片段 2026-10-31", "",
         "非盲測，檔名直接標系統。8 句中性語氣繁中句，每句 6 個版本；全部 44.1kHz、-23 LUFS，頭尾靜音已修齊。",
         "每句固定 seed（SEED+句序），每版只生成 1 次。", "", "## 版本說明", ""]
    L += [f"- `{v}`：{d}" for v, d in VERSIONS]
    L += ["", "## 句子與要注意聽的字", ""]
    for sid, text, focus, exp in SENTENCES:
        L += [f"### {sid}", f"- 句子：{text}", f"- 要注意聽的字：{focus}", f"- 台灣預期讀法：{exp}", ""]
    if asr:
        L += ["## Whisper 轉寫與拼音聲母比對（輔助，非判定）", "",
              "Whisper 是以語意修正的識別器，會把發音偏差「補正」回正確字，所以字錯數只反映明顯偏離；"
              "捲舌->平舌欄位僅在轉寫與原句長度相同時才能逐字比對。", "",
              "| 句 | 版本 | 轉寫 | 字差異 | 捲舌->平舌 | 平舌->捲舌 |", "|---|---|---|---|---|---|"]
        agg = {}
        from opencc import OpenCC
        s2t = OpenCC("s2t")
        simp = {}
        for sid, text, _, _ in SENTENCES:
            for ver, _ in VERSIONS:
                h = asr.get(f"{sid}__{ver}", "")
                rep = retroflex_report(text, h)
                n_simp = sum(1 for c in h if "一" <= c <= "鿿" and s2t.convert(c) != c)
                simp[ver] = simp.get(ver, 0) + n_simp
                if rep:
                    a = agg.setdefault(ver, [0, 0, 0, 0, 0])
                    a[0] += rep["ret_n"]; a[1] += rep["ret_to_flat"]; a[2] += rep["flat_n"]
                    a[3] += rep["flat_to_ret"]; a[4] += rep["char_diff"]
                cell = (f"{rep['char_diff']} | {rep['ret_to_flat']}/{rep['ret_n']} | {rep['flat_to_ret']}/{rep['flat_n']}"
                        if rep else "長度不同 | - | -")
                L.append(f"| {sid} | {ver} | {h} | {cell} |")
        L += ["", "### 彙總（僅含長度相同、可比對的句子）", "",
              "| 版本 | 捲舌->平舌 | 平舌->捲舌 | 字差異合計 |", "|---|---|---|---|"]
        for ver, _ in VERSIONS:
            if ver in agg:
                a = agg[ver]
                L.append(f"| {ver} | {a[1]}/{a[0]} | {a[3]}/{a[2]} | {a[4]} |")
    if asr:
        L += ["", "### Whisper 輸出簡體字數（簡體字越多，Whisper 越傾向判成中國腔/簡體語料；僅為弱線索）", "",
              "| 版本 | 簡體字數（8 句合計） |", "|---|---|"]
        L += [f"| {v} | {simp.get(v, 0)} |" for v, _ in VERSIONS]
    L += ["", "## 音長 (秒，含頭尾靜音)", ""]
    L += [f"- {k}: {v}" for k, v in durs.items()]
    readme = "\n".join(L) + "\n"
    with open(os.path.join(FINAL, "README.md"), "w", encoding="utf-8") as f:
        f.write(readme)
    shutil.copytree(FINAL, DEST, dirs_exist_ok=True)
    print("copied to", DEST)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["gen25", "genvox", "asr", "build"])
    a = ap.parse_args()
    {"gen25": cmd_gen25, "genvox": cmd_genvox, "asr": cmd_asr, "build": cmd_build}[a.stage](a)
