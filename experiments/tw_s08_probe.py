"""s08 開頭「週末」被讀成「休默/休末」的定位實驗（base 模型，ULT/ref-only，多 seed、改寫 variants）。"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tw_accent_check as T, tw_lora_eval as E, es_asr
import soundfile as sf

BODY = "我想去市場買點菜，順便繞去書店逛逛，晚上再回家煮一鍋熱湯給大家喝。"
VARS = {
    "orig": "週末" + BODY,
    "zhou_char": "周末" + BODY,
    "pinyin": "{zhou1}{mo4}" + BODY,
    "pinyin_zhou_only": "{zhou1}末" + BODY,
    "pre_word": "這個週末" + BODY,
    "pinyin_guo": "週末" + BODY.replace("一鍋", "一{guo1}"),
}
def main():
    model = E.load_model(); sr = model.tts_model.sample_rate
    E.set_tag(model, "base")
    n_path, n_text = T.neutral_ref()
    E.synth(model, "你好。", 1, n_path, n_text)
    import torch, numpy as np
    res = {}
    for name, text in VARS.items():
        for mode in ("ult", "ref"):
            for seed in (1038, 11, 22):
                torch.manual_seed(seed)
                kw = dict(reference_wav_path=n_path if mode == "ult" else T.R1_16K)
                if mode == "ult":
                    kw.update(prompt_wav_path=n_path, prompt_text=n_text)
                w = np.concatenate(list(model.generate_streaming(text=text, cfg_value=2.0, inference_timesteps=10, normalize=False, **kw)))
                p = f"outputs/tw_lora_eval/s08probe_{name}_{mode}_{seed}.wav"; sf.write(p, w, sr)
                res[f"{name}|{mode}|{seed}"] = es_asr.transcribe(p)[:14]
                print(name, mode, seed, res[f"{name}|{mode}|{seed}"], flush=True)
    json.dump(res, open("outputs/tw_lora_eval/s08_probe.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
main()
