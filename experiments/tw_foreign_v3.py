"""外語 v3（日文參考音候選、英文 prompt 改善），單一 Vox 實例（LoRA 掛載後關閉）、全程 torch.compile（optimize=True）。
階段（.venv-voxcpm，一次一個 GPU 工作；請設 PYTHONUTF8=1）：
  vcand   Vox 自舉 prompt 候選（en：neutral ULT；ja：假名化 + neutral ULT）
  verify  候選與 R1/R2 視窗的 ASR / SIM / 韻律檢查 -> cand_report.json、windows.json
  select  依規則挑 prompt，做修靜音（top_db=30）、高通，寫 prompts/（CPU）
  gen     測試句全矩陣，逐檔記 gen 秒數、首包延遲
  score   SIM / CER / 開頭多出內容，輸出到 DEST/{en,ja}/ 平鋪
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

import tw_foreign_v3_audio as A  # noqa: E402
import tw_foreign_v3_lib as L  # noqa: E402

TOP_DB = 30


def kana_norm():
    from japanese_kana import JapaneseKanaNormalizer
    return JapaneseKanaNormalizer()


def load_wav(p):
    y, sr = sf.read(p, dtype="float64")
    return (y if y.ndim == 1 else y.mean(axis=1)), sr


def trim30(y, sr, pad_head=0.12, pad_tail=0.25):
    import librosa
    yt, _ = librosa.effects.trim(y, top_db=TOP_DB, frame_length=1024, hop_length=256)
    return np.concatenate([np.zeros(int(sr * pad_head)), yt, np.zeros(int(sr * pad_tail))])


def make_model():
    import tw_lora_eval as E
    m = E.load_model(optimize=True)
    E.set_tag(m, E.DEFAULT_LORA_TAG)
    m.set_lora_enabled(False)  # 外語：LoRA 關閉
    return m


class Gen:
    """包 generate_streaming：量 gen 秒數與首包延遲（prompt 編碼含在內），固定 seed。"""

    def __init__(self, model):
        self.m, self.sr = model, model.tts_model.sample_rate

    def __call__(self, text, seed, ref, prompt=None, cfg=2.0, steps=10):
        import torch
        torch.manual_seed(seed)
        kw = dict(reference_wav_path=ref)
        if prompt:
            kw.update(prompt_wav_path=ref, prompt_text=prompt)
        torch.cuda.synchronize()
        t = time.perf_counter()
        first, ch = None, []
        for c in self.m.generate_streaming(text=text, cfg_value=cfg, inference_timesteps=steps, normalize=False, **kw):
            if first is None:
                torch.cuda.synchronize()
                first = time.perf_counter() - t
            ch.append(c)
        torch.cuda.synchronize()
        el = time.perf_counter() - t
        w = np.concatenate(ch)
        d = len(w) / self.sr
        return w, {"gen_s": round(el, 2), "first_s": round(first, 3), "audio_s": round(d, 2), "rtf": round(el / d, 3)}


def warm(g, n_path, n_text):
    """每個模式暖機：短、中、長各一句（避免 compile 形狀重編譯算進計時）。"""
    for t in ("Hello.", "The weather is nice today. I finished organizing everything.",
              "Please remember to save your work before closing the window, and I will check the settings again tomorrow morning."):
        g(t, 1, n_path, n_text)


def cmd_vcand(a):
    import tw_accent_check as T
    kana = kana_norm()
    n_path, n_text = T.neutral_ref()
    g = Gen(make_model())
    warm(g, n_path, n_text)
    os.makedirs(L.CAND, exist_ok=True)
    meta = {}
    for lang, texts in L.CAND_TEXT.items():
        for i, t in enumerate(texts):
            for s in (0, 1):
                sent = kana.normalize(t) if lang == "ja" else t
                w, m = g(sent, L.SEED + 300 + i * 10 + s, n_path, n_text)
                name = f"VOX_{lang}{i + 1}s{s}"
                sf.write(os.path.join(L.CAND, name + ".wav"), w, g.sr)
                meta[name] = {"text": t, "sent": sent, **m}
                print(name, m, flush=True)
    json.dump(meta, open(os.path.join(L.W, "vox_cand_meta.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def asr_pair(wm, path, lang):
    import es_asr
    import librosa
    y = librosa.load(path, sr=16000)[0]
    segs, _ = wm.transcribe(y, language=lang, beam_size=5)
    return es_asr.transcribe(path, language=lang), "".join(s.text for s in segs).strip()


def sim_fn():
    from resemblyzer import VoiceEncoder, preprocess_wav

    import tw_accent_check as T
    enc = VoiceEncoder("cuda")
    r1 = enc.embed_utterance(preprocess_wav(T.R1_16K))

    def sim(path):
        e = enc.embed_utterance(preprocess_wav(path))
        return round(float(np.dot(r1, e) / (np.linalg.norm(r1) * np.linalg.norm(e))), 4)
    return sim, enc


def cmd_verify(a):
    import glob

    import librosa
    from faster_whisper import WhisperModel
    kana = kana_norm()
    sim, enc = sim_fn()
    wm = WhisperModel("medium", device="cuda", compute_type="float16")
    rep = {}
    for p in sorted(glob.glob(os.path.join(L.CAND, "*.wav"))):
        name = os.path.basename(p)[:-4]
        lang, idx = name.split("_")[1][:2], int(name.split("_")[1][2]) - 1
        text = L.CAND_TEXT[lang][idx]
        y, sr = load_wav(p)
        yt = trim30(y, sr)
        tp = os.path.join(L.W, "_tmp.wav")
        sf.write(tp, yt, sr)
        es, wh = asr_pair(wm, tp, lang)
        r = {"text": text, "trim_s": round(len(yt) / sr - 0.37, 2), "es": es, "whisper": wh,
             "cer_es": L.cer(text, es), "cer_wh": L.cer(text, wh), "sim": sim(tp), **A.prosody(yt, sr)}
        if lang == "ja":
            r["cer_es_kana"], r["cer_wh_kana"] = L.cer(kana.normalize(text), kana.normalize(es)), L.cer(kana.normalize(text), kana.normalize(wh))
        rep[name] = r
        print(name, {k: r[k] for k in ("trim_s", "cer_es", "cer_wh", "sim", "f0_std_st", "rms_std_db")}, flush=True)
    json.dump(rep, open(os.path.join(L.W, "cand_report.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    # R1 / R2 的句界視窗：whisper 詞級時間戳，連續 1–3 句、4–10 秒，算韻律當「語氣是否平穩」的代理
    wins = {}
    for ref in ("R1", "R2"):
        y, sr = load_wav(os.path.join(L.REFS, ref + ".wav"))
        y16 = librosa.resample(y, orig_sr=sr, target_sr=16000)
        segs, _ = wm.transcribe(y16, language="en", beam_size=5, word_timestamps=True)
        words = [w for s in segs for w in s.words]
        sents, cur = [], []
        for w in words:
            cur.append(w)
            if re.search(r"[\.\?\!]$", w.word.strip()):
                sents.append(cur)
                cur = []
        for i in range(len(sents)):
            for j in range(i, min(i + 3, len(sents))):
                t0, t1 = sents[i][0].start, sents[j][-1].end
                if not 3.5 <= t1 - t0 <= 10.5:
                    continue
                t0c, t1c = max(0, t0 - 0.05), min(len(y) / sr, t1 + 0.12)
                seg = y[int(t0c * sr): int(t1c * sr)]
                txt = "".join(w.word for s in sents[i:j + 1] for w in s).strip()
                wins[f"{ref}_{i}-{j}"] = {"ref": ref, "t0": round(t0c, 2), "t1": round(t1c, 2), "text": txt, **A.prosody(seg, sr),
                                           **(A.head_lowfreq_db(seg, sr) or {})}
                print(f"{ref}_{i}-{j}", wins[f"{ref}_{i}-{j}"], flush=True)
    json.dump(wins, open(os.path.join(L.W, "windows.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def cmd_gen(a):
    import tw_accent_check as T
    kana = kana_norm()
    n_path, n_text = T.neutral_ref()
    P = json.load(open(os.path.join(L.PROMPTS, "prompts.json"), encoding="utf-8"))
    g = Gen(make_model())
    os.makedirs(L.RAW, exist_ok=True)
    meta_p = os.path.join(L.W, "gen_meta.json")
    meta = json.load(open(meta_p, encoding="utf-8")) if (a.only and os.path.exists(meta_p)) else {}
    keys = list(L.SENT)
    for lang in ("en", "ja"):
        if a.only and a.only not in (lang, "all"):
            continue
        specs = {}  # variant -> (ref, prompt_text or None)
        for name, p in P.items():
            if p["lang"] != lang:
                continue
            specs[f"REF-{name}"] = (p["wav"], None)
            specs[f"ULT-{name}"] = (p["wav"], p["text"])
        if lang == "en":
            specs["REF-R1"] = (os.path.join(L.REFS, "R1.wav"), None)  # v2 對照：未處理的 R1 原檔
        else:
            specs["ULT-NEUTRAL"] = (n_path, n_text)  # v2 最佳路徑：假名化 + 中文 neutral ULT
        first = next(iter(specs.values()))
        for ref, pt in (first, specs.get("ULT-NEUTRAL", first)):  # 暖機：各模式短中長
            warm(g, ref, pt)
        for i, k in enumerate(keys):
            if not k.startswith(lang):
                continue
            text = kana.normalize(L.SENT[k]) if lang == "ja" else L.SENT[k]
            for v, (ref, pt) in specs.items():
                w, m = g(text, L.SEED + 70 + i, ref, pt)
                sf.write(os.path.join(L.RAW, f"{k}__{v}.wav"), w, g.sr)
                meta[f"{k}__{v}"] = {"sent": text, "prompt": pt, **m}
                print(k, v, m["rtf"], m["first_s"], flush=True)
        json.dump(meta, open(meta_p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def _extra_prefix(ref, hyp):
    nw = lambda s: re.sub(r"[^a-z0-9 ]+", "", s.lower()).split()  # noqa: E731
    r, h = nw(ref), nw(hyp)
    for i in range(len(h)):
        if h[i:i + 3] == r[:3]:
            return " ".join(h[:i])
    return "（原文開頭未對上）"


def cmd_score(a):
    import glob
    from math import gcd

    import torch
    from faster_whisper import WhisperModel
    from scipy.signal import resample_poly

    import tw_pinyin_check as PC
    kana = kana_norm()
    meta = json.load(open(os.path.join(L.W, "gen_meta.json"), encoding="utf-8"))
    v25 = json.load(open(os.path.join(L.W, "v25_meta.json"), encoding="utf-8"))
    sim, enc = sim_fn()
    res = {}
    for p in sorted(glob.glob(os.path.join(L.RAW, "*.wav"))):
        key = os.path.basename(p)[:-4]
        k, v = key.split("__")
        lang = k[:2]
        y, sr = load_wav(p)
        g = gcd(sr, 44100)
        fin, _ = PC._post(resample_poly(y, 44100 // g, sr // g), 44100, top_db=TOP_DB)
        d = os.path.join(L.DEST, lang)
        os.makedirs(d, exist_ok=True)
        f = os.path.join(d, key + ".wav")
        sf.write(f, fin, 44100, subtype="PCM_16")
        m = meta.get(key) or v25.get(key, {})
        res[key] = {"file": f, "sim": sim(f), "audio_s": round(len(fin) / 44100, 2),
                    **({k2: m[k2] for k2 in ("rtf", "first_s", "gen_s") if k2 in m} if v != "V25" and v != "V25-EMO" else {"rtf_eager": m.get("rtf")})}
    del enc
    torch.cuda.empty_cache()
    wm = WhisperModel("medium", device="cuda", compute_type="float16")
    for key, r in res.items():
        k = key.split("__")[0]
        lang, text = k[:2], L.SENT[k]
        r["es"], r["whisper"] = asr_pair(wm, r["file"], lang)
        r["cer_es"], r["cer_wh"] = L.cer(text, r["es"]), L.cer(text, r["whisper"])
        if lang == "ja":
            r["cer_es_kana"], r["cer_wh_kana"] = L.cer(kana.normalize(text), kana.normalize(r["es"])), L.cer(kana.normalize(text), kana.normalize(r["whisper"]))
        else:
            r["extra_prefix"] = _extra_prefix(text, r["whisper"])
        print(key, r["sim"], r["cer_es"], r["cer_wh"], flush=True)
    json.dump(res, open(os.path.join(L.W, "score.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def cmd_select(a):
    """挑 prompt（規則見 README）：時長 6–10 秒、ASR 對得上、句界結束；中性度以 F0/RMS 變異當代理。
    處理：top_db=30 修靜音、70Hz 高通；DN 變體另加輕度頻譜閘降噪。寫 prompts/ 與 prompts.json。"""
    import librosa
    kana = kana_norm()
    rep = json.load(open(os.path.join(L.W, "cand_report.json"), encoding="utf-8"))
    wins = json.load(open(os.path.join(L.W, "windows.json"), encoding="utf-8"))
    v2 = os.path.join(L.ROOT, "outputs", "foreign_v2", "refs")
    os.makedirs(L.PROMPTS, exist_ok=True)
    ptxt2 = lambda n: json.load(open(os.path.join(v2, n + ".json"), encoding="utf-8"))["text"]  # noqa: E731
    # name: (lang, 來源 wav, 起訖秒或 None, 文字, 說明)
    pick = {
        "R1c": ("en", os.path.join(v2, "R1c.wav"), None, ptxt2("R1c"), "v2 的 R1c（R1 的 3.04–10.75s）重新以 top_db=30 修靜音"),
        "R2c": ("en", os.path.join(v2, "R2c.wav"), None, ptxt2("R2c"), "v2 的 R2c（R2 的 0–6.0s）重新修靜音"),
        "R1n": ("en", os.path.join(L.REFS, "R1.wav"), (wins["R1_2-3"]["t0"], wins["R1_2-3"]["t1"]), wins["R1_2-3"]["text"], "R1 的第 3–4 句（較平穩段，避開開頭起伏大的第一句）"),
        "R2n": ("en", os.path.join(L.REFS, "R2.wav"), (wins["R2_1-3"]["t0"], wins["R2_1-3"]["t1"]), wins["R2_1-3"]["text"], "R2 的第 2–4 句（F0 變異最小的視窗，避開後半 Magnificent / What? / Um）"),
        "EV25a": ("en", os.path.join(L.CAND, "V25_en4s0.wav"), None, L.CAND_TEXT["en"][3], "V25（現行路徑，R1 角色向量）合成的中性英文"),
        "EV25b": ("en", os.path.join(L.CAND, "V25_en3s0.wav"), None, L.CAND_TEXT["en"][2], "V25 合成的中性英文（另一句）"),
        "EVOXa": ("en", os.path.join(L.CAND, "VOX_en1s0.wav"), None, L.CAND_TEXT["en"][0], "Vox 自舉：neutral ULT 合成的中性英文"),
        "EVOXb": ("en", os.path.join(L.CAND, "VOX_en2s0.wav"), None, L.CAND_TEXT["en"][1], "Vox 自舉（另一句）"),
        "JV25a": ("ja", os.path.join(L.CAND, "V25_ja3s0.wav"), None, kana.normalize(L.CAND_TEXT["ja"][2]), "(a) V25 合成的中性日文；逐字稿為假名化文字"),
        "JV25b": ("ja", os.path.join(L.CAND, "V25_ja2s1.wav"), None, kana.normalize(L.CAND_TEXT["ja"][1]), "(a) V25 合成的中性日文（另一句）"),
        "JVOXa": ("ja", os.path.join(L.CAND, "VOX_ja3s0.wav"), None, kana.normalize(L.CAND_TEXT["ja"][2]), "(b) Vox 自舉：KANA_ULT-NEUTRAL 路徑合成"),
        "JVOXb": ("ja", os.path.join(L.CAND, "VOX_ja2s1.wav"), None, kana.normalize(L.CAND_TEXT["ja"][1]), "(b) Vox 自舉（另一句）"),
        "R4c": ("ja", os.path.join(v2, "R4c.wav"), None, kana.normalize(ptxt2("R4c")), "(c) ITA 原聲（R4 的 1.9–9.8s），逐字稿假名化"),
    }
    dn_of = ["R1c", "R2n", "EV25a"]  # 降噪前後比較只做這三個，不擴散
    out = {}

    def noise_floor_db(y, sr):
        rms = librosa.feature.rms(y=y, frame_length=1024, hop_length=256)[0]
        return round(float(20 * np.log10(np.percentile(rms, 10) + 1e-7)), 1)

    for name, (lang, src, seg, text, desc) in pick.items():
        y, sr = load_wav(src)
        if seg:
            y = y[int(seg[0] * sr): int(seg[1] * sr)]
        before = {"head_low_db": A.head_lowfreq_db(y, sr), "noise_floor_db": noise_floor_db(y, sr)}
        yp = A.highpass(trim30(y, sr), sr, 70.0)
        yp[: int(sr * 0.02)] *= np.linspace(0, 1, int(sr * 0.02))  # 20ms 淡入，避免高通後的起始爆音
        variants = {name: yp}
        if name in dn_of:
            variants[name + "DN"] = A.spectral_gate(yp, sr)
        for vn, yy in variants.items():
            wp = os.path.join(L.PROMPTS, vn + ".wav")
            sf.write(wp, yy, sr)
            out[vn] = {"lang": lang, "wav": wp, "text": text, "desc": desc + ("；另加輕度頻譜閘降噪（-12dB 閘）" if vn.endswith("DN") else ""),
                       "src": os.path.relpath(src, L.ROOT), "seg": seg, "dur_s": round(len(yy) / sr, 2), **A.prosody(yy, sr),
                       "head_low_before": before["head_low_db"], "head_low_after": A.head_lowfreq_db(yy, sr),
                       "noise_floor_before_db": before["noise_floor_db"], "noise_floor_after_db": noise_floor_db(yy, sr)}
            print(vn, {k: out[vn][k] for k in ("dur_s", "f0_std_st", "head_low_before", "head_low_after", "noise_floor_before_db", "noise_floor_after_db")})
    json.dump(out, open(os.path.join(L.PROMPTS, "prompts.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=float)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["vcand", "verify", "select", "gen", "score"])
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    {"vcand": cmd_vcand, "verify": cmd_verify, "select": cmd_select, "gen": cmd_gen, "score": cmd_score}[a.stage](a)
