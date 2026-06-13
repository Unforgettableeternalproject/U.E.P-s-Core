"""
LoRA A/B 比較測試：同一句話分別用 LoRA OFF 和 ON 生成，方便對比。
"""
import os
import sys
import gc

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

OUTPUT_DIR = os.path.join(PROJECT_ROOT, "outputs", "comparison", "lora_ab")
ADAPTER_DIR = os.path.join(
    PROJECT_ROOT, "experiments", "index-tts-lora", "finetune_models", "checkpoints", "gpt_best"
)

TEST_SENTENCES = [
    "今天天氣真好，我想出去走走。",
    "你知道為什麼鴨嘴獸泰瑞都不會被認出來嗎？",
    "這個軟體已經更新到最新版本了。",
    "我們一起搭捷運去看電影好不好。",
    "她說她最近在練瑜珈，感覺身體好多了。",
    "Hello, this is a test of the text to speech system.",
    "我昨天去夜市逛了一圈，買了好多東西回來。",
]

SCALES = [0.0, 0.5, 0.85, 1.0]


def main():
    import torch
    from Entry import build_legacy_session

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print(f"Adapter: {ADAPTER_DIR}")
    print(f"Output:  {OUTPUT_DIR}")
    print(f"Scales:  {SCALES}")
    print(f"Sentences: {len(TEST_SENTENCES)}")
    print()

    # Build session
    session = build_legacy_session(
        project_root=PROJECT_ROOT,
        output_dir=OUTPUT_DIR,
        device="cuda",
        playback=False,
        use_accel=True,
    )
    session.load()

    # Generate for each scale
    for scale in SCALES:
        label = f"lora_{int(scale*100):03d}"
        print(f"\n{'='*50}")
        print(f"  Scale: {scale:.0%} ({label})")
        print(f"{'='*50}")

        if scale == 0.0:
            # Make sure LoRA is off
            try:
                session.unload_lora()
            except Exception:
                pass
        else:
            # Load adapter at this scale
            try:
                session.unload_lora()
            except Exception:
                pass
            session.load_lora(ADAPTER_DIR, scale=scale)

        for i, text in enumerate(TEST_SENTENCES):
            out_path = os.path.join(OUTPUT_DIR, f"s{i:02d}_{label}.wav")
            print(f"  [{label}] {text[:40]}...", end="", flush=True)
            session.save(text, out_path)
            print(f"  ✅")

        # Clean up between scales
        torch.cuda.empty_cache()

    # Unload at the end
    try:
        session.unload_lora()
    except Exception:
        pass

    print(f"\n{'='*50}")
    print(f"  完成！輸出: {OUTPUT_DIR}")
    print(f"  每句比較:")
    for i, text in enumerate(TEST_SENTENCES):
        print(f"\n  [{i}] {text[:50]}")
        for scale in SCALES:
            label = f"lora_{int(scale*100):03d}"
            print(f"      {label}: s{i:02d}_{label}.wav")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
