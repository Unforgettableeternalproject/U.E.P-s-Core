"""步驟 2 評估與最終產出：VoxCPM2 台灣華語口音 LoRA 各 checkpoint 比較。

條件：U.E.P 參考音 ULT（neutral.wav + 逐字稿），固定 seed，文字經 TaiwanPronunciationNormalizer（同步驟 1 的 TWN）。
測試句：8 句鑑別句 + TaiMECS 真人錄音句（只當測試集）+ 英文 2 句 + 日文 2 句。
指標：SIM 對 R1（resemblyzer）、Whisper 簡體字數、CER（弱指標；中文 CER 去除拉丁字母後計算）。

階段（.venv-voxcpm）：
  gen    --tags base,step_0000100,...   生成 outputs/tw_lora_eval/<tag>/*.wav
  score  --tags ...                     算指標寫 metrics_<tag>.json
  report                                彙整成 summary.md
  final  --tag step_XXXXXXX             最終 8 句：TWLORA / TWLORA-TWN / 另含 TWN 與 TWLORA-TWN 的 ref120 / post120
"""
import argparse
import json
import os
import re
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tw_accent_check as T  # noqa: E402
import tw_pinyin_check as P  # noqa: E402
from taiwan_pronunciation import TaiwanPronunciationNormalizer  # noqa: E402

EV = os.path.join(ROOT, "outputs", "tw_lora_eval")
CKPT = os.path.join(ROOT, "models_cache", "vox_lora_tw", "checkpoints")
TAIMECS = os.path.join(ROOT, "outputs", "tw_lora_data", "taimecs", "metadata.jsonl")
EN = ["The weather is nice today. I finished organizing everything on my desk. What should we do next?",
      "I'm sorry, I lost the backup files. I promised I would keep them safe, and I let you down."]
JA = ["今日はいい天気ですね。机の上の仕事を全部片付けました。次は何をしましょうか？",
      "ねえねえ、聞いて！新しいモデルのテスト、一発で全部合格したんだよ！すごくない？"]


def eval_items():
    items = [(sid, "zh8", text) for sid, text, _, _ in T.SENTENCES]
    rows = [json.loads(l) for l in open(TAIMECS, encoding="utf-8")]
    hum = [r for r in rows if r.get("source") == "human"][:8]
    items += [(f"tm{i + 1:02d}", "tm", r["text"]) for i, r in enumerate(hum)]
    items += [(f"en{i + 1}", "en", t) for i, t in enumerate(EN)]
    items += [(f"ja{i + 1}", "ja", t) for i, t in enumerate(JA)]
    return items


def load_model(first_tag=None):
    from voxcpm import VoxCPM
    from voxcpm.model.voxcpm2 import LoRAConfig
    cfg = LoRAConfig(enable_lm=True, enable_dit=True, enable_proj=False, r=32, alpha=32, dropout=0.0)
    return VoxCPM.from_pretrained(T.MODEL_DIR, load_denoiser=False, optimize=False, local_files_only=True, lora_config=cfg)


def set_tag(model, tag):
    if tag == "base":
        model.unload_lora()
    else:
        model.unload_lora()
        loaded, skipped = model.load_lora(os.path.join(CKPT, tag))
        print(tag, "loaded", len(loaded), "skipped", len(skipped), flush=True)


def synth(model, text, seed, ref, ref_text):
    import torch
    torch.manual_seed(seed)
    ch = list(model.generate_streaming(text=text, cfg_value=2.0, inference_timesteps=10, normalize=False,
                                       reference_wav_path=ref, prompt_wav_path=ref, prompt_text=ref_text))
    return np.concatenate(ch)


def cmd_gen(a):
    import soundfile as sf
    norm = TaiwanPronunciationNormalizer()
    n_path, n_text = T.neutral_ref()
    model = load_model()
    sr = model.tts_model.sample_rate
    items = eval_items()
    synth(model, "你好。", 1, n_path, n_text)
    for tag in a.tags.split(","):
        d = os.path.join(EV, tag)
        os.makedirs(d, exist_ok=True)
        set_tag(model, tag)
        for i, (key, kind, text) in enumerate(items):
            sent = norm.normalize(text) if kind in ("zh8", "tm") else text
            w = synth(model, sent, T.SEED + i, n_path, n_text)
            sf.write(os.path.join(d, f"{key}.wav"), w, sr)
        print("gen done", tag, flush=True)


def _strip_cer(s, lang):
    s = re.sub(r"[\s\.\,\!\?\;\:、。，！？；：…「」『』\(\)\-~～\"'・]+", "", s).lower()
    if lang == "zh":
        s = re.sub(r"[a-z0-9]+", "", s)
    return s


def cmd_score(a):
    import librosa
    from opencc import OpenCC
    from rapidfuzz.distance import Levenshtein
    from resemblyzer import VoiceEncoder, preprocess_wav
    enc = VoiceEncoder("cuda")
    r1 = enc.embed_utterance(preprocess_wav(T.R1_16K))
    items = eval_items()
    s2t = OpenCC("s2t")
    res = {}
    for tag in a.tags.split(","):
        res[tag] = {}
        for key, kind, text in items:
            p = os.path.join(EV, tag, f"{key}.wav")
            e = enc.embed_utterance(preprocess_wav(p))
            sim = float(np.dot(r1, e) / (np.linalg.norm(r1) * np.linalg.norm(e)))
            res[tag][key] = {"sim": round(sim, 4)}
    del enc
    import torch
    torch.cuda.empty_cache()
    from faster_whisper import WhisperModel
    wm = WhisperModel("medium", device="cuda", compute_type="float16")
    for tag in res:
        for key, kind, text in items:
            lang = {"zh8": "zh", "tm": "zh", "en": "en", "ja": "ja"}[kind]
            y, _ = librosa.load(os.path.join(EV, tag, f"{key}.wav"), sr=16000, mono=True)
            segs, _ = wm.transcribe(y, language=lang, beam_size=5)
            hyp = "".join(s.text for s in segs).strip()
            ref = _strip_cer(s2t.convert(text) if lang == "zh" else text, lang)
            h = _strip_cer(s2t.convert(hyp) if lang == "zh" else hyp, lang)
            r = res[tag][key]
            r["asr"] = hyp
            r["cer"] = round(Levenshtein.distance(ref, h) / max(1, len(ref)), 4)
            r["simp"] = sum(1 for c in hyp if "一" <= c <= "鿿" and s2t.convert(c) != c) if lang == "zh" else 0
        json.dump(res[tag], open(os.path.join(EV, f"metrics_{tag}.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("scored", tag, flush=True)


def cmd_report(a):
    import glob
    mean = lambda xs: round(float(np.mean(xs)), 4) if xs else None  # noqa: E731
    rows = []
    for p in sorted(glob.glob(os.path.join(EV, "metrics_*.json"))):
        tag = os.path.basename(p)[8:-5]
        m = json.load(open(p, encoding="utf-8"))
        g = lambda pre, f: [v[f] for k, v in m.items() if k.startswith(pre)]  # noqa: E731
        rows.append((tag, mean(g("s0", "sim")), mean(g("tm", "sim")), mean(g("en", "sim")), mean(g("ja", "sim")),
                     sum(g("s0", "simp")), sum(g("tm", "simp")), mean(g("s0", "cer")), mean(g("tm", "cer")),
                     mean(g("en", "cer")), mean(g("ja", "cer"))))
    L = ["| ckpt | SIM zh8 | SIM TaiMECS | SIM en | SIM ja | 簡體字 zh8 | 簡體字 TM | CER zh8 | CER TM | CER en | CER ja |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    L += ["| " + " | ".join(str(x) for x in r) + " |" for r in rows]
    open(os.path.join(EV, "summary.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")
    print("\n".join(L))


_CJK = re.compile(r"[一-鿿]")


def is_foreign(text):
    """不含任何漢字 = 外語句：不掛台灣腔 LoRA、不經 TWN 前處理。中英混雜（含漢字）仍走 LoRA + TWN。"""
    return not _CJK.search(text)


def synth_auto(model, text, seed, ref, ref_text, norm):
    """引擎接入用：逐句決定是否啟用 LoRA 與 TWN。"""
    if is_foreign(text):
        model.set_lora_enabled(False)
        return synth(model, text, seed, ref, ref_text)
    model.set_lora_enabled(True)
    return synth(model, norm.normalize(text), seed, ref, ref_text)


def cmd_foreign(a):
    """外語回歸：LoRA 掛載但逐句 off，與 base（無 LoRA）比較；並量測 set_lora_enabled 切換開銷。"""
    import time

    import librosa
    import soundfile as sf
    from resemblyzer import VoiceEncoder, preprocess_wav
    norm = TaiwanPronunciationNormalizer()
    n_path, n_text = T.neutral_ref()
    model = load_model()
    sr = model.tts_model.sample_rate
    set_tag(model, a.tag)
    synth_auto(model, "你好。", 1, n_path, n_text, norm)
    items = eval_items()
    out = {}
    t0 = time.perf_counter()
    for _ in range(50):
        model.set_lora_enabled(False)
        model.set_lora_enabled(True)
    toggle_ms = (time.perf_counter() - t0) / 100 * 1000
    d = os.path.join(EV, a.tag + "_foreign_off")
    os.makedirs(d, exist_ok=True)
    for i, (key, kind, text) in enumerate(items):
        if kind not in ("en", "ja"):
            continue
        t0 = time.perf_counter()
        w = synth_auto(model, text, T.SEED + i, n_path, n_text, norm)
        el = time.perf_counter() - t0
        sf.write(os.path.join(d, f"{key}.wav"), w, sr)
        b, _ = sf.read(os.path.join(EV, "base", f"{key}.wav"))
        n = min(len(b), len(w))
        out[key] = {"max_abs_diff_vs_base": float(np.max(np.abs(b[:n] - w[:n]))), "len_equal": len(b) == len(w), "gen_s": round(el, 2)}
    # 對照：LoRA 開著（不切換）的外語輸出，量切換前後的差異
    model.set_lora_enabled(True)
    for i, (key, kind, text) in enumerate(items):
        if kind in ("en", "ja"):
            w = synth(model, text, T.SEED + i, n_path, n_text)
            b, _ = sf.read(os.path.join(d, f"{key}.wav"))
            n = min(len(b), len(w))
            out[key]["max_abs_diff_lora_on_vs_off"] = float(np.max(np.abs(b[:n] - w[:n])))
    out["toggle_ms_per_call"] = round(toggle_ms, 4)
    json.dump(out, open(os.path.join(EV, "foreign_check.json"), "w", encoding="utf-8"), indent=1)
    print(json.dumps(out, indent=1))


def cmd_final(a):
    import soundfile as sf
    from math import gcd

    import librosa
    from scipy.signal import resample_poly
    norm = TaiwanPronunciationNormalizer()
    n_path, n_text = T.neutral_ref()
    y16, _ = librosa.load(n_path, sr=16000, mono=True)
    ref120 = os.path.join(EV, "neutral_ref120.wav")
    sf.write(ref120, P._stretch_wsola(y16, 16000, 1.2), 16000)
    model = load_model()
    sr = model.tts_model.sample_rate
    sr_t = 44100
    raw = os.path.join(EV, "final_raw")
    os.makedirs(raw, exist_ok=True)
    stats = {}

    def emit(sid, ver, w):
        w = np.asarray(w, dtype="float64")
        g = gcd(sr, sr_t)
        fin, speech = P._post(resample_poly(w, sr_t // g, sr // g), sr_t)
        sf.write(os.path.join(raw, f"{sid}__{ver}.wav"), w, sr)
        os.makedirs(os.path.join(T.DEST, sid), exist_ok=True)
        sf.write(os.path.join(T.DEST, sid, f"{sid}__{ver}.wav"), fin, sr_t, subtype="PCM_16")
        nch = sum("一" <= c <= "鿿" for c in dict((s[0], s[1]) for s in T.SENTENCES)[sid])
        stats[f"{sid}__{ver}"] = round(speech / nch, 3)

    synth(model, "你好。", 1, n_path, n_text)
    sents = {sid: text for sid, text, _, _ in T.SENTENCES}
    sent_log = {}
    for tag, lora in (("TWN", False), ("TWLORA", True)):
        set_tag(model, a.tag if lora else "base")
        for i, (sid, text) in enumerate(sents.items()):
            seed = T.SEED + i
            twn = norm.normalize(text)
            if lora:  # TWLORA：無前處理；TWLORA-TWN：加前處理
                emit(sid, "VOX-ULT-TWLORA", synth(model, text, seed, n_path, n_text))
            name = "VOX-ULT-TWLORA-TWN" if lora else "VOX-ULT-TWN"
            base_w = synth(model, twn, seed, n_path, n_text)
            if lora:
                emit(sid, name, base_w)
            sent_log[sid] = {"original": text, "twn": twn}
            emit(sid, f"{name}-SPD-ref120", synth(model, twn, seed, ref120, n_text))
            emit(sid, f"{name}-SPD-post120", P._stretch_wsola(base_w, sr, 1.2))
        print("final done", tag, flush=True)
    json.dump({"ckpt": a.tag, "s_per_char": stats, "sent": sent_log},
              open(os.path.join(EV, "final_stats.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["gen", "score", "report", "final", "foreign"])
    ap.add_argument("--tags", default="base")
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    {"gen": cmd_gen, "score": cmd_score, "report": cmd_report, "final": cmd_final, "foreign": cmd_foreign}[a.stage](a)
