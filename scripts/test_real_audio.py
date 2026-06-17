#!/usr/bin/env python3
"""
真實音訊整合測試 — 用 MultiChannelSTTEngine + FileChannel 跑 test_files/

測試場景：
  1. 單通道英文 (en_test.wav)
  2. 單通道中文 (zh_test.wav)
  3. 雙通道並行 (en + zh 同時)
  4. 多說話人 Discord 錄音 (multi_test.mp3) — 單通道但多人

Usage:
    python scripts/test_real_audio.py                # 跑全部
    python scripts/test_real_audio.py --test en      # 只跑英文
    python scripts/test_real_audio.py --test multi   # 只跑多人
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import numpy as np

# 確保能找到專案模組
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.columns import Columns

from echo_stt.core.config import load_config, get
from echo_stt.core.multi_channel_engine import MultiChannelSTTEngine
from echo_stt.core.multi_channel_schemas import TranscriptEntry
from echo_stt.output.formatter import TranscriptFormatter

console = Console()
TEST_DIR = Path(__file__).resolve().parent.parent / "echo_stt" / "test_files"


def load_audio(path: Path, target_sr: int = 16000) -> np.ndarray:
    """載入音訊檔並轉為 16kHz int16 mono。"""
    import librosa
    y, sr = librosa.load(str(path), sr=target_sr, mono=True)
    # float32 → int16
    return (y * 32768).clip(-32768, 32767).astype(np.int16)


def make_engine_config() -> dict:
    """建立測試用 config。"""
    config = load_config()
    mc = config.setdefault("multi_channel", {})
    mc["enabled"] = True
    mc["num_workers"] = 1
    mc["queue_max_size"] = 100
    mc["max_channels"] = 4
    mc["drain_timeout_s"] = 10.0
    mc["noise_filter_enabled"] = False
    mc["vad_config"] = {
        "sample_rate": 16000,
        "sensitivity": 0.5,
        "min_speech_duration": 0.3,
        "use_silero": False,  # 用 energy VAD，避免 torch.hub 問題
        "trailing_silence_ms": 800,
        "max_segment_ms": 15000,
        "min_speech_ms": 200,
        "adaptive_threshold": True,
    }
    return config


def run_single_channel(engine: MultiChannelSTTEngine, name: str, audio: np.ndarray,
                       speaker: str = "Unknown") -> None:
    """單通道測試。"""
    console.rule(f"[bold]{name}[/]")

    entries: list[TranscriptEntry] = []
    base_time = time.time()

    def on_entry(entry: TranscriptEntry):
        entries.append(entry)
        console.print(TranscriptFormatter.format_entry_rich(entry, base_time))

    # 暫存原始 callback
    old_cb = engine.on_entry
    engine.on_entry = on_entry
    engine._aggregator.on_entry = engine._on_entry_internal

    engine.start_session()
    engine.add_file_channel(
        channel_id=name,
        audio_data=audio,
        speaker_label=speaker.lower().replace(" ", "_"),
        speaker_name=speaker,
    )

    # 等 FileChannel 完成
    ch = engine._channel_manager.get_channel(name)
    if ch and hasattr(ch, "wait_for_completion"):
        ch.wait_for_completion(timeout=30.0)

    # 等佇列排空
    time.sleep(2.0)
    engine._worker_pool.drain(timeout=10.0)

    # 顯示結果
    console.print()
    transcript = engine.get_transcript("readable")
    console.print(Panel(transcript, title=f"[bold]Transcript — {name}[/]", border_style="cyan"))

    metrics = engine.get_metrics()
    console.print(f"  Entries: {metrics.total_entries} | Audio: {metrics.total_audio_duration:.1f}s")

    # 清理
    engine.remove_channel(name)
    engine.on_entry = old_cb
    engine._aggregator.on_entry = engine._on_entry_internal
    console.print()


def run_dual_channel(engine: MultiChannelSTTEngine, audio_en: np.ndarray,
                     audio_zh: np.ndarray) -> None:
    """雙通道並行測試。"""
    console.rule("[bold]Dual Channel (EN + ZH parallel)[/]")

    entries: list[TranscriptEntry] = []
    base_time = time.time()

    def on_entry(entry: TranscriptEntry):
        entries.append(entry)
        console.print(TranscriptFormatter.format_entry_rich(entry, base_time))

    old_cb = engine.on_entry
    engine.on_entry = on_entry
    engine._aggregator.on_entry = engine._on_entry_internal

    engine.start_session()
    engine.add_file_channel("en", audio_en, speaker_label="en_speaker", speaker_name="English")
    engine.add_file_channel("zh", audio_zh, speaker_label="zh_speaker", speaker_name="中文")

    console.print("  [dim]Waiting for both channels to complete...[/]")

    # 等兩個 channel 完成
    for cid in ("en", "zh"):
        ch = engine._channel_manager.get_channel(cid)
        if ch and hasattr(ch, "wait_for_completion"):
            ch.wait_for_completion(timeout=30.0)

    time.sleep(3.0)
    engine._worker_pool.drain(timeout=10.0)

    console.print()
    transcript = engine.get_transcript("readable")
    console.print(Panel(transcript, title="[bold]Transcript — Dual Channel[/]", border_style="magenta"))

    # 按通道分顯示
    session = engine._aggregator.get_session()
    if session:
        en_entries = session.get_entries_by_channel("en")
        zh_entries = session.get_entries_by_channel("zh")
        console.print(f"  EN entries: {len(en_entries)} | ZH entries: {len(zh_entries)} | Total: {session.total_segments}")

    engine.remove_channel("en")
    engine.remove_channel("zh")
    engine.on_entry = old_cb
    engine._aggregator.on_entry = engine._on_entry_internal
    console.print()


def run_multi_speaker(engine: MultiChannelSTTEngine, audio: np.ndarray) -> None:
    """多說話人測試（Discord 錄音，單通道但多人）。"""
    console.rule("[bold]Multi-Speaker (Discord recording)[/]")

    entries: list[TranscriptEntry] = []
    base_time = time.time()

    def on_entry(entry: TranscriptEntry):
        entries.append(entry)
        console.print(TranscriptFormatter.format_entry_rich(entry, base_time))

    old_cb = engine.on_entry
    engine.on_entry = on_entry
    engine._aggregator.on_entry = engine._on_entry_internal

    engine.start_session()
    engine.add_file_channel(
        "discord",
        audio_data=audio,
        speaker_label="discord_mix",
        speaker_name="Discord",
    )

    ch = engine._channel_manager.get_channel("discord")
    if ch and hasattr(ch, "wait_for_completion"):
        ch.wait_for_completion(timeout=90.0)

    time.sleep(3.0)
    engine._worker_pool.drain(timeout=10.0)

    console.print()

    # 完整 transcript
    transcript = engine.get_transcript("readable")
    console.print(Panel(transcript, title="[bold]Transcript — Discord Multi-Speaker[/]", border_style="yellow"))

    # SRT
    srt = engine.get_transcript("srt")
    if srt:
        console.print(Panel(srt, title="[bold]SRT Output[/]", border_style="dim"))

    metrics = engine.get_metrics()
    console.print(f"  Entries: {metrics.total_entries} | Audio: {metrics.total_audio_duration:.1f}s | Speakers: {engine._aggregator.speakers}")

    engine.remove_channel("discord")
    engine.on_entry = old_cb
    engine._aggregator.on_entry = engine._on_entry_internal
    console.print()


def main():
    parser = argparse.ArgumentParser(description="Real audio integration test")
    parser.add_argument("--test", choices=["en", "zh", "dual", "multi", "all"], default="all")
    args = parser.parse_args()

    console.print(Panel(
        "[bold cyan]Real Audio Integration Test[/]\n"
        "[dim]MultiChannelSTTEngine + FileChannel + 真實音訊[/]",
        border_style="cyan",
    ))

    # 載入音訊
    console.print()
    with console.status("[cyan]Loading audio files...[/]", spinner="dots"):
        audio_en = load_audio(TEST_DIR / "en_test.wav") if args.test in ("en", "dual", "all") else None
        audio_zh = load_audio(TEST_DIR / "zh_test.wav") if args.test in ("zh", "dual", "all") else None
        audio_multi = load_audio(TEST_DIR / "multi_test.mp3") if args.test in ("multi", "all") else None

    for name, audio in [("en_test", audio_en), ("zh_test", audio_zh), ("multi_test", audio_multi)]:
        if audio is not None:
            console.print(f"  [green]✓[/] {name}: {len(audio)/16000:.1f}s, {len(audio)} samples")

    # 建立引擎
    console.print()
    config = make_engine_config()

    with console.status("[cyan]Initializing MultiChannelSTTEngine...[/]", spinner="dots"):
        engine = MultiChannelSTTEngine(config=config)
        ok = engine.initialize()

    if not ok:
        console.print("[red]✗ Engine initialization failed![/]")
        return 1

    console.print(f"  [green]✓[/] Engine ready (device={engine.get_metrics().device})")
    engine.start()
    console.print()

    # 跑測試
    try:
        if args.test in ("en", "all") and audio_en is not None:
            run_single_channel(engine, "en_test", audio_en, "English Speaker")

        if args.test in ("zh", "all") and audio_zh is not None:
            run_single_channel(engine, "zh_test", audio_zh, "中文說話者")

        if args.test in ("dual", "all") and audio_en is not None and audio_zh is not None:
            run_dual_channel(engine, audio_en, audio_zh)

        if args.test in ("multi", "all") and audio_multi is not None:
            run_multi_speaker(engine, audio_multi)

    finally:
        engine.shutdown()
        console.print("[dim]Engine shut down. Done.[/]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
