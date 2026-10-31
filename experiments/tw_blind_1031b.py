"""盲測素材 uep-voice-1031b：TWLORA-TWN-SPD（ref120 / post120）vs 現行 IndexTTS（V25-L085）。
1) gen：用修正後的字典重做 8 句 VOX-ULT-TWLORA-TWN 與 -SPD-ref120 / -post120（LoRA=step_0000600，ULT neutral）。
   為擋掉單次隨機失誤（如 s03 的「這 P4」），每個輸出用 ES ASR 檢查，字元錯誤率 > GATE 就換 seed 重生（最多 3 次），
   實際 seed 與每次 CER 記在 outputs/tw_lora_eval/final2_log.json。V25 沒有做這種篩選。
2) build：建 outputs/blind_sets/uep-voice-1031b/（沿用 webapp 契約），解盲 key 放 outputs/blind_keys/ 之外。
不啟動 server。"""
import argparse, json, os, random, sys, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from math import gcd
import numpy as np, soundfile as sf
import tw_accent_check as T, tw_lora_eval as E, tw_pinyin_check as P, es_asr
from taiwan_pronunciation import TaiwanPronunciationNormalizer

EV = E.EV
SET = "uep-voice-1031b"
BASE = os.path.join(T.ROOT, "outputs", "blind_sets", SET)
KEYDIR = os.path.join(T.ROOT, "outputs", "blind_keys")
GATE = 0.12
VERS = {"ref120": "VOX-ULT-TWLORA-TWN-SPD-ref120", "post120": "VOX-ULT-TWLORA-TWN-SPD-post120", "v25": "V25-L085"}


def cer(ref, hyp):
    import re
    from opencc import OpenCC
    from rapidfuzz.distance import Levenshtein
    f = lambda t: re.sub(r"[^一-鿿A-Za-z0-9]+", "", OpenCC("s2t").convert(t)).lower()  # noqa: E731
    r, h = f(ref), f(hyp)
    return Levenshtein.distance(r, h) / max(1, len(r))


def cmd_gen(a):
    norm = TaiwanPronunciationNormalizer()
    n_path, n_text = T.neutral_ref()
    ref120 = os.path.join(EV, "neutral_ref120.wav")
    model = E.load_model(); sr = model.tts_model.sample_rate
    E.set_tag(model, E.DEFAULT_LORA_TAG)
    E.synth(model, "你好。", 1, n_path, n_text)
    raw = os.path.join(EV, "final2_raw"); os.makedirs(raw, exist_ok=True)
    log = {}

    def gated(text, base_seed, ref, tag):
        tries = []
        for k in range(4):
            seed = base_seed + 100 * k
            w = E.synth(model, text, seed, ref, n_text)
            p = os.path.join(raw, f"{tag}_try{k}.wav"); sf.write(p, w, sr)
            c = cer(text_plain(text), es_asr.transcribe(p))
            tries.append({"seed": seed, "cer": round(c, 3)})
            if c <= GATE:
                break
        best = min(range(len(tries)), key=lambda j: tries[j]["cer"]) if tries[-1]["cer"] > GATE else len(tries) - 1
        w = sf.read(os.path.join(raw, f"{tag}_try{best}.wav"), dtype="float64")[0]
        return w, tries, best

    def text_plain(t):
        import re
        return re.sub(r"\{[^{}]*\}", "", t)

    for i, (sid, text, _, _) in enumerate(T.SENTENCES):
        twn = norm.normalize(text)
        # CER 比對用原句（含 {…} 的替換字已還原為原字）
        def run(ref, tag):
            global text_plain
            return gated(twn, T.SEED + i, ref, f"{sid}_{tag}")
        text_plain = lambda t, _o=text: _o  # noqa: E731
        base_w, t1, b1 = run(n_path, "base")
        ref_w, t2, b2 = run(ref120, "ref120")
        post_w = P._stretch_wsola(np.asarray(base_w, dtype="float64"), sr, 1.2)
        for name, w in (("VOX-ULT-TWLORA-TWN", base_w), ("VOX-ULT-TWLORA-TWN-SPD-ref120", ref_w), ("VOX-ULT-TWLORA-TWN-SPD-post120", post_w)):
            g = gcd(sr, 44100)
            from scipy.signal import resample_poly
            fin, _ = P._post(resample_poly(np.asarray(w, dtype="float64"), 44100 // g, sr // g), 44100)
            sf.write(os.path.join(T.DEST, sid, f"{sid}__{name}.wav"), fin, 44100, subtype="PCM_16")
        log[sid] = {"sent": twn, "base": t1, "ref120": t2, "base_pick": b1, "ref120_pick": b2}
        print(sid, t1, t2, flush=True)
    json.dump(log, open(os.path.join(EV, "final2_log.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def cmd_build(a):
    import pyloudnorm as pyln
    from resemblyzer import VoiceEncoder, preprocess_wav
    enc = VoiceEncoder("cuda")
    r1 = enc.embed_utterance(preprocess_wav(T.R1_16K))
    rng = random.Random(1031)
    shutil.rmtree(BASE, ignore_errors=True)
    os.makedirs(os.path.join(BASE, "audio")); os.makedirs(os.path.join(BASE, "reviews")); os.makedirs(KEYDIR, exist_ok=True)
    items, key = [], {}
    meter = pyln.Meter(44100)
    for n, (sid, text, _, _) in enumerate(T.SENTENCES, 1):
        iid = f"i{n:02d}"
        order = list(VERS); rng.shuffle(order)
        cands, cid_of = [], {}
        for k, v in enumerate(order, 1):
            src = os.path.join(T.DEST, sid, f"{sid}__{VERS[v]}.wav")
            cid = f"c{k}"; cid_of[v] = cid
            rel = f"audio/{iid}_{cid}.wav"; shutil.copyfile(src, os.path.join(BASE, rel))
            y, sr = sf.read(src, dtype="float64")
            e = enc.embed_utterance(preprocess_wav(src))
            sim = float(np.dot(r1, e) / (np.linalg.norm(r1) * np.linalg.norm(e)))
            asr = es_asr.transcribe(src)
            cands.append({"cand_id": cid, "system": v, "params": {"source": VERS[v], "lora": E.DEFAULT_LORA_TAG if v != "v25" else None},
                          "file": rel, "metrics": {"sim_r1": round(sim, 4), "cer": round(cer(text, asr), 4), "rtf": None, "first_chunk_s": None,
                                                    "audio_s": round(len(y) / sr, 3), "lufs": round(meter.integrated_loudness(y), 2)}})
            key[f"{iid}/{cid}"] = {"sentence": sid, "system": v, "source": VERS[v]}
        items.append({"item_id": iid, "text": text, "lang": "zh", "emotion": "neutral", "candidates": cands,
                      "pairs": [[cid_of["ref120"], cid_of["v25"]], [cid_of["post120"], cid_of["v25"]], [cid_of["ref120"], cid_of["post120"]]]})
    json.dump({"set": SET, "created": "2026-11-01", "description": "同一角色聲音的多種合成版本比較，請依自然度與角色相似度擇一。", "items": items},
              open(os.path.join(BASE, "manifest.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    json.dump(key, open(os.path.join(KEYDIR, f"{SET}_key.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("built", len(items), "items")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("stage", choices=["gen", "build"]); a = ap.parse_args()
    {"gen": cmd_gen, "build": cmd_build}[a.stage](a)
