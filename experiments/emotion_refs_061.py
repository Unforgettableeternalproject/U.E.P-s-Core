"""補 0.61 情緒參考音（user 0.5 對應 raw 0.61）。沿用 emotion_refs.py 的文本、seed（1000+take）與挑選規則。

階段：gen（IndexTTS 2.5 + LoRA 0.85）、asr（ES ASR）、select（讀 SIM + CER 挑片段）。
SIM 由 emotion_sim.py 在 .venv-voxcpm 算（輸入 refs061/sim_list.json，輸出 refs061/sim.json）。
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import emotion_common as C  # noqa: E402

STRENGTH = 0.61
OUT = os.path.join(C.OUT, "refs061")
CANDS = os.path.join(OUT, "candidates")
GEN_JSON = os.path.join(OUT, "gen.json")
ASR_JSON = os.path.join(OUT, "asr.json")
SIM_LIST = os.path.join(OUT, "sim_list.json")
SIM_JSON = os.path.join(OUT, "sim.json")
SEL_JSON = os.path.join(OUT, "selected.json")


def cmd_gen(a):
    sys.path.insert(0, C.ROOT)
    os.chdir(C.ROOT)
    import soundfile as sf
    import torch
    from echo_tts.config.emotion_presets import get_preset_overrides, get_preset_vector, load_presets, scale_to_strength
    from echo_tts.runtime.indextts_backend import IndexTTS2Backend
    from echo_tts.runtime.indextts_generate import default_generation_paths

    presets = load_presets()
    os.makedirs(CANDS, exist_ok=True)
    be = IndexTTS2Backend(paths=default_generation_paths(C.ROOT), device="cuda", use_fp16=True,
                          use_cuda_kernel=True, use_accel=False)
    be.load()
    be.load_lora()
    print("LoRA:", be.get_lora_status(), "cap", be.engine.emo_max_strength, be.engine.emo_bias_cap, flush=True)
    items = []
    for emo in C.EMOTIONS:
        for take in range(1, C.TAKES + 1):
            fn = f"{emo}_061_t{take}.wav"
            path = os.path.join(CANDS, fn)
            vec = scale_to_strength(list(get_preset_vector(presets[emo])), STRENGTH)
            be.emotion_vector = vec if sum(vec) > 0 else None
            torch.manual_seed(1000 + take)
            t0 = time.perf_counter()
            be.generate(C.CLIP_TEXTS[emo], path, language="zh", emotion_overrides=get_preset_overrides(presets[emo]))
            el = time.perf_counter() - t0
            info = sf.info(path)
            dur = round(info.frames / info.samplerate, 2)
            print(fn, "dur", dur, "sum", round(sum(vec), 4), "gen", round(el, 2), flush=True)
            items.append({"emotion": emo, "strength": STRENGTH, "take": take, "seed": 1000 + take, "file": f"candidates/{fn}",
                          "text": C.CLIP_TEXTS[emo], "vector": [round(v, 4) for v in vec], "vector_sum": round(sum(vec), 4),
                          "audio_s": dur, "gen_s": round(el, 2)})
    C.jdump(GEN_JSON, {"items": items})
    C.jdump(SIM_LIST, {i["file"]: os.path.join(OUT, i["file"]) for i in items})


def cmd_asr(a):
    import es_asr
    gen = C.jload(GEN_JSON)
    C.jdump(ASR_JSON, {i["file"]: es_asr.transcribe(os.path.join(OUT, i["file"])) for i in gen["items"]})


def cmd_select(a):
    gen, asr, sim = C.jload(GEN_JSON), C.jload(ASR_JSON), C.jload(SIM_JSON)
    sel = {}
    for it in gen["items"]:
        it["asr"] = asr[it["file"]]
        it["cer"] = round(C.cer(it["text"], it["asr"]), 4)
        it["sim_r1"] = sim[it["file"]]
        it["in_range"] = 8.0 <= it["audio_s"] <= 12.0
    for emo in C.EMOTIONS:
        cs = [i for i in gen["items"] if i["emotion"] == emo]
        best = min(cs, key=lambda i: (not i["in_range"], round(i["cer"] / 0.03), -i["sim_r1"]))
        sel[emo] = best
        print(emo, [(i["take"], i["audio_s"], i["cer"], i["sim_r1"]) for i in cs], "->", best["take"])
    C.jdump(SEL_JSON, {"items": gen["items"], "selected": sel})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["gen", "asr", "select"])
    a = ap.parse_args()
    {"gen": cmd_gen, "asr": cmd_asr, "select": cmd_select}[a.stage](a)
