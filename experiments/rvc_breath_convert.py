"""
RVC Breath Conversion

Convert generic breath SFX to UEP's voice using the RVC pipeline
from U.E.P-s-Core. Run from the U.E.P-s-Core root directory.

Usage:
    python experiments/rvc_breath_convert.py --batch
    python experiments/rvc_breath_convert.py --input path/to/breath.wav --pitch 10
"""

import os
import sys
import argparse
from pathlib import Path

# ── 路徑設定 ──
UEP_CORE_ROOT = Path(r"C:\Users\Bernie\source\repos\Unforgettableeternalproject\U.E.P-s-Core")
TEST_TTS_ROOT = Path(r"C:\Users\Bernie\source\repos\Unforgettableeternalproject\TestSeparateTTSSystem")
SFX_DIR = TEST_TTS_ROOT / "echo_tts" / "modules" / "tts_module" / "sfx"
OUTPUT_DIR = SFX_DIR / "rvc_converted"

# RVC model
MODEL_DIR = "models/tts/deprecated_rvc"
MODEL_NAME = "U.E.P_sweet.pth"
HUBERT_PATH = "models/tts/hubert_base.pt"
RMVPE_PATH = "models/tts/rmvpe.pt"


def convert_breath(
    input_path: str,
    output_path: str,
    f0_up_key: int = 0,
    f0_method: str = "rmvpe",
    index_rate: float = 0,
    protect: float = 0.33,
    rms_mix_rate: float = 0.25,
    disable_f0: bool = False,
):
    """Run RVC voice conversion on a breath audio file."""
    # Fix Windows console encoding
    if sys.stdout.encoding and sys.stdout.encoding.lower().startswith('cp'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    # Must clear argv before importing U.E.P config (it uses argparse)
    sys.argv = [sys.argv[0]]

    # Setup paths
    sys.path.insert(0, str(UEP_CORE_ROOT))
    original_cwd = os.getcwd()
    os.chdir(str(UEP_CORE_ROOT))

    try:
        import librosa
        import soundfile as sf
        import torch
        from modules.tts_module.deprecated.infer_core import (
            load_model_data,
            get_hubert_model,
            get_rmvpe_model,
            get_config,
        )

        print(f"📂 Input:  {input_path}")
        print(f"📂 Output: {output_path}")
        print(f"🎵 Pitch shift: {f0_up_key} semitones")
        print(f"🎤 F0 method: {f0_method}")
        print(f"🔇 F0 disabled: {disable_f0}")
        print()

        # Load config and model
        print("🔧 Loading RVC model...")
        config = get_config()
        tgt_sr, net_g, vc, version, index_file, if_f0 = load_model_data(
            MODEL_NAME, MODEL_DIR
        )
        print(f"   Model: {MODEL_NAME} (version={version}, sr={tgt_sr}, f0={if_f0})")

        # Load input audio (resample to 16kHz mono for RVC)
        print("🎵 Loading breath audio...")
        audio, _ = librosa.load(input_path, sr=16000, mono=True)
        duration = len(audio) / 16000
        print(f"   Duration: {duration:.2f}s, samples: {len(audio)}")

        # Load Hubert + RMVPE
        print("🧠 Loading Hubert + RMVPE...")
        hubert = get_hubert_model(config.device, config.is_half)
        rmvpe = get_rmvpe_model(str(UEP_CORE_ROOT / RMVPE_PATH), config.device, config.is_half)
        vc.model_rmvpe = rmvpe

        # Run conversion
        print("🔄 Running RVC conversion...")
        # For breathing sounds: generate a flat F0 file so the NSF model
        # gets stable pitch input instead of noisy RMVPE output
        f0_file = None
        if disable_f0:
            import numpy as np
            import tempfile
            # Create a flat F0 at ~220Hz (A3, a neutral female pitch)
            # The pipeline will use this instead of extracting from audio
            hop_length = 160  # RVC default hop
            f0_len = len(audio) // hop_length + 1
            flat_f0 = np.full(f0_len, 220.0, dtype=np.float64)
            f0_file = os.path.join(tempfile.gettempdir(), "poise_flat_f0.npy")
            np.save(f0_file, flat_f0)
            print(f"   🎯 Using flat F0 at 220Hz ({f0_len} frames)")

        with torch.inference_mode():
            audio_out, new_sr = vc.pipeline(
                hubert,
                net_g,
                0,  # speaker_id
                audio,
                input_path,
                [0, 0, 0],  # times
                f0_up_key,
                f0_method,
                index_file if index_rate > 0 else "",
                index_rate,
                if_f0,  # always keep F0 enabled (NSF model requires it)
                3,  # filter_radius
                tgt_sr,
                0,  # resample_sr
                rms_mix_rate,
                version,
                protect,
                f0_file,  # flat F0 when disable_f0=True
            )

        # Save output
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        sf.write(output_path, audio_out, new_sr)

        out_duration = len(audio_out) / new_sr
        print(f"\n✅ Done! Output: {output_path}")
        print(f"   Duration: {out_duration:.2f}s, sample rate: {new_sr}Hz")

    finally:
        os.chdir(original_cwd)


def batch_convert(input_dir: Path, output_dir: Path, **kwargs):
    """Convert all audio files in a directory."""
    extensions = {'.wav', '.mp3', '.flac', '.ogg'}
    files = [f for f in input_dir.iterdir() if f.suffix.lower() in extensions]

    if not files:
        print(f"No audio files found in {input_dir}")
        return

    print(f"Found {len(files)} audio files to convert")
    print(f"Output directory: {output_dir}")
    print("=" * 60)

    for i, f in enumerate(files):
        print(f"\n[{i+1}/{len(files)}] Converting: {f.name}")
        output_path = str(output_dir / f"uep_{f.stem}.wav")
        try:
            convert_breath(str(f), output_path, **kwargs)
        except Exception as e:
            print(f"❌ Failed: {e}")
            import traceback
            traceback.print_exc()


def main():
    parser = argparse.ArgumentParser(description="Convert breath SFX to UEP voice via RVC")
    parser.add_argument("--input", type=str, default=None,
                        help="Input audio file path (default: all files in sfx/)")
    parser.add_argument("--output", type=str, default=None,
                        help="Output audio file path")
    parser.add_argument("--pitch", type=int, default=0,
                        help="Pitch shift in semitones (default: 0)")
    parser.add_argument("--f0-method", type=str, default="rmvpe",
                        choices=["rmvpe", "harvest", "crepe", "pm"],
                        help="F0 extraction method (default: rmvpe)")
    parser.add_argument("--index-rate", type=float, default=0,
                        help="FAISS index rate 0-1 (default: 0, skip index)")
    parser.add_argument("--protect", type=float, default=0.33,
                        help="Voiceless consonant protection 0-0.5 (default: 0.33)")
    parser.add_argument("--rms-mix", type=float, default=0.25,
                        help="RMS envelope mix rate (default: 0.25)")
    parser.add_argument("--no-f0", action="store_true",
                        help="Disable F0 pitch tracking (for non-speech audio like breathing)")
    parser.add_argument("--batch", action="store_true",
                        help="Batch convert all files in sfx/ directory")

    args = parser.parse_args()

    kwargs = dict(
        f0_up_key=args.pitch,
        f0_method=args.f0_method,
        index_rate=args.index_rate,
        protect=args.protect,
        rms_mix_rate=args.rms_mix,
        disable_f0=args.no_f0,
    )

    if args.batch or args.input is None:
        # Batch mode: convert all files in sfx/
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        batch_convert(SFX_DIR, OUTPUT_DIR, **kwargs)
    else:
        # Single file mode
        output = args.output or str(OUTPUT_DIR / f"uep_{Path(args.input).stem}.wav")
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        convert_breath(args.input, output, **kwargs)


if __name__ == "__main__":
    main()
