"""外語（英/日）診斷：分辨問題出在模型本身、參考音語言，還是 ULT 逐字稿。
輸出 E:/Materials/Voices/UEP/2026-10-31_foreign_check/{en1,en2,en3,ja1,ja2,ja3}/<id>__<VERSION>.wav + README。
版本定義見 README_TEXT。V25 直接取 TestSeparateTTSSystem/outputs/v25_final 既有檔（B_en*/B_ja*）。"""
import json, os, shutil, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from math import gcd
import numpy as np, soundfile as sf, torch
from scipy.signal import resample_poly
import tw_accent_check as T, tw_lora_eval as E, tw_pinyin_check as P, es_asr

DEST = "E:/Materials/Voices/UEP/2026-10-31_foreign_check"
V25 = os.path.join(os.path.dirname(T.ROOT), "TestSeparateTTSSystem", "outputs", "v25_final")
REFS = os.path.join(T.ROOT, "outputs", "voxcpm2_zeroshot", "refs")
REFJ = json.load(open(os.path.join(REFS, "refs.json"), encoding="utf-8"))
ITEMS = [  # (id, lang, 文字, V25 檔名, V25 備註)
    ("en1", "en", "The weather is nice today. I finished organizing everything on my desk. What should we do next?", "B_en.wav", "中性"),
    ("en2", "en", "Haha! Guess what? The new model passed every single test on the first try! Let's get it running right now!", "B_en_happy05.wav", "V25 帶 happy 0.5 情緒向量"),
    ("en3", "en", "I'm sorry, I lost the backup files. I promised I would keep them safe, and I let you down.", None, ""),
    ("ja1", "ja", "今日はいい天気ですね。机の上の仕事を全部片付けました。次は何をしましょうか？", "B_ja.wav", "中性"),
    ("ja2", "ja", "ねえねえ、聞いて！新しいモデルのテスト、一発で全部合格したんだよ！すごくない？", "B_ja_happy065.wav", "V25 帶 happy 0.65 情緒向量"),
    ("ja3", "ja", "あ...ごめんなさい……大事なデータを失くしちゃった……ちゃんと守るって約束したのに……", "B_ja_sad065.wav", "V25 帶 sad 0.65 情緒向量"),
]
README_TEXT = """# 外語診斷（英文／日文）2026-10-31

目的：判斷英日聽起來怪，問題出在「模型本身」、「參考音語言」還是「ULT 逐字稿」。
全部 44.1kHz、-23 LUFS、頭尾靜音已修齊；VOX 各版 seed 相同（SEED+句序），cfg 2.0、steps 10、normalize=False。
en3 沒有現成 V25 檔，故該句無 V25 版。V25 的 happy/sad 句帶情緒向量，VOX 各版為純文字無情緒提示，比較時請注意。

## 版本定義

- `V25`：現行 IndexTTS 2.5（TestSeparateTTSSystem/outputs/v25_final 既有檔，B 系列，現行線上設定）
- `VOX-BASE-ULT-R1`：完全沒載 LoRA 的乾淨 VoxCPM2；ULT 模式，參考音與 prompt 音檔都是 R1（uep_reference_v25.wav，英文），prompt 逐字稿為 Whisper 轉出的 R1 逐字稿（結尾斷在 "As a"，不是完整句尾）
- `VOX-BASE-REF-R1`：同上乾淨模型，只給參考音 R1（reference only，無逐字稿）
- `VOX-BASE-REF-R2`：只給參考音，用 uep_pancake.wav（英文 pancake 錄音）
- `VOX-BASE-REF-R4`（僅日文）：只給參考音，ITA 日文原始參考音（RVC Datasets ITA_emotion_normal_synchronized_wav/UEP 的 emoNormal 片段，經 VAD 挑 12 秒）
- `VOX-TWLORA-OFF`：目前 synth_auto 路徑：掛 LoRA(step_0000300) 後 `set_lora_enabled(False)`、不經 TWN，ULT 模式，參考音是中文 neutral.wav 加其中文逐字稿
- `VOX-BASE-ULT-NEUTRAL`（額外對照）：乾淨模型，路徑與 TWLORA-OFF 相同（中文 neutral.wav ULT）；用來確認 LoRA 關閉後是否與沒載 LoRA 一致
"""

def post(w, sr):
    g = gcd(sr, 44100)
    fin, _ = P._post(resample_poly(np.asarray(w, dtype="float64"), 44100 // g, sr // g), 44100)
    return fin

def main():
    from voxcpm import VoxCPM
    model = VoxCPM.from_pretrained(T.MODEL_DIR, load_denoiser=False, optimize=False, local_files_only=True)  # 乾淨模型
    sr = model.tts_model.sample_rate
    n_path, n_text = T.neutral_ref()
    R1, R2, R4 = (os.path.join(REFS, f"{k}.wav") for k in ("R1", "R2", "R4"))
    r1_text = REFJ["R1"]["transcript"]
    def gen(m, text, seed, ref, ptxt=None):
        torch.manual_seed(seed)
        kw = dict(reference_wav_path=ref)
        if ptxt:
            kw.update(prompt_wav_path=ref, prompt_text=ptxt)
        return np.concatenate(list(m.generate_streaming(text=text, cfg_value=2.0, inference_timesteps=10, normalize=False, **kw)))
    gen(model, "Hello.", 1, R1)
    out = {}
    for i, (iid, lang, text, v25f, v25note) in enumerate(ITEMS):
        seed = T.SEED + 70 + i
        d = f"{DEST}/{iid}"; os.makedirs(d, exist_ok=True)
        vers = {"VOX-BASE-ULT-R1": gen(model, text, seed, R1, r1_text), "VOX-BASE-REF-R1": gen(model, text, seed, R1),
                "VOX-BASE-REF-R2": gen(model, text, seed, R2), "VOX-BASE-ULT-NEUTRAL": gen(model, text, seed, n_path, n_text)}
        if lang == "ja":
            vers["VOX-BASE-REF-R4"] = gen(model, text, seed, R4)
        for v, w in vers.items():
            f = f"{d}/{iid}__{v}.wav"; sf.write(f, post(w, sr), 44100, subtype="PCM_16")
            out[f"{iid}__{v}"] = es_asr.transcribe(f, language=lang)
        if v25f:
            y, s0 = sf.read(f"{V25}/{v25f}", dtype="float64")
            y = y if y.ndim == 1 else y.mean(axis=1)
            g = gcd(s0, 44100); fin, _ = P._post(resample_poly(y, 44100 // g, s0 // g), 44100)
            f = f"{d}/{iid}__V25.wav"; sf.write(f, fin, 44100, subtype="PCM_16"); out[f"{iid}__V25"] = es_asr.transcribe(f, language=lang)
        print(iid, flush=True)
    del model; torch.cuda.empty_cache()
    m2 = E.load_model(); E.set_tag(m2, "step_0000300"); m2.set_lora_enabled(False)
    for i, (iid, lang, text, *_r) in enumerate(ITEMS):
        w = gen(m2, text, T.SEED + 70 + i, n_path, n_text)
        f = f"{DEST}/{iid}/{iid}__VOX-TWLORA-OFF.wav"; sf.write(f, post(w, sr), 44100, subtype="PCM_16")
        out[f"{iid}__VOX-TWLORA-OFF"] = es_asr.transcribe(f, language=lang)
    json.dump(out, open(f"{T.ROOT}/outputs/tw_lora_eval/foreign_check.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    L = [README_TEXT, "## 句子", ""]
    for iid, lang, text, v25f, note in ITEMS:
        L += [f"- {iid}（{lang}）：{text}" + (f"　〔V25：{v25f}，{note}〕" if v25f else "")]
    L += ["", "## ES ASR（Breeze2.5）轉寫，輔助用", ""] + [f"- {k}：{v}" for k, v in out.items()]
    open(f"{DEST}/README.md", "w", encoding="utf-8", newline="\n").write("\n".join(L) + "\n")
main()
