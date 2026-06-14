"""
LoRA 推論測試：用 LoRA 訓練過的 GPT 跑 IndexTTS2，對比原始模型。

策略：
  1. 暫時把 gpt.pth 替換成 LoRA merged 版本
  2. 跑幾句測試
  3. 還原 gpt.pth
"""

import os
import sys
import shutil
import time

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

CHECKPOINT_DIR = os.path.join(PROJECT_ROOT, "echo_tts", "modules", "tts_module", "checkpoints")
ORIGINAL_GPT = os.path.join(CHECKPOINT_DIR, "gpt.pth")
LORA_GPT = os.path.join(PROJECT_ROOT, "experiments", "index-tts-lora", "finetune_models", "checkpoints", "gpt_best_v2_merged.pth")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "outputs", "comparison", "lora_test")

TEST_SENTENCES = [
    ("那個小孩在哪裡玩呢？", "01_basic"),
    ("我搭計程車去捷運站買東西", "02_taiwan_vocab"),
    ("她很漂亮，喜歡在什麼時候去那個地方", "03_tones"),
    ("你知道為什麼鴨嘴獸泰瑞都不會被認出來嗎", "04_breezy_ref"),
    ("今天天氣真好，我想出去走走。", "05_daily"),
]


def main():
    from pathlib import Path
    from Entry import build_legacy_session

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("=" * 60)
    print("  LoRA 推論 A/B 測試")
    print("=" * 60)

    # ── Phase A: 原始模型 ──
    print("\n[Phase A] 用原始 GPT 生成...")
    session = build_legacy_session(
        project_root=PROJECT_ROOT,
        output_dir=OUTPUT_DIR,
        device="cuda",
        playback=False,
    )
    session.load()

    for text, label in TEST_SENTENCES:
        out_path = os.path.join(OUTPUT_DIR, f"{label}_A_original.wav")
        print(f"  A: {text}")
        session.save(text, out_path)

    # 釋放 GPU
    del session
    import torch
    torch.cuda.empty_cache()
    import gc
    gc.collect()
    print("\n  原始模型已釋放")

    # ── 替換 GPT checkpoint ──
    print(f"\n[Swap] 替換 gpt.pth → LoRA merged 版本...")
    backup_path = ORIGINAL_GPT + ".backup"
    os.rename(ORIGINAL_GPT, backup_path)
    shutil.copy2(LORA_GPT, ORIGINAL_GPT)
    print("  已替換")

    try:
        # ── Phase B: LoRA 模型 ──
        print("\n[Phase B] 用 LoRA GPT 生成...")
        session = build_legacy_session(
            project_root=PROJECT_ROOT,
            output_dir=OUTPUT_DIR,
            device="cuda",
            playback=False,
        )
        session.load()

        for text, label in TEST_SENTENCES:
            out_path = os.path.join(OUTPUT_DIR, f"{label}_B_lora.wav")
            print(f"  B: {text}")
            session.save(text, out_path)

        del session
        torch.cuda.empty_cache()
        gc.collect()

    finally:
        # ── 還原 GPT checkpoint ──
        print(f"\n[Restore] 還原原始 gpt.pth...")
        os.remove(ORIGINAL_GPT)
        os.rename(backup_path, ORIGINAL_GPT)
        print("  已還原")

    print("\n" + "=" * 60)
    print("  測試完成！")
    print(f"  輸出: {OUTPUT_DIR}")
    print()
    for _, label in TEST_SENTENCES:
        print(f"  {label}:")
        print(f"    A: {label}_A_original.wav")
        print(f"    B: {label}_B_lora.wav")
    print("=" * 60)


if __name__ == "__main__":
    main()
