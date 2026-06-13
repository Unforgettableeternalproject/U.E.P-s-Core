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

from test_tts.benchmarks.metrics import LatencySample, summarize_latency, summarize_profiles
from test_tts.cli.interactive import InteractiveShell
from test_tts.runtime.audio import NullAudioPlayer, SimpleAudioPlayer, StreamingAudioPlayer
from test_tts.runtime.backend_contract import legacy_backend_info
from test_tts.runtime.legacy_backend import LegacyIndexTTS2Backend
from test_tts.runtime.legacy_generate import default_generation_paths, generate_with_legacy_indextts2
from test_tts.runtime.session import TTSInteractiveSession

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


def build_legacy_session(
    project_root: str | Path | None = None,
    output_dir: str | Path = "outputs/tts/shell",
    device: str | None = "cuda",
    use_fp16: bool = True,
    use_cuda_kernel: bool = True,
    use_accel: bool = True,
    emotion_vector: list[float] | None = None,
    playback: bool = True,
) -> TTSInteractiveSession:
    root = Path(project_root or Path(__file__).resolve().parent)
    backend = LegacyIndexTTS2Backend(
        paths=default_generation_paths(root),
        device=device,
        use_fp16=use_fp16,
        use_cuda_kernel=use_cuda_kernel,
        use_accel=use_accel,
        emotion_vector=emotion_vector,
    )
    resolved_output = Path(output_dir)
    if not resolved_output.is_absolute():
        resolved_output = root / resolved_output
    player = StreamingAudioPlayer() if playback else NullAudioPlayer()
    return TTSInteractiveSession(backend=backend, output_dir=resolved_output, player=player)


def run_shell(device: str | None, use_fp16: bool, use_cuda_kernel: bool, use_accel: bool, emotion_vector: list[float] | None, playback: bool) -> int:
    session = build_legacy_session(
        project_root=Path(__file__).resolve().parent,
        device=device,
        use_fp16=use_fp16,
        use_cuda_kernel=use_cuda_kernel,
        use_accel=use_accel,
        emotion_vector=emotion_vector,
        playback=playback,
    )

    # Auto-load LoRA on startup (if adapter exists)
    try:
        session.load()  # Ensure engine is loaded first
        session.load_lora()
        lora_status = session.get_lora_status()
        lora_msg = f"🧬 LoRA auto-loaded (scale={lora_status['scale']:.0%})"
    except Exception:
        lora_msg = "🧬 LoRA: not available (adapter not found)"

    shell = InteractiveShell(session=session, write=console.print)
    console.print(
        "TTS shell ready. Commands:\n"
        "  say <text>              — generate and play\n"
        "  save --output <path> <text>\n"
        "  profile <text>          — synthesis with timing report\n"
        "  emotion [preset|H A S F D M U C]\n"
        "  language [en|zh]        — auto-translates mismatched input\n"
        "  lora [on|off|<scale>]   — toggle 台灣腔 LoRA\n"
        "  load / unload / status / help / exit\n"
        f"  {lora_msg}"
    )
    while True:
        try:
            command = input("tts> ")
        except (EOFError, KeyboardInterrupt):
            console.print("")
            break
        try:
            keep_running = shell.execute(command)
        except Exception as exc:
            console.print(f"Error: {exc}")
            keep_running = True
        if not keep_running:
            break
    return 0


def run_profile(text: str, device: str | None, use_fp16: bool, use_cuda_kernel: bool, use_accel: bool, emotion_vector: list[float] | None, repeat: int) -> int:
    session = build_legacy_session(
        project_root=Path(__file__).resolve().parent,
        device=device,
        use_fp16=use_fp16,
        use_cuda_kernel=use_cuda_kernel,
        use_accel=use_accel,
        emotion_vector=emotion_vector,
        playback=False,
    )

    profiles = []
    for i in range(repeat):
        if repeat > 1:
            console.print(f"Run {i + 1}/{repeat}...")
        result = session.profile(text)
        profiles.append(result.report.synthesis)

        # Print full report only for first run (includes init timings)
        if i == 0:
            console.print(result.report.format_report())

    if repeat > 1:
        console.print("")
        console.print(f"Summary over {repeat} runs:")
        summary = summarize_profiles(profiles)
        for key, value in summary.items():
            if isinstance(value, float):
                console.print(f"  {key}: {value:.1f}")
            else:
                console.print(f"  {key}: {value}")

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

    shell_cmd = subparsers.add_parser("shell", help="Start a persistent TTS shell that keeps the model loaded")
    shell_cmd.add_argument("--device", default="cuda")
    shell_cmd.add_argument("--no-fp16", action="store_true")
    shell_cmd.add_argument("--no-cuda-kernel", action="store_true")
    shell_cmd.add_argument("--no-playback", action="store_true", help="Generate on say without audio playback")
    shell_cmd.add_argument("--no-accel", action="store_true", help="Disable AccelInferenceEngine (CUDA Graph + Flash Attention)")
    shell_cmd.add_argument("--emotion", type=float, nargs=8, metavar=("H", "A", "S", "F", "D", "M", "U", "C"),
                           help="8D emotion vector: Happy Angry Sad aFraid Disgusted Melancholic sUrprised Calm (0.0-1.0 each)")

    profile_cmd = subparsers.add_parser("profile", help="Run profiled synthesis with stage-level timing")
    profile_cmd.add_argument("--text", required=True, help="Text to synthesize")
    profile_cmd.add_argument("--device", default="cuda")
    profile_cmd.add_argument("--no-fp16", action="store_true")
    profile_cmd.add_argument("--no-cuda-kernel", action="store_true")
    profile_cmd.add_argument("--no-accel", action="store_true", help="Disable AccelInferenceEngine (CUDA Graph + Flash Attention)")
    profile_cmd.add_argument("--emotion", type=float, nargs=8, metavar=("H", "A", "S", "F", "D", "M", "U", "C"),
                           help="8D emotion vector: Happy Angry Sad aFraid Disgusted Melancholic sUrprised Calm (0.0-1.0 each)")
    profile_cmd.add_argument("--repeat", type=int, default=1, help="Number of synthesis runs (for averaging)")

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
    if args.command == "shell":
        return run_shell(
            device=args.device,
            use_fp16=not args.no_fp16,
            use_cuda_kernel=not args.no_cuda_kernel,
            use_accel=not args.no_accel,
            emotion_vector=args.emotion,
            playback=not args.no_playback,
        )
    if args.command == "profile":
        return run_profile(
            text=args.text,
            device=args.device,
            use_fp16=not args.no_fp16,
            use_cuda_kernel=not args.no_cuda_kernel,
            use_accel=not args.no_accel,
            emotion_vector=args.emotion,
            repeat=args.repeat,
        )
    raise ValueError(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
