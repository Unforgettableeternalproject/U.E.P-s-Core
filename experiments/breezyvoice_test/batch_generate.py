"""
BreezyVoice 批次生成腳本：從指定行號開始，為訓練句子生成台灣腔音檔。

用法（從 experiments/breezyvoice_test/ 目錄，用 BreezyVoice 的 venv）：
    python batch_generate.py --start 100 --end 339

會生成 tw_0100_builtin.wav ~ tw_0338_builtin.wav 到 lora_training_data/，
並且更新 audio_list.txt。
"""

import argparse
import os
import sys
import time

# Force UTF-8 on Windows — g2pw and other libraries don't specify encoding
os.environ["PYTHONUTF8"] = "1"
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# Setup paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
BREEZY_ROOT = os.environ.get("BREEZY_ROOT", os.path.join(SCRIPT_DIR, "BreezyVoice"))

sys.path.insert(0, BREEZY_ROOT)
sys.path.insert(0, os.path.join(BREEZY_ROOT, "third_party", "Matcha-TTS"))

SENTENCES_FILE = os.path.join(PROJECT_ROOT, "experiments", "index-tts-lora", "uep_tts_training_sentences_500.txt")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "experiments", "lora_training_data")
AUDIO_LIST = os.path.join(OUTPUT_DIR, "audio_list.txt")

TARGET_SR = 24000  # IndexTTS2 訓練期望的 sample rate
BREEZY_SR = 22050  # BreezyVoice 輸出 sample rate


def main():
    parser = argparse.ArgumentParser(description="BreezyVoice 批次生成台灣腔訓練語料")
    parser.add_argument("--start", type=int, default=100, help="起始行號 (0-indexed)")
    parser.add_argument("--end", type=int, default=None, help="結束行號 (不含，None=到最後)")
    parser.add_argument("--model_path", type=str, default="MediaTek-Research/BreezyVoice-300M")
    parser.add_argument("--reference_audio", type=str, default=None,
                        help="參考音檔 (None = BreezyVoice 內建)")
    parser.add_argument("--reference_text", type=str, default=None,
                        help="參考音檔轉寫 (None = 內建)")
    parser.add_argument("--dry-run", action="store_true", help="只列出要生成的句子，不實際生成")
    args = parser.parse_args()

    # Read sentences
    with open(SENTENCES_FILE, "r", encoding="utf-8") as f:
        sentences = [line.strip() for line in f if line.strip()]

    total = len(sentences)
    start = args.start
    end = args.end or total
    end = min(end, total)

    print(f"📋 總句數: {total}")
    print(f"📋 生成範圍: [{start}, {end}) = {end - start} 句")
    print(f"📁 輸出目錄: {OUTPUT_DIR}")
    print()

    if args.dry_run:
        for i in range(start, end):
            fname = f"tw_{i:04d}_builtin.wav"
            print(f"  [{i:04d}] {sentences[i][:50]}{'...' if len(sentences[i]) > 50 else ''} -> {fname}")
        print(f"\n(dry-run, 共 {end - start} 句)")
        return

    # ── Load BreezyVoice ──
    import torch
    import torchaudio

    torch.set_num_threads(4)
    print(f"🔧 PyTorch: {torch.__version__}, CUDA: {torch.cuda.is_available()}")
    if torch.cuda.is_available():
        print(f"🔧 GPU: {torch.cuda.get_device_name(0)}")

    print("📦 載入 BreezyVoice...")
    t0 = time.perf_counter()

    from single_inference import CustomCosyVoice, get_bopomofo_rare
    from g2pw import G2PWConverter
    from cosyvoice.utils.file_utils import load_wav

    cosyvoice = CustomCosyVoice(args.model_path)
    bopomofo_converter = G2PWConverter()
    print(f"✅ 模型載入完成 ({time.perf_counter() - t0:.1f}s)\n")

    # Reference audio
    ref_audio_path = args.reference_audio or os.path.join(BREEZY_ROOT, "data", "example.wav")
    ref_text = args.reference_text or "在密碼學中，加密是將明文資訊改變為難以讀取的密文內容，使之不可讀的方法。只有擁有解密方法的對象，經由解密過程才能將密文還原為正常可讀的內容。"

    prompt_speech_16k = load_wav(ref_audio_path, 16000)
    ref_text_norm = cosyvoice.frontend.text_normalize_new(ref_text, split=False)
    ref_text_bopomo = get_bopomofo_rare(ref_text_norm, bopomofo_converter)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # ── Batch generate ──
    success = 0
    failed = []
    total_audio_sec = 0.0
    total_infer_sec = 0.0

    for i in range(start, end):
        text = sentences[i]
        fname = f"tw_{i:04d}_builtin.wav"
        out_path = os.path.join(OUTPUT_DIR, fname)

        # Skip if already exists
        if os.path.exists(out_path):
            info = torchaudio.info(out_path)
            if info.num_frames > 0:
                print(f"  [{i:04d}] ⏭️  已存在，跳過")
                success += 1
                continue

        print(f"  [{i:04d}/{end-1}] {text[:40]}{'...' if len(text) > 40 else ''}", end="", flush=True)

        try:
            synth_text_norm = cosyvoice.frontend.text_normalize_new(text, split=False)
            synth_text_bopomo = get_bopomofo_rare(synth_text_norm, bopomofo_converter)

            t0 = time.perf_counter()
            output = cosyvoice.inference_zero_shot_no_normalize(
                synth_text_bopomo, ref_text_bopomo, prompt_speech_16k
            )
            infer_time = time.perf_counter() - t0

            audio = output['tts_speech']  # [1, samples] @ 22050

            # Resample to 24kHz for IndexTTS2 training
            if BREEZY_SR != TARGET_SR:
                audio = torchaudio.functional.resample(audio, BREEZY_SR, TARGET_SR)

            torchaudio.save(out_path, audio, TARGET_SR)

            audio_sec = audio.shape[1] / TARGET_SR
            total_audio_sec += audio_sec
            total_infer_sec += infer_time
            success += 1

            print(f"  ✅ {audio_sec:.1f}s ({infer_time:.1f}s)")

        except Exception as e:
            failed.append((i, text, str(e)))
            print(f"  ❌ {e}")

        # Periodic VRAM cleanup
        if (i - start) % 50 == 49:
            torch.cuda.empty_cache()

    # ── Update audio_list.txt ──
    print(f"\n📝 更新 audio_list.txt...")
    entries = []
    for i in range(0, end):
        fname = f"tw_{i:04d}_builtin.wav"
        fpath = os.path.join(OUTPUT_DIR, fname)
        if os.path.exists(fpath):
            entries.append(f"{fpath}\t{sentences[i]}")

    with open(AUDIO_LIST, "w", encoding="utf-8") as f:
        f.write("\n".join(entries) + "\n")
    print(f"   ✓ {len(entries)} 筆記錄寫入")

    # ── Summary ──
    print(f"\n{'='*50}")
    print(f"  完成！成功: {success}, 失敗: {len(failed)}")
    if total_audio_sec > 0:
        print(f"  總音頻: {total_audio_sec/60:.1f} 分鐘")
        print(f"  總推論: {total_infer_sec/60:.1f} 分鐘")
        print(f"  平均 RTF: {total_infer_sec/total_audio_sec:.2f}x")
    if failed:
        print(f"\n  失敗清單:")
        for idx, text, err in failed:
            print(f"    [{idx:04d}] {text[:30]}... -> {err}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
