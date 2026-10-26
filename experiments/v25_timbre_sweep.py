"""IndexTTS 2.5 音色保留掃描 — 參考音檔篩選 + 情感強度/alpha 階梯。

背景：副語言探測顯示情感向量 0.8+ 會讓訓練集音色壓過 U.E.P 角色音色。
2.0 生產管線的邊界是 max_strength=0.3（向量總和上限，保留 70% 原始聲音），
本實驗驗證 2.5 上「0.3 邊界 + emo_alpha 壓低」的組合甜蜜點。

Part A：6 個主專案 Test 參考音 × 同一句中性文字 → 挑音色最像的參考
Part B：固定參考 × happy {0.3, 0.5, 0.8} × emo_alpha {1.0, 0.65} → 強度階梯

用法：
    python experiments/v25_timbre_sweep.py
"""
import os
import sys
import time

UPSTREAM_REPO = r"C:\Users\Bernie\source\repos\Other\index-tts\index-tts"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CKPT_DIR = os.path.join(PROJECT_ROOT, "echo_tts", "modules", "tts_module", "checkpoints_v25")
TEST_REF_DIR = r"C:\Users\Bernie\source\repos\Unforgettableeternalproject\U.E.P-s-Core\outputs\tts\Test"
OUT_DIR = os.path.join(PROJECT_ROOT, "outputs", "v25_timbre")

sys.path.insert(0, UPSTREAM_REPO)

import transformers.pytorch_utils as _pu

if not hasattr(_pu, "is_torch_greater_or_equal_than_1_13"):
    _pu.is_torch_greater_or_equal_than_1_13 = True

# 2.5 tokenizer 對部分繁體詞元解析不完整，比照 2.0 front.py 先做 t2s 轉換
from opencc import OpenCC

_T2S = OpenCC("t2s")

NEUTRAL_TEXT = _T2S.convert("你好阿，我是 U.E.P 的角色音色測試，這是一句中性文字。")
LAUGH_TEXT = _T2S.convert("哈哈哈哈哈！太好笑了吧！")

# Part B 使用的固定參考（最長的 Test 檔，之後可依 Part A 結果換）
PART_B_REF = "uep_673330ec.wav"

# (強度, alpha)
LADDER = [
    (0.3, 1.0),
    (0.5, 1.0),
    (0.8, 1.0),
    (0.3, 0.65),
    (0.5, 0.65),
    (0.8, 0.65),
]


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    from indextts.infer_v2_5 import IndexTTS2

    tts = IndexTTS2(
        cfg_path=os.path.join(CKPT_DIR, "config.yaml"),
        model_dir=CKPT_DIR,
        use_bf16=True,
    )
    print(">> 模型就緒", flush=True)

    # Part A：參考音檔篩選（中性句，無情感向量）
    refs = sorted(f for f in os.listdir(TEST_REF_DIR) if f.endswith(".wav"))
    print(f">> Part A：{len(refs)} 個參考音檔", flush=True)
    for ref in refs:
        tag = os.path.splitext(ref)[0]
        out_path = os.path.join(OUT_DIR, f"A_{tag}.wav")
        t0 = time.perf_counter()
        tts.infer(
            spk_audio_prompt=os.path.join(TEST_REF_DIR, ref),
            text=NEUTRAL_TEXT,
            lang="ZH",
            output_path=out_path,
        )
        print(f">> A_{tag}: {time.perf_counter() - t0:.2f}s", flush=True)

    # Part B：強度階梯（笑聲案例）
    ref_path = os.path.join(TEST_REF_DIR, PART_B_REF)
    print(f">> Part B：參考 {PART_B_REF}，{len(LADDER)} 組", flush=True)
    for strength, alpha in LADDER:
        name = f"B_happy{int(strength * 10):02d}_alpha{int(alpha * 100):03d}"
        out_path = os.path.join(OUT_DIR, f"{name}.wav")
        t0 = time.perf_counter()
        tts.infer(
            spk_audio_prompt=ref_path,
            text=LAUGH_TEXT,
            lang="ZH",
            output_path=out_path,
            emo_vector=[strength, 0, 0, 0, 0, 0, 0, 0],
            emo_alpha=alpha,
        )
        print(f">> {name}: {time.perf_counter() - t0:.2f}s", flush=True)

    print(f"\n=== 掃描完成，輸出目錄：{OUT_DIR} ===")


if __name__ == "__main__":
    main()
