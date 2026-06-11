from __future__ import annotations

import argparse
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

try:
    from rich.console import Console
except ModuleNotFoundError:
    class Console:  # type: ignore[no-redef]
        def print(self, message):
            print(message)

from test_tts.benchmarks.metrics import LatencySample, summarize_latency
from test_tts.runtime.backend_contract import legacy_backend_info
from test_tts.runtime.legacy_generate import generate_with_legacy_indextts2

console = Console()


def smoke() -> int:
    info = legacy_backend_info(Path(__file__).resolve().parent)
    console.print("TTS experiment scaffold is available.")
    console.print(f"Backend: {info.name}")
    console.print(f"Legacy source: {info.source_path}")
    console.print(f"Emotion vectors: {info.supports_emotion_vector}")
    return 0


def benchmark(text: str) -> int:
    # Placeholder benchmark for infrastructure validation. Real timings will be
    # populated once the IndexTTS2 pipeline is instrumented.
    sample = LatencySample(
        text_chars=len(text),
        audio_duration_ms=1000.0,
        gpt_ms=0.0,
        s2mel_ms=0.0,
        vocoder_ms=0.0,
        first_audio_ms=0.0,
    )
    console.print(summarize_latency([sample]))
    return 0


def generate(text: str, output: str, device: str | None, use_fp16: bool, use_cuda_kernel: bool) -> int:
    output_path = generate_with_legacy_indextts2(
        text=text,
        output_path=output,
        project_root=Path(__file__).resolve().parent,
        device=device,
        use_fp16=use_fp16,
        use_cuda_kernel=use_cuda_kernel,
    )
    console.print(f"Generated audio: {output_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="TTS latency experiment CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("smoke", help="Verify scaffold and copied legacy source paths")

    bench = subparsers.add_parser("benchmark", help="Run a placeholder benchmark")
    bench.add_argument("--text", default="Hello from the TTS experiment project.")

    gen = subparsers.add_parser("generate", help="Generate speech through copied legacy IndexTTS2")
    gen.add_argument("--text", required=True)
    gen.add_argument("--output", default="outputs/tts/generated.wav")
    gen.add_argument("--device", default="cuda")
    gen.add_argument("--no-fp16", action="store_true")
    gen.add_argument("--no-cuda-kernel", action="store_true")

    args = parser.parse_args()
    if args.command == "smoke":
        return smoke()
    if args.command == "benchmark":
        return benchmark(args.text)
    if args.command == "generate":
        return generate(
            text=args.text,
            output=args.output,
            device=args.device,
            use_fp16=not args.no_fp16,
            use_cuda_kernel=not args.no_cuda_kernel,
        )
    raise ValueError(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())

