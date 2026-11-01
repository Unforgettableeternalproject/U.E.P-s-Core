"""語調 A/B 步驟 2：IndexTTS 2.5（uep_v25 + LoRA 0.85，無情緒向量）生成同樣 6 句，作為「抑揚頓挫」參考上限。

參數同 tw_accent_check.cmd_gen25：LoRA scale 0.85、emotion_vector=None、language=zh；每句 seed = 句 seed（7000+序）。
共用環境、PYTHONUTF8=1、一次一個 GPU 行程。輸出 outputs/prosody_ab/raw/v25/<句>__V25-L085.wav 與 v25_gen.json。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import emotion_common as C  # noqa: E402
import prosody_ab_lib as L  # noqa: E402


def main():
    sys.path.insert(0, C.ROOT)
    os.chdir(C.ROOT)
    import soundfile as sf
    import torch
    from echo_tts.runtime.indextts_backend import IndexTTS2Backend
    from echo_tts.runtime.indextts_generate import default_generation_paths

    be = IndexTTS2Backend(paths=default_generation_paths(C.ROOT), device="cuda", use_fp16=True, use_cuda_kernel=True, use_accel=False)
    be.load()
    be.load_lora(scale=0.85)
    be.emotion_vector = None
    be.set_lora_scale(0.85)
    print("LoRA:", be.get_lora_status(), flush=True)
    d = os.path.join(L.RAW, "v25")
    os.makedirs(d, exist_ok=True)
    # 暖機一次（不計）
    be.generate("你好，今天天氣真不錯。", os.path.join(d, "_warm.wav"), language="zh")
    rows = []
    for sid, text in L.SENTS:
        seed = L.seed_of(sid)
        p = os.path.join(d, f"{sid}__V25-L085.wav")
        torch.manual_seed(seed)
        t0 = time.perf_counter()
        be.generate(text, p, language="zh")
        el = time.perf_counter() - t0
        info = sf.info(p)
        dur = info.frames / info.samplerate
        rows.append({"sid": sid, "text": text, "seed": seed, "file": f"v25/{sid}__V25-L085.wav", "sr": info.samplerate,
                     "audio_s": round(dur, 2), "gen_s": round(el, 2), "rtf": round(el / dur, 3)})
        print(rows[-1], flush=True)
    C.jdump(os.path.join(L.OUT, "v25_gen.json"), {"rows": rows, "lora": str(be.get_lora_status())})
    print("v25 done", flush=True)


if __name__ == "__main__":
    main()
