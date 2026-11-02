"""
IndexTTS2 (v2) 語音特徵提取工具

替代 index-tts-lora 的 extract_codec.py，使用 v2 的 Semantic Codec
（wav2vec2bert + RepCodec）而非 v1.5 的 DVAE。

輸出格式與 index-tts-lora 的 train.py 相容：
  - {name}_codes.npy — semantic discrete codes (codebook_size=8192)
  - {name}_mel.npy — 100-band mel spectrogram (24kHz)
  - {name}_condition.npy — 32-token conditioning vector
  - metadata.jsonl — 路徑和時長資訊
  - metadata_train.jsonl / metadata_valid.jsonl — 90/10 分割

用法：
  python extract_codec_v2.py --audio_list path/to/audio_list.txt --extract_condition

需要 IndexTTS2 的 checkpoints 和 HuggingFace 存取（wav2vec2bert, MaskGCT semantic codec）。
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
import torch
import torchaudio
import safetensors.torch
from tqdm import tqdm

# ── 路徑設定 ──
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
ENGINE_ROOT = os.path.join(PROJECT_ROOT, "echo_tts", "modules", "tts_module")
CHECKPOINT_DIR = os.path.join(ENGINE_ROOT, "checkpoints")

# 正確設定 package import 路徑：
# echo_tts/modules/ 是 package root，讓 tts_module.tts_utils.maskgct_utils 的
# 相對 import (.maskgct.models...) 能正確解析
sys.path.insert(0, os.path.join(PROJECT_ROOT, "echo_tts", "modules"))

# ── 常量 ──
DEFAULT_OUTPUT_DIR = os.path.join(PROJECT_ROOT, "experiments", "lora_training_data", "processed_data")
TRAIN_SPLIT_RATIO = 0.9

# Mel config (must match IndexTTS2 config.yaml)
MEL_CONFIG = {
    "sample_rate": 24000,
    "n_fft": 1024,
    "hop_length": 256,
    "win_length": 1024,
    "n_mels": 100,
    "mel_fmin": 0,
}


class SemanticCodeExtractor:
    """
    使用 IndexTTS2 的 wav2vec2bert + RepCodec 提取 semantic codes。
    """

    def __init__(self, device: str = "cuda"):
        self.device = torch.device(device)
        self._load_models()

    def _load_models(self):
        """載入 wav2vec2bert 和 semantic codec"""
        from tts_module.tts_utils.maskgct_utils import build_semantic_model, build_semantic_codec
        from huggingface_hub import hf_hub_download
        from omegaconf import OmegaConf

        # 1. 載入 config
        config_path = os.path.join(CHECKPOINT_DIR, "config.yaml")
        cfg = OmegaConf.load(config_path)

        # 2. wav2vec2bert (for feature extraction)
        print("  [1/3] 載入 wav2vec2bert...")
        t0 = time.perf_counter()
        stats_path = os.path.join(CHECKPOINT_DIR, cfg.w2v_stat)
        self.semantic_model, self.semantic_mean, self.semantic_std = build_semantic_model(stats_path)
        self.semantic_model = self.semantic_model.to(self.device)
        self.semantic_model.eval()
        print(f"        OK ({time.perf_counter()-t0:.1f}s)")

        # 3. Semantic Codec (RepCodec)
        print("  [2/3] 載入 Semantic Codec...")
        t0 = time.perf_counter()
        self.semantic_codec = build_semantic_codec(cfg.semantic_codec)
        codec_ckpt = hf_hub_download("amphion/MaskGCT", filename="semantic_codec/model.safetensors")
        safetensors.torch.load_model(self.semantic_codec, codec_ckpt)
        self.semantic_codec = self.semantic_codec.to(self.device)
        self.semantic_codec.eval()
        print(f"        OK ({time.perf_counter()-t0:.1f}s)")

        # 4. Feature extractor (for wav2vec2bert input preprocessing)
        print("  [3/3] 載入 SeamlessM4T Feature Extractor...")
        from transformers import SeamlessM4TFeatureExtractor
        self.feature_extractor = SeamlessM4TFeatureExtractor.from_pretrained("facebook/w2v-bert-2.0")
        print("        OK")

    @torch.no_grad()
    def extract_codes(self, audio: torch.Tensor, sr: int) -> torch.Tensor:
        """
        從音頻提取 semantic discrete codes。

        Args:
            audio: (1, T) or (T,) waveform tensor
            sr: sample rate

        Returns:
            codes: (1, T') discrete code indices, codebook_size=8192
        """
        # Resample to 16kHz (wav2vec2bert 需要 16kHz)
        if sr != 16000:
            audio_16k = torchaudio.functional.resample(audio, sr, 16000)
        else:
            audio_16k = audio

        if audio_16k.dim() == 2:
            audio_16k = audio_16k.squeeze(0)

        # Feature extraction (SeamlessM4T preprocessor)
        inputs = self.feature_extractor(
            audio_16k.cpu().numpy(),
            sampling_rate=16000,
            return_tensors="pt",
        )
        input_features = inputs["input_features"].to(self.device)
        attention_mask = inputs.get("attention_mask")
        if attention_mask is not None:
            attention_mask = attention_mask.to(self.device)

        # wav2vec2bert forward → layer 17 features
        outputs = self.semantic_model(
            input_features=input_features,
            attention_mask=attention_mask,
            output_hidden_states=True,
        )
        feat = outputs.hidden_states[17]  # (1, T, 1024)

        # 正規化
        feat = (feat - self.semantic_mean.to(feat)) / self.semantic_std.to(feat)

        # RepCodec quantize → discrete codes
        semantic_code, _ = self.semantic_codec.quantize(feat)  # (1, T')

        return semantic_code


class MelExtractor:
    """提取 100-band mel spectrogram (24kHz)"""

    def __init__(self):
        self.mel_transform = torchaudio.transforms.MelSpectrogram(
            sample_rate=MEL_CONFIG["sample_rate"],
            n_fft=MEL_CONFIG["n_fft"],
            hop_length=MEL_CONFIG["hop_length"],
            win_length=MEL_CONFIG["win_length"],
            n_mels=MEL_CONFIG["n_mels"],
            f_min=MEL_CONFIG["mel_fmin"],
            norm="slaney",
            mel_scale="slaney",
        )

    def extract(self, audio: torch.Tensor, sr: int) -> torch.Tensor:
        """
        Args:
            audio: (1, T) waveform
            sr: sample rate

        Returns:
            mel: (1, n_mels, T') mel spectrogram
        """
        if sr != MEL_CONFIG["sample_rate"]:
            audio = torchaudio.functional.resample(audio, sr, MEL_CONFIG["sample_rate"])

        mel = self.mel_transform(audio)
        # Log mel
        mel = torch.log(torch.clamp(mel, min=1e-5))
        return mel


class ConditionExtractor:
    """
    從 mel spectrogram 提取 conditioning vector（用 GPT 的 conformer_perceiver）。
    """

    def __init__(self, device: str = "cuda"):
        self.device = torch.device(device)
        self.model = None

    def load_model(self, config_path: str, gpt_path: str):
        """載入 GPT 模型（只為了提取 conditioning）"""
        from omegaconf import OmegaConf

        # 先嘗試 index-tts-lora 的 UnifiedVoice，再嘗試我們自己的
        lora_repo = os.path.join(PROJECT_ROOT, "experiments", "index-tts-lora")
        if lora_repo not in sys.path:
            sys.path.insert(0, lora_repo)

        try:
            from indextts.gpt.model import UnifiedVoice
        except ImportError:
            from tts_module.gpt.model import UnifiedVoice

        cfg = OmegaConf.load(config_path)

        # 建立模型（需要匹配 v2 的 gpt config）
        gpt_cfg = dict(cfg.gpt)
        # 移除 v2 新增的 emo_condition_module（UnifiedVoice 可能不支援）
        gpt_cfg.pop("emo_condition_module", None)

        model = UnifiedVoice(**gpt_cfg)
        state = torch.load(gpt_path, map_location="cpu", weights_only=True)
        # v2 checkpoint 可能有 emo 相關的額外 keys
        model.load_state_dict(state, strict=False)
        model.eval().to(self.device)
        self.model = model
        print("  GPT 模型載入完成（用於 conditioning 提取）")

    @torch.no_grad()
    def extract(self, mel: torch.Tensor) -> np.ndarray:
        """
        Args:
            mel: (1, n_mels, T) mel spectrogram

        Returns:
            condition: (32, dim) conditioning vector
        """
        mel = mel.to(self.device)
        mel_length = torch.tensor([mel.shape[-1]], device=self.device)
        condition = self.model.get_conditioning(mel, mel_length)
        return condition.squeeze(0).cpu().float().numpy()


def parse_audio_line(line: str) -> Tuple[str, str]:
    """解析 audio_list 的一行"""
    line = line.strip()
    if "\t" in line:
        return line.split("\t", 1)
    elif "|" in line:
        return line.split("|", 1)
    raise ValueError(f"不支援的格式: {line}")


def main():
    parser = argparse.ArgumentParser(description="IndexTTS2 (v2) 語音特徵提取")
    parser.add_argument("--audio_list", required=True, help="音頻列表 (TSV: path\\ttext)")
    parser.add_argument("--output_dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--extract_condition", action="store_true",
                        help="同時提取 conditioning vector（需要載入 GPT 模型）")
    parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    args = parser.parse_args()

    # ── 建立輸出目錄 ──
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    list_name = Path(args.audio_list).stem
    output_dir = os.path.join(args.output_dir, f"{list_name}_{timestamp}")
    os.makedirs(output_dir, exist_ok=True)

    print("=" * 60)
    print("  IndexTTS2 (v2) 語音特徵提取")
    print("=" * 60)
    print(f"  音頻列表: {args.audio_list}")
    print(f"  輸出目錄: {output_dir}")
    print(f"  設備: {args.device}")
    print(f"  提取 conditioning: {args.extract_condition}")
    print()

    # ── 載入模型 ──
    print("載入模型...")
    codec_extractor = SemanticCodeExtractor(device=args.device)
    mel_extractor = MelExtractor()

    condition_extractor = None
    if args.extract_condition:
        print("  載入 GPT 模型（conditioning 提取用）...")
        condition_extractor = ConditionExtractor(device=args.device)
        config_path = os.path.join(CHECKPOINT_DIR, "config.yaml")
        gpt_path = os.path.join(CHECKPOINT_DIR, "gpt.pth")
        try:
            condition_extractor.load_model(config_path, gpt_path)
        except Exception as e:
            print(f"  ⚠️  GPT 載入失敗: {e}")
            print("  將跳過 conditioning 提取")
            condition_extractor = None

    print()

    # ── 讀取音頻列表 ──
    with open(args.audio_list, "r", encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip()]

    print(f"共 {len(lines)} 個音頻檔案")
    print()

    # ── 處理每個音頻 ──
    metadata_path = os.path.join(output_dir, "metadata.jsonl")
    condition_files = []
    success = 0
    fail = 0

    for line in tqdm(lines, desc="提取特徵"):
        try:
            wav_path, text = parse_audio_line(line)
        except ValueError as e:
            print(f"  跳過: {e}")
            fail += 1
            continue

        if not os.path.exists(wav_path):
            print(f"  找不到: {wav_path}")
            fail += 1
            continue

        try:
            # 載入音頻
            audio, sr = torchaudio.load(wav_path)

            # 1. Semantic codes
            codes = codec_extractor.extract_codes(audio, sr)
            codes_np = codes.cpu().numpy()

            # 2. Mel spectrogram
            mel = mel_extractor.extract(audio, sr)
            mel_np = mel.cpu().numpy()

            # 3. Duration
            audio_24k = audio
            if sr != 24000:
                audio_24k = torchaudio.functional.resample(audio, sr, 24000)
            duration = audio_24k.shape[-1] / 24000

            # 4. 保存
            base_name = os.path.basename(wav_path)
            codes_path = os.path.join(output_dir, f"{base_name}_codes.npy")
            mel_path = os.path.join(output_dir, f"{base_name}_mel.npy")
            np.save(codes_path, codes_np)
            np.save(mel_path, mel_np)

            # 5. Conditioning (optional)
            condition_path = None
            if condition_extractor is not None:
                condition = condition_extractor.extract(mel)
                condition_path = os.path.join(output_dir, f"{base_name}_condition.npy")
                np.save(condition_path, condition)
                condition_files.append(condition_path)

            # 6. Metadata
            entry = {
                "audio": os.path.abspath(wav_path),
                "text": text,
                "codes": os.path.abspath(codes_path),
                "mels": os.path.abspath(mel_path),
                "duration": round(duration, 4),
            }
            if condition_path:
                entry["condition"] = os.path.abspath(condition_path)

            with open(metadata_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

            success += 1

        except Exception as e:
            print(f"  失敗 ({wav_path}): {e}")
            import traceback
            traceback.print_exc()
            fail += 1

    print()
    print(f"提取完成: 成功 {success}, 失敗 {fail}")

    # ── 分割 train/valid ──
    if success > 0:
        with open(metadata_path, "r", encoding="utf-8") as f:
            all_lines = f.readlines()

        np.random.shuffle(all_lines)
        valid_size = max(1, int(len(all_lines) * (1 - TRAIN_SPLIT_RATIO)))
        train_lines = all_lines[:-valid_size]
        valid_lines = all_lines[-valid_size:]

        train_path = os.path.join(output_dir, "metadata_train.jsonl")
        valid_path = os.path.join(output_dir, "metadata_valid.jsonl")
        with open(train_path, "w", encoding="utf-8") as f:
            f.writelines(train_lines)
        with open(valid_path, "w", encoding="utf-8") as f:
            f.writelines(valid_lines)

        print(f"  訓練集: {len(train_lines)} 條 → {train_path}")
        print(f"  驗證集: {len(valid_lines)} 條 → {valid_path}")

    # ── Medoid conditioning ──
    if condition_files and len(condition_files) > 1:
        print("\n計算 medoid conditioning...")
        conditions = [np.load(f) for f in condition_files]
        conditions_array = np.stack(conditions)
        N = conditions_array.shape[0]
        flat = conditions_array.reshape(N, -1)

        # 簡化版 medoid：用平均距離
        dist_sums = np.zeros(N)
        for i in range(N):
            dist_sums[i] = np.sum(np.linalg.norm(flat - flat[i], axis=1))
        medoid_idx = np.argmin(dist_sums)
        medoid_path = os.path.join(output_dir, "medoid_condition.npy")
        np.save(medoid_path, conditions[medoid_idx])
        print(f"  Medoid: index {medoid_idx} → {medoid_path}")

    # ── Speaker info ──
    speaker_info = {
        "speaker": list_name,
        "sample_num": success,
        "total_duration_in_seconds": sum(
            json.loads(l)["duration"] for l in open(metadata_path, encoding="utf-8")
        ),
        "train_jsonl": os.path.abspath(os.path.join(output_dir, "metadata_train.jsonl")),
        "valid_jsonl": os.path.abspath(os.path.join(output_dir, "metadata_valid.jsonl")),
    }
    if condition_files:
        speaker_info["medoid_condition"] = os.path.abspath(os.path.join(output_dir, "medoid_condition.npy"))

    speaker_info["total_duration_in_minutes"] = speaker_info["total_duration_in_seconds"] / 60
    speaker_info["avg_duration"] = speaker_info["total_duration_in_seconds"] / max(success, 1)

    speaker_info_path = os.path.join(output_dir, "speaker_info.json")
    with open(speaker_info_path, "w", encoding="utf-8") as f:
        json.dump([speaker_info], f, ensure_ascii=False, indent=2)

    print()
    print("=" * 60)
    print(f"  完成！")
    print(f"  輸出: {output_dir}")
    print(f"  語料: {success} 條, {speaker_info['total_duration_in_minutes']:.1f} 分鐘")
    print("=" * 60)


if __name__ == "__main__":
    main()
