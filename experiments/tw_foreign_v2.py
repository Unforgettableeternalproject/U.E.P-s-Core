"""外語 v2：英文怪開頭修正（乾淨 prompt）、日文假名化、英文參數比較，全部用同一個 Vox 實例（掛 LoRA 後關閉）。
階段（.venv-voxcpm，一次一個 GPU 工作）：
  prep   裁出乾淨的 prompt（句界結束）寫 outputs/foreign_v2/refs
  gen    生成所有版本，量 RTF
  score  ES ASR + 原版 whisper(medium) + SIM(R1) + 英文開頭多出內容檢查
"""
import argparse
import json
import os
import re
import sys
import time
from math import gcd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
import torch  # noqa: E402
from scipy.signal import resample_poly  # noqa: E402

import es_asr  # noqa: E402
import tw_accent_check as T  # noqa: E402
import tw_lora_eval as E  # noqa: E402
import tw_pinyin_check as P  # noqa: E402
from tw_foreign_check import ITEMS, V25 as V25DIR  # noqa: E402

ROOT = T.ROOT
W = os.path.join(ROOT, "outputs", "foreign_v2")
RAW = os.path.join(W, "raw")
REFS = os.path.join(W, "refs")
SRC = os.path.join(ROOT, "outputs", "voxcpm2_zeroshot", "refs")
DEST = "E:/Materials/Voices/UEP/2026-11-01_foreign_v2"
V25MAP = {"en1": "B_en.wav", "en2": "B_en_happy05.wav", "en3": "B_en_sad05.wav",
          "ja1": "B_ja.wav", "ja2": "B_ja_happy065.wav", "ja3": "B_ja_sad065.wav"}
# 乾淨 prompt：句界切點（秒）與逐字稿（逐字稿以 whisper word timestamps + ES ASR 交叉確認，非人工校對）
PROMPTS = {
    "R1c": ("R1", 3.04, 10.75, "This could endanger yourself and the victim. Instead, call the hotline or police emergency number."),
    "R2c": ("R2", 0.0, 6.0, "Do you like pancakes? I do love pancakes a lot. They are sweet and delicious."),
    "R4c": ("R4", 1.9, 9.8, "ストラットフォードオンエイウォンは、シェイクスピアの生まれたところですが、毎年多くの観光客が訪れます。"),
}
GRID = [(1.5, 10), (1.5, 20), (2.0, 20), (2.5, 10), (2.5, 20)]  # 另加預設 (2.0, 10)


def cmd_prep(a):
    import librosa
    os.makedirs(REFS, exist_ok=True)
    for k, (src, t0, t1, txt) in PROMPTS.items():
        y = librosa.load(os.path.join(SRC, src + ".wav"), sr=16000, mono=True)[0]
        seg = y[int(t0 * 16000): int(t1 * 16000)]
        sf.write(os.path.join(REFS, k + ".wav"), seg, 16000)
        json.dump({"text": txt, "src": src, "t0": t0, "t1": t1}, open(os.path.join(REFS, k + ".json"), "w", encoding="utf-8"), ensure_ascii=False)
        print(k, round(len(seg) / 16000, 2), txt)


def cmd_gen(a):
    from japanese_kana import JapaneseKanaNormalizer
    from taiwan_pronunciation import TaiwanPronunciationNormalizer
    kana = JapaneseKanaNormalizer()
    n_path, n_text = T.neutral_ref()
    refs = {k: os.path.join(SRC, k + ".wav") for k in ("R1", "R2", "R4")}
    for k in PROMPTS:
        refs[k] = os.path.join(REFS, k + ".wav")
    ptxt = {k: json.load(open(os.path.join(REFS, k + ".json"), encoding="utf-8"))["text"] for k in PROMPTS}
    model = E.load_model()
    sr = model.tts_model.sample_rate
    E.set_tag(model, E.DEFAULT_LORA_TAG)
    model.set_lora_enabled(False)  # 外語：LoRA 關閉
    os.makedirs(RAW, exist_ok=True)
    meta = json.load(open(os.path.join(W, "gen_meta.json"), encoding="utf-8")) if a.only else {}

    def gen(key, text, seed, ref, prompt=None, cfg=2.0, steps=10):
        torch.manual_seed(seed)
        kw = dict(reference_wav_path=ref)
        if prompt:
            kw.update(prompt_wav_path=ref, prompt_text=prompt)
        torch.cuda.synchronize()
        t = time.perf_counter()
        w = np.concatenate(list(model.generate_streaming(text=text, cfg_value=cfg, inference_timesteps=steps, normalize=False, **kw)))
        torch.cuda.synchronize()
        el = time.perf_counter() - t
        sf.write(os.path.join(RAW, key + ".wav"), w, sr)
        meta[key] = {"sent": text, "prompt": prompt, "cfg": cfg, "steps": steps, "gen_s": round(el, 2),
                     "audio_s": round(len(w) / sr, 2), "rtf": round(el / (len(w) / sr), 2)}
        print(key, meta[key]["rtf"], flush=True)

    gen("warm", "Hello.", 1, refs["R2"])
    for i, (iid, lang, text, *_r) in enumerate(ITEMS):
        seed = T.SEED + 70 + i
        if a.only and iid not in a.only.split(","):
            continue
        if lang == "en":
            gen(f"{iid}__REF-R1", text, seed, refs["R1"])
            gen(f"{iid}__REF-R2", text, seed, refs["R2"])
            gen(f"{iid}__ULT-R1c", text, seed, refs["R1c"], ptxt["R1c"])
            gen(f"{iid}__ULT-R2c", text, seed, refs["R2c"], ptxt["R2c"])
            for cfg, st in GRID:
                gen(f"{iid}__REF-R2_cfg{cfg}_t{st}", text, seed, refs["R2"], cfg=cfg, steps=st)
                gen(f"{iid}__ULT-R2c_cfg{cfg}_t{st}", text, seed, refs["R2c"], ptxt["R2c"], cfg=cfg, steps=st)
        else:
            kt, kp = kana.normalize(text), kana.normalize(ptxt["R4c"])
            gen(f"{iid}__REF-R4", text, seed, refs["R4"])
            gen(f"{iid}__KANA_REF-R4", kt, seed, refs["R4"])
            gen(f"{iid}__ULT-R4c", text, seed, refs["R4c"], ptxt["R4c"])
            gen(f"{iid}__KANA_ULT-R4c", kt, seed, refs["R4c"], kp)
            gen(f"{iid}__KANA_ULT-NEUTRAL", kt, seed, n_path, n_text)
            gen(f"{iid}__KANA_REF-R1", kt, seed, refs["R1"])
    # 同一實例回到中文：開 LoRA 做 s01 的 TWLORA-TWN（ULT neutral），驗證切換後中文可用
    if not a.only:
        model.set_lora_enabled(True)
        gen("zh_check__TWLORA-TWN", TaiwanPronunciationNormalizer().normalize(T.SENTENCES[0][1]), T.SEED, n_path, n_text)
    json.dump(meta, open(os.path.join(W, "gen_meta.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def _post(w, sr):
    g = gcd(sr, 44100)
    return P._post(resample_poly(np.asarray(w, dtype="float64"), 44100 // g, sr // g), 44100)[0]


def _norm_words(s):
    return re.sub(r"[^a-z0-9 ]+", "", s.lower()).split()


def extra_prefix(ref, hyp):
    r, h = _norm_words(ref), _norm_words(hyp)
    for i in range(len(h)):
        if h[i:i + 3] == r[:3]:
            return " ".join(h[:i])
    return "（原文開頭未對上）"


def _cer(ref, hyp):
    from rapidfuzz.distance import Levenshtein
    f = lambda t: re.sub(r"[\s\.\,\!\?、。，！？…\-~～「」]+", "", t).lower()  # noqa: E731
    r, h = f(ref), f(hyp)
    return round(Levenshtein.distance(r, h) / max(1, len(r)), 3)


def cmd_score(a):
    import librosa
    from faster_whisper import WhisperModel
    from resemblyzer import VoiceEncoder, preprocess_wav
    meta = json.load(open(os.path.join(W, "gen_meta.json"), encoding="utf-8"))
    enc = VoiceEncoder("cuda")
    r1 = enc.embed_utterance(preprocess_wav(T.R1_16K))
    items = {iid: (lang, text) for iid, lang, text, *_ in ITEMS}
    res = {}

    def sim(f):
        e = enc.embed_utterance(preprocess_wav(f))
        return round(float(np.dot(r1, e) / (np.linalg.norm(r1) * np.linalg.norm(e))), 4)

    for key in meta:
        if key == "warm":
            continue
        iid = key.split("__")[0]
        d = os.path.join(DEST, iid)
        os.makedirs(d, exist_ok=True)
        w, sr0 = sf.read(os.path.join(RAW, key + ".wav"), dtype="float64")
        f = os.path.join(d, f"{key}.wav")
        sf.write(f, _post(w, sr0), 44100, subtype="PCM_16")
        res[key] = {"sim": sim(f), "file": f}
    for iid, v25f in V25MAP.items():
        y, s0 = sf.read(os.path.join(V25DIR, v25f), dtype="float64")
        y = y if y.ndim == 1 else y.mean(axis=1)
        f = os.path.join(DEST, iid, f"{iid}__V25.wav")
        sf.write(f, _post(y, s0), 44100, subtype="PCM_16")
        res[f"{iid}__V25"] = {"sim": sim(f), "file": f}
    del enc
    torch.cuda.empty_cache()
    wm = WhisperModel("medium", device="cuda", compute_type="float16")
    for key, r in res.items():
        iid = key.split("__")[0]
        lang, text = ("zh", T.SENTENCES[0][1]) if iid == "zh_check" else items[iid]
        y = librosa.load(r["file"], sr=16000)[0]
        segs, _ = wm.transcribe(y, language=lang, beam_size=5)
        r["whisper"] = "".join(s.text for s in segs).strip()
        r["es"] = es_asr.transcribe(r["file"], language=lang)
        r["cer_whisper"], r["cer_es"] = _cer(text, r["whisper"]), _cer(text, r["es"])
        if lang == "en":
            r["extra_prefix"] = extra_prefix(text, r["whisper"])
        r["rtf"] = meta.get(key, {}).get("rtf")
    json.dump(res, open(os.path.join(W, "score.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("scored", len(res))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["prep", "gen", "score"])
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    {"prep": cmd_prep, "gen": cmd_gen, "score": cmd_score}[a.stage](a)
