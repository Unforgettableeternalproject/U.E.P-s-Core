"""外語 v3 的 V25（IndexTTS 2.5）部分，core env 執行：
  cand   用現行路徑（IndexTTS2Backend、uep_v25 角色向量 = R1 衍生、LoRA 不載入、無情緒向量）合成 prompt 候選
  test   合成 6 en + 6 ja 對照句（中性）；en2/en3/ja2/ja3 另做帶情緒向量版（與 v2 的 V25 同向量）
輸出 outputs/foreign_v3/{cand,raw}/V25_*.wav 與 v25_meta.json。"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tw_foreign_v3_lib as L  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["cand", "test"])
    a = ap.parse_args()
    sys.path.insert(0, L.ROOT)
    import soundfile as sf
    import torch
    from echo_tts.runtime.indextts_backend import IndexTTS2Backend
    from echo_tts.runtime.indextts_generate import default_generation_paths
    backend = IndexTTS2Backend(paths=default_generation_paths(L.ROOT), device="cuda", use_fp16=True,
                               use_cuda_kernel=True, use_accel=False)
    backend.load()
    backend.emotion_vector = None
    meta_p = os.path.join(L.W, "v25_meta.json")
    meta = json.load(open(meta_p, encoding="utf-8")) if os.path.exists(meta_p) else {}

    def gen(out, text, lang, seed, vec=None):
        backend.emotion_vector = vec
        torch.manual_seed(seed)
        t = time.perf_counter()
        backend.generate(text, out, language=lang)
        el = time.perf_counter() - t
        info = sf.info(out)
        d = info.frames / info.samplerate
        meta[os.path.basename(out)[:-4]] = {"text": text, "emo": vec, "seed": seed, "gen_s": round(el, 2), "audio_s": round(d, 2), "rtf": round(el / d, 2)}
        print(os.path.basename(out), meta[os.path.basename(out)[:-4]]["rtf"], flush=True)

    gen(os.path.join(L.W, "_warm.wav"), "こんにちは。", "ja", 1)
    if a.stage == "cand":
        os.makedirs(L.CAND, exist_ok=True)
        for lang, texts in L.CAND_TEXT.items():
            for i, t in enumerate(texts):
                for s in (0, 1):
                    gen(os.path.join(L.CAND, f"V25_{lang}{i + 1}s{s}.wav"), t, lang, L.SEED + 300 + i * 10 + s)
    else:
        os.makedirs(L.RAW, exist_ok=True)
        for i, (k, t) in enumerate(L.SENT.items()):
            seed = L.SEED + 70 + i
            gen(os.path.join(L.RAW, f"{k}__V25.wav"), t, L.LANG[k], seed)
            if k in L.EMO:
                gen(os.path.join(L.RAW, f"{k}__V25-EMO.wav"), t, L.LANG[k], seed, L.EMO[k])
    json.dump(meta, open(meta_p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    backend.emotion_vector = None


if __name__ == "__main__":
    main()
