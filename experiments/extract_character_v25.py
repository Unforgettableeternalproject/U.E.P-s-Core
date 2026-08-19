"""U.E.P 角色特徵萃取（IndexTTS 2.5 管線）。

從正式參考音資產 echo_tts/assets/reference/uep_reference_v25.wav 萃取：
- spk_cond_emb：w2v-bert 語義特徵（merge_emovec / emo conditioning 用）
- style：CAMPPlus speaker embedding [1,192]（GPT campplus conditioning + CFM style）
- prompt_condition：s2mel length_regulator 輸出（CFM prompt）
- ref_mel：參考 mel 頻譜（CFM 用）
- emo_indices：8 個情感類別的最相似說話人索引

輸出 echo_tts/models/tts/uep_v25.pt + uep_v25_metadata.json。
使用上游 infer_v2_5 的萃取邏輯（借上游 IndexTTS2 初始化以載入輔助模型）。

用法：
    python experiments/extract_character_v25.py
"""
import json
import os
import sys
import time

UPSTREAM_REPO = r"C:\Users\Bernie\source\repos\Other\index-tts\index-tts"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CKPT_DIR = os.path.join(PROJECT_ROOT, "echo_tts", "modules", "tts_module", "checkpoints_v25")
REF_WAV = os.path.join(PROJECT_ROOT, "echo_tts", "assets", "reference", "uep_reference_v25.wav")
OUT_DIR = os.path.join(PROJECT_ROOT, "echo_tts", "models", "tts")
OUT_PT = os.path.join(OUT_DIR, "uep_v25.pt")
OUT_META = os.path.join(OUT_DIR, "uep_v25_metadata.json")

sys.path.insert(0, UPSTREAM_REPO)

import transformers.pytorch_utils as _pu

if not hasattr(_pu, "is_torch_greater_or_equal_than_1_13"):
    _pu.is_torch_greater_or_equal_than_1_13 = True

import torch
import torchaudio


def main() -> None:
    from indextts.infer_v2_5 import IndexTTS2, find_most_similar_cosine

    os.makedirs(OUT_DIR, exist_ok=True)
    tts = IndexTTS2(
        cfg_path=os.path.join(CKPT_DIR, "config.yaml"),
        model_dir=CKPT_DIR,
        use_bf16=False,  # 萃取用 fp32，engine 載入時再依 use_fp16 轉換
    )

    t0 = time.perf_counter()
    audio, sr = tts._load_and_cut_audio(REF_WAV, 15)
    audio_22k = torchaudio.transforms.Resample(sr, 22050)(audio)
    audio_16k = torchaudio.transforms.Resample(sr, 16000)(audio)

    inputs = tts.extract_features(audio_16k, sampling_rate=16000, return_tensors="pt")
    input_features = inputs["input_features"].to(tts.device)
    attention_mask = inputs["attention_mask"].to(tts.device)
    spk_cond_emb = tts.get_emb(input_features, attention_mask)

    ref_mel = tts.mel_fn(audio_22k.to(spk_cond_emb.device).float())
    ref_target_lengths = torch.LongTensor([ref_mel.size(2)]).to(ref_mel.device)

    feat = torchaudio.compliance.kaldi.fbank(
        audio_16k.to(ref_mel.device), num_mel_bins=80, dither=0, sample_frequency=16000,
    )
    feat = feat - feat.mean(dim=0, keepdim=True)
    style = tts.campplus_model(feat.unsqueeze(0))

    prompt_condition = tts.s2mel.models['length_regulator'](
        spk_cond_emb, ylens=ref_target_lengths, n_quantizers=3, f0=None,
    )[0]

    emo_indices = [int(find_most_similar_cosine(style, tmp)) for tmp in tts.spk_matrix]
    extraction_s = time.perf_counter() - t0

    features = {
        "spk_cond_emb": spk_cond_emb.cpu(),
        "style": style.cpu(),
        "prompt_condition": prompt_condition.cpu(),
        "ref_mel": ref_mel.cpu(),
        "emo_indices": torch.tensor(emo_indices, dtype=torch.long),
    }
    torch.save(features, OUT_PT)

    metadata = {
        "audio_path": "echo_tts/assets/reference/uep_reference_v25.wav",
        "model_version": 2.5,
        "extraction_time": extraction_s,
        "device": str(tts.device),
        "emo_indices": emo_indices,
        "feature_shapes": {k: list(v.shape) for k, v in features.items()},
        "audio_duration": audio.shape[-1] / sr,
    }
    with open(OUT_META, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    print(f">> 萃取完成 ({extraction_s:.2f}s)")
    for k, v in features.items():
        print(f"   {k}: {list(v.shape)} {v.dtype}")
    print(f">> 已保存: {OUT_PT}")


if __name__ == "__main__":
    main()
