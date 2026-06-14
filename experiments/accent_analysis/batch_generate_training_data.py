"""
BreezyVoice 批量生成台灣腔訓練語料

用多個 reference 語音輪流合成，產出 LoRA 訓練用的語料。
輸出格式符合 index-tts-lora 的 extract_codec.py 需求：
  audio.wav\t文字

用法：
  cd experiments/breezyvoice_test
  python ../accent_analysis/batch_generate_training_data.py

注意：必須在 breezyvoice_test 目錄下執行（或設定 BREEZY_ROOT）。
      使用 breezyvoice_test 的 .venv 環境。
"""

import argparse
import os
import sys
import time
import traceback

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
BREEZY_DIR = os.path.join(PROJECT_ROOT, "experiments", "breezyvoice_test")
BREEZY_ROOT = os.path.join(BREEZY_DIR, "BreezyVoice")

sys.path.insert(0, BREEZY_ROOT)
sys.path.insert(0, os.path.join(BREEZY_ROOT, "third_party", "Matcha-TTS"))

OUTPUT_DIR = os.path.join(PROJECT_ROOT, "experiments", "lora_training_data")
SENTENCES_FILE = os.path.join(SCRIPT_DIR, "training_sentences.txt")

# Reference 語音樣本（不同情緒/風格，增加多樣性）
REFERENCE_SAMPLES = [
    {
        "audio": os.path.join(BREEZY_DIR, "sample_01_calm_discussion.wav"),
        "text": None,  # 會用 Whisper 轉寫
        "label": "calm",
    },
    {
        "audio": os.path.join(BREEZY_DIR, "sample_02_excited_chat.wav"),
        "text": None,
        "label": "excited",
    },
    {
        "audio": os.path.join(BREEZY_DIR, "sample_03_narrative_long.wav"),
        "text": None,
        "label": "narrative",
    },
    {
        "audio": os.path.join(BREEZY_DIR, "sample_04_phone_call.wav"),
        "text": None,
        "label": "phone",
    },
    {
        "audio": os.path.join(BREEZY_DIR, "sample_05_melancholic.wav"),
        "text": None,
        "label": "melancholic",
    },
]

# 內建 reference（BreezyVoice 自帶的範例）
BUILTIN_REFERENCE = {
    "audio": os.path.join(BREEZY_ROOT, "data", "example.wav"),
    "text": "在密碼學中，加密是將明文資訊改變為難以讀取的密文內容，使之不可讀的方法。只有擁有解密方法的對象，經由解密過程才能將密文還原為正常可讀的內容。",
    "label": "builtin",
}


def load_sentences(path: str) -> list:
    """載入訓練句子"""
    with open(path, "r", encoding="utf-8") as f:
        sentences = [line.strip() for line in f if line.strip()]
    return sentences


def main():
    parser = argparse.ArgumentParser(description="BreezyVoice 批量生成台灣腔訓練語料")
    parser.add_argument("--model_path", default="MediaTek-Research/BreezyVoice-300M")
    parser.add_argument("--max_sentences", type=int, default=100,
                        help="最多生成幾句（用於測試時縮小範圍）")
    parser.add_argument("--start_from", type=int, default=0,
                        help="從第幾句開始（用於中斷恢復）")
    parser.add_argument("--use_builtin_ref", action="store_true",
                        help="只用 BreezyVoice 內建 reference（不用自訂樣本）")
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    import torch
    torch.set_num_threads(4)
    import torchaudio

    print(f"PyTorch: {torch.__version__}")
    print(f"CUDA: {torch.cuda.is_available()}")
    if torch.cuda.is_available():
        print(f"GPU: {torch.cuda.get_device_name(0)}")
    print(f"BreezyVoice root: {BREEZY_ROOT}")
    print()

    # ── 載入 BreezyVoice ──
    print("載入 BreezyVoice 模型...")
    t0 = time.perf_counter()

    from single_inference import CustomCosyVoice, get_bopomofo_rare
    from g2pw import G2PWConverter
    from cosyvoice.utils.file_utils import load_wav

    cosyvoice = CustomCosyVoice(args.model_path)
    bopomofo_converter = G2PWConverter()

    load_time = time.perf_counter() - t0
    print(f"模型載入完成 ({load_time:.1f}s)")
    print()

    # ── 準備 reference 語音 ──
    references = []
    if args.use_builtin_ref:
        ref = BUILTIN_REFERENCE
        prompt_speech = load_wav(ref["audio"], 16000)
        ref_text_norm = cosyvoice.frontend.text_normalize_new(ref["text"], split=False)
        ref_text_bopomo = get_bopomofo_rare(ref_text_norm, bopomofo_converter)
        references.append({
            "speech": prompt_speech,
            "text_bopomo": ref_text_bopomo,
            "label": ref["label"],
        })
    else:
        # 先用內建 reference 做第一個（最穩定）
        ref = BUILTIN_REFERENCE
        prompt_speech = load_wav(ref["audio"], 16000)
        ref_text_norm = cosyvoice.frontend.text_normalize_new(ref["text"], split=False)
        ref_text_bopomo = get_bopomofo_rare(ref_text_norm, bopomofo_converter)
        references.append({
            "speech": prompt_speech,
            "text_bopomo": ref_text_bopomo,
            "label": ref["label"],
        })

        # 加入自訂 reference（用 Whisper 轉寫）
        for ref in REFERENCE_SAMPLES:
            if not os.path.exists(ref["audio"]):
                print(f"  跳過不存在的 reference: {ref['audio']}")
                continue
            try:
                prompt_speech = load_wav(ref["audio"], 16000)

                # 如果沒有提供轉寫文字，用 Whisper
                if ref["text"] is None:
                    print(f"  Whisper 轉寫: {ref['label']}...")
                    from single_inference import transcribe_audio
                    ref_text = transcribe_audio(ref["audio"])
                    print(f"    → {ref_text[:50]}...")
                else:
                    ref_text = ref["text"]

                ref_text_norm = cosyvoice.frontend.text_normalize_new(ref_text, split=False)
                ref_text_bopomo = get_bopomofo_rare(ref_text_norm, bopomofo_converter)
                references.append({
                    "speech": prompt_speech,
                    "text_bopomo": ref_text_bopomo,
                    "label": ref["label"],
                })
                print(f"  已載入 reference: {ref['label']}")
            except Exception as e:
                print(f"  載入 reference 失敗 ({ref['label']}): {e}")
                continue

    print(f"\n共 {len(references)} 個 reference 語音")
    print()

    # ── 載入句子 ──
    sentences = load_sentences(SENTENCES_FILE)
    total = min(len(sentences), args.max_sentences)
    sentences = sentences[args.start_from : args.start_from + total]
    print(f"準備生成 {len(sentences)} 句（從第 {args.start_from} 句開始）")
    print()

    # ── 批量生成 ──
    audio_list_path = os.path.join(OUTPUT_DIR, "audio_list.txt")
    success_count = 0
    fail_count = 0
    total_audio_duration = 0.0

    with open(audio_list_path, "a" if args.start_from > 0 else "w", encoding="utf-8") as audio_list_f:
        for i, sentence in enumerate(sentences):
            idx = args.start_from + i
            # 輪流使用不同 reference
            ref = references[idx % len(references)]

            print(f"[{idx+1:03d}/{args.start_from + total}] ({ref['label']}) {sentence}")

            try:
                # 前處理
                synth_text_norm = cosyvoice.frontend.text_normalize_new(sentence, split=False)
                synth_text_bopomo = get_bopomofo_rare(synth_text_norm, bopomofo_converter)

                # 合成
                t0 = time.perf_counter()
                output = cosyvoice.inference_zero_shot_no_normalize(
                    synth_text_bopomo, ref["text_bopomo"], ref["speech"]
                )
                gen_time = time.perf_counter() - t0

                audio = output["tts_speech"]
                duration = audio.shape[1] / 22050

                # 保存（24kHz，符合 IndexTTS2 的訓練需求）
                # 先 resample 到 24kHz
                audio_24k = torchaudio.functional.resample(audio, 22050, 24000)
                filename = f"tw_{idx:04d}_{ref['label']}.wav"
                output_path = os.path.join(OUTPUT_DIR, filename)
                torchaudio.save(output_path, audio_24k, 24000)

                # 寫入 audio list
                audio_list_f.write(f"{os.path.abspath(output_path)}\t{sentence}\n")
                audio_list_f.flush()

                total_audio_duration += duration
                success_count += 1
                print(f"  OK {duration:.1f}s (gen: {gen_time:.1f}s) → {filename}")

            except Exception as e:
                fail_count += 1
                print(f"  FAIL: {e}")
                traceback.print_exc()

    # ── 摘要 ──
    print()
    print("=" * 60)
    print(f"  批量生成完成")
    print(f"  成功: {success_count}, 失敗: {fail_count}")
    print(f"  總語音時長: {total_audio_duration:.1f}s ({total_audio_duration/60:.1f} min)")
    print(f"  音檔目錄: {OUTPUT_DIR}")
    print(f"  Audio list: {audio_list_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
