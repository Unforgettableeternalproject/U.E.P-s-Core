"""VoxCPM2 零樣本克隆 U.E.P 聲音實測（不微調）。

階段（獨立執行，避免 ASR / SIM 模型污染 VRAM 量測）：
  prep      準備 16kHz 參考音並用 Whisper 轉寫
  gen       合成並量測首包延遲 / RTF / VRAM，寫 manifest.json
  score     對 manifest 內每檔算 SIM（resemblyzer，對 R1）與 ASR 字元錯誤率
  baseline  對現行 v25_final 的 24 檔算同方法 SIM
  report    彙整成 summary.json / summary.md

使用 worktree 內的 .venv-voxcpm 執行：
  .venv-voxcpm/Scripts/python.exe experiments/voxcpm2_zeroshot.py prep
"""
import argparse
import json
import os
import platform
import re
import statistics
import subprocess
import sys
import time

import numpy as np
import soundfile as sf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPOS = os.path.dirname(ROOT)
OUT = os.path.join(ROOT, "outputs", "voxcpm2_zeroshot")
REFS = os.path.join(OUT, "refs")
MANIFEST = os.path.join(OUT, "manifest.json")
MODEL_DIR = os.path.join(ROOT, "models_cache", "VoxCPM2")

MAIN = os.path.join(REPOS, "TestSeparateTTSSystem")
CORE = os.path.join(REPOS, "U.E.P-s-Core")
V25_DIR = os.path.join(MAIN, "outputs", "v25_final")
SRC_REFS = {
    "R1": os.path.join(MAIN, "echo_tts", "assets", "reference", "uep_reference_v25.wav"),
    "R2": os.path.join(CORE, "outputs", "tts", "uep_pancake.wav"),
    "R3": os.path.join(CORE, "outputs", "tts", "rvc_normal.wav"),
    "R4": r"E:\ProgramFiles\RVC\Datasets\ITA_emotion_normal_synchronized_wav\UEP",
}
# 參考音語言（ASR 的 language；R1 由 Whisper 自動判斷後記錄）
REF_LANG = {"R1": None, "R2": "en", "R3": "en", "R4": "ja"}
SR_REF = 16000

# 沿用 experiments/v25_final_matrix.py 第 39-49 行
TEXTS = {
    "zh_neutral": "今天的天气不错，我把桌面上的工作都整理完了，接下来要做什么呢？",
    "zh_happy": "哈哈，你猜怎么着？新模型的测试一次就通过了！我们赶快把它装上去试试吧！",
    "zh_sad": "我...对不起……我把之前的存档弄丢了，明明答应过你要好好保管的……",
    "en_neutral": "The weather is nice today. I finished organizing everything on my desk. What should we do next?",
    "en_happy": "Haha! Guess what? The new model passed every single test on the first try! Let's get it running right now!",
    "en_sad": "I...I'm sorry... I lost the backup files. I promised I would keep them safe, and I let you down...",
    "ja_neutral": "今日はいい天気ですね。机の上の仕事を全部片付けました。次は何をしましょうか？",
    "ja_happy": "ねえねえ、聞いて！新しいモデルのテスト、一発で全部合格したんだよ！すごくない？",
    "ja_sad": "あ...ごめんなさい……大事なデータを失くしちゃった……ちゃんと守るって約束したのに……",
    # 台灣口語中文（腔調測試，自擬）
    "tw_neutral1": "欸，你有看到昨天那個新聞嗎？齁，真的超扯的啦，我到現在還是不敢相信。",
    "tw_neutral2": "那個便當也太好吃了吧，老闆還多送我一顆滷蛋，超佛心的欸。",
    "tw_neutral3": "不是啦，我跟你說，這個錯誤真的很靠北，我找了一整個下午才發現是少一個逗號。",
}
STYLE = {"happy": "(cheerful tone)", "sad": "(sad tone)"}
TAKES = 2
ULT_KEYS = ["zh_neutral", "en_neutral", "ja_neutral", "tw_neutral1", "tw_neutral2", "tw_neutral3"]


def lang_of(key):
    return key.split("_")[0]


def emo_of(key):
    k = key.split("_", 1)[1]
    return "happy" if k == "happy" else "sad" if k == "sad" else "neutral"


# ---------------------------------------------------------------- prep
def load_mono(path, sr):
    import librosa
    y, _ = librosa.load(path, sr=sr, mono=True)
    return y


def pick_vad_segment(y, sr=16000, win_s=12.0, max_gap_s=0.4):
    """用 webrtcvad 找連續、無長靜音、語音占比最高的視窗；回傳起點秒數。"""
    import webrtcvad
    vad = webrtcvad.Vad(2)
    fl = int(sr * 0.03)
    pcm = (np.clip(y, -1, 1) * 32767).astype(np.int16)
    n = len(pcm) // fl
    flags = np.array([vad.is_speech(pcm[i * fl:(i + 1) * fl].tobytes(), sr) for i in range(n)])
    wf = int(win_s / 0.03)
    gap_f = int(max_gap_s / 0.03)
    best, best_score = None, -1
    for s in range(0, n - wf, int(0.5 / 0.03)):
        w = flags[s:s + wf]
        run = mx = 0
        for f in w:
            run = 0 if f else run + 1
            mx = max(mx, run)
        if mx > gap_f:
            continue
        score = w.mean()
        if score > best_score:
            best, best_score = s, score
    if best is None:
        raise RuntimeError("找不到符合條件的片段")
    return best * 0.03, float(best_score)


def cmd_prep(args):
    os.makedirs(REFS, exist_ok=True)
    info = {}
    # R1 / R2：整段降取樣
    for k in ("R1", "R2"):
        y = load_mono(SRC_REFS[k], SR_REF)
        sf.write(os.path.join(REFS, f"{k}.wav"), y, SR_REF)
        info[k] = {"source": SRC_REFS[k], "dur_s": round(len(y) / SR_REF, 2), "note": "整段"}
    # R3：VAD 挑 12 秒
    y = load_mono(SRC_REFS["R3"], SR_REF)
    st, ratio = pick_vad_segment(y, SR_REF, 12.0)
    seg = y[int(st * SR_REF):int((st + 12.0) * SR_REF)]
    sf.write(os.path.join(REFS, "R3.wav"), seg, SR_REF)
    info["R3"] = {"source": SRC_REFS["R3"], "dur_s": 12.0, "start_s": round(st, 2),
                  "speech_ratio": round(ratio, 3), "note": "webrtcvad(agg=2) 連續無>0.4s靜音視窗"}
    # R4：UEP 資料集挑第一個 10–16 秒的 emoNormal
    d = SRC_REFS["R4"]
    chosen = None
    for fn in sorted(os.listdir(d)):
        if not fn.startswith("emoNormal") or not fn.endswith(".wav"):
            continue
        i = sf.info(os.path.join(d, fn))
        dur = i.frames / i.samplerate
        if 10.0 <= dur <= 16.0:
            chosen = (fn, i.samplerate, dur)
            break
    fn, sr0, dur = chosen
    y = load_mono(os.path.join(d, fn), SR_REF)
    sf.write(os.path.join(REFS, "R4.wav"), y, SR_REF)
    info["R4"] = {"source": os.path.join(d, fn), "dur_s": round(dur, 2), "orig_sr": sr0, "note": "emoNormal 整檔"}

    # Whisper 轉寫（ultimate cloning 用）
    from faster_whisper import WhisperModel
    try:
        wm = WhisperModel("medium", device="cuda", compute_type="float16")
        dev = "cuda"
    except Exception as e:  # noqa: BLE001
        print("whisper cuda 失敗，改 cpu:", e)
        wm = WhisperModel("medium", device="cpu", compute_type="int8")
        dev = "cpu"
    for k in ("R1", "R2", "R3", "R4"):
        segs, meta = wm.transcribe(load_mono(os.path.join(REFS, f"{k}.wav"), 16000), language=REF_LANG[k], beam_size=5)
        txt = "".join(s.text for s in segs).strip()
        info[k]["transcript"] = txt
        info[k]["lang"] = REF_LANG[k] or meta.language
        info[k]["lang_note"] = "指定" if REF_LANG[k] else f"Whisper 自動判斷 p={meta.language_probability:.2f}"
        print(k, info[k]["lang"], txt)
    info["_whisper_device"] = dev
    with open(os.path.join(REFS, "refs.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- gen
def env_info():
    import torch
    e = {"python": sys.version.split()[0], "platform": platform.platform(), "torch": torch.__version__,
         "cuda": torch.version.cuda, "arch_list": torch.cuda.get_arch_list(),
         "gpu": torch.cuda.get_device_name(0)}
    try:
        e["driver"] = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"], text=True).strip()
    except Exception:  # noqa: BLE001
        e["driver"] = "unknown"
    try:
        import voxcpm
        from importlib.metadata import version
        e["voxcpm"] = version("voxcpm")
    except Exception:  # noqa: BLE001
        pass
    return e


def cmd_gen(args):
    import torch
    from voxcpm import VoxCPM
    refs = json.load(open(os.path.join(REFS, "refs.json"), encoding="utf-8"))
    os.makedirs(OUT, exist_ok=True)
    env = env_info()
    env["optimize(torch.compile)"] = args.optimize
    torch.cuda.reset_peak_memory_stats()
    t0 = time.time()
    model = VoxCPM.from_pretrained(MODEL_DIR, load_denoiser=False, optimize=args.optimize,
                                   local_files_only=True)
    env["load_s"] = round(time.time() - t0, 1)
    env["vram_after_load_gb"] = round(torch.cuda.max_memory_allocated() / 1e9, 2)
    sr = model.tts_model.sample_rate
    env["output_sr"] = sr

    def run_one(text, ref_path, mode, prompt_text, seed):
        torch.manual_seed(seed)
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
        kw = {"reference_wav_path": ref_path}
        if mode == "ult":
            kw.update(prompt_wav_path=ref_path, prompt_text=prompt_text)
        t = time.perf_counter()
        gen = model.generate_streaming(text=text, cfg_value=2.0, inference_timesteps=10, **kw)
        chunks, t_first = [], None
        for c in gen:
            if t_first is None:
                torch.cuda.synchronize()
                t_first = time.perf_counter() - t
            chunks.append(c)
        torch.cuda.synchronize()
        total = time.perf_counter() - t
        wav = np.concatenate(chunks)
        return wav, t_first, total, len(chunks), torch.cuda.max_memory_allocated() / 1e9

    # 暖機（不記錄）
    for i in range(args.warmup):
        run_one("今天的天气不错。", os.path.join(REFS, "R1.wav"), "ref", None, 1000 + i)
    # 參考音編碼耗時（首包延遲內含此項；實際服務可快取）
    cache_build = {}
    for k in refs:
        if k.startswith("_"):
            continue
        ts = []
        for _ in range(3):
            torch.cuda.synchronize()
            t = time.perf_counter()
            model.tts_model.build_prompt_cache(prompt_text=None, prompt_wav_path=None,
                                               reference_wav_path=os.path.join(REFS, f"{k}.wav"))
            torch.cuda.synchronize()
            ts.append(time.perf_counter() - t)
        cache_build[k] = round(statistics.median(ts), 3)
    env["ref_cache_build_s"] = cache_build

    items = []
    plan = []
    for mode in ("ref", "ult"):
        for rk in ("R1", "R2", "R3", "R4"):
            for key in TEXTS:
                if mode == "ult" and key not in ULT_KEYS:
                    continue
                plan.append((mode, rk, key))
    if args.limit:
        plan = plan[:args.limit]
    for n, (mode, rk, key) in enumerate(plan):
        emo = emo_of(key)
        hint = STYLE.get(emo, "") if mode == "ref" else ""
        text = hint + TEXTS[key]
        for take in range(1, TAKES + 1):
            fname = f"{mode}_{rk}_{key}_t{take}.wav"
            seed = 42 + take
            err = None
            try:
                wav, t_first, total, nch, peak = run_one(text, os.path.join(REFS, f"{rk}.wav"), mode,
                                                         refs[rk]["transcript"], seed)
            except Exception as e:  # noqa: BLE001
                err = repr(e)
                print("FAIL", fname, err)
                items.append({"file": fname, "error": err})
                continue
            sf.write(os.path.join(OUT, fname), wav, sr)
            dur = len(wav) / sr
            items.append({
                "file": fname, "mode": mode, "ref": rk, "ref_lang": refs[rk]["lang"], "text_key": key,
                "lang": "zh" if lang_of(key) == "tw" else lang_of(key), "is_tw": lang_of(key) == "tw",
                "emotion": emo, "style_hint": hint or None, "text": TEXTS[key], "take": take, "seed": seed,
                "audio_s": round(dur, 3), "first_chunk_s": round(t_first, 3), "total_s": round(total, 3),
                "rtf": round(total / dur, 3), "n_chunks": nch, "vram_peak_gb": round(peak, 2),
            })
            print(f"[{n + 1}/{len(plan)}] {fname} dur={dur:.1f}s first={t_first:.2f}s rtf={total / dur:.2f}")
    env["vram_peak_overall_gb"] = max([i.get("vram_peak_gb", 0) for i in items] + [env["vram_after_load_gb"]])
    man = {"env": env, "refs": {k: v for k, v in refs.items() if not k.startswith("_")},
           "params": {"cfg_value": 2.0, "inference_timesteps": 10, "takes": TAKES, "warmup": args.warmup},
           "items": items}
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- score
def norm_text(s, lang):
    s = re.sub(r"[\s\.\,\!\?\;\:、。，！？；：…「」『』\(\)\-~～\"'・]+", "", s).lower()
    if lang == "zh":
        from opencc import OpenCC
        s = OpenCC("s2t").convert(s)
    return s


def cmd_score(args):
    from resemblyzer import VoiceEncoder, preprocess_wav
    enc = VoiceEncoder("cuda")

    def emb(path):
        return enc.embed_utterance(preprocess_wav(path))

    def cos(a, b):
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))

    r1 = emb(os.path.join(REFS, "R1.wav"))
    refemb = {k: emb(os.path.join(REFS, f"{k}.wav")) for k in ("R1", "R2", "R3", "R4")}
    man = json.load(open(MANIFEST, encoding="utf-8"))
    man["ref_ref_sim"] = {k: round(cos(r1, v), 4) for k, v in refemb.items()}
    for it in man["items"]:
        if "error" in it:
            continue
        e = emb(os.path.join(OUT, it["file"]))
        it["sim_r1"] = round(cos(r1, e), 4)
        it["sim_own_ref"] = round(cos(refemb[it["ref"]], e), 4)
    # ASR（在 SIM 之後載入）
    from faster_whisper import WhisperModel
    from rapidfuzz.distance import Levenshtein
    try:
        wm = WhisperModel("medium", device="cuda", compute_type="float16")
    except Exception as e:  # noqa: BLE001
        print("whisper cuda 失敗，改 cpu:", e)
        wm = WhisperModel("medium", device="cpu", compute_type="int8")
    for it in man["items"]:
        if "error" in it:
            continue
        segs, _ = wm.transcribe(load_mono(os.path.join(OUT, it["file"]), 16000), language=it["lang"], beam_size=5)
        hyp = "".join(s.text for s in segs).strip()
        ref = norm_text(it["text"], it["lang"])
        h = norm_text(hyp, it["lang"])
        it["asr_text"] = hyp
        it["asr_cer"] = round(Levenshtein.distance(ref, h) / max(1, len(ref)), 4)
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=2)


def cmd_baseline(args):
    from resemblyzer import VoiceEncoder, preprocess_wav
    enc = VoiceEncoder("cuda")
    emb = lambda p: enc.embed_utterance(preprocess_wav(p))  # noqa: E731
    r1 = emb(os.path.join(REFS, "R1.wav"))
    res = []
    for fn in sorted(os.listdir(V25_DIR)):
        if not fn.endswith(".wav"):
            continue
        e = emb(os.path.join(V25_DIR, fn))
        res.append({"file": fn, "sim_r1": round(float(np.dot(r1, e) / (np.linalg.norm(r1) * np.linalg.norm(e))), 4)})
    with open(os.path.join(OUT, "v25_baseline_sim.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(len(res), "files, mean", statistics.mean(r["sim_r1"] for r in res))


# ---------------------------------------------------------------- report
def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 3) if xs else None


def cmd_report(args):
    man = json.load(open(MANIFEST, encoding="utf-8"))
    its = [i for i in man["items"] if "error" not in i]
    groups = {}
    for i in its:
        lang = "tw" if i["is_tw"] else i["lang"]
        groups.setdefault((i["mode"], i["ref"], lang), []).append(i)
    rows = []
    for (mode, ref, lang), g in sorted(groups.items()):
        rows.append({"mode": mode, "ref": ref, "ref_lang": g[0]["ref_lang"], "text_lang": lang, "n": len(g),
                     "sim_r1": mean([x.get("sim_r1") for x in g]),
                     "sim_own_ref": mean([x.get("sim_own_ref") for x in g]),
                     "rtf": mean([x["rtf"] for x in g]),
                     "first_chunk_s": mean([x["first_chunk_s"] for x in g]),
                     "cer": mean([x.get("asr_cer") for x in g])})
    bl = json.load(open(os.path.join(OUT, "v25_baseline_sim.json"), encoding="utf-8"))
    bl_by = {}
    for r in bl:
        n = r["file"]
        k = "zh" if "_zh" in n else "en" if "_en" in n else "ja"
        bl_by.setdefault(k, []).append(r["sim_r1"])
    summ = {"rows": rows, "v25_sim_by_lang": {k: mean(v) for k, v in bl_by.items()},
            "v25_sim_all": mean([r["sim_r1"] for r in bl]), "ref_ref_sim": man.get("ref_ref_sim"), "env": man["env"]}
    with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summ, f, ensure_ascii=False, indent=2)
    print("| mode | ref(lang) | text | n | SIM | SIMown | RTF | first(s) | CER |")
    for r in rows:
        print(f"| {r['mode']} | {r['ref']}({r['ref_lang']}) | {r['text_lang']} | {r['n']} | {r['sim_r1']} | "
              f"{r['sim_own_ref']} | {r['rtf']} | {r['first_chunk_s']} | {r['cer']} |")
    print("v25:", summ["v25_sim_by_lang"], summ["v25_sim_all"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["prep", "gen", "score", "baseline", "report"])
    ap.add_argument("--optimize", action="store_true", help="啟用 torch.compile（Windows 預期失敗）")
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    {"prep": cmd_prep, "gen": cmd_gen, "score": cmd_score, "baseline": cmd_baseline, "report": cmd_report}[a.stage](a)
