"""情緒聆聽實驗步驟 2：VOX（VoxCPM2）情緒候選生成與延遲量測（共用環境，PYTHONUTF8=1，一次一個 GPU 行程）。

設定照 echo_tts/runtime/vox_backend.py 的 zh：LoRA step_0000600 開（scale 1.0 = alpha/r）、TaiwanPronunciationNormalizer、
normalize 不使用（直接走 tts_model._generate_with_prompt_cache）、cfg 2.0、steps 10、optimize=True、streaming、不重試。

版本：
  VOX-NEUTRAL  正式後端 zh：ref = prompt = zh_neutral_x1.2.wav（ref120）+ 逐字稿
  A-065        ULT-EMO：ref = prompt = 情緒片段（strength 0.65）+ 逐字稿
  B-057/065/073  COMBO：ref = ref120（鎖音色）、prompt = 情緒片段（該強度）+ 逐字稿
  B-065-L085   同 B-065，LoRA scaling 改 0.85（正式後端為 1.0）
延遲：t_first = 呼叫 stream 起算（含台灣讀音前處理）到第一個 chunk；total = 到 generator 結束；rtf = total / 音訊秒數。
"""
import argparse
import gc
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import emotion_common as C  # noqa: E402

VERSIONS = ["VOX-NEUTRAL", "A-065", "B-057", "B-065", "B-073", "B-065-L085"]
WARMUP = ("你好。", "週末我想去市場買點菜，順便繞去書店逛逛。",
          "今天早上我先把信箱整理了一遍，接著確認了下午會議要用的資料，等一下還要去超商領包裹，應該可以在天黑之前把事情都處理完。")


def set_lora_scale(model, scale):
    """直接改 LoRALinear 的 scaling buffer（不觸發 torch.compile 重編譯）；1.0 = alpha/r。"""
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

    clips = C.jload(C.REFS_JSON)["clips"]
    norm = TaiwanPronunciationNormalizer()
    t0 = time.perf_counter()
    cfg = LoRAConfig(enable_lm=True, enable_dit=True, enable_proj=False, r=32, alpha=32, dropout=0.0)
    model = VoxCPM.from_pretrained(C.MODEL_DIR, load_denoiser=False, optimize=True, local_files_only=True,
                                   device="cuda", lora_config=cfg)
    model.load_lora(C.LORA_DIR)
    model.set_lora_enabled(True)
    tts = model.tts_model
    sr = tts.sample_rate
    print("model ready", round(time.perf_counter() - t0, 1), "s sr", sr, flush=True)

    def build(version, emo):
        if version == "VOX-NEUTRAL":
            return tts.build_prompt_cache(prompt_text=C.NEUTRAL_TEXT, prompt_wav_path=C.NEUTRAL_REF,
                                          reference_wav_path=C.NEUTRAL_REF)
        key = "065" if version in ("A-065", "B-065-L085") else version.split("-")[1]
        clip = clips[emo][key]
        wav = os.path.join(C.REFS, clip["file"])
        if version == "A-065":
            return tts.build_prompt_cache(prompt_text=clip["text"], prompt_wav_path=wav, reference_wav_path=wav)
        return tts.build_prompt_cache(prompt_text=clip["text"], prompt_wav_path=wav, reference_wav_path=C.NEUTRAL_REF)

    def stream(text, cache, seed):
        torch.manual_seed(seed)
        t = time.perf_counter()
        body = norm.normalize(text)
        raw = tts._generate_with_prompt_cache(target_text=body, prompt_cache=cache, min_len=2, max_len=4096,
                                              inference_timesteps=10, cfg_value=2.0, retry_badcase=False,
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
        return np.concatenate(chunks), first, total

    # 暖機：中性、A、B 各跑短中長（含 LoRA 0.85），讓 compile 與 CUDA 快取穩定
    t0 = time.perf_counter()
    caches_w = {"VOX-NEUTRAL": build("VOX-NEUTRAL", None), "A-065": build("A-065", "happy"), "B-065": build("B-065", "sad")}
    for v, cache in caches_w.items():
        for w in WARMUP:
            stream(w, cache, 1)
    set_lora_scale(model, 0.85)
    for w in WARMUP:
        stream(w, caches_w["B-065"], 1)
    set_lora_scale(model, 1.0)
    print("warmup done", round(time.perf_counter() - t0, 1), "s", flush=True)

    os.makedirs(C.VOX_DIR, exist_ok=True)
    rows = []
    items = [it for it in C.sentence_items() if not a.emotions or it[1] in a.emotions.split(",")]
    for sid, emo, idx, text, seed in items:
        for v in VERSIONS:
            cache = build(v, emo)
            set_lora_scale(model, 0.85 if v.endswith("L085") else 1.0)
            audio, first, total = stream(text, cache, seed)
            fn = f"{sid}__{v}.wav"
            sf.write(os.path.join(C.VOX_DIR, fn), audio, sr)
            dur = len(audio) / sr
            rows.append({"sid": sid, "emotion": emo, "version": v, "file": f"vox/{fn}", "text": text, "seed": seed,
                         "audio_s": round(dur, 2), "first_s": round(first, 3), "total_s": round(total, 3),
                         "rtf": round(total / dur, 3),
                         "prompt_feat_len": int(cache["audio_feat"].size(0)),
                         "ref_feat_len": int(cache["ref_audio_feat"].size(0))})
            print(fn, "dur", rows[-1]["audio_s"], "first", rows[-1]["first_s"], "rtf", rows[-1]["rtf"], flush=True)
        set_lora_scale(model, 1.0)
        C.jdump(C.VOX_JSON, {"sr": sr, "rows": rows, "versions": VERSIONS})
    del model, tts
    gc.collect()
    torch.cuda.empty_cache()
    print("vox done", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--emotions", default=None, help="逗號分隔，僅供煙霧測試")
    main(ap.parse_args())
