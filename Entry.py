#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Echo STT System — Rich CLI 互動式測試介面

仿照 TestSeperateMemorySystem / TestSeparateSessionControl 的 CLI 風格，
提供模型狀態面板、即時辨識、基本指令互動。

Usage:
    python Entry.py                         # 預設設定
    python Entry.py --device 1              # 指定麥克風
    python Entry.py --model openai/whisper-large-v3  # 指定模型
    python Entry.py --task translate         # 翻譯模式
    python Entry.py --debug                  # 除錯日誌
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Optional, Dict, Any

try:
    from rich.columns import Columns
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Prompt
    from rich.table import Table
    from rich.text import Text

    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False

import numpy as np

from echo_stt.core.config import load_config, get
from echo_stt.core.engine import STTEngine

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Rich Panels
# ═══════════════════════════════════════════════════════════════════════════


def render_engine_panel(engine: STTEngine) -> Panel:
    """引擎狀態面板"""
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("Key", style="dim", width=16)
    table.add_column("Value")

    table.add_row("Model", engine.whisper_model_id)
    table.add_row("Device", f"[bold]{engine.device}[/]")
    table.add_row("Dtype", str(engine.torch_dtype).split(".")[-1])
    table.add_row("Sample Rate", f"{engine.sample_rate:,} Hz")

    # Mic
    mic_label = f"[{engine.device_index}]" if engine.device_index is not None else "[auto]"
    table.add_row("Microphone", mic_label)

    # Task
    task = engine._recognition_task
    task_color = "cyan" if task == "transcribe" else "yellow"
    table.add_row("Task", f"[{task_color}]{task}[/{task_color}]")

    # 狀態
    status = "[green]Ready[/]" if engine.is_initialized else "[red]Not initialized[/]"
    table.add_row("Status", status)

    return Panel(table, title="[bold cyan]Engine[/]", border_style="cyan", width=40)


def render_stats_panel(engine: STTEngine) -> Panel:
    """效能統計面板"""
    perf = engine.get_performance_window()

    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("Key", style="dim", width=18)
    table.add_column("Value")

    table.add_row("Recognitions", str(perf.get("total_recognitions", 0)))
    table.add_row("Audio Duration", f"{perf.get('total_audio_duration', 0):.1f}s")
    table.add_row("Avg Duration", f"{perf.get('avg_audio_duration', 0):.1f}s")
    table.add_row("VAD Triggers", str(perf.get("vad_triggers", 0)))

    last_conf = perf.get("recognition_confidence")
    if last_conf is not None:
        conf_color = "green" if last_conf >= 0.8 else "yellow" if last_conf >= 0.5 else "red"
        table.add_row("Last Confidence", f"[{conf_color}]{last_conf:.2f}[/{conf_color}]")

    last_text_len = perf.get("text_length")
    if last_text_len is not None:
        table.add_row("Last Text Length", f"{last_text_len} chars")

    return Panel(table, title="[bold blue]Stats[/]", border_style="blue", width=38)


def render_history_panel(history: list, last_n: int = 8) -> Panel:
    """辨識歷史面板"""
    if not history:
        return Panel("[dim]No recognition results yet.[/dim]",
                     title="[bold]History[/]", border_style="dim")

    table = Table(show_header=True, box=None, padding=(0, 1))
    table.add_column("#", style="dim", width=3)
    table.add_column("Text", overflow="ellipsis", max_width=50)
    table.add_column("Conf", style="dim", width=6, justify="right")
    table.add_column("Time", style="dim", width=6, justify="right")

    display = history[-last_n:]
    start_idx = len(history) - len(display)

    for i, entry in enumerate(display):
        idx = start_idx + i + 1
        text = entry.get("text", "")[:60]
        if len(entry.get("text", "")) > 60:
            text += "..."

        conf = entry.get("confidence", 0)
        conf_color = "green" if conf >= 0.8 else "yellow" if conf >= 0.5 else "red"

        proc_time = entry.get("processing_time", 0)
        table.add_row(
            str(idx),
            text if text else "[dim](empty)[/dim]",
            f"[{conf_color}]{conf:.2f}[/{conf_color}]",
            f"{proc_time:.1f}s",
        )

    title = f"[bold]History[/] [dim]({len(display)}/{len(history)})[/dim]"
    return Panel(table, title=title, border_style="white")


def show_dashboard(engine: STTEngine, history: list, console: Console) -> None:
    """完整儀表板"""
    console.print()
    console.print(Columns([
        render_engine_panel(engine),
        render_stats_panel(engine),
    ], expand=True))
    console.print(render_history_panel(history))


# ═══════════════════════════════════════════════════════════════════════════
# CLI 主類別
# ═══════════════════════════════════════════════════════════════════════════


class EchoSTTCLI:
    """Echo STT Rich CLI — 互動式語音辨識測試介面"""

    def __init__(self, args: Optional[argparse.Namespace] = None):
        self.console = Console()
        self.args = args
        self.engine: Optional[STTEngine] = None
        self.history: list = []  # 辨識歷史
        self._start_time = time.time()

    def initialize(self) -> bool:
        """初始化 STT 引擎"""
        self.console.print(Panel(
            "[bold cyan]Echo STT System[/]\n"
            "[dim]獨立語音識別子系統 — Whisper + VAD + Speaker ID[/]",
            border_style="cyan",
        ))

        # 載入配置並套用 CLI 參數覆蓋
        config = load_config()

        if self.args:
            if self.args.device is not None:
                config.setdefault("audio", {})["device_index"] = self.args.device
            if self.args.model:
                config.setdefault("whisper", {})["model_id"] = self.args.model
                config.setdefault("whisper", {})["use_local_model"] = False
            if self.args.task:
                config.setdefault("recognition", {})["task"] = self.args.task

        with self.console.status("[cyan]Loading Whisper model..."):
            self.engine = STTEngine(config=config)
            success = self.engine.initialize()

        if not success:
            self.console.print("[red]✗ Engine initialization failed![/]")
            return False

        self.console.print(f"[green]✓[/] Model: [bold]{self.engine.whisper_model_id}[/]")
        self.console.print(f"[green]✓[/] Device: [bold]{self.engine.device}[/]")

        mic_label = f"[{self.engine.device_index}]" if self.engine.device_index is not None else "[auto]"
        self.console.print(f"[green]✓[/] Microphone: [bold]{mic_label}[/]")

        self.console.print(f"[green]✓[/] Task: [bold]{self.engine._recognition_task}[/]")
        self.console.print()

        return True

    def show_menu(self) -> None:
        """顯示選單"""
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Cmd", style="bold cyan", width=14)
        table.add_column("Description")

        table.add_row("record [SEC]", "Record & transcribe (default 5s)")
        table.add_row("listen [SEC]", "Continuous listening (default 30s)")
        table.add_row("text <TEXT>", "Text input (bypass STT)")
        table.add_row("dashboard", "Show engine status & stats")
        table.add_row("history", "Show recognition history")
        table.add_row("devices", "List audio input devices")
        table.add_row("help", "Show this menu")
        table.add_row("exit", "Exit")

        self.console.print(Panel(table, title="[bold]Commands[/]", border_style="dim"))

    def run(self) -> None:
        """主迴圈"""
        if not self.initialize():
            return

        self.show_menu()

        while True:
            try:
                cmd = Prompt.ask("[bold]stt[/]").strip()
            except (EOFError, KeyboardInterrupt):
                break

            if not cmd or cmd.lower() == "help":
                self.show_menu()
                continue

            if cmd.lower() in ("exit", "quit", "q"):
                break

            self._dispatch(cmd)

            self.console.rule()
            self.show_menu()

        if self.engine:
            self.engine.shutdown()
        self.console.print("[dim]Session ended.[/dim]")

    def _dispatch(self, cmd: str) -> None:
        """指令路由"""
        lower = cmd.lower()

        if lower.startswith("record"):
            self._cmd_record(cmd)
        elif lower.startswith("listen"):
            self._cmd_listen(cmd)
        elif lower.startswith("text "):
            self._cmd_text(cmd[5:])
        elif lower in ("dashboard", "d"):
            show_dashboard(self.engine, self.history, self.console)
        elif lower in ("history", "h"):
            self.console.print(render_history_panel(self.history, last_n=20))
        elif lower == "devices":
            self._cmd_devices()
        else:
            self.console.print(f"[red]Unknown command: {cmd}[/]")

    # ── 指令處理 ──────────────────────────────────────────────────────────

    def _cmd_record(self, cmd: str) -> None:
        """手動錄音辨識"""
        parts = cmd.split()
        duration = 5.0
        if len(parts) > 1:
            try:
                duration = float(parts[1])
            except ValueError:
                self.console.print("[red]Usage: record [seconds][/]")
                return

        self.console.print(f"\n🎤 [bold]Recording {duration:.0f}s...[/] Speak now!\n")

        result = self.engine.handle({
            "mode": "manual",
            "duration": duration,
            "enable_speaker_id": False,
        })

        self._display_result(result)
        self.history.append(result)

    def _cmd_listen(self, cmd: str) -> None:
        """持續監聽模式"""
        parts = cmd.split()
        duration = 30.0
        if len(parts) > 1:
            try:
                duration = float(parts[1])
            except ValueError:
                self.console.print("[red]Usage: listen [seconds][/]")
                return

        self.console.print(
            f"\n👂 [bold]Continuous listening for {duration:.0f}s...[/]\n"
            "[dim]Press Ctrl+C to stop early[/]\n"
        )

        self.engine.should_stop_listening = False

        try:
            result = self.engine.handle({
                "mode": "continuous",
                "duration": duration,
                "enable_speaker_id": False,
            })
            self.history.append(result)
            self.console.print("[green]✓ Listening completed.[/]")
        except KeyboardInterrupt:
            self.engine.stop_listening()
            self.console.print("\n[yellow]⏹ Listening stopped.[/]")

    def _cmd_text(self, text: str) -> None:
        """文字輸入模式"""
        text = text.strip()
        if not text:
            self.console.print("[red]Usage: text <your message>[/]")
            return

        result = self.engine.handle_text_input(text)
        self._display_result(result)
        self.history.append(result)

    def _cmd_devices(self) -> None:
        """列出音訊輸入設備"""
        import pyaudio
        pa = pyaudio.PyAudio()

        table = Table(show_header=True, box=None, padding=(0, 1))
        table.add_column("Index", style="bold", width=6)
        table.add_column("Name", max_width=50)
        table.add_column("Channels", width=9, justify="right")

        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            max_input = info.get("maxInputChannels", 0)
            if isinstance(max_input, int) and max_input > 0:
                name = str(info.get("name", "Unknown"))
                # 標記當前使用的設備
                marker = ""
                if self.engine and self.engine.device_index == i:
                    marker = " [green]◄ active[/]"
                table.add_row(str(i), f"{name}{marker}", str(max_input))

        pa.terminate()
        self.console.print(Panel(table, title="[bold]Audio Input Devices[/]", border_style="cyan"))

    def _display_result(self, result: dict) -> None:
        """顯示辨識結果"""
        text = result.get("text", "")
        confidence = result.get("confidence", 0.0)
        proc_time = result.get("processing_time", 0.0)
        error = result.get("error")

        if error:
            self.console.print(f"[red]✗ {error}[/]")
            return

        if not text or text.isspace():
            self.console.print("[dim]🔇 No speech detected.[/dim]")
            return

        # 信心度顏色
        conf_color = "green" if confidence >= 0.8 else "yellow" if confidence >= 0.5 else "red"

        self.console.print(Panel(
            f"[bold]{text}[/]",
            title=f"[{conf_color}]Confidence: {confidence:.2f}[/{conf_color}] | Time: {proc_time:.2f}s",
            border_style=conf_color,
        ))


# ═══════════════════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════════════════


def main():
    if not _HAS_RICH:
        print("ERROR: Rich is required for CLI mode.")
        print("Install with: pip install rich")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Echo STT System — Rich CLI")
    parser.add_argument("--device", type=int, help="Microphone device index")
    parser.add_argument("--model", type=str, help="Override Whisper model ID")
    parser.add_argument("--task", choices=["transcribe", "translate"],
                        help="Recognition task (default: from config)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s | %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING)

    cli = EchoSTTCLI(args)
    try:
        cli.run()
    except KeyboardInterrupt:
        Console().print("\n[dim]Interrupted.[/dim]")


if __name__ == "__main__":
    main()
