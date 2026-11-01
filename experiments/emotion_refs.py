"""情緒聆聽實驗步驟 1：IndexTTS 2.5 情緒參考音（strength 0.57 / 0.65 / 0.73）與 V25 對照句。

階段（共用環境 C:/Users/Bernie/.uep/venv-inference-312，PYTHONUTF8=1，一次一個 GPU 行程）：
  gen     IndexTTS 2.5 + LoRA 0.85 + uep_v25：每情緒 x 3 強度 x 2 take 的情緒片段，另生成 V25 對照句（strength 0.65）
  asr     ES ASR 轉寫所有候選（算 CER）
  select  讀 SIM（emotion_sim.py 產生，.venv-voxcpm 的 resemblyzer）+ CER，每個 (情緒, 強度) 挑一個，寫 emotion_clips.json
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import emotion_common as C  # noqa: E402

ASR_JSON = os.path.join(C.REFS, "asr.json")
SIM_JSON = os.path.join(C.REFS, "sim.json")


def cmd_gen(a):
    sys.path.insert(0, C.ROOT)
    os.chdir(C.ROOT)
    import soundfile as sf
    import torch
    from echo_tts.config.emotion_presets import (
        EMOTION_LABELS, get_preset_overrides, get_preset_vector, load_presets, scale_to_strength,
    )
    from echo_tts.runtime.indextts_backend import IndexTTS2Backend
    from echo_tts.runtime.indextts_generate import default_generation_paths

    presets = load_presets()
    os.makedirs(C.CANDS, exist_ok=True)
    os.makedirs(C.V25_DIR, exist_ok=True)
    be = IndexTTS2Backend(paths=default_generation_paths(C.ROOT), device="cuda", use_fp16=True,
                          use_cuda_kernel=True, use_accel=False)
    be.load()
    be.load_lora()  # gpt_best，scale 0.85
    lora = be.get_lora_status()
    print("LoRA:", lora, flush=True)
    # 引擎預設把情緒向量總和壓在 config emotion.max_strength（0.65）與 emo_bias_cap（0.65），
    # 0.73 的向量會被等比壓回 0.65（輸出與 0.65 逐位元相同）。為了讓 raw 0.73 真的送進模型，放寬到 0.73。
    be.engine.emo_max_strength = 0.73
    be.engine.emo_bias_cap = 0.73
    items, v25 = [], []
    t_all = time.perf_counter()

    def run(emo, strength, text, path, seed):
        vec = scale_to_strength(list(get_preset_vector(presets[emo])), strength)
        be.emotion_vector = vec if sum(vec) > 0 else None
        torch.manual_seed(seed)
        t0 = time.perf_counter()
        be.generate(text, path, language="zh", emotion_overrides=get_preset_overrides(presets[emo]))
        el = time.perf_counter() - t0
        info = sf.info(path)
        return vec, round(info.frames / info.samplerate, 2), round(el, 2)

    emos = a.emotions.split(",") if a.emotions else C.EMOTIONS
    keys = a.strengths.split(",") if a.strengths else list(C.STRENGTHS)
    for emo in emos:
        for key, st in C.STRENGTHS.items():
            if key not in keys:
                continue
            for take in range(1, C.TAKES + 1):
                fn = f"{emo}_{key}_t{take}.wav"
                path = os.path.join(C.CANDS, fn)
                vec, dur, el = run(emo, st, C.CLIP_TEXTS[emo], path, 1000 + take)
                print(fn, "dur", dur, "gen", el, flush=True)
                items.append({"emotion": emo, "strength_key": key, "strength": st, "take": take, "seed": 1000 + take,
                              "file": f"candidates/{fn}", "text": C.CLIP_TEXTS[emo], "vector": [round(v, 4) for v in vec],
                              "vector_sum": round(sum(vec), 4), "audio_s": dur, "gen_s": el})
    if not a.skip_v25:
        for sid, emo, idx, text, seed in C.sentence_items():
            if emo not in emos:
                continue
            path = os.path.join(C.V25_DIR, f"{sid}__V25.wav")
            vec, dur, el = run(emo, 0.65, text, path, seed)
            print(sid, "V25 dur", dur, "gen", el, flush=True)
            v25.append({"sid": sid, "emotion": emo, "text": text, "seed": seed, "file": f"v25/{sid}__V25.wav",
                        "vector": [round(v, 4) for v in vec], "audio_s": dur, "gen_s": el})
    if a.strengths:  # 只重生部分強度：併回既有 gen.json
        old = C.jload(C.GEN_JSON)
        items = [i for i in old["items"] if i["strength_key"] not in keys] + items
        v25 = old["v25"]
    C.jdump(C.GEN_JSON, {"lora": {k: (v if isinstance(v, (int, float, str, bool, type(None))) else str(v))
                                  for k, v in lora.items()},
                         "labels": EMOTION_LABELS, "items": items, "v25": v25,
                         "total_gen_wall_s": round(time.perf_counter() - t_all, 1)})
    print("gen done", flush=True)


def cmd_asr(a):
    import es_asr
    gen = C.jload(C.GEN_JSON)
    out = {}
    for it in gen["items"]:
        out[it["file"]] = es_asr.transcribe(os.path.join(C.REFS, it["file"]))
    for it in gen["v25"]:
        out[it["file"]] = es_asr.transcribe(os.path.join(C.OUT, it["file"]))
    C.jdump(ASR_JSON, out)
    print("asr done", len(out), flush=True)


def cmd_select(a):
    gen = C.jload(C.GEN_JSON)
    asr = C.jload(ASR_JSON)
    sim = C.jload(SIM_JSON)
    clips = {}
    rows = []
    for it in gen["items"]:
        it["asr"] = asr[it["file"]]
        it["cer"] = round(C.cer(it["text"], it["asr"]), 4)
        it["sim_r1"] = sim[it["file"]]
        it["in_range"] = 8.0 <= it["audio_s"] <= 12.0
    # 挑選規則：長度在 8-12 秒內優先，再比 CER（差 0.03 以內視為同級），同級取 SIM 最高
    for emo in C.EMOTIONS:
        for key in C.STRENGTHS:
            cs = [i for i in gen["items"] if i["emotion"] == emo and i["strength_key"] == key]
            best = min(cs, key=lambda i: (not i["in_range"], round(i["cer"] / 0.03), -i["sim_r1"]))
            for i in cs:
                i["selected"] = i is best
            clips.setdefault(emo, {})[key] = {"file": best["file"], "take": best["take"], "text": best["text"],
                                              "asr": best["asr"], "cer": best["cer"], "sim_r1": best["sim_r1"],
                                              "audio_s": best["audio_s"], "vector": best["vector"],
                                              "vector_sum": best["vector_sum"], "strength": best["strength"],
                                              "in_range": best["in_range"]}
            rows.append((emo, key, best["take"], best["audio_s"], best["cer"], best["sim_r1"],
                         [(i["take"], i["audio_s"], i["cer"], i["sim_r1"]) for i in cs]))
    C.jdump(C.REFS_JSON, {"lora": gen["lora"], "r1": C.R1_PATH, "sim_method": "resemblyzer cosine vs R1",
                          "rule": "8-12 秒內優先 > CER（差 0.03 內視同）> SIM 最高", "clips": clips, "items": gen["items"],
                          "v25": gen["v25"]})
    for r in rows:
        print(r)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["gen", "asr", "select"])
    ap.add_argument("--emotions", default=None)
    ap.add_argument("--skip-v25", action="store_true")
    ap.add_argument("--strengths", default=None, help="只重生指定強度，如 073（併回既有 gen.json）")
    a = ap.parse_args()
    {"gen": cmd_gen, "asr": cmd_asr, "select": cmd_select}[a.stage](a)
