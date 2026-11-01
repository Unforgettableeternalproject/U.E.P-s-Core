# -*- coding: utf-8 -*-
import sys, io, argparse, os
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Parse our args IMMEDIATELY before any project imports can steal sys.argv
# (deprecated config.py calls argparse.parse_args() at module init)
_parser = argparse.ArgumentParser(description="Generate audio with Edge TTS + RVC")
_parser.add_argument("--voice", choices=["normal", "sweet"], default="normal")
_parser.add_argument("--text", type=str, default=None)
_parser.add_argument("--output", type=str, default=None)
_parser.add_argument("--edge-voice", type=str, default="en-US-AvaNeural")
_parser.add_argument("--f0-up-key", type=int, default=0)
_ARGS = _parser.parse_args()
# Clear argv so deprecated config.py's argparse won't choke
sys.argv = [sys.argv[0]]

"""
Quick script: Generate audio using Edge TTS + RVC (deprecated pipeline)

Usage:
    python devtools/generate_rvc_voice.py --voice normal --text "..." --output output.wav
    python devtools/generate_rvc_voice.py --voice sweet --text "..." --output output.wav
    python devtools/generate_rvc_voice.py --voice normal  (uses default long text, ~1 min)

Must be run from U.E.P-s-Core root directory.
"""
import asyncio
import time

# Add project root to path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Default text (~1 minute of speech)
DEFAULT_TEXT = """
Bernie, I will use this paragraph to test the voice quality and stability of the old RVC pipeline.
This is a comprehensive test that covers various sentence structures, including short phrases,
medium-length sentences, and longer passages with multiple clauses.

The weather today is quite pleasant. I hope you are having a wonderful day.
Sometimes, when we look back at the choices we have made, we realize that every small decision
has led us to where we are now. That is both terrifying and beautiful at the same time.

Let me also test some emotional range. I am so happy to see you again!
But wait, did something go wrong? Oh no, that is terrible news.
Well, regardless of what happens, we will get through this together.
After all, that is what friends are for, is it not?

Technology continues to advance at an incredible pace. Artificial intelligence,
machine learning, and neural networks are reshaping how we interact with the world.
The future holds both promise and uncertainty, but one thing is clear:
we must adapt and grow alongside these changes.
""".strip()


def find_rvc_weights(voice_name: str):
    """Find RVC weight files for the given voice name."""
    base = os.path.join(ROOT, "models", "tts")
    deprecated = os.path.join(base, "deprecated_rvc")
    alt = os.path.join(deprecated, "RVC_weights", "Could change to this")

    # Search order: deprecated_rvc root -> alt folder -> models/tts root
    search_dirs = [deprecated, alt, base]

    pth_file = None
    index_file = None

    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            lower = f.lower()
            if voice_name.lower() in lower:
                full = os.path.join(d, f)
                if f.endswith(".pth") and pth_file is None:
                    pth_file = full
                elif f.endswith(".index") and index_file is None:
                    index_file = full

    return pth_file, index_file


async def main():
    args = _ARGS

    text = args.text or DEFAULT_TEXT
    voice_name = f"U.E.P_{args.voice}"

    # Find weights
    pth_file, index_file = find_rvc_weights(args.voice)

    if not pth_file:
        print(f"❌ Could not find RVC weights for '{args.voice}'")
        print(f"   Searched in: models/tts/deprecated_rvc/")
        print(f"   Expected: U.E.P_{args.voice}.pth")
        sys.exit(1)

    print(f"🎤 Voice: {voice_name}")
    print(f"   .pth:   {pth_file}")
    print(f"   .index: {index_file or '(none)'}")
    print(f"   Edge:   {args.edge_voice}")
    print(f"   Text:   {len(text)} chars")

    # Output path
    output = args.output or os.path.join(ROOT, "outputs", "tts", f"rvc_{args.voice}.wav")
    os.makedirs(os.path.dirname(output), exist_ok=True)

    # Import the deprecated pipeline
    # Clear sys.argv BEFORE import — deprecated config.py calls argparse at module init
    original_argv = sys.argv[:]
    sys.argv = [sys.argv[0]]
    try:
        from modules.tts_module.deprecated.infer_core import run_tts
    except ImportError as e:
        sys.argv = original_argv
        print(f"Import error: {e}")
        print("   Make sure you're running from U.E.P-s-Core root")
        print("   Required packages: edge-tts, librosa, faiss-cpu, pyworld, torchcrepe, soundfile")
        sys.exit(1)
    sys.argv = original_argv

    print(f"\n🚀 Starting Edge TTS + RVC pipeline...")
    t0 = time.perf_counter()

    result = await run_tts(
        tts_text=text,
        model_name=os.path.basename(pth_file),
        model_path=os.path.dirname(pth_file),
        index_file=None,  # skip faiss index — often incompatible
        output_path=output,
        f0_up_key=args.f0_up_key,
        f0_method="rmvpe",
        index_rate=0,
        protect=0.33,
        voice=args.edge_voice,
    )

    elapsed = time.perf_counter() - t0

    if result["status"] == "success":
        print(f"\n✅ Done in {elapsed:.1f}s")
        print(f"   Output: {result['output_path']}")
        print(f"\n💡 To create an IndexTTS2 character file from this audio:")
        print(f"   Use the upstream IndexTTS2 feature extraction on this WAV")
    else:
        print(f"\n❌ Failed: {result['message']}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
