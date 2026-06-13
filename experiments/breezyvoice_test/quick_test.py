"""
BreezyVoice 快速推論測試
用法：python quick_test.py [--text "你要合成的文字"] [--model_path "模型路徑"]

必須從 BreezyVoice/ 目錄下執行，或設定 BREEZY_ROOT 環境變數。
"""

import argparse
import os
import sys
import time

# Setup paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BREEZY_ROOT = os.environ.get("BREEZY_ROOT", os.path.join(SCRIPT_DIR, "BreezyVoice"))
sys.path.insert(0, BREEZY_ROOT)
sys.path.insert(0, os.path.join(BREEZY_ROOT, "third_party", "Matcha-TTS"))

import torch
torch.set_num_threads(4)  # 多給一些 threads

import torchaudio

print(f"🔧 PyTorch: {torch.__version__}")
print(f"🔧 CUDA available: {torch.cuda.is_available()}")
if torch.cuda.is_available():
    print(f"🔧 GPU: {torch.cuda.get_device_name(0)}")
print(f"🔧 BreezyVoice root: {BREEZY_ROOT}")
print()


def main():
    parser = argparse.ArgumentParser(description="BreezyVoice 快速推論測試")
    parser.add_argument("--text", type=str, default="知道為什麼鴨嘴獸泰瑞都不會被認出來嗎",
                        help="要合成的文本")
    parser.add_argument("--reference_audio", type=str, default=None,
                        help="參考音檔路徑 (None = 用內建範例)")
    parser.add_argument("--reference_text", type=str, default=None,
                        help="參考音檔的文字轉寫 (None = 用內建範例)")
    parser.add_argument("--model_path", type=str, default="MediaTek-Research/BreezyVoice-300M",
                        help="模型路徑或 HuggingFace ID")
    parser.add_argument("--output", type=str, default="output_breezy_test.wav",
                        help="輸出音檔路徑")
    args = parser.parse_args()

    # Default reference audio
    if args.reference_audio is None:
        args.reference_audio = os.path.join(BREEZY_ROOT, "data", "example.wav")
    if args.reference_text is None:
        args.reference_text = "在密碼學中，加密是將明文資訊改變為難以讀取的密文內容，使之不可讀的方法。只有擁有解密方法的對象，經由解密過程才能將密文還原為正常可讀的內容。"

    if not os.path.exists(args.reference_audio):
        print(f"❌ 參考音檔不存在: {args.reference_audio}")
        sys.exit(1)

    # ── Step 1: 載入模型 ──
    print("📦 載入 BreezyVoice 模型...")
    t0 = time.perf_counter()

    from single_inference import CustomCosyVoice, get_bopomofo_rare
    from g2pw import G2PWConverter
    from cosyvoice.utils.file_utils import load_wav

    cosyvoice = CustomCosyVoice(args.model_path)
    bopomofo_converter = G2PWConverter()

    load_time = time.perf_counter() - t0
    print(f"✅ 模型載入完成 ({load_time:.1f}s)")
    print()

    # ── Step 2: 前處理 ──
    print(f"📝 合成文本: \"{args.text}\"")
    print(f"🎤 參考音檔: {args.reference_audio}")

    prompt_speech_16k = load_wav(args.reference_audio, 16000)

    # Normalize
    ref_text_norm = cosyvoice.frontend.text_normalize_new(args.reference_text, split=False)
    synth_text_norm = cosyvoice.frontend.text_normalize_new(args.text, split=False)

    # Bopomofo annotation
    ref_text_bopomo = get_bopomofo_rare(ref_text_norm, bopomofo_converter)
    synth_text_bopomo = get_bopomofo_rare(synth_text_norm, bopomofo_converter)

    print(f"   注音標註: {synth_text_bopomo}")
    print()

    # ── Step 3: 推論 ──
    print("🔊 開始推論...")
    t0 = time.perf_counter()

    output = cosyvoice.inference_zero_shot_no_normalize(
        synth_text_bopomo, ref_text_bopomo, prompt_speech_16k
    )

    infer_time = time.perf_counter() - t0
    audio_duration = output['tts_speech'].shape[1] / 22050

    print(f"✅ 推論完成!")
    print(f"   推論時間: {infer_time:.2f}s")
    print(f"   音頻時長: {audio_duration:.2f}s")
    print(f"   RTF: {infer_time / audio_duration:.2f}x")
    print()

    # ── Step 4: 保存 ──
    output_path = os.path.join(SCRIPT_DIR, args.output)
    torchaudio.save(output_path, output['tts_speech'], 22050)
    print(f"💾 已保存: {output_path}")


if __name__ == "__main__":
    main()
