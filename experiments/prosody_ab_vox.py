"""語調 A/B 步驟 1：VOX 全部合成（一個 GPU 行程，共用環境，PYTHONUTF8=1）。

設定同正式後端 zh：LoRA step_0000600（scale 1.0）、台灣讀音前處理、cfg 2.0、steps 10、optimize=True、streaming、不重試；
基準 BASE = ULT（ref = prompt = zh_neutral_x1.2.wav ref120 + 逐字稿）、整段一次合成。

因素：
  split   BASE（整段）vs SPLIT-OLD（舊切法：首段 <= 10 字、其餘約 20 字，逐段獨立合成後串接）
  ref     REF-happy057 / REF-surprised073 / REF-disgusted073（ULT，prompt = reference = 該片段 x1.2 + 逐字稿）
  cfg     CFG25 / CFG30
  lora    LORA060 / LORA085
  cont    連續 6 句：(i) 每句獨立、(ii) COMBO（ref120 鎖音色 + 上一句尾 2-4 秒音訊與對應文字）、(iii) COMBO（上一句完整音訊與文字）
  brk     s2 / s4 含括號 vs 移除括號，各 3 個 seed
生出的 raw 檔放 outputs/prosody_ab/raw/<因素>/；量測存 outputs/prosody_ab/vox_gen.json。
同一時間只載入一個 TTS 模型；接續實驗要的「上一句尾段對應文字」用 CPU 上的 ES ASR（Breeze int8，word timestamps）對齊。
"""
import argparse
import difflib
import gc
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import emotion_common as C  # noqa: E402
import prosody_ab_lib as L  # noqa: E402

REF_DIR = os.path.join(L.OUT, "refs")
REFS = {  # 名稱 -> (x1.2 wav, 逐字稿)
    "surprised073": ("surprised073_x1.2.wav", C.CLIP_TEXTS["surprised"]),
    "disgusted073": ("disgusted073_x1.2.wav", C.CLIP_TEXTS["disgusted"]),
}
WARM = ("你好。", "週末我想去市場買點菜，順便繞去書店逛逛。",
        "今天早上我先把信箱整理了一遍，接著確認了下午會議要用的資料，等一下還要去超商領包裹，應該可以在天黑之前把事情都處理完。")


def set_lora_scale(model, scale):
    from voxcpm.modules.layers.lora import LoRALinear
    n = 0
    for m in model.tts_model.modules():
        if isinstance(m, LoRALinear):
            m.scaling.fill_(m._base_scaling * scale)
            n += 1
    return n


def main(a):
    import numpy as np
    import soundfile as sf
    import torch
    from taiwan_pronunciation import TaiwanPronunciationNormalizer
    from voxcpm import VoxCPM
    from voxcpm.model.voxcpm2 import LoRAConfig

    norm = TaiwanPronunciationNormalizer()
    t0 = time.perf_counter()
    cfg = LoRAConfig(enable_lm=True, enable_dit=True, enable_proj=False, r=32, alpha=32, dropout=0.0)
    model = VoxCPM.from_pretrained(C.MODEL_DIR, load_denoiser=False, optimize=True, local_files_only=True,
                                   device="cuda", lora_config=cfg)
    model.load_lora(C.LORA_DIR)
    model.set_lora_enabled(True)
    tts = model.tts_model
    assert tts.sample_rate == L.SR
    print("model ready", round(time.perf_counter() - t0, 1), "s", flush=True)

    def build_ult(wav, text):
        return tts.build_prompt_cache(prompt_text=text, prompt_wav_path=wav, reference_wav_path=wav)

    def gen(text, cache, seed, cfg_value=2.0):
        torch.manual_seed(seed)
        t = time.perf_counter()
        body = norm.normalize(L.es_sanitize(text))  # ES adapter 先 sanitize_for_tts 再進 backend（保留「」『』）
        raw = tts._generate_with_prompt_cache(target_text=body, prompt_cache=cache, min_len=2, max_len=4096,
                                              inference_timesteps=10, cfg_value=cfg_value, retry_badcase=False,
                                              streaming=True)
        chunks, first = [], None
        try:
            for wav, _, _ in raw:
                c = wav.squeeze(0).cpu().numpy().astype(np.float32).reshape(-1)
                if c.size:
                    if first is None:
                        first = time.perf_counter() - t
                    chunks.append(c)
        finally:
            raw.close()
        total = time.perf_counter() - t
        y = np.concatenate(chunks)
        return y, first, total, [len(c) for c in chunks]

    rows = []

    def save(folder, name, y, text, seed, first, total, chunk_lens, **extra):
        d = os.path.join(L.RAW, folder)
        os.makedirs(d, exist_ok=True)
        fn = f"{name}.wav"
        sf.write(os.path.join(d, fn), y, L.SR, subtype="FLOAT")
        dur = len(y) / L.SR
        r = {"folder": folder, "name": name, "file": f"{folder}/{fn}", "text": text, "seed": seed, "audio_s": round(dur, 2),
             "first_s": round(first, 3), "total_s": round(total, 3), "rtf": round(total / dur, 3),
             "chunk_lens": chunk_lens, "n_chunks": len(chunk_lens)}
        r.update(extra)
        rows.append(r)
        print(name, "dur", r["audio_s"], "first", r["first_s"], "rtf", r["rtf"], flush=True)
        return r

    def dump():
        C.jdump(os.path.join(L.OUT, "vox_gen.json"), {"rows": rows, "sr": L.SR})

    # ---------------------------------------------------------------- 快取與暖機
    base = build_ult(C.NEUTRAL_REF, C.NEUTRAL_TEXT)
    ref_caches = {k: build_ult(os.path.join(REF_DIR, w), t) for k, (w, t) in REFS.items()}
    t0 = time.perf_counter()
    for w in WARM:
        gen(w, base, 1)
    for k, c in ref_caches.items():
        for w in WARM[1:]:
            gen(w, c, 1)
    print("warmup done", round(time.perf_counter() - t0, 1), "s", flush=True)

    # ---------------------------------------------------------------- BASE、SPLIT-OLD、REF、CFG、LORA
    for sid, text in L.SENTS:
        seed = L.seed_of(sid)
        y, f, t, cl = gen(text, base, seed)
        save("base", f"{sid}__BASE", y, text, seed, f, t, cl)
        if sid not in L.KEEP:
            continue
        segs = L.old_split(text)
        parts, tot, first = [], 0.0, None
        for k, sg in enumerate(segs):
            y2, f2, t2, cl2 = gen(sg, base, seed + 10 * (k + 1))
            first = f2 if first is None else first
            tot += t2
            parts.append(y2)
            os.makedirs(os.path.join(L.RAW, "split"), exist_ok=True)
            sf.write(os.path.join(L.RAW, "split", f"{sid}__SPLIT-OLD_seg{k + 1}.wav"), y2, L.SR, subtype="FLOAT")
        dur = sum(len(p) for p in parts) / L.SR
        rows.append({"folder": "split", "name": f"{sid}__SPLIT-OLD", "file": None, "text": text, "seed": seed, "segments": segs,
                     "seg_files": [f"split/{sid}__SPLIT-OLD_seg{k + 1}.wav" for k in range(len(segs))],
                     "seg_audio_s": [round(len(p) / L.SR, 2) for p in parts], "audio_s": round(dur, 2), "first_s": round(first, 3),
                     "total_s": round(tot, 3), "rtf": round(tot / dur, 3)})
        print(sid, "SPLIT-OLD", segs, "dur", round(dur, 2), "first", round(first, 3), "rtf", round(tot / dur, 3), flush=True)
    dump()

    # ---------------------------------------------------------------- tail：串流 vs 非串流（句尾是否被截）
    for sid, text in L.SENTS:
        seed = L.seed_of(sid)
        body = norm.normalize(L.es_sanitize(text))
        y2, f2, t2, cl2 = gen(text, base, seed)  # 串流再跑一次（估 run-to-run 差異）
        save("tail", f"{sid}__STREAM2", y2, text, seed, f2, t2, cl2)
        torch.manual_seed(seed)
        t = time.perf_counter()
        out = tts.generate_with_prompt_cache(target_text=body, prompt_cache=base, min_len=2, max_len=4096,
                                             inference_timesteps=10, cfg_value=2.0, retry_badcase=False)
        wav = out[0].reshape(-1).cpu().numpy().astype(np.float32)
        tt = time.perf_counter() - t
        save("tail", f"{sid}__NONSTREAM", wav, text, seed, tt, tt, [len(wav)])
    dump()
    for sid, text in L.SENTS:
        if sid not in L.KEEP:
            continue
        seed = L.seed_of(sid)
        for k, c in ref_caches.items():
            y, f, t, cl = gen(text, c, seed)
            save("ref", f"{sid}__REF-{k}", y, text, seed, f, t, cl, ref=k)
    dump()
    # ---------------------------------------------------------------- 括號
    for sid in L.BRACKET_SIDS:
        text = dict(L.SENTS)[sid]
        for off in L.BRACKET_SEEDS:
            seed = L.seed_of(sid) + off
            for tag, tx in (("with", text), ("strip", L.strip_brackets(text))):
                y, f, t, cl = gen(tx, base, seed)
                save("brk", f"{sid}__BRK-{tag}-seed{off}", y, tx, seed, f, t, cl, tag=tag, seed_off=off)
    dump()

    del model, tts
    gc.collect()
    torch.cuda.empty_cache()
    print("vox done", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    main(ap.parse_args())
