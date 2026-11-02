"""煙霧測試：optimize=True 下 LoRA 載入、set_lora_enabled 切換與 streaming 首包延遲是否正常（.venv-voxcpm）。"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    import numpy as np
    import torch

    import tw_accent_check as T
    import tw_lora_eval as E
    n_path, n_text = T.neutral_ref()
    t0 = time.perf_counter()
    model = E.load_model(optimize=True)
    print("load+warm", round(time.perf_counter() - t0, 1), flush=True)
    E.set_tag(model, E.DEFAULT_LORA_TAG)
    sr = model.tts_model.sample_rate
    texts = {"zh": "週末我想去市場買點菜，順便繞去書店逛逛，晚上再回家煮一鍋熱湯給大家喝。",
             "zh_s": "步驟很簡單。", "en": "The weather is nice today. I finished organizing everything on my desk. What should we do next?",
             "en_s": "Hello there."}
    log = []

    def run(label, text, lora):
        model.set_lora_enabled(lora)
        torch.manual_seed(7)
        torch.cuda.synchronize()
        t = time.perf_counter()
        first, chunks = None, []
        for c in model.generate_streaming(text=text, cfg_value=2.0, inference_timesteps=10, normalize=False,
                                          reference_wav_path=n_path, prompt_wav_path=n_path, prompt_text=n_text):
            if first is None:
                torch.cuda.synchronize()
                first = time.perf_counter() - t
            chunks.append(c)
        torch.cuda.synchronize()
        el = time.perf_counter() - t
        w = np.concatenate(chunks)
        d = len(w) / sr
        r = {"label": label, "lora": lora, "gen_s": round(el, 2), "first_s": round(first, 3), "audio_s": round(d, 2), "rtf": round(el / d, 2),
             "finite": bool(np.isfinite(w).all()), "peak": round(float(np.abs(w).max()), 3)}
        log.append(r)
        print(r, flush=True)

    for rep in range(2):
        for lora, keys in ((True, ("zh", "zh_s", "zh")), (False, ("en", "en_s", "en")), (True, ("zh", "zh_s", "zh")), (False, ("en", "en_s", "en"))):
            for k in keys:
                run(f"{rep}-{k}", texts[k], lora)
    json.dump(log, open(os.path.join(E.ROOT, "outputs", "foreign_v3", "compile_smoke.json"), "w"), indent=1)
    del model
    torch.cuda.empty_cache()


if __name__ == "__main__":
    main()
