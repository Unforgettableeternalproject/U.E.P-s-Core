"""VoxCPM2 以 2.5 情緒參考音做廣泛比對，並產出盲測 set。

階段（分環境執行；狀態集中在 outputs/voxcpm2_vs_v25/manifest.json）：
  v25    [core env]     登記 v25_final 既有 9 句，並用 2.5(LoRA+uep_v25) 補生成缺的（台灣口語、其餘六種情緒）
  vox    [.venv-voxcpm] VoxCPM2 生成各變體，記錄首包延遲 / RTF
  score  [.venv-voxcpm] 全部候選算 SIM(對 R1) 與 Whisper CER
  blind  [core env]     統一 44.1kHz / -23 LUFS，建 outputs/blind_sets/<set>/manifest.json
  report [任一]          彙整指標表

  core env = U.E.P-s-Core/env/Scripts/python.exe
"""
import argparse
import json
import os
import random
import re
import shutil
import statistics
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPOS = os.path.dirname(ROOT)
MAIN = os.path.join(REPOS, "TestSeparateTTSSystem")
WORK = os.path.join(ROOT, "outputs", "voxcpm2_vs_v25")
RAW = os.path.join(WORK, "raw")
MANIFEST = os.path.join(WORK, "manifest.json")
EMO_DIR = os.path.join(ROOT, "outputs", "v25_emotion_refs")
EMO_JSON = os.path.join(EMO_DIR, "emotion_refs.json")
R1_PATH = os.path.join(ROOT, "echo_tts", "assets", "reference", "uep_reference_v25.wav")
V25_DIR = os.path.join(MAIN, "outputs", "v25_final")
MODEL_DIR = os.path.join(ROOT, "models_cache", "VoxCPM2")
SET_NAME = "20261031_voxcpm2_vs_v25"
BLIND_DIR = os.path.join(ROOT, "outputs", "blind_sets", SET_NAME)
TARGET_SR = 44100
TARGET_LUFS = -23.0
PAD_S = 0.25  # 盲測音檔統一頭尾靜音長度

# (item_id, lang, emotion, text, v25_final 既有檔名或 None)
ITEMS = [
    ("zh_neutral", "zh", "neutral", "今天的天气不错，我把桌面上的工作都整理完了，接下来要做什么呢？", "B_zh_LORA"),
    ("zh_happy", "zh", "happy", "哈哈，你猜怎么着？新模型的测试一次就通过了！我们赶快把它装上去试试吧！", "B_zh_LORA_happy05"),
    ("zh_sad", "zh", "sad", "我...对不起……我把之前的存档弄丢了，明明答应过你要好好保管的……", "B_zh_LORA_sad05"),
    ("en_neutral", "en", "neutral", "The weather is nice today. I finished organizing everything on my desk. What should we do next?", "B_en"),
    ("en_happy", "en", "happy", "Haha! Guess what? The new model passed every single test on the first try! Let's get it running right now!", "B_en_happy05"),
    ("en_sad", "en", "sad", "I...I'm sorry... I lost the backup files. I promised I would keep them safe, and I let you down...", "B_en_sad05"),
    ("ja_neutral", "ja", "neutral", "今日はいい天気ですね。机の上の仕事を全部片付けました。次は何をしましょうか？", "B_ja"),
    ("ja_happy", "ja", "happy", "ねえねえ、聞いて！新しいモデルのテスト、一発で全部合格したんだよ！すごくない？", "B_ja_happy065"),
    ("ja_sad", "ja", "sad", "あ...ごめんなさい……大事なデータを失くしちゃった……ちゃんと守るって約束したのに……", "B_ja_sad065"),
    ("tw_neutral1", "tw", "neutral", "欸，你有看到昨天那個新聞嗎？齁，真的超扯的啦，我到現在還是不敢相信。", None),
    ("tw_neutral2", "tw", "neutral", "那個便當也太好吃了吧，老闆還多送我一顆滷蛋，超佛心的欸。", None),
    ("tw_neutral3", "tw", "neutral", "不是啦，我跟你說，這個錯誤真的很靠北，我找了一整個下午才發現是少一個逗號。", None),
    # 其餘六種情緒各一句（自擬，與參考音文本不同）
    ("zh_angry", "zh", "angry", "你說會準時到，結果又讓我等了一個小時，每次都這樣，我真的受夠了！", None),
    ("zh_afraid", "zh", "afraid", "那個聲音……是不是從後面傳來的？拜託你不要丟下我一個人在這裡，我真的很害怕。", None),
    ("zh_disgusted", "zh", "disgusted", "欸，你別把那個拿過來啦，那個便當都餿掉了，聞起來超噁心的，快點拿去丟掉。", None),
    ("zh_melancholic", "zh", "melancholic", "傍晚的風吹過空蕩蕩的操場，我忽然想起很久以前的夏天，一切都好遠好遠了。", None),
    ("zh_surprised", "zh", "surprised", "哇！你竟然記得我的生日，還準備了蛋糕？我完全沒想到，這也太驚喜了吧！", None),
    ("zh_calm", "zh", "calm", "先不用擔心，事情已經處理好了。你坐下來喝杯茶，我們慢慢把剩下的步驟看一遍。", None),
]
HINTS = {"happy": "(cheerful tone)", "sad": "(sad tone)", "angry": "(angry tone)", "afraid": "(frightened tone)",
         "disgusted": "(disgusted tone)", "melancholic": "(melancholic tone)",
         "surprised": "(surprised tone)", "calm": "(calm tone)"}
# VoxCPM2 變體：mode ult=ultimate cloning(prompt 加逐字稿)，ref=只給參考音
# src=emo 用該情緒參考音；src=neutral_hint 用 neutral 參考音加括號情緒提示（僅非 neutral 情緒）
VARIANTS = [
    ("ult_cfg2_t10", {"mode": "ult", "src": "emo", "cfg_value": 2.0, "inference_timesteps": 10}),
    ("ult_cfg3_t10", {"mode": "ult", "src": "emo", "cfg_value": 3.0, "inference_timesteps": 10}),
    ("ult_cfg2_t20", {"mode": "ult", "src": "emo", "cfg_value": 2.0, "inference_timesteps": 20}),
    ("ref_cfg2_t10", {"mode": "ref", "src": "emo", "cfg_value": 2.0, "inference_timesteps": 10}),
    ("hint_cfg2_t10", {"mode": "ref", "src": "neutral_hint", "cfg_value": 2.0, "inference_timesteps": 10}),
]
SEED = 42


def load_man():
    return json.load(open(MANIFEST, encoding="utf-8"))


def save_man(m):
    os.makedirs(WORK, exist_ok=True)
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)


def emo_refs():
    d = json.load(open(EMO_JSON, encoding="utf-8"))
    refs = {}
    for it in d["items"]:
        if it.get("selected"):
            refs[it["emotion"]] = {"path": os.path.join(EMO_DIR, it["selected_file"]), "text": it["text"],
                                   "vector": it["vector"], "sim_r1": it["sim_r1"]}
    return refs


def lang_key(lang):
    return "zh" if lang == "tw" else lang


# ---------------------------------------------------------------- v25
def cmd_v25(args):
    os.makedirs(RAW, exist_ok=True)
    man = {"items": {}, "created": datetime.now().isoformat(timespec="seconds")}
    todo = []
    for item_id, lang, emo, text, existing in ITEMS:
        man["items"][item_id] = {"text": text, "lang": lang, "emotion": emo, "cands": []}
        if existing:
            dst = os.path.join(RAW, f"v25_{item_id}.wav")
            shutil.copy2(os.path.join(V25_DIR, existing + ".wav"), dst)
            man["items"][item_id]["cands"].append({
                "key": "v25", "system": "v25", "raw": f"raw/v25_{item_id}.wav",
                "params": {"source": f"v25_final/{existing}.wav", "reference": "uep_reference_v25 (slice_04)"},
                "metrics": {}})
        else:
            todo.append((item_id, lang, emo, text))

    sys.path.insert(0, ROOT)
    import soundfile as sf
    import torch
    from echo_tts.config.emotion_presets import (
        get_preset_overrides, get_preset_vector, load_presets, scale_to_user_strength)
    from echo_tts.runtime.indextts_backend import IndexTTS2Backend
    from echo_tts.runtime.indextts_generate import default_generation_paths

    presets = load_presets()
    backend = IndexTTS2Backend(paths=default_generation_paths(ROOT), device="cuda", use_fp16=True,
                               use_cuda_kernel=True, use_accel=False)
    backend.load()
    backend.load_lora()
    for item_id, lang, emo, text in todo:
        entry = presets[emo]
        vec = scale_to_user_strength(list(get_preset_vector(entry)), 0.5)
        backend.emotion_vector = vec if sum(vec) > 0 else None
        torch.manual_seed(SEED)
        out = os.path.join(RAW, f"v25_{item_id}.wav")
        first = {}
        t0 = time.perf_counter()

        def on_seg(audio, idx, total, _f=first, _t0=t0):
            _f.setdefault("t", time.perf_counter() - _t0)

        backend.generate(text, out, language="zh", emotion_overrides=get_preset_overrides(entry),
                         on_segment_audio=on_seg)
        total = time.perf_counter() - t0
        info = sf.info(out)
        dur = info.frames / info.samplerate
        print(f"v25 {item_id} dur={dur:.1f}s gen={total:.1f}s", flush=True)
        man["items"][item_id]["cands"].append({
            "key": "v25", "system": "v25", "raw": f"raw/v25_{item_id}.wav",
            "params": {"source": "IndexTTS2Backend + LoRA gpt_best(0.85) + uep_v25", "emotion": emo,
                       "vector": [round(v, 4) for v in vec], "seed": SEED},
            "metrics": {"audio_s": round(dur, 3), "total_s": round(total, 3), "rtf": round(total / dur, 3),
                        "first_chunk_s": round(first.get("t", total), 3)}})
    # 既有項目的 v25 候選不含生成計時（來自 v25_final）；已補生成者才有 RTF
    save_man(man)


# ---------------------------------------------------------------- vox
def cmd_vox(args):
    import numpy as np
    import soundfile as sf
    import torch
    from voxcpm import VoxCPM
    man = load_man()
    refs = emo_refs()
    model = VoxCPM.from_pretrained(MODEL_DIR, load_denoiser=False, optimize=False, local_files_only=True)
    sr = model.tts_model.sample_rate
    man["vox_env"] = {"sr": sr, "gpu": torch.cuda.get_device_name(0), "torch": torch.__version__}

    def run_one(text, ref_path, prompt_text, cfg, steps, seed):
        torch.manual_seed(seed)
        torch.cuda.synchronize()
        kw = {"reference_wav_path": ref_path}
        if prompt_text:
            kw.update(prompt_wav_path=ref_path, prompt_text=prompt_text)
        t = time.perf_counter()
        gen = model.generate_streaming(text=text, cfg_value=cfg, inference_timesteps=steps, **kw)
        chunks, t_first = [], None
        for c in gen:
            if t_first is None:
                torch.cuda.synchronize()
                t_first = time.perf_counter() - t
            chunks.append(c)
        torch.cuda.synchronize()
        return np.concatenate(chunks), t_first, time.perf_counter() - t

    for _ in range(args.warmup):
        run_one("今天的天气不错。", refs["neutral"]["path"], None, 2.0, 10, 1)

    n = 0
    for item_id, it in man["items"].items():
        emo = it["emotion"]
        have = {c["key"] for c in it["cands"]}
        for vkey, vp in VARIANTS:
            if vp["src"] == "neutral_hint" and emo == "neutral":
                continue
            if vkey in have:
                continue
            if vp["src"] == "emo":
                ref = refs[emo]
                text = it["text"]
                hint = None
            else:
                ref = refs["neutral"]
                hint = HINTS[emo]
                text = hint + it["text"]
            prompt_text = ref["text"] if vp["mode"] == "ult" else None
            wav, t_first, total = run_one(text, ref["path"], prompt_text, vp["cfg_value"],
                                          vp["inference_timesteps"], SEED)
            fn = f"raw/vox_{item_id}_{vkey}.wav"
            sf.write(os.path.join(WORK, fn), wav, sr)
            dur = len(wav) / sr
            n += 1
            print(f"[{n}] {item_id} {vkey} dur={dur:.1f}s first={t_first:.2f}s rtf={total / dur:.2f}", flush=True)
            it["cands"].append({
                "key": vkey, "system": "voxcpm2", "raw": fn,
                "params": {"mode": vp["mode"], "ref_emotion": "neutral" if hint else emo, "style_hint": hint,
                           "cfg_value": vp["cfg_value"], "inference_timesteps": vp["inference_timesteps"],
                           "seed": SEED},
                "metrics": {"audio_s": round(dur, 3), "total_s": round(total, 3), "rtf": round(total / dur, 3),
                            "first_chunk_s": round(t_first, 3)}})
        save_man(man)


# ---------------------------------------------------------------- score
def norm_text(s, lang):
    s = re.sub(r"[\s\.\,\!\?\;\:、。，！？；：…「」『』\(\)\-~～\"'・]+", "", s).lower()
    if lang == "zh":
        from opencc import OpenCC
        s = OpenCC("s2t").convert(s)
    return s


def cmd_score(args):
    import librosa
    import numpy as np
    from resemblyzer import VoiceEncoder, preprocess_wav
    man = load_man()
    enc = VoiceEncoder("cuda")
    emb = lambda p: enc.embed_utterance(preprocess_wav(p))  # noqa: E731
    cos = lambda a, b: float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))  # noqa: E731
    r1 = emb(R1_PATH)
    for it in man["items"].values():
        for c in it["cands"]:
            c["metrics"]["sim_r1"] = round(cos(r1, emb(os.path.join(WORK, c["raw"]))), 4)
    del enc
    from faster_whisper import WhisperModel
    from rapidfuzz.distance import Levenshtein
    try:
        wm = WhisperModel("medium", device="cuda", compute_type="float16")
    except Exception as e:  # noqa: BLE001
        print("whisper cuda 失敗，改 cpu:", e)
        wm = WhisperModel("medium", device="cpu", compute_type="int8")
    for it in man["items"].values():
        lang = lang_key(it["lang"])
        for c in it["cands"]:
            y, _ = librosa.load(os.path.join(WORK, c["raw"]), sr=16000, mono=True)
            segs, _ = wm.transcribe(y, language=lang, beam_size=5)
            hyp = "".join(s.text for s in segs).strip()
            ref = norm_text(it["text"], lang)
            c["metrics"]["asr_text"] = hyp
            c["metrics"]["cer"] = round(Levenshtein.distance(ref, norm_text(hyp, lang)) / max(1, len(ref)), 4)
            if "audio_s" not in c["metrics"]:
                c["metrics"]["audio_s"] = round(len(y) / 16000, 3)
    save_man(man)


# ---------------------------------------------------------------- blind
def cmd_blind(args):
    from math import gcd

    import numpy as np
    import pyloudnorm as pyln
    import soundfile as sf
    from scipy.signal import resample_poly
    man = load_man()
    audio_dir = os.path.join(BLIND_DIR, "audio")
    if os.path.isdir(BLIND_DIR):
        shutil.rmtree(BLIND_DIR)
    os.makedirs(audio_dir)
    rng = random.Random(20261031)
    meter = pyln.Meter(TARGET_SR)
    pad = np.zeros(int(TARGET_SR * PAD_S))

    def trim_pad(y):
        """修掉頭尾靜音（-45 dBFS 以下），再補上統一長度的頭尾靜音。"""
        idx = np.where(np.abs(y) > 10 ** (-45 / 20))[0]
        if len(idx):
            y = y[max(0, idx[0] - int(0.01 * TARGET_SR)): idx[-1] + 1 + int(0.03 * TARGET_SR)]
        return np.concatenate([pad, y, pad])

    items_out, id_map = [], {}
    for n, (item_id, it) in enumerate(man["items"].items(), 1):
        new_item = f"i{n:02d}"
        order = list(range(len(it["cands"])))
        rng.shuffle(order)
        cands = []
        for k, oi in enumerate(order, 1):
            c = it["cands"][oi]
            y, sr = sf.read(os.path.join(WORK, c["raw"]), dtype="float64")
            if y.ndim > 1:
                y = y.mean(axis=1)
            if sr != TARGET_SR:
                g = gcd(sr, TARGET_SR)
                y = resample_poly(y, TARGET_SR // g, sr // g)
            y = trim_pad(y)
            lufs0 = meter.integrated_loudness(y)
            y = pyln.normalize.loudness(y, lufs0, TARGET_LUFS)
            peak = float(np.max(np.abs(y)))
            if peak > 0.98:  # 避免削波，僅在必要時等比降低
                y = y * (0.98 / peak)
            lufs1 = meter.integrated_loudness(y)
            cid = f"c{k}"
            rel = f"audio/{new_item}_{cid}.wav"
            sf.write(os.path.join(BLIND_DIR, rel), y, TARGET_SR, subtype="PCM_16")
            id_map[f"{new_item}/{cid}"] = {"item_id": item_id, "key": c["key"], "raw": c["raw"]}
            m = c["metrics"]
            cands.append({"cand_id": cid, "system": c["system"], "params": c["params"], "file": rel,
                          "metrics": {"sim_r1": m.get("sim_r1"), "cer": m.get("cer"), "rtf": m.get("rtf"),
                                      "first_chunk_s": m.get("first_chunk_s"),
                                      "audio_s": round(len(y) / TARGET_SR, 3), "lufs": round(lufs1, 2)}})
        items_out.append({"item_id": new_item, "text": it["text"], "lang": it["lang"], "emotion": it["emotion"],
                          "candidates": cands})
    # description 會經公開 API 顯示給評分者，不可含系統名稱
    desc = "同一角色聲音的多種合成版本比較，請依自然度、情緒表現與角色相似度評分。"
    with open(os.path.join(BLIND_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"set": SET_NAME, "created": datetime.now().isoformat(timespec="seconds"),
                   "description": desc, "items": items_out}, f, ensure_ascii=False, indent=2)
    # 解盲對照放在 set 目錄之外，避免被網站讀到或外流
    with open(os.path.join(WORK, "blind_key.json"), "w", encoding="utf-8") as f:
        json.dump(id_map, f, ensure_ascii=False, indent=2)
    print(f"set: {len(items_out)} items, {sum(len(i['candidates']) for i in items_out)} candidates")


# ---------------------------------------------------------------- report
def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 3) if xs else None


def cmd_report(args):
    man = load_man()
    rows = {}
    for it in man["items"].values():
        if it["lang"] == "zh" and it["emotion"] not in ("neutral", "happy", "sad"):
            grp = "zh_emo6"
        else:
            grp = it["lang"]
        for c in it["cands"]:
            rows.setdefault((c["key"], grp), []).append(c["metrics"])
    fmt = lambda k, g, ms: (f"| {k} | {g} | {len(ms)} | {mean([m.get('sim_r1') for m in ms])} | "  # noqa: E731
                            f"{mean([m.get('cer') for m in ms])} | {mean([m.get('rtf') for m in ms])} | "
                            f"{mean([m.get('first_chunk_s') for m in ms])} |")
    print("| variant | group | n | SIM | CER | RTF | first(s) |")
    for (k, g), ms in sorted(rows.items(), key=lambda x: (x[0][1], x[0][0])):
        print(fmt(k, g, ms))
    print("--- overall by variant ---")
    allv = {}
    for (k, g), ms in rows.items():
        allv.setdefault(k, []).extend(ms)
    for k, ms in allv.items():
        print(fmt(k, "all", ms))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["v25", "vox", "score", "blind", "report"])
    ap.add_argument("--warmup", type=int, default=3)
    a = ap.parse_args()
    {"v25": cmd_v25, "vox": cmd_vox, "score": cmd_score, "blind": cmd_blind, "report": cmd_report}[a.stage](a)
