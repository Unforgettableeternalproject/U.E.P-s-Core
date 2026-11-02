"""IndexTTS 2.5（uep_v25 角色 + LoRA gpt_best）產生各情緒中文參考音。

階段（分兩個環境執行）：
  gen     用現行環境（U.E.P-s-Core/env）生成 9 種情緒 x 3 次，寫 candidates 與 gen.json
  select  用 .venv-voxcpm（有 resemblyzer）對 R1 算 SIM，挑最高者入選，寫 emotion_refs.json

  <core python> experiments/v25_emotion_refs.py gen
  .venv-voxcpm/Scripts/python.exe experiments/v25_emotion_refs.py select [--copy-to DIR]

情緒向量依 emotion_presets.yaml 的配方，以 scale_to_user_strength(vec, 0.5) 縮放到總和 0.65（定案甜蜜點）。
生成路徑同 Entry.py：IndexTTS2Backend + load_lora（預設 scale 0.85）+ uep_v25 角色。
"""
import argparse
import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "outputs", "v25_emotion_refs")
CANDS = os.path.join(OUT, "candidates")
GEN_JSON = os.path.join(OUT, "gen.json")
FINAL_JSON = os.path.join(OUT, "emotion_refs.json")
R1_PATH = os.path.join(ROOT, "echo_tts", "assets", "reference", "uep_reference_v25.wav")
TAKES = 3

# 名稱以 emotion_presets.yaml 為準：neutral + 八維各自的基礎 preset
EMOTIONS = ["neutral", "happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm"]

# 每種情緒一段約 10-15 秒的繁中台灣用語文本（自擬）
TEXTS = {
    "neutral": "今天早上我先把信箱整理了一遍，接著確認了下午會議要用的資料，等一下還要去超商領包裹，應該可以在天黑之前把事情都處理完。",
    "happy": "太好了，我們終於成功了！這個月的努力總算有回報，我真的超開心的，今天晚上一定要找大家一起去吃頓好的慶祝一下！",
    "angry": "你到底有沒有在聽我說話？這已經是第三次了，我明明交代過很多遍，你還是照自己的想法亂來，真的太過分了啦！",
    "sad": "我一直想著那天晚上的事，心裡就好難過。早知道我就多陪陪她了，現在什麼都來不及了，我真的好想她。",
    "afraid": "你有聽到嗎？樓上好像有人在走動……可是這間房子明明只有我一個人。我不敢回頭，手一直在發抖，怎麼辦啊？",
    "disgusted": "噁，這是什麼味道啊？冰箱裡的剩菜居然放到發霉了，上面還長了一堆奇怪的東西，我看了都快吐了，真的受不了。",
    "melancholic": "雨一直下，窗外的路燈都變得模糊了。翻著以前的照片，才發現日子過得好快，那些人和那些事，都回不來了呢。",
    "surprised": "咦？等一下，你說什麼？真的假的，他居然就這樣突然出現在門口！我完全沒有想到，嚇了我一大跳耶！",
    "calm": "沒關係，慢慢來就好。先深呼吸一下，把手邊的事情一件一件做完，不用著急，時間還很充裕，我們一起處理。",
}


def cmd_gen(args):
    sys.path.insert(0, ROOT)
    import soundfile as sf
    import torch
    from echo_tts.config.emotion_presets import (
        EMOTION_LABELS, get_preset_overrides, get_preset_vector, load_presets,
        scale_to_user_strength, user_to_strength,
    )
    from echo_tts.runtime.indextts_backend import IndexTTS2Backend
    from echo_tts.runtime.indextts_generate import default_generation_paths

    presets = load_presets()
    os.makedirs(CANDS, exist_ok=True)
    backend = IndexTTS2Backend(paths=default_generation_paths(ROOT), device="cuda", use_fp16=True,
                               use_cuda_kernel=True, use_accel=False)
    backend.load()
    backend.load_lora()  # 預設 gpt_best，scale 0.85（同 Entry.py）
    lora = backend.get_lora_status()
    print("LoRA:", lora, flush=True)

    items = []
    for emo in EMOTIONS:
        entry = presets[emo]
        vec = scale_to_user_strength(list(get_preset_vector(entry)), 0.5)
        overrides = get_preset_overrides(entry)
        backend.emotion_vector = vec if sum(vec) > 0 else None
        for take in range(1, args.takes + 1):
            seed = 1000 + take
            torch.manual_seed(seed)
            fname = f"{emo}_t{take}.wav"
            path = os.path.join(CANDS, fname)
            t0 = time.perf_counter()
            backend.generate(TEXTS[emo], path, language="zh", emotion_overrides=overrides)
            el = time.perf_counter() - t0
            info = sf.info(path)
            dur = info.frames / info.samplerate
            print(f"{fname} dur={dur:.1f}s gen={el:.1f}s", flush=True)
            items.append({"emotion": emo, "take": take, "seed": seed, "file": f"candidates/{fname}",
                          "text": TEXTS[emo], "vector": [round(v, 4) for v in vec],
                          "vector_labels": EMOTION_LABELS, "vector_sum": round(sum(vec), 4),
                          "strength": round(user_to_strength(0.5), 3), "overrides": overrides,
                          "audio_s": round(dur, 2), "gen_s": round(el, 2)})
    with open(GEN_JSON, "w", encoding="utf-8") as f:
        json.dump({"lora": {k: (v if isinstance(v, (int, float, str, bool, type(None))) else str(v))
                            for k, v in lora.items()},
                   "character": "echo_tts/models/tts/uep_v25.pt", "language": "zh", "items": items},
                  f, ensure_ascii=False, indent=2)


def cmd_select(args):
    import numpy as np
    from resemblyzer import VoiceEncoder, preprocess_wav
    enc = VoiceEncoder("cuda")
    emb = lambda p: enc.embed_utterance(preprocess_wav(p))  # noqa: E731
    r1 = emb(R1_PATH)
    cos = lambda a, b: float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))  # noqa: E731
    gen = json.load(open(GEN_JSON, encoding="utf-8"))
    best = {}
    for it in gen["items"]:
        it["sim_r1"] = round(cos(r1, emb(os.path.join(OUT, it["file"]))), 4)
        if it["emotion"] not in best or it["sim_r1"] > best[it["emotion"]]["sim_r1"]:
            best[it["emotion"]] = it
    for it in gen["items"]:
        it["selected"] = best[it["emotion"]] is it
        if it["selected"]:
            shutil.copy2(os.path.join(OUT, it["file"]), os.path.join(OUT, f"{it['emotion']}.wav"))
            it["selected_file"] = f"{it['emotion']}.wav"
    gen["r1"] = R1_PATH
    gen["sim_method"] = "resemblyzer cosine vs uep_reference_v25.wav (R1)"
    with open(FINAL_JSON, "w", encoding="utf-8") as f:
        json.dump(gen, f, ensure_ascii=False, indent=2)
    print("| emotion | selected | SIM | all SIM | dur |")
    for emo in EMOTIONS:
        s = best[emo]
        allsim = [i["sim_r1"] for i in gen["items"] if i["emotion"] == emo]
        print(f"| {emo} | t{s['take']} | {s['sim_r1']} | {allsim} | {s['audio_s']}s |")
    if args.copy_to:
        os.makedirs(args.copy_to, exist_ok=True)
        for emo in EMOTIONS:
            shutil.copy2(os.path.join(OUT, f"{emo}.wav"), os.path.join(args.copy_to, f"{emo}.wav"))
        shutil.copytree(CANDS, os.path.join(args.copy_to, "candidates"), dirs_exist_ok=True)
        shutil.copy2(FINAL_JSON, os.path.join(args.copy_to, "emotion_refs.json"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["gen", "select"])
    ap.add_argument("--takes", type=int, default=TAKES)
    ap.add_argument("--copy-to", default=None)
    a = ap.parse_args()
    {"gen": cmd_gen, "select": cmd_select}[a.stage](a)
