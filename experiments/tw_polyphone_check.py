"""破音詞逐詞聽測：修正前（commit 5dd1b1a 的字典）vs 修正後（目前字典），TWLORA（step_0000300），ULT。
輸出 E:/Materials/Voices/UEP/2026-10-31_polyphone_check/pNN/pNN__TWLORA-TWN-{before,after}.wav + README。"""
import json, os, subprocess, sys, types
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, soundfile as sf
import tw_accent_check as T, tw_lora_eval as E, tw_pinyin_check as P, es_asr
from taiwan_pronunciation import TaiwanPronunciationNormalizer, dictionary_markdown

DEST = "E:/Materials/Voices/UEP/2026-10-31_polyphone_check"
OLD_COMMIT = "5dd1b1a"
SHORT = [
    ("p01", "週末我想去逛逛，順便買個便當。", "週末、逛逛"),
    ("p02", "這個參數需要調校一下，順便把系統重構。", "調校、重構"),
    ("p03", "先生，你知道衣服放在哪裡嗎？", "先生、知道、衣服"),
    ("p04", "他的字寫得很差，但是兩個版本的差別不大。", "很差（套用 cha1）、差別（待裁決，未套用）"),
    ("p05", "下週我要出差，差不多要三天。", "週、出差、差不多（後兩者待裁決，未套用）"),
    ("p06", "這些東西有什麼意思？你看看就知道了。", "東西、意思、看看（待裁決，未套用）、知道"),
    ("p07", "一會兒我要去市場買週刊，等一會兒再回來。", "一會兒（待裁決，未套用）、週刊"),
    ("p08", "重構之後品質很差，所以我要重新調校整個系統。", "重構、品質、很差、調校"),
]

def old_normalizer():
    src = subprocess.check_output(["git", "show", f"{OLD_COMMIT}:experiments/taiwan_pronunciation.py"], cwd=T.ROOT).decode("utf-8")
    m = types.ModuleType("tp_old"); exec(compile(src, "tp_old", "exec"), m.__dict__)
    return m.TaiwanPronunciationNormalizer()

def main():
    from math import gcd
    from scipy.signal import resample_poly
    new, old = TaiwanPronunciationNormalizer(), old_normalizer()
    n_path, n_text = T.neutral_ref()
    model = E.load_model(); sr = model.tts_model.sample_rate
    E.set_tag(model, "step_0000300")
    E.synth(model, "你好。", 1, n_path, n_text)
    info = []
    for i, (pid, text, focus) in enumerate(SHORT):
        os.makedirs(f"{DEST}/{pid}", exist_ok=True)
        row = {"id": pid, "text": text, "focus": focus}
        for tag, nm in (("before", old), ("after", new)):
            sent = nm.normalize(text)
            w = E.synth(model, sent, T.SEED + 50 + i, n_path, n_text)
            g = gcd(sr, 44100)
            fin, _ = P._post(resample_poly(np.asarray(w, dtype="float64"), 44100 // g, sr // g), 44100)
            f = f"{DEST}/{pid}/{pid}__TWLORA-TWN-{tag}.wav"; sf.write(f, fin, 44100, subtype="PCM_16")
            row[tag] = {"sent": sent, "asr": es_asr.transcribe(f)}
        info.append(row); print(row, flush=True)
    json.dump(info, open(f"{T.ROOT}/outputs/tw_lora_eval/polyphone_check.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
if __name__ == "__main__":
    main()
