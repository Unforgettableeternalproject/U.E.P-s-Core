"""
設定 LoRA 訓練環境

做三件事：
1. 從 v2 GPT checkpoint 提取 conditioning，生成每個樣本的 condition.npy
2. 設定 finetune_models/ 的 checkpoint symlinks
3. 更新 speaker_info.json 和 metadata，補上 conditioning 路徑
"""

import json
import os
import sys
import shutil
import numpy as np
import torch

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
CHECKPOINT_DIR = os.path.join(PROJECT_ROOT, "legacy_tts", "modules", "tts_module", "checkpoints")
LORA_REPO = os.path.join(PROJECT_ROOT, "experiments", "index-tts-lora")
FINETUNE_DIR = os.path.join(LORA_REPO, "finetune_models")
PROCESSED_BASE = os.path.join(PROJECT_ROOT, "experiments", "lora_training_data", "processed_data")


def find_latest_processed_dir():
    """找到最新的 processed_data 子目錄"""
    if not os.path.exists(PROCESSED_BASE):
        raise FileNotFoundError(f"找不到 {PROCESSED_BASE}")
    dirs = [d for d in os.listdir(PROCESSED_BASE)
            if os.path.isdir(os.path.join(PROCESSED_BASE, d)) and d.startswith("audio_list_")]
    if not dirs:
        raise FileNotFoundError("沒有找到 processed data 目錄")
    return os.path.join(PROCESSED_BASE, sorted(dirs)[-1])


def step1_generate_conditioning(processed_dir: str):
    """
    從 v2 GPT checkpoint 提取 conditioning，為每個訓練樣本生成 condition.npy。
    使用 perceiver_encoder.latents (32, 1280) 作為固定 conditioning。
    """
    print("[Step 1] 生成 conditioning vectors...")

    # 從 v2 checkpoint 提取 perceiver_encoder latents
    gpt_path = os.path.join(CHECKPOINT_DIR, "gpt.pth")
    print(f"  載入 v2 GPT checkpoint: {gpt_path}")
    state = torch.load(gpt_path, map_location="cpu", weights_only=True)

    # perceiver_encoder.latents 是 conditioning 的初始 query，shape (32, 1280)
    latents = state["perceiver_encoder.latents"].numpy()
    print(f"  perceiver_encoder.latents shape: {latents.shape}")

    # 保存為 medoid_condition
    medoid_path = os.path.join(processed_dir, "medoid_condition.npy")
    np.save(medoid_path, latents)
    print(f"  已保存 medoid_condition: {medoid_path}")

    # 為每個訓練樣本生成 condition.npy（全部使用相同的 conditioning）
    metadata_path = os.path.join(processed_dir, "metadata.jsonl")
    updated_lines = []
    condition_count = 0

    with open(metadata_path, "r", encoding="utf-8") as f:
        for line in f:
            entry = json.loads(line.strip())
            # 生成 per-sample condition
            base_name = os.path.basename(entry["audio"])
            cond_path = os.path.join(processed_dir, f"{base_name}_condition.npy")
            np.save(cond_path, latents)
            entry["condition"] = os.path.abspath(cond_path)
            updated_lines.append(json.dumps(entry, ensure_ascii=False) + "\n")
            condition_count += 1

    # 覆寫 metadata
    with open(metadata_path, "w", encoding="utf-8") as f:
        f.writelines(updated_lines)

    # 重新分割 train/valid（保留原始比例）
    np.random.seed(42)
    np.random.shuffle(updated_lines)
    valid_size = max(1, int(len(updated_lines) * 0.1))
    train_lines = updated_lines[:-valid_size]
    valid_lines = updated_lines[-valid_size:]

    train_path = os.path.join(processed_dir, "metadata_train.jsonl")
    valid_path = os.path.join(processed_dir, "metadata_valid.jsonl")
    with open(train_path, "w", encoding="utf-8") as f:
        f.writelines(train_lines)
    with open(valid_path, "w", encoding="utf-8") as f:
        f.writelines(valid_lines)

    print(f"  已為 {condition_count} 個樣本生成 conditioning")
    print(f"  Train: {len(train_lines)}, Valid: {len(valid_lines)}")

    # 更新 speaker_info
    speaker_info = [{
        "speaker": "audio_list",
        "sample_num": condition_count,
        "avg_duration": sum(json.loads(l)["duration"] for l in updated_lines) / len(updated_lines),
        "total_duration_in_seconds": sum(json.loads(l)["duration"] for l in updated_lines),
        "total_duration_in_minutes": sum(json.loads(l)["duration"] for l in updated_lines) / 60,
        "total_duration_in_hours": sum(json.loads(l)["duration"] for l in updated_lines) / 3600,
        "train_jsonl": os.path.abspath(train_path),
        "valid_jsonl": os.path.abspath(valid_path),
        "medoid_condition": os.path.abspath(medoid_path),
    }]

    speaker_info_path = os.path.join(processed_dir, "speaker_info.json")
    with open(speaker_info_path, "w", encoding="utf-8") as f:
        json.dump(speaker_info, f, ensure_ascii=False, indent=2)
    print(f"  已更新 speaker_info: {speaker_info_path}")

    return medoid_path


def step2_setup_checkpoints():
    """
    設定 finetune_models/ 目錄，symlink 到我們的 v2 checkpoints。
    """
    print("\n[Step 2] 設定 checkpoint symlinks...")
    os.makedirs(FINETUNE_DIR, exist_ok=True)

    # 需要的檔案對應
    links = {
        "gpt.pth": os.path.join(CHECKPOINT_DIR, "gpt.pth"),
        "bpe.model": os.path.join(CHECKPOINT_DIR, "bpe.model"),
        # DVAE: v2 不用 DVAE，但 config 有引用。建立一個 placeholder 或跳過
        # bigvgan: v2 用 HuggingFace 的 BigVGAN，不需要本地檔案
    }

    for name, target in links.items():
        link_path = os.path.join(FINETUNE_DIR, name)
        if os.path.exists(link_path):
            os.remove(link_path)
        if os.path.exists(target):
            # Windows: 用 copy 代替 symlink（避免權限問題）
            print(f"  複製 {name} → {link_path}")
            if os.path.getsize(target) > 100_000_000:  # > 100MB, 用 symlink
                try:
                    os.symlink(target, link_path)
                    print(f"    (symlink, {os.path.getsize(target)/1e9:.1f} GB)")
                except OSError:
                    print(f"    symlink 失敗，跳過大檔案（請手動複製或 mklink）")
            else:
                shutil.copy2(target, link_path)
        else:
            print(f"  ⚠️ 找不到: {target}")

    print("  完成")


def step3_update_config(processed_dir: str):
    """
    更新 index-tts-lora 的 config.yaml，適配 v2。
    """
    print("\n[Step 3] 更新訓練配置...")

    config_path = os.path.join(FINETUNE_DIR, "config.yaml")

    # 讀取現有 v2 config 作為基礎
    v2_config_path = os.path.join(CHECKPOINT_DIR, "config.yaml")

    import yaml
    with open(v2_config_path, "r", encoding="utf-8") as f:
        v2_cfg = yaml.safe_load(f)

    # 讀取 index-tts-lora 的訓練配置
    lora_config_path = os.path.join(FINETUNE_DIR, "config.yaml")
    with open(lora_config_path, "r", encoding="utf-8") as f:
        lora_cfg = yaml.safe_load(f)

    # 合併：用 v2 的模型配置 + index-tts-lora 的訓練配置
    merged = {}

    # 模型配置：用 v2 的
    merged["dataset"] = v2_cfg["dataset"]
    merged["gpt"] = v2_cfg["gpt"]
    # 移除 emo_condition_module（LoRA 不需要）
    # 保留它在 config 裡，但 model loading 時忽略

    # 訓練配置：用 index-tts-lora 的
    merged["train"] = lora_cfg["train"]
    merged["train"]["data_path"] = os.path.abspath(os.path.dirname(processed_dir))

    # LoRA 配置
    merged["train"]["optimizer"] = lora_cfg["train"]["optimizer"]
    merged["train"]["lora"] = lora_cfg["train"]["lora"]

    # Checkpoint 路徑
    merged["gpt_checkpoint"] = "gpt.pth"
    merged["version"] = "2.0"

    # DVAE/BigVGAN：v2 不需要，但保留佔位
    merged["dvae_checkpoint"] = "dvae.pth"  # 不存在但 extract_codec 不需要了
    merged["bigvgan_checkpoint"] = "bigvgan_generator.pth"

    # 保留 VQVAE config（兼容 index-tts-lora 的 import，即使不用）
    if "vqvae" in lora_cfg:
        merged["vqvae"] = lora_cfg["vqvae"]

    # 寫回
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(merged, f, default_flow_style=False, allow_unicode=True)

    print(f"  已更新: {config_path}")
    print(f"  GPT config: {merged['gpt']['model_dim']}d, {merged['gpt']['heads']}h, {merged['gpt']['layers']}L")
    print(f"  LoRA: r={merged['train']['lora']['r']}, alpha={merged['train']['lora']['lora_alpha']}")
    print(f"  Data path: {merged['train']['data_path']}")


def main():
    print("=" * 60)
    print("  LoRA 訓練環境設定")
    print("=" * 60)
    print()

    processed_dir = find_latest_processed_dir()
    print(f"使用 processed data: {processed_dir}")
    print()

    step1_generate_conditioning(processed_dir)
    step2_setup_checkpoints()
    step3_update_config(processed_dir)

    print()
    print("=" * 60)
    print("  設定完成！")
    print()
    print("  下一步：")
    print(f"    cd {LORA_REPO}")
    print(f"    python train.py")
    print()
    print("  注意：train.py 需要修改以支援 v2 GPT 模型載入。")
    print("  主要改動：model.load_state_dict(state, strict=False)")
    print("=" * 60)


if __name__ == "__main__":
    main()
