"""
LoRA 強度縮放測試：比較不同 LoRA 混合比例對腔調和情緒的影響。
生成 30% / 50% / 70% / 100% 四個版本。
"""
import os, sys, shutil, gc
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

CHECKPOINT_DIR = os.path.join(PROJECT_ROOT, "echo_tts", "modules", "tts_module", "checkpoints")
ORIGINAL_GPT = os.path.join(CHECKPOINT_DIR, "gpt.pth")
CKPT_DIR = os.path.join(PROJECT_ROOT, "experiments", "index-tts-lora", "finetune_models", "checkpoints")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "outputs", "comparison", "lora_scaling")

SCALES = [
    ("30", os.path.join(CKPT_DIR, "gpt_lora_scale_30.pth")),
    ("50", os.path.join(CKPT_DIR, "gpt_lora_scale_50.pth")),
    ("70", os.path.join(CKPT_DIR, "gpt_lora_scale_70.pth")),
    ("100", os.path.join(CKPT_DIR, "gpt_best_v2_merged.pth")),
]

TEST_SENTENCES = [
    ("今天天氣真好，我想出去走走。", "daily"),
    ("你知道為什麼鴨嘴獸泰瑞都不會被認出來嗎", "breezy"),
]

def main():
    import torch
    from pathlib import Path
    from Entry import build_legacy_session

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    backup = ORIGINAL_GPT + ".backup"

    # Backup original
    os.rename(ORIGINAL_GPT, backup)

    try:
        for scale_name, ckpt_path in SCALES:
            print(f"\n{'='*50}")
            print(f"  LoRA Scale: {scale_name}%")
            print(f"{'='*50}")

            # Swap checkpoint
            if os.path.exists(ORIGINAL_GPT):
                os.remove(ORIGINAL_GPT)
            shutil.copy2(ckpt_path, ORIGINAL_GPT)

            session = build_legacy_session(
                project_root=PROJECT_ROOT, output_dir=OUTPUT_DIR,
                device="cuda", playback=False,
            )
            session.load()

            for text, label in TEST_SENTENCES:
                out = os.path.join(OUTPUT_DIR, f"{label}_lora{scale_name}.wav")
                print(f"  [{scale_name}%] {text}")
                session.save(text, out)

            del session
            torch.cuda.empty_cache()
            gc.collect()
    finally:
        # Restore
        if os.path.exists(ORIGINAL_GPT):
            os.remove(ORIGINAL_GPT)
        os.rename(backup, ORIGINAL_GPT)
        print("\n原始 gpt.pth 已還原")

    print(f"\n{'='*50}")
    print(f"  完成！輸出: {OUTPUT_DIR}")
    for _, label in TEST_SENTENCES:
        print(f"  {label}:")
        for s, _ in SCALES:
            print(f"    lora{s}%: {label}_lora{s}.wav")
    print(f"{'='*50}")

if __name__ == "__main__":
    main()
