"""盲測素材 uep-voice-1101：IndexTTS（V25-L085 現行）對「每個語言定案的新版本」，每題兩個候選、一組配對。
中文新版本固定 TWLORA-TWN-SPD-ref120（LoRA=step_0000600、TWN 前處理、參考音先加速 1.2 倍再 ULT 克隆）。
en / ja 等艾斯維爾在 foreign_v3 選定版本後，用 build 的 --en / --ja 加入（讀 foreign_v3 的成品與 score.json）。

階段（.venv-voxcpm，請設 PYTHONUTF8=1，一次一個 GPU 工作）：
  gen    中文 8 句，torch.compile（optimize=True）、暖機後生成；每個輸出用 ES ASR 把關，
         CER > GATE 就換 seed 重生（最多 3 次重試），seed 與每次 CER 記在 outputs/blind_work/uep-voice-1101/gen_log.json。
         V25 端沒有做這種把關（沿用 2026-10-31_tw_accent_check 既有 V25-L085）。
  build  建 outputs/blind_sets/uep-voice-1101/（沿用 webapp 契約），解盲 key 放 outputs/blind_keys/uep-voice-1101_key.json。
         item_id / cand_id 為中性編號；音檔 44.1kHz、-23 LUFS、去頭尾靜音。
不啟動 server。
"""
import argparse
import json
import os
import random
import re
import shutil
import sys
from math import gcd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

import tw_accent_check as T  # noqa: E402
import tw_blind_1031b as B  # noqa: E402  只借 cer()
import tw_lora_eval as E  # noqa: E402
import tw_pinyin_check as P  # noqa: E402

SET = "uep-voice-1101"
BASE = os.path.join(T.ROOT, "outputs", "blind_sets", SET)
KEYDIR = os.path.join(T.ROOT, "outputs", "blind_keys")
WORK = os.path.join(T.ROOT, "outputs", "blind_work", SET)
FV3 = os.path.join(T.ROOT, "outputs", "foreign_v3")
GATE = 0.12
NEW_ZH = "VOX-ULT-TWLORA-TWN-SPD-ref120"
OLD = "V25-L085"


def cmd_gen(a):
    import time

    import torch
    from scipy.signal import resample_poly

    from taiwan_pronunciation import TaiwanPronunciationNormalizer
    import es_asr
    norm = TaiwanPronunciationNormalizer()
    n_path, n_text = T.neutral_ref()
    ref120 = os.path.join(E.EV, "neutral_ref120.wav")
    model = E.load_model(optimize=True)
    sr = model.tts_model.sample_rate
    E.set_tag(model, E.DEFAULT_LORA_TAG)
    model.set_lora_enabled(True)
    raw = os.path.join(WORK, "raw")
    os.makedirs(raw, exist_ok=True)
    os.makedirs(os.path.join(WORK, "zh"), exist_ok=True)

    def synth(text, seed):
        torch.manual_seed(seed)
        torch.cuda.synchronize()
        t = time.perf_counter()
        first, ch = None, []
        for c in model.generate_streaming(text=text, cfg_value=2.0, inference_timesteps=10, normalize=False,
                                          reference_wav_path=ref120, prompt_wav_path=ref120, prompt_text=n_text):
            if first is None:
                torch.cuda.synchronize()
                first = time.perf_counter() - t
            ch.append(c)
        torch.cuda.synchronize()
        el = time.perf_counter() - t
        w = np.concatenate(ch)
        return w, {"gen_s": round(el, 2), "first_s": round(first, 3), "audio_s": round(len(w) / sr, 2), "rtf": round(el / (len(w) / sr), 3)}

    for t in ("你好。", "週末我想去市場買點菜，順便繞去書店逛逛。", T.SENTENCES[1][1], T.SENTENCES[7][1]):  # 暖機：短、中、長
        synth(norm.normalize(t), 1)
    log_p = os.path.join(WORK, "gen_log.json")
    only = [x for x in a.only.split(",") if x]
    log = json.load(open(log_p, encoding="utf-8")) if only else {}  # --only 時保留其他句的紀錄
    for i, (sid, text, _, _) in enumerate(T.SENTENCES):
        if only and sid not in only:
            continue
        twn = norm.normalize(text)
        tries = []
        for k in range(4):
            seed = T.SEED + i + 100 * k
            w, m = synth(twn, seed)
            p = os.path.join(raw, f"{sid}_try{k}.wav")
            sf.write(p, w, sr)
            c = B.cer(text, es_asr.transcribe(p))
            tries.append({"seed": seed, "cer": round(c, 3), **m})
            if c <= GATE:
                break
        best = len(tries) - 1 if tries[-1]["cer"] <= GATE else min(range(len(tries)), key=lambda j: tries[j]["cer"])
        w = sf.read(os.path.join(raw, f"{sid}_try{best}.wav"), dtype="float64")[0]
        g = gcd(sr, 44100)
        fin, _ = P._post(resample_poly(w, 44100 // g, sr // g), 44100)
        sf.write(os.path.join(WORK, "zh", f"{sid}.wav"), fin, 44100, subtype="PCM_16")
        if only:  # 句子改文字時，同步覆蓋 accent_check 資料夾中的對應檔
            sf.write(os.path.join(T.DEST, sid, f"{sid}__{NEW_ZH}.wav"), fin, 44100, subtype="PCM_16")
        log[sid] = {"sent": twn, "tries": tries, "pick": best, "gate_ok": tries[best]["cer"] <= GATE}
        print(sid, [(t["seed"], t["cer"], t["rtf"], t["first_s"]) for t in tries], flush=True)
    json.dump(log, open(log_p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def zh_specs():
    """中文 8 題：(語言, 句子編號, 文字, [(system, 檔案, 額外 metrics)])；新版本在前、V25 在後（顯示順序會被隨機打亂）。"""
    log = json.load(open(os.path.join(WORK, "gen_log.json"), encoding="utf-8"))
    out = []
    for sid, text, _, _ in T.SENTENCES:
        t = log[sid]["tries"][log[sid]["pick"]]
        out.append(("zh", sid, text, [
            ("new", os.path.join(WORK, "zh", f"{sid}.wav"), NEW_ZH, {"rtf": t["rtf"], "first_chunk_s": t["first_s"]}),
            ("v25", os.path.join(T.DEST, sid, f"{sid}__{OLD}.wav"), OLD, {})]))
    return out


def foreign_specs(lang, variant):
    """en / ja：選定版本與 V25 都取自 foreign_v3 的成品（DEST/<lang>/<id>__<variant>.wav）與 score.json。"""
    import tw_foreign_v3_lib as L
    score = json.load(open(os.path.join(FV3, "score.json"), encoding="utf-8"))
    out = []
    for k, text in L.SENT.items():
        if not k.startswith(lang):
            continue
        d = os.path.join(L.DEST, lang)
        s = score.get(f"{k}__{variant}", {})
        out.append((lang, k, text, [
            ("new", os.path.join(d, f"{k}__{variant}.wav"), variant, {"rtf": s.get("rtf"), "first_chunk_s": s.get("first_s")}),
            ("v25", os.path.join(d, f"{k}__V25.wav"), "V25", {})]))
    return out


def cmd_build(a):
    import pyloudnorm as pyln
    from resemblyzer import VoiceEncoder, preprocess_wav

    import es_asr
    specs = zh_specs()
    if a.en:
        specs += foreign_specs("en", a.en)
    if a.ja:
        specs += foreign_specs("ja", a.ja)
    enc = VoiceEncoder("cuda")
    r1 = enc.embed_utterance(preprocess_wav(T.R1_16K))
    rng = random.Random(1101)
    shutil.rmtree(BASE, ignore_errors=True)
    os.makedirs(os.path.join(BASE, "audio"))
    os.makedirs(os.path.join(BASE, "reviews"))
    os.makedirs(KEYDIR, exist_ok=True)
    meter = pyln.Meter(44100)
    items, key = [], {}
    for n, (lang, sid, text, cands_in) in enumerate(specs, 1):
        iid = f"i{n:02d}"
        order = list(range(len(cands_in)))
        rng.shuffle(order)
        cands, cid_of = [], {}
        for k, idx in enumerate(order, 1):
            role, src, label, extra = cands_in[idx]
            cid = f"c{k}"
            cid_of[role] = cid
            rel = f"audio/{iid}_{cid}.wav"
            shutil.copyfile(src, os.path.join(BASE, rel))
            y, sr = sf.read(src, dtype="float64")
            e = enc.embed_utterance(preprocess_wav(src))
            sim = float(np.dot(r1, e) / (np.linalg.norm(r1) * np.linalg.norm(e)))
            asr = es_asr.transcribe(src, language=lang)
            c_er = B.cer(text, asr) if lang == "zh" else None
            if c_er is None:
                import tw_foreign_v3_lib as L
                c_er = L.cer(text, asr)
            cands.append({"cand_id": cid, "system": role, "params": {"source": label, "lora": E.DEFAULT_LORA_TAG if (lang == "zh" and role == "new") else None},
                          "file": rel, "metrics": {"sim_r1": round(sim, 4), "cer": round(c_er, 4), "rtf": extra.get("rtf"), "first_chunk_s": extra.get("first_chunk_s"),
                                                    "audio_s": round(len(y) / sr, 3), "lufs": round(meter.integrated_loudness(y), 2)}})
            key[f"{iid}/{cid}"] = {"sentence": sid, "lang": lang, "system": role, "source": label}
        items.append({"item_id": iid, "text": text, "lang": lang, "emotion": "neutral", "candidates": cands,
                      "pairs": [[cid_of["new"], cid_of["v25"]]]})
    json.dump({"set": SET, "created": "2026-11-01", "description": "同一角色聲音的兩個合成版本比較，請依自然度與角色相似度擇一。", "items": items},
              open(os.path.join(BASE, "manifest.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    json.dump(key, open(os.path.join(KEYDIR, f"{SET}_key.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("built", len(items), "items")


def cmd_check(a):
    """用 webapp 的 BlindStore 實際讀 manifest：題數、每題恰好一組配對、音檔路徑都在 set 目錄內。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("blind_server", os.path.join(T.ROOT, "webapp", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    store = srv.BlindStore(os.path.join(T.ROOT, "outputs", "blind_sets"))
    pairs = store.pairs(SET)
    m = store.manifest(SET)
    base = os.path.realpath(BASE)
    bad = [c["file"] for it in m["items"] for c in it["candidates"]
           if not os.path.realpath(os.path.join(BASE, c["file"])).startswith(base + os.sep)]
    per_item = {}
    for p in pairs:
        per_item[p["item_id"]] = per_item.get(p["item_id"], 0) + 1
    leak = re.search(r'"system"\s*:\s*"(new|v25)"', json.dumps(store.quiz(SET, "a" * 32) or {}))
    print({"items": len(m["items"]), "pairs": len(pairs), "one_pair_each": all(v == 1 for v in per_item.values()) and len(per_item) == len(m["items"]),
           "bad_paths": bad, "quiz_leaks_system": bool(leak)})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["gen", "build", "check"])
    ap.add_argument("--only", default="", help="gen 只重生指定句（如 s03）")
    ap.add_argument("--en", default="", help="英文選定版本（foreign_v3 的版本名，如 ULT-R1c）")
    ap.add_argument("--ja", default="", help="日文選定版本")
    a = ap.parse_args()
    {"gen": cmd_gen, "build": cmd_build, "check": cmd_check}[a.stage](a)
