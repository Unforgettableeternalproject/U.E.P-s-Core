"""IndexTTS 2.5 多語言一致性測試 — 英文/日文體驗驗證。

基準（艾斯維爾 2026-08-19 定案，第二輪：ccd92300 聽感太硬改用 de051c45）：
- 參考音：U.E.P-s-Core/outputs/tts/Test/uep_de051c45.wav
- 情感基準：happy 0.5 / emo_alpha 1.0

驗證目標：英文與日文下音色是否與中文一致、情感向量行為是否相同。

用法：
    python experiments/v25_multilang_test.py
"""
import os
import sys
import time

UPSTREAM_REPO = r"C:\Users\Bernie\source\repos\Other\index-tts\index-tts"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CKPT_DIR = os.path.join(PROJECT_ROOT, "echo_tts", "modules", "tts_module", "checkpoints_v25")
SPK_PROMPT = r"C:\Users\Bernie\source\repos\Unforgettableeternalproject\U.E.P-s-Core\outputs\tts\Test\uep_de051c45.wav"
OUT_DIR = os.path.join(PROJECT_ROOT, "outputs", "v25_multilang")

sys.path.insert(0, UPSTREAM_REPO)

import transformers.pytorch_utils as _pu

if not hasattr(_pu, "is_torch_greater_or_equal_than_1_13"):
    _pu.is_torch_greater_or_equal_than_1_13 = True

HAPPY_05 = [0.5, 0, 0, 0, 0, 0, 0, 0]

# (檔名, lang, 文字, 情感向量或 None)
CASES = [
    # 中文對照組（與音色掃描同基準，作為跨語言比較的錨點）
    ("zh_neutral", "ZH", "今天的天气不错，我把桌面上的工作都整理完了，接下来要做什么呢？", None),
    # 英文：中性 + 情感
    ("en_neutral", "EN", "The weather is nice today. I finished organizing everything on my desk. What should we do next?", None),
    ("en_happy05", "EN", "Haha, that's hilarious! I can't believe it actually worked!", HAPPY_05),
    # 日文：中性 + 情感
    ("ja_neutral", "JA", "今日はいい天気ですね。机の上の仕事を全部片付けました。次は何をしましょうか？", None),
    ("ja_happy05", "JA", "あはは、面白すぎる！本当にうまくいったなんて信じられない！", HAPPY_05),
    # 中英混合（U.E.P 日常對話常見情境）
    ("mixed_zh_en", "ZH", "这个 module 的 refactor 完成了，performance 提升了不少哦。", None),
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

    for name, lang, text, emo in CASES:
        out_path = os.path.join(OUT_DIR, f"{name}.wav")
        kwargs = {}
        if emo is not None:
            kwargs["emo_vector"] = emo
        t0 = time.perf_counter()
        tts.infer(
            spk_audio_prompt=SPK_PROMPT,
            text=text,
            lang=lang,
            output_path=out_path,
            **kwargs,
        )
        print(f">> {name} [{lang}]: {time.perf_counter() - t0:.2f}s", flush=True)

    print(f"\n=== 多語言測試完成，輸出目錄：{OUT_DIR} ===")


if __name__ == "__main__":
    main()
