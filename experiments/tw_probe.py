"""定位用探針：同一句在 base / LoRA、ULT / ref-only、多 seed、文字改寫下的 ES ASR 轉寫。
用法：python experiments/tw_probe.py s03   （變體定義在 VARIANTS）"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, soundfile as sf, torch
import tw_accent_check as T, tw_lora_eval as E, es_asr
from taiwan_pronunciation import TaiwanPronunciationNormalizer

S = dict((sid, t) for sid, t, _, _ in T.SENTENCES)
VARIANTS = {
    "s03": {
        "plain": S["s03"],
        "twn": TaiwanPronunciationNormalizer().normalize(S["s03"]),
        "no_pi": "這個貨的質量不錯，我們下星期再研究一次。",
        "only_head": "這批貨的質量不錯。",
    },
    "s08b": {
        "A_yi4_guo_tang": "{zhou1}末我想去市場買點菜，順便繞去書店逛逛，晚上再回家煮{yi4}{guo1}{re4}{tang1}給大家喝。",
        "B_yi_guo_tang": "{zhou1}末我想去市場買點菜，順便繞去書店逛逛，晚上再回家煮一{guo1}{re4}{tang1}給大家喝。",
        "C_yi4_only": "{zhou1}末我想去市場買點菜，順便繞去書店逛逛，晚上再回家煮{yi4}鍋熱湯給大家喝。",
        "D_guo_only": "{zhou1}末我想去市場買點菜，順便繞去書店逛逛，晚上再回家煮一{guo1}熱湯給大家喝。",
        "E_tang_only": "{zhou1}末我想去市場買點菜，順便繞去書店逛逛，晚上再回家煮一鍋{re4}{tang1}給大家喝。",
    },
    "s08": {
        "orig": S["s08"],
        "fixed": TaiwanPronunciationNormalizer().normalize(S["s08"]),
        "fixed_guo": TaiwanPronunciationNormalizer().normalize(S["s08"]).replace("一鍋", "一{guo1}鍋"),
        "fixed_tang": TaiwanPronunciationNormalizer().normalize(S["s08"]).replace("熱湯", "{re4}{tang1}"),
    },
}

MODES = ("ult", "ref")

def main(sid):
    global MODES
    if sid.startswith("s08"):
        MODES = ("ult",)
    model = E.load_model(); sr = model.tts_model.sample_rate
    n_path, n_text = T.neutral_ref()
    res = {}
    for tag in ("base", E.DEFAULT_LORA_TAG):
        E.set_tag(model, tag)
        E.synth(model, "你好。", 1, n_path, n_text)
        for name, text in VARIANTS[sid].items():
            for mode in MODES:
                for seed in (1033, 11, 22):
                    torch.manual_seed(seed)
                    kw = dict(reference_wav_path=n_path if mode == "ult" else T.R1_16K)
                    if mode == "ult":
                        kw.update(prompt_wav_path=n_path, prompt_text=n_text)
                    w = np.concatenate(list(model.generate_streaming(text=text, cfg_value=2.0, inference_timesteps=10, normalize=False, **kw)))
                    p = f"outputs/tw_lora_eval/probe_{sid}_{tag}_{name}_{mode}_{seed}.wav"; sf.write(p, w, sr)
                    k = f"{tag}|{name}|{mode}|{seed}"; res[k] = es_asr.transcribe(p)[:45]
                    print(k, res[k], flush=True)
    json.dump({"texts": VARIANTS[sid], "res": res}, open(f"outputs/tw_lora_eval/probe_{sid}.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
if __name__ == "__main__":
    main(sys.argv[1])
