#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Echo STT System — Rich CLI 互動式測試介面

仿照 TestSeperateMemorySystem / TestSeparateSessionControl 的 CLI 風格，
提供模型狀態面板、即時辨識、多通道管理、config 管理。

Usage:
    python Entry.py                         # 預設設定
    python Entry.py --device 1              # 指定麥克風
    python Entry.py --model openai/whisper-large-v3  # 指定模型
    python Entry.py --task translate         # 翻譯模式
    python Entry.py --debug                  # 除錯日誌
    python Entry.py --mc                     # 啟動多通道引擎
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Optional, Dict, Any, List

try:
    from rich.columns import Columns
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Prompt
    from rich.table import Table
    from rich.tree import Tree

    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False

import numpy as np

from echo_stt.core.config import load_config, get, reset_cache
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

    # Backend
    backend = getattr(engine, "_backend_type", "unknown")
    if backend == "faster-whisper":
        model_display = getattr(engine, "_fw_model_size", "?")
        compute = getattr(engine, "_fw_compute_type", "?")
        table.add_row("Backend", f"[bold]faster-whisper[/]")
        table.add_row("Model", f"[bold]{model_display}[/]")
        table.add_row("Compute", compute)
    else:
        table.add_row("Backend", f"[bold]{backend}[/]")
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

    # Speaker ID
    speaker_status = "[green]on[/]" if getattr(engine, "_speaker_enabled", False) else "[dim]off[/]"
    table.add_row("Speaker ID", speaker_status)

    # Keyword
    kw_status = "[green]on[/]" if getattr(engine, "_keyword_enabled", True) else "[dim]off[/]"
    table.add_row("Smart Wake", kw_status)

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


def render_mc_panel(mc_engine) -> Panel:
    """多通道引擎面板"""
    metrics = mc_engine.get_metrics()

    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("Key", style="dim", width=18)
    table.add_column("Value")

    # 狀態
    state = mc_engine.state.value
    state_colors = {
        "created": "dim", "initialized": "yellow",
        "running": "green", "stopped": "red", "shutdown": "dim red",
    }
    sc = state_colors.get(state, "white")
    table.add_row("State", f"[{sc}]{state}[/{sc}]")

    table.add_row("Channels", f"{metrics.active_channels} active / {metrics.total_channels_ever} total")
    table.add_row("Model", metrics.model_name or "—")
    table.add_row("Device", metrics.device or "—")
    table.add_row("Uptime", f"{metrics.uptime:.1f}s")
    table.add_row("Entries", str(metrics.total_entries))
    table.add_row("Audio", f"{metrics.total_audio_duration:.1f}s")

    # Queue
    q = metrics.queue
    table.add_row("Queue", f"{q.queue_size}/{q.max_size} [dim](dropped: {q.total_dropped})[/]")

    return Panel(table, title="[bold magenta]Multi-Channel Engine[/]", border_style="magenta", width=42)


def render_channel_table(mc_engine) -> Panel:
    """通道狀態表"""
    statuses = mc_engine.get_channel_status()
    if not statuses:
        return Panel("[dim]No channels.[/]", title="[bold]Channels[/]", border_style="dim")

    table = Table(show_header=True, box=None, padding=(0, 1))
    table.add_column("ID", style="bold", width=14)
    table.add_column("Type", width=10)
    table.add_column("State", width=12)
    table.add_column("Segments", width=9, justify="right")
    table.add_column("VAD", width=5)

    state_colors = {
        "idle": "dim", "listening": "cyan", "speech": "green bold",
        "processing": "yellow", "stopped": "red", "error": "red bold",
    }

    for cid, status in statuses.items():
        sc = state_colors.get(status.state.value, "white")
        vad_icon = "🟢" if status.vad_is_speech else "⚫"
        table.add_row(
            cid,
            status.channel_type.value,
            f"[{sc}]{status.state.value}[/{sc}]",
            str(status.segments_produced),
            vad_icon,
        )

    return Panel(table, title="[bold]Channels[/]", border_style="cyan")


def render_config_tree(config: dict, title: str = "Config") -> Panel:
    """Config 樹狀顯示"""
    tree = Tree(f"[bold]{title}[/]")

    def add_branch(parent_tree, data, prefix=""):
        for key, value in data.items():
            if isinstance(value, dict):
                branch = parent_tree.add(f"[bold cyan]{key}[/]")
                add_branch(branch, value, prefix=f"{prefix}{key}.")
            else:
                val_str = str(value)
                if isinstance(value, bool):
                    color = "green" if value else "red"
                    val_str = f"[{color}]{value}[/{color}]"
                elif value is None:
                    val_str = "[dim]null[/]"
                elif isinstance(value, (int, float)):
                    val_str = f"[yellow]{value}[/]"
                else:
                    val_str = f"[white]{value}[/]"
                parent_tree.add(f"[dim]{key}[/] = {val_str}")

    add_branch(tree, config)
    return Panel(tree, border_style="dim", padding=(1, 2))


def render_vad_panel(engine: STTEngine) -> Panel:
    """VAD 狀態面板"""
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("Key", style="dim", width=20)
    table.add_column("Value")

    # Basic VAD (energy)
    vad = engine.vad_module
    table.add_row("Energy VAD", "[green]active[/]")
    table.add_row("  Sample Rate", f"{vad.sample_rate} Hz")
    table.add_row("  Sensitivity", f"{vad.sensitivity:.2f}")
    table.add_row("  Min Speech", f"{vad.min_speech_duration:.2f}s")

    # Speaker ID
    if engine.speaker_module:
        table.add_row("Speaker Module", "[green]loaded[/]")
    else:
        table.add_row("Speaker Module", "[dim]disabled[/]")

    # Keyword
    if engine.keyword_detector:
        table.add_row("Keyword Detector", "[green]loaded[/]")
    else:
        table.add_row("Keyword Detector", "[dim]disabled[/]")

    return Panel(table, title="[bold green]VAD & Modules[/]", border_style="green", width=40)


def show_dashboard(engine: STTEngine, history: list, console: Console,
                   mc_engine=None) -> None:
    """完整儀表板"""
    console.print()
    console.print(Columns([
        render_engine_panel(engine),
        render_stats_panel(engine),
    ], expand=True))

    if mc_engine is not None and mc_engine.is_initialized:
        console.print(Columns([
            render_mc_panel(mc_engine),
            render_channel_table(mc_engine),
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
        self.config: dict = {}
        self.engine: Optional[STTEngine] = None
        self.mc_engine = None  # MultiChannelSTTEngine (lazy)
        self.history: list = []
        self._start_time = time.time()

    def initialize(self) -> bool:
        """初始化 STT 引擎"""
        self.console.print(Panel(
            "[bold cyan]Echo STT System[/]\n"
            "[dim]獨立語音識別子系統 — Whisper + VAD + Speaker ID + Multi-Channel[/]",
            border_style="cyan",
        ))

        # 載入配置並套用 CLI 參數覆蓋
        self.config = load_config()

        if self.args:
            if self.args.device is not None:
                self.config.setdefault("audio", {})["device_index"] = self.args.device
            if self.args.model:
                whisper_cfg = self.config.setdefault("whisper", {})
                whisper_cfg["model_id"] = self.args.model
                whisper_cfg["use_local_model"] = False
                model_arg = self.args.model
                if "/" in model_arg:
                    name_part = model_arg.split("/")[-1]
                    if name_part.startswith("whisper-"):
                        whisper_cfg["model_size"] = name_part[len("whisper-"):]
                    elif name_part.startswith("faster-whisper-"):
                        whisper_cfg["model_size"] = name_part[len("faster-whisper-"):]
                    else:
                        whisper_cfg["model_size"] = name_part
                else:
                    whisper_cfg["model_size"] = model_arg
            if self.args.task:
                self.config.setdefault("recognition", {})["task"] = self.args.task

        with self.console.status("[cyan]Loading Whisper model..."):
            self.engine = STTEngine(config=self.config)
            success = self.engine.initialize()

        if not success:
            self.console.print("[red]✗ Engine initialization failed![/]")
            return False

        backend = getattr(self.engine, "_backend_type", "?")
        model = getattr(self.engine, "_fw_model_size", self.engine.whisper_model_id)
        self.console.print(f"[green]✓[/] Backend: [bold]{backend}[/] ({model})")
        self.console.print(f"[green]✓[/] Device: [bold]{self.engine.device}[/]")

        mic_label = f"[{self.engine.device_index}]" if self.engine.device_index is not None else "[auto]"
        self.console.print(f"[green]✓[/] Microphone: [bold]{mic_label}[/]")
        self.console.print(f"[green]✓[/] Task: [bold]{self.engine._recognition_task}[/]")

        # 多通道引擎（--mc 或 config 啟用）
        mc_requested = getattr(self.args, "mc", False) if self.args else False
        mc_enabled = get("multi_channel.enabled", False, config=self.config)

        if mc_requested or mc_enabled:
            self._init_mc_engine()

        self.console.print()
        return True

    def _init_mc_engine(self) -> bool:
        """初始化多通道引擎"""
        try:
            from echo_stt.core.multi_channel_engine import MultiChannelSTTEngine

            # 強制啟用
            self.config.setdefault("multi_channel", {})["enabled"] = True

            self.mc_engine = MultiChannelSTTEngine(
                config=self.config,
                on_entry=self._on_mc_entry,
                speaker_module=self.engine.speaker_module if self.engine else None,
            )
            # 注入已初始化的 STTEngine（共用模型）
            self.mc_engine._stt_engine = self.engine

            if self.mc_engine.initialize():
                self.console.print("[green]✓[/] Multi-Channel Engine: [bold]ready[/]")
                return True
            else:
                self.console.print("[yellow]⚠ Multi-Channel Engine: init failed[/]")
                self.mc_engine = None
                return False
        except Exception as e:
            self.console.print(f"[yellow]⚠ Multi-Channel Engine: {e}[/]")
            self.mc_engine = None
            return False

    def _on_mc_entry(self, entry) -> None:
        """多通道辨識結果的即時 callback"""
        from echo_stt.output.formatter import TranscriptFormatter

        if self.mc_engine and self.mc_engine._aggregator:
            session = self.mc_engine._aggregator.get_session()
            base = session.started_at if session else time.time()
        else:
            base = time.time()

        rich_line = TranscriptFormatter.format_entry_rich(entry, base)
        self.console.print(rich_line)

    def show_menu(self) -> None:
        """顯示選單"""
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Cmd", style="bold cyan", width=22)
        table.add_column("Description")

        # 基本命令
        table.add_row("[bold]── Basic ──", "")
        table.add_row("record [SEC]", "Record & transcribe (default 5s)")
        table.add_row("listen [SEC]", "Continuous listening (default 30s)")
        table.add_row("text <TEXT>", "Text input (bypass STT)")

        # 面板
        table.add_row("[bold]── View ──", "")
        table.add_row("dashboard / d", "Show engine status & stats")
        table.add_row("history / h", "Show recognition history")
        table.add_row("vad", "Show VAD & module status")
        table.add_row("devices", "List audio input devices")

        # Config
        table.add_row("[bold]── Config ──", "")
        table.add_row("config", "Show current config")
        table.add_row("config <section>", "Show section (e.g. config whisper)")
        table.add_row("config set <key> <val>", "Set value (e.g. config set vad.sensitivity 0.8)")
        table.add_row("config reload", "Reload config from file")

        # 多通道
        if self.mc_engine is not None:
            table.add_row("[bold]── Multi-Channel ──", "")
            table.add_row("mc start", "Start multi-channel engine")
            table.add_row("mc stop", "Stop engine (keep session)")
            table.add_row("mc status", "Show channels & queue")
            table.add_row("mc add <id> [device]", "Add mic channel")
            table.add_row("mc remove <id>", "Remove channel")
            table.add_row("mc transcript", "Show transcript (readable)")
            table.add_row("mc srt", "Show transcript (SRT)")
            table.add_row("mc session new", "Start new session")
        else:
            table.add_row("[bold]── Multi-Channel ──", "")
            table.add_row("mc init", "Initialize multi-channel engine")

        table.add_row("[bold]── System ──", "")
        table.add_row("help", "Show this menu")
        table.add_row("exit / q", "Exit")

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
            self.console.rule(style="dim")

        self._shutdown()

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
            show_dashboard(self.engine, self.history, self.console, self.mc_engine)
        elif lower in ("history", "h"):
            self.console.print(render_history_panel(self.history, last_n=20))
        elif lower == "devices":
            self._cmd_devices()
        elif lower == "vad":
            self._cmd_vad()
        elif lower.startswith("config"):
            self._cmd_config(cmd)
        elif lower.startswith("mc"):
            self._cmd_mc(cmd)
        else:
            self.console.print(f"[red]Unknown command: {cmd}[/]")

    def _shutdown(self) -> None:
        """優雅關閉"""
        if self.mc_engine is not None:
            try:
                # 不讓 mc_engine 關掉共用的 STTEngine
                self.mc_engine._stt_engine = None
                self.mc_engine.shutdown()
            except Exception:
                pass

        if self.engine:
            self.engine.shutdown()

        self.console.print("[dim]Session ended.[/dim]")

    # ── 基本命令 ──────────────────────────────────────────────────────────

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

    def _on_realtime_result(self, result) -> None:
        """即時辨識結果 callback（listen 模式）"""
        from echo_stt.output.formatter import TranscriptFormatter
        from echo_stt.core.multi_channel_schemas import TranscriptEntry

        text = result.text if hasattr(result, "text") else result.get("text", "")
        if not text or not text.strip():
            return

        confidence = (
            result.confidence if hasattr(result, "confidence")
            else result.get("confidence", 0.0)
        )
        metadata = result.metadata if hasattr(result, "metadata") else result.get("metadata", {})
        speaker = "unknown"
        if hasattr(metadata, "get"):
            speaker = metadata.get("speaker_id", "unknown")

        now = time.time()
        base = self._listen_start_time if hasattr(self, "_listen_start_time") else now

        # 使用 TranscriptFormatter 的 Rich 格式
        entry = TranscriptEntry(
            segment_id="live",
            text=text,
            confidence=confidence,
            channel_id="mic_0",
            channel_type="local_mic",
            speaker_label=speaker,
            speaker_name=speaker if speaker != "unknown" else "Unknown",
            start_ts=now,
            end_ts=now,
        )
        self.console.print(TranscriptFormatter.format_entry_rich(entry, base))

        # 加入歷史
        self.history.append({
            "text": text,
            "confidence": confidence,
            "speaker": speaker,
            "timestamp": now - base,
            "processing_time": metadata.get("processing_time", 0) if hasattr(metadata, "get") else 0,
        })

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

        self._listen_start_time = time.time()
        original_callback = self.engine.result_callback
        self.engine.result_callback = self._on_realtime_result
        self.engine.should_stop_listening = False

        try:
            result = self.engine.handle({
                "mode": "continuous",
                "duration": duration,
                "enable_speaker_id": False,
            })

            metadata = result.get("metadata", {})
            success = metadata.get("successful_segments", 0)
            failed = metadata.get("failed_segments", 0)
            self.console.print(
                f"\n[green]✓ Listening completed.[/] "
                f"[dim]({success} segments, {failed} failed)[/dim]"
            )
        except KeyboardInterrupt:
            self.engine.stop_listening()
            self.console.print("\n[yellow]⏹ Listening stopped.[/]")
        finally:
            self.engine.result_callback = original_callback

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
                marker = ""
                if self.engine and self.engine.device_index == i:
                    marker = " [green]◄ active[/]"
                table.add_row(str(i), f"{name}{marker}", str(max_input))

        pa.terminate()
        self.console.print(Panel(table, title="[bold]Audio Input Devices[/]", border_style="cyan"))

    def _cmd_vad(self) -> None:
        """顯示 VAD 與模組狀態"""
        self.console.print()
        self.console.print(render_vad_panel(self.engine))

        # 如果有多通道引擎，顯示進階 VAD 資訊
        if self.mc_engine and self.mc_engine.is_initialized:
            cm = self.mc_engine._channel_manager
            if cm and cm.channel_count > 0:
                table = Table(show_header=True, box=None, padding=(0, 1))
                table.add_column("Channel", style="bold", width=14)
                table.add_column("VAD Backend", width=14)
                table.add_column("State", width=18)
                table.add_column("Energy", width=10, justify="right")

                for cid, status in cm.get_all_status().items():
                    ch = cm.get_channel(cid)
                    vad_name = "unknown"
                    muzzle_state = ""
                    if ch and hasattr(ch, "_vad"):
                        vad = ch._vad
                        vad_name = getattr(vad, "backend_name", "energy")
                        if hasattr(vad, "_muzzle"):
                            muzzle_state = f" ({vad._muzzle.state.value})"

                    speech = "[green]SPEECH[/]" if status.vad_is_speech else "[dim]silence[/]"
                    table.add_row(
                        cid,
                        vad_name,
                        f"{speech}{muzzle_state}",
                        f"{status.current_energy:.4f}",
                    )

                self.console.print(Panel(
                    table,
                    title="[bold green]Advanced VAD (per channel)[/]",
                    border_style="green",
                ))

    # ── Config 命令 ───────────────────────────────────────────────────────

    def _cmd_config(self, cmd: str) -> None:
        """Config 管理命令"""
        parts = cmd.split(maxsplit=3)

        if len(parts) == 1:
            # config → 顯示全部
            self.console.print(render_config_tree(self.config, "stt_config.yaml"))
            return

        sub = parts[1].lower()

        if sub == "reload":
            reset_cache()
            self.config = load_config(force_reload=True)
            self.console.print("[green]✓ Config reloaded from file.[/]")
            return

        if sub == "set":
            if len(parts) < 4:
                self.console.print("[red]Usage: config set <key.path> <value>[/]")
                return
            key_path = parts[2]
            value_str = parts[3]
            self._config_set(key_path, value_str)
            return

        # config <section> → 顯示特定區段
        section = self.config.get(sub)
        if section is not None:
            if isinstance(section, dict):
                self.console.print(render_config_tree({sub: section}, f"config.{sub}"))
            else:
                self.console.print(f"[bold]{sub}[/] = {section}")
        else:
            self.console.print(f"[red]Section '{sub}' not found in config.[/]")

    def _config_set(self, key_path: str, value_str: str) -> None:
        """設定 config 值（dot-notation）"""
        # 型別推斷
        value: Any
        if value_str.lower() == "true":
            value = True
        elif value_str.lower() == "false":
            value = False
        elif value_str.lower() == "null" or value_str.lower() == "none":
            value = None
        else:
            try:
                value = int(value_str)
            except ValueError:
                try:
                    value = float(value_str)
                except ValueError:
                    value = value_str

        # 寫入 config dict
        keys = key_path.split(".")
        current = self.config
        for k in keys[:-1]:
            current = current.setdefault(k, {})
        old = current.get(keys[-1], "<unset>")
        current[keys[-1]] = value

        self.console.print(
            f"[green]✓[/] [bold]{key_path}[/]: {old} → [yellow]{value}[/]"
        )
        self.console.print("[dim]Note: runtime config changed. Use 'config reload' to revert to file.[/]")

    # ── Multi-Channel 命令 ────────────────────────────────────────────────

    def _cmd_mc(self, cmd: str) -> None:
        """Multi-Channel 引擎命令"""
        parts = cmd.split()

        if len(parts) < 2:
            if self.mc_engine and self.mc_engine.is_initialized:
                # mc → 顯示狀態
                self.console.print()
                self.console.print(Columns([
                    render_mc_panel(self.mc_engine),
                    render_channel_table(self.mc_engine),
                ], expand=True))
            else:
                self.console.print("[dim]Multi-channel engine not initialized. Use 'mc init'.[/]")
            return

        sub = parts[1].lower()

        if sub == "init":
            if self.mc_engine and self.mc_engine.is_initialized:
                self.console.print("[yellow]Already initialized.[/]")
            else:
                self._init_mc_engine()
            return

        # 以下命令需要 mc_engine 已存在
        if self.mc_engine is None:
            self.console.print("[red]Multi-channel engine not initialized. Use 'mc init' first.[/]")
            return

        if sub == "start":
            if self.mc_engine.is_running:
                self.console.print("[yellow]Already running.[/]")
            else:
                try:
                    self.mc_engine.start()
                    self.console.print("[green]✓ Multi-channel engine started.[/]")
                except Exception as e:
                    self.console.print(f"[red]✗ {e}[/]")

        elif sub == "stop":
            self.mc_engine.stop()
            self.console.print("[yellow]⏹ Multi-channel engine stopped.[/]")

        elif sub == "status":
            self.console.print()
            self.console.print(Columns([
                render_mc_panel(self.mc_engine),
                render_channel_table(self.mc_engine),
            ], expand=True))

        elif sub == "add":
            self._mc_add_channel(parts[2:])

        elif sub == "remove":
            if len(parts) < 3:
                self.console.print("[red]Usage: mc remove <channel_id>[/]")
                return
            cid = parts[2]
            if self.mc_engine.remove_channel(cid):
                self.console.print(f"[green]✓ Removed channel: {cid}[/]")
            else:
                self.console.print(f"[red]Channel '{cid}' not found.[/]")

        elif sub == "transcript":
            output = self.mc_engine.get_transcript("readable")
            self.console.print(Panel(output, title="[bold]Transcript[/]", border_style="white"))

        elif sub == "srt":
            output = self.mc_engine.get_transcript("srt")
            if output:
                self.console.print(Panel(output, title="[bold]SRT[/]", border_style="white"))
            else:
                self.console.print("[dim]No entries.[/]")

        elif sub == "session":
            if len(parts) > 2 and parts[2].lower() == "new":
                session = self.mc_engine.start_session()
                self.console.print(f"[green]✓ New session: {session.session_id}[/]")
            else:
                session = self.mc_engine._aggregator.get_session() if self.mc_engine._aggregator else None
                if session:
                    self.console.print(
                        f"Session: [bold]{session.session_id}[/] | "
                        f"Entries: {session.total_segments} | "
                        f"Audio: {session.total_audio_duration:.1f}s | "
                        f"Speakers: {', '.join(session.get_speakers()) or 'none'}"
                    )
                else:
                    self.console.print("[dim]No active session.[/]")

        else:
            self.console.print(f"[red]Unknown mc command: {sub}[/]")

    def _mc_add_channel(self, args: List[str]) -> None:
        """新增麥克風通道"""
        if not args:
            self.console.print("[red]Usage: mc add <channel_id> [device_index][/]")
            return

        cid = args[0]
        device_idx = None
        if len(args) > 1:
            try:
                device_idx = int(args[1])
            except ValueError:
                self.console.print(f"[red]Invalid device index: {args[1]}[/]")
                return

        try:
            self.mc_engine.add_local_mic(
                channel_id=cid,
                device_index=device_idx,
                speaker_name=cid.capitalize(),
            )
            self.console.print(
                f"[green]✓ Added mic channel: {cid}[/]"
                + (f" (device {device_idx})" if device_idx is not None else "")
            )
        except Exception as e:
            self.console.print(f"[red]✗ {e}[/]")

    # ── 結果顯示 ──────────────────────────────────────────────────────────

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
    parser.add_argument("--mc", action="store_true",
                        help="Enable multi-channel engine on startup")
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
