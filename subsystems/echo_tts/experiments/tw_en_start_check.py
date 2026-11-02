"""foreign_check 英文檔開頭檢查：逐檔 ES ASR + 原版 whisper(medium,en,word timestamps)，轉寫與原文對齊找多出內容，並看首個詞的起點。"""
import glob, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import librosa, numpy as np
import es_asr
from tw_foreign_check import ITEMS

D = "E:/Materials/Voices/UEP/2026-10-31_foreign_check"
norm = lambda s: re.sub(r"[^a-z0-9 ]+", "", s.lower()).split()  # noqa: E731

def extra_prefix(ref, hyp):
    r, h = norm(ref), norm(hyp)
    for i in range(len(h)):  # 找原文前 3 個詞在轉寫中的起點
        if h[i:i + 3] == r[:3] or (len(h) - i >= 2 and h[i:i + 2] == r[:2] and i > 0 and False):
            return " ".join(h[:i])
    return None

def main():
    from faster_whisper import WhisperModel
    wm = WhisperModel("medium", device="cuda", compute_type="float16")
    out = {}
    for iid, lang, text, *_ in ITEMS:
        if lang != "en":
            continue
        for f in sorted(glob.glob(f"{D}/{iid}/*.wav")):
            ver = os.path.basename(f).split("__")[1][:-4]
            y = librosa.load(f, sr=16000)[0]
            segs, _ = wm.transcribe(y, language="en", beam_size=5, word_timestamps=True)
            words = [w for s in segs for w in s.words]
            wtxt = "".join(w.word for w in words).strip()
            first = words[0].start if words else None
            rms = librosa.feature.rms(y=y, frame_length=400, hop_length=160)[0]
            on = float(np.argmax(rms > 0.1 * rms.max()) * 0.01)
            es = es_asr.transcribe(f, language="en")
            out[f"{iid}__{ver}"] = {"whisper": wtxt, "es": es, "extra_whisper": extra_prefix(text, wtxt), "extra_es": extra_prefix(text, es),
                                    "energy_onset_s": round(on, 2), "first_word_s": None if first is None else round(first, 2),
                                    "dur_s": round(len(y) / 16000, 2)}
            print(iid, ver, out[f"{iid}__{ver}"]["extra_whisper"], "|", out[f"{iid}__{ver}"]["extra_es"], on, flush=True)
    json.dump(out, open("outputs/tw_lora_eval/en_start_check.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)

if __name__ == "__main__":
    main()
