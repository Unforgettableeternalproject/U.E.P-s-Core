"""品質健全性：對 latency_bench 產出的音檔抽樣算 ES ASR（Breeze）CER，Vox 另算 SIM 對 R1（.venv-voxcpm，PYTHONUTF8=1）。

兩個引擎都跑完、GPU 釋放後再執行。抽樣：每引擎每語言 短/中 各前 10 筆、長 10 筆全取，加上 turn 段落。
CER 以原文為準（ja 的 Vox 輸入是假名化文字，但仍與原文漢字比；另附 kana 化後的 CER 才公平）。
"""
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import latency_bench_lib as L  # noqa: E402


def main():
    import numpy as np
    from rapidfuzz.distance import Levenshtein

    import es_asr
    import tw_accent_check as T
    from japanese_kana import JapaneseKanaNormalizer
    kana = JapaneseKanaNormalizer()
    paras = json.load(open(os.path.join(L.OUT, "paras_split.json"), encoding="utf-8"))

    def norm(t, lang):
        t = re.sub(r"[^一-鿿぀-ヿA-Za-z0-9]+", "", t).lower()
        return t

    def cer(ref, hyp, lang):
        r, h = norm(ref, lang), norm(hyp, lang)
        return Levenshtein.distance(r, h) / max(1, len(r))

    from resemblyzer import VoiceEncoder, preprocess_wav
    enc = VoiceEncoder("cuda")
    r1 = enc.embed_utterance(preprocess_wav(T.R1_16K))

    def sim(path):
        e = enc.embed_utterance(preprocess_wav(path))
        return float(np.dot(r1, e) / (np.linalg.norm(r1) * np.linalg.norm(e)))

    out = {}
    for eng in ("vox", "v25"):
        rows = []
        for lang in L.LANGS:
            for b, n in (("short", 10), ("mid", 10), ("long", 10)):
                for i in range(n):
                    p = os.path.join(L.WAV, eng, f"{lang}_{b}_{i:02d}.wav")
                    if not os.path.exists(p):
                        continue
                    ref = L.TEXTS[lang][b][i % 4]
                    hyp = es_asr.transcribe(p, language=lang)
                    r = {"engine": eng, "lang": lang, "bucket": b, "i": i, "ref": ref, "asr": hyp, "cer": cer(ref, hyp, lang)}
                    if lang == "ja":
                        r["cer_kana"] = cer(kana.normalize(ref), kana.normalize(hyp), lang)
                    if eng == "vox":
                        r["sim_r1"] = sim(p)
                    rows.append(r)
            p = os.path.join(L.WAV, eng, f"turn_{lang}.wav")
            if eng == "v25" and os.path.exists(p):
                ref = "".join(paras[lang][0])
                hyp = es_asr.transcribe(p, language=lang)
                rows.append({"engine": eng, "lang": lang, "bucket": "turn", "ref": ref, "asr": hyp, "cer": cer(ref, hyp, lang)})
            if eng == "vox":
                ps = sorted(glob.glob(os.path.join(L.WAV, "vox", f"turn_{lang}_*.wav")))
                if ps:
                    import soundfile as sf
                    ys = [sf.read(q, dtype="float32")[0] for q in ps]
                    sr = sf.info(ps[0]).samplerate
                    tmp = os.path.join(L.OUT, f"_turn_{lang}.wav")
                    sf.write(tmp, np.concatenate(ys), sr)
                    ref = "".join(paras[lang][0])
                    hyp = es_asr.transcribe(tmp, language=lang)
                    rows.append({"engine": eng, "lang": lang, "bucket": "turn", "ref": ref, "asr": hyp, "cer": cer(ref, hyp, lang), "sim_r1": sim(tmp)})
                    os.remove(tmp)
        out[eng] = rows
        print(eng, len(rows), flush=True)
    L.jdump(os.path.join(L.RAW, "quality.json"), out)


if __name__ == "__main__":
    main()
