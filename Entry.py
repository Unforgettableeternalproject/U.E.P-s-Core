#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Echo STT System — 互動式 Rich CLI + 辨識工具

Usage:
    python Entry.py                          # 互動式 Rich CLI（預設 shell）
    python Entry.py shell                    # 同上
    python Entry.py shell --mc               # 啟動時啟用多通道引擎
    python Entry.py shell --model large-v3   # 指定模型
    python Entry.py smoke                    # 驗證環境
"""
from __future__ import annotations

import argparse
import shlex
import sys
import time
from typing import Optional, Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# ── Rich imports（graceful fallback） ──
try:
    from rich.console import Console as _RichConsole, Group
    from rich.columns import Columns
    from rich.panel import Panel
    from rich.prompt import Prompt
    from rich.table import Table
    from rich.text import Text
    from rich.tree import Tree

    _HAS_RICH = True
except ModuleNotFoundError:
    _HAS_RICH = False

    class _RichConsole:  # type: ignore[no-redef]
        """Minimal fallback when rich is not installed."""
        def print(self, *args, **kwargs):
            print(*args)
        def status(self, *args, **kwargs):
            import contextlib
            return contextlib.nullcontext()
        def rule(self, *args, **kwargs):
            print("─" * 60)

import numpy as np

from echo_stt.core.config import load_config, get, reset_cache
from echo_stt.core.engine import STTEngine

console = _RichConsole()


def _require_rich():
    """Guard for features that need the full Rich library."""
    if not _HAS_RICH:
        print("Error: 'rich' package is required for the interactive CLI.")
        print("Install with: pip install rich>=13.7.0")
        raise SystemExit(1)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Rich UI 元件
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def render_engine_panel(engine: STTEngine) -> Panel:
    """引擎狀態面板"""
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("key", style="dim", width=16)
    table.add_column("value", style="bold cyan")

    backend = getattr(engine, "_backend_type", "unknown")
    if backend == "faster-whisper":
        model = getattr(engine, "_fw_model_size", "?")
        compute = getattr(engine, "_fw_compute_type", "?")
        table.add_row("Backend", "faster-whisper")
        table.add_row("Model", model)
        table.add_row("Compute", compute)
    else:
        table.add_row("Backend", backend)
        table.add_row("Model", engine.whisper_model_id)

    table.add_row("Device", engine.device)
    table.add_row("Dtype", str(engine.torch_dtype).split(".")[-1])
    table.add_row("Sample Rate", f"{engine.sample_rate:,} Hz")

    mic = f"[{engine.device_index}]" if engine.device_index is not None else "[auto]"
    table.add_row("Microphone", mic)

    task = engine._recognition_task
    task_style = "cyan" if task == "transcribe" else "yellow"
    table.add_row("Task", Text(task, style=task_style))

    table.add_row("Speaker ID", Text("on", style="green") if getattr(engine, "_speaker_enabled", False) else Text("off", style="dim"))
    table.add_row("Smart Wake", Text("on", style="green") if getattr(engine, "_keyword_enabled", True) else Text("off", style="dim"))

    status = Text("Ready", style="green") if engine.is_initialized else Text("Not initialized", style="red")
    table.add_row("Status", status)

    return Panel(table, title="[bold]🎙 Engine[/]", border_style="cyan", width=44)


def render_stats_panel(engine: STTEngine) -> Panel:
    """效能統計面板"""
    perf = engine.get_performance_window()

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("key", style="dim", width=18)
    table.add_column("value", style="bold cyan")

    table.add_row("Recognitions", str(perf.get("total_recognitions", 0)))
    table.add_row("Audio Duration", f"{perf.get('total_audio_duration', 0):.1f}s")
    table.add_row("Avg Duration", f"{perf.get('avg_audio_duration', 0):.1f}s")
    table.add_row("VAD Triggers", str(perf.get("vad_triggers", 0)))

    last_conf = perf.get("recognition_confidence")
    if last_conf is not None:
        conf_color = "green" if last_conf >= 0.8 else "yellow" if last_conf >= 0.5 else "red"
        table.add_row("Last Confidence", Text(f"{last_conf:.2f}", style=conf_color))

    last_text_len = perf.get("text_length")
    if last_text_len is not None:
        table.add_row("Last Text Length", f"{last_text_len} chars")

    return Panel(table, title="[bold]📊 Stats[/]", border_style="blue", width=44)


def render_history_panel(history: list, last_n: int = 8) -> Panel:
    """辨識歷史面板"""
    if not history:
        return Panel("[dim]No recognition results yet.[/dim]",
                     title="[bold]📜 History[/]", border_style="dim")

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
            Text(f"{conf:.2f}", style=conf_color),
            f"{proc_time:.1f}s",
        )

    title = f"[bold]📜 History[/] [dim]({len(display)}/{len(history)})[/dim]"
    return Panel(table, title=title, border_style="white")


def render_vad_panel(engine: STTEngine) -> Panel:
    """VAD 與模組狀態面板"""
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("key", style="dim", width=20)
    table.add_column("value", style="bold")

    vad = engine.vad_module
    table.add_row("Energy VAD", Text("active", style="green"))
    table.add_row("  Sensitivity", f"[cyan]{vad.sensitivity:.2f}[/]")
    table.add_row("  Min Speech", f"[cyan]{vad.min_speech_duration:.2f}s[/]")
    table.add_row("Speaker Module", Text("loaded", style="green") if engine.speaker_module else Text("disabled", style="dim"))
    table.add_row("Keyword Detector", Text("loaded", style="green") if engine.keyword_detector else Text("disabled", style="dim"))

    return Panel(table, title="[bold]🔊 VAD & Modules[/]", border_style="green", width=44)


def render_mc_panel(mc_engine) -> Panel:
    """多通道引擎面板"""
    metrics = mc_engine.get_metrics()

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("key", style="dim", width=18)
    table.add_column("value", style="bold cyan")

    state = mc_engine.state.value
    state_colors = {"created": "dim", "initialized": "yellow", "running": "green", "stopped": "red", "shutdown": "dim red"}
    table.add_row("State", Text(state, style=state_colors.get(state, "white")))
    table.add_row("Channels", f"{metrics.active_channels} active / {metrics.total_channels_ever} total")
    table.add_row("Model", metrics.model_name or "—")
    table.add_row("Device", metrics.device or "—")
    table.add_row("Uptime", f"{metrics.uptime:.1f}s")
    table.add_row("Entries", str(metrics.total_entries))
    table.add_row("Audio", f"{metrics.total_audio_duration:.1f}s")

    q = metrics.queue
    table.add_row("Queue", f"{q.queue_size}/{q.max_size} [dim](dropped: {q.total_dropped})[/]")

    return Panel(table, title="[bold]🔀 Multi-Channel[/]", border_style="magenta", width=44)


def render_channel_table(mc_engine) -> Panel:
    """通道狀態表"""
    statuses = mc_engine.get_channel_status()
    if not statuses:
        return Panel("[dim]No channels.[/]", title="[bold]Channels[/]", border_style="dim", width=44)

    table = Table(show_header=True, box=None, padding=(0, 1))
    table.add_column("ID", style="bold", width=14)
    table.add_column("Type", width=10)
    table.add_column("State", width=12)
    table.add_column("Segs", width=5, justify="right")
    table.add_column("VAD", width=4)

    state_colors = {"idle": "dim", "listening": "cyan", "speech": "green bold", "processing": "yellow", "stopped": "red", "error": "red bold"}

    for cid, status in statuses.items():
        sc = state_colors.get(status.state.value, "white")
        vad_icon = "🟢" if status.vad_is_speech else "⚫"
        table.add_row(cid, status.channel_type.value, Text(status.state.value, style=sc), str(status.segments_produced), vad_icon)

    return Panel(table, title="[bold]📡 Channels[/]", border_style="cyan", width=44)


def render_config_tree(config: dict, title: str = "Config") -> Panel:
    """Config 樹狀顯示"""
    tree = Tree(f"[bold]{title}[/]")

    def _add(parent, data):
        for key, value in data.items():
            if isinstance(value, dict):
                _add(parent.add(f"[bold cyan]{key}[/]"), value)
            else:
                if isinstance(value, bool):
                    v = Text(str(value), style="green" if value else "red")
                elif value is None:
                    v = Text("null", style="dim")
                elif isinstance(value, (int, float)):
                    v = Text(str(value), style="yellow")
                else:
                    v = Text(str(value), style="white")
                parent.add(Text.assemble((f"{key} = ", "dim"), v))

    _add(tree, config)
    return Panel(tree, border_style="dim", padding=(1, 2))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 指令處理（獨立函式，接收 session/engine）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _cmd_record(engine: STTEngine, args: list[str], history: list) -> None:
    """手動錄音辨識"""
    duration = 5.0
    if args:
        duration = float(args[0])

    console.print(f"\n  🎤 [bold]Recording {duration:.0f}s...[/] Speak now!\n")
    result = engine.handle({"mode": "manual", "duration": duration, "enable_speaker_id": False})
    _display_result(result)
    history.append(result)


def _cmd_listen(engine: STTEngine, args: list[str], history: list) -> None:
    """持續監聽模式"""
    from echo_stt.output.formatter import TranscriptFormatter
    from echo_stt.core.multi_channel_schemas import TranscriptEntry

    duration = 30.0
    if args:
        duration = float(args[0])

    console.print(f"\n  👂 [bold]Continuous listening for {duration:.0f}s...[/]\n  [dim]Press Ctrl+C to stop early[/]\n")

    listen_start = time.time()

    def on_result(result):
        text = result.text if hasattr(result, "text") else result.get("text", "")
        if not text or not text.strip():
            return
        confidence = result.confidence if hasattr(result, "confidence") else result.get("confidence", 0.0)
        metadata = result.metadata if hasattr(result, "metadata") else result.get("metadata", {})
        speaker = metadata.get("speaker_id", "unknown") if hasattr(metadata, "get") else "unknown"

        now = time.time()
        entry = TranscriptEntry(
            segment_id="live", text=text, confidence=confidence,
            channel_id="mic_0", channel_type="local_mic",
            speaker_label=speaker, speaker_name=speaker if speaker != "unknown" else "Unknown",
            start_ts=now, end_ts=now,
        )
        console.print(TranscriptFormatter.format_entry_rich(entry, listen_start))
        history.append({
            "text": text, "confidence": confidence, "speaker": speaker,
            "timestamp": now - listen_start,
            "processing_time": metadata.get("processing_time", 0) if hasattr(metadata, "get") else 0,
        })

    original_callback = engine.result_callback
    engine.result_callback = on_result
    engine.should_stop_listening = False

    try:
        result = engine.handle({"mode": "continuous", "duration": duration, "enable_speaker_id": False})
        metadata = result.get("metadata", {})
        console.print(f"\n  [green]✓ Listening completed.[/] [dim]({metadata.get('successful_segments', 0)} segments, {metadata.get('failed_segments', 0)} failed)[/dim]")
    except KeyboardInterrupt:
        engine.stop_listening()
        console.print("\n  [yellow]⏹ Listening stopped.[/]")
    finally:
        engine.result_callback = original_callback


def _cmd_devices(engine: STTEngine) -> None:
    """列出音訊輸入設備"""
    import pyaudio
    pa = pyaudio.PyAudio()

    table = Table(show_header=True, box=None, padding=(0, 1))
    table.add_column("Index", style="bold", width=6)
    table.add_column("Name", max_width=50)
    table.add_column("Ch", width=4, justify="right")

    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        max_input = info.get("maxInputChannels", 0)
        if isinstance(max_input, int) and max_input > 0:
            name = str(info.get("name", "Unknown"))
            marker = " [green]◄[/]" if engine.device_index == i else ""
            table.add_row(str(i), f"{name}{marker}", str(max_input))

    pa.terminate()
    console.print(Panel(table, title="[bold]🎧 Audio Input Devices[/]", border_style="cyan"))


def _cmd_device(engine: STTEngine, args: list[str]) -> None:
    """顯示或切換音訊輸入設備"""
    import pyaudio

    if not args:
        # 無參數：顯示目前設備
        current = engine.device_index
        if current is None:
            console.print("  🎧 Current device: [cyan][auto] (system default)[/]")
        else:
            pa = pyaudio.PyAudio()
            try:
                info = pa.get_device_info_by_index(current)
                name = info.get("name", "Unknown")
                console.print(f"  🎧 Current device: [cyan][{current}] {name}[/]")
            finally:
                pa.terminate()
        return

    sub = args[0].lower()

    if sub == "auto":
        engine.device_index = None
        # 重建 PyAudio config
        engine.pa_config["input_device_index"] = None
        console.print("  [green]✓[/] 🎧 Switched to [cyan][auto] (system default)[/]")
        return

    # 數字 → 切換到指定設備
    try:
        idx = int(sub)
    except ValueError:
        console.print("  [dim]Usage: device [index | auto][/]")
        return

    # 驗證設備存在且有輸入通道
    pa = pyaudio.PyAudio()
    try:
        if idx < 0 or idx >= pa.get_device_count():
            console.print(f"  [red]Device index {idx} out of range (0-{pa.get_device_count()-1})[/]")
            return

        info = pa.get_device_info_by_index(idx)
        max_input = info.get("maxInputChannels", 0)
        if not isinstance(max_input, int) or max_input <= 0:
            console.print(f"  [red]Device {idx} has no input channels[/]")
            return

        name = info.get("name", "Unknown")
    finally:
        pa.terminate()

    engine.device_index = idx
    console.print(f"  [green]✓[/] 🎧 Switched to [cyan][{idx}] {name}[/]")


def _cmd_config(config: dict, args: list[str]) -> dict:
    """Config 管理（show / set / reload）"""
    if not args:
        console.print(render_config_tree(config, "stt_config.yaml"))
        return config

    sub = args[0].lower()

    if sub == "reload":
        reset_cache()
        config = load_config(force_reload=True)
        console.print("  [green]✓[/] Config reloaded from file.")
        return config

    if sub == "set":
        if len(args) < 3:
            console.print("  [dim]Usage: config set <key.path> <value>[/]")
            return config

        key_path, value_str = args[1], args[2]
        # 型別推斷
        if value_str.lower() == "true":
            value: Any = True
        elif value_str.lower() == "false":
            value = False
        elif value_str.lower() in ("null", "none"):
            value = None
        else:
            try:
                value = int(value_str)
            except ValueError:
                try:
                    value = float(value_str)
                except ValueError:
                    value = value_str

        keys = key_path.split(".")
        current = config
        for k in keys[:-1]:
            current = current.setdefault(k, {})
        old = current.get(keys[-1], "<unset>")
        current[keys[-1]] = value
        console.print(f"  [green]✓[/] [bold]{key_path}[/]: {old} → [yellow]{value}[/]")
        return config

    # config <section>
    section = config.get(sub)
    if section is not None:
        if isinstance(section, dict):
            console.print(render_config_tree({sub: section}, f"config.{sub}"))
        else:
            console.print(f"  [bold]{sub}[/] = {section}")
    else:
        console.print(f"  [dim]Section '{sub}' not found.[/]")
    return config


def _cmd_mc(mc_engine, engine: STTEngine, config: dict, args: list[str]) -> Any:
    """Multi-Channel 引擎命令"""
    if mc_engine is None:
        if args and args[0].lower() == "init":
            return _mc_init(engine, config)
        console.print("  [dim]Multi-channel not initialized. Use 'mc init'.[/]")
        return None

    if not args:
        # mc → 狀態面板
        console.print()
        console.print(Columns([render_mc_panel(mc_engine), render_channel_table(mc_engine)], expand=True))
        return mc_engine

    sub = args[0].lower()
    rest = args[1:]

    match sub:
        case "init":
            console.print("  [yellow]Already initialized.[/]")

        case "start":
            if mc_engine.is_running:
                console.print("  [yellow]Already running.[/]")
            else:
                mc_engine.start()
                console.print("  [green]✓[/] Multi-channel engine started.")

        case "stop":
            mc_engine.stop()
            console.print("  [yellow]⏹[/] Multi-channel engine stopped.")

        case "status":
            console.print()
            console.print(Columns([render_mc_panel(mc_engine), render_channel_table(mc_engine)], expand=True))

        case "add":
            if not rest:
                console.print("  [dim]Usage: mc add <channel_id> [device_index][/]")
            else:
                cid = rest[0]
                dev = int(rest[1]) if len(rest) > 1 else None
                mc_engine.add_local_mic(channel_id=cid, device_index=dev, speaker_name=cid.capitalize())
                console.print(f"  [green]✓[/] Added mic: {cid}" + (f" (device {dev})" if dev is not None else ""))

        case "remove":
            if not rest:
                console.print("  [dim]Usage: mc remove <channel_id>[/]")
            else:
                if mc_engine.remove_channel(rest[0]):
                    console.print(f"  [green]✓[/] Removed: {rest[0]}")
                else:
                    console.print(f"  [red]Channel '{rest[0]}' not found.[/]")

        case "transcript" | "t":
            output = mc_engine.get_transcript("readable")
            console.print(Panel(output, title="[bold]Transcript[/]", border_style="white"))

        case "srt":
            output = mc_engine.get_transcript("srt")
            console.print(Panel(output or "[dim]No entries.[/]", title="[bold]SRT[/]", border_style="white"))

        case "session":
            if rest and rest[0].lower() == "new":
                s = mc_engine.start_session()
                console.print(f"  [green]✓[/] New session: {s.session_id}")
            else:
                s = mc_engine._aggregator.get_session() if mc_engine._aggregator else None
                if s:
                    console.print(f"  Session: [bold]{s.session_id}[/] | Entries: {s.total_segments} | Audio: {s.total_audio_duration:.1f}s | Speakers: {', '.join(s.get_speakers()) or 'none'}")
                else:
                    console.print("  [dim]No active session.[/]")

        case _:
            console.print(f"  [dim]Unknown mc command: {sub}[/]")

    return mc_engine


def _mc_init(engine: STTEngine, config: dict):
    """初始化多通道引擎"""
    from echo_stt.core.multi_channel_engine import MultiChannelSTTEngine

    config.setdefault("multi_channel", {})["enabled"] = True
    mc = MultiChannelSTTEngine(config=config, speaker_module=engine.speaker_module if engine else None)
    mc._stt_engine = engine

    if mc.initialize():
        console.print("  [green]✓[/] Multi-channel engine ready.")
        return mc
    else:
        console.print("  [red]✗ Multi-channel init failed.[/]")
        return None


def _display_result(result: dict) -> None:
    """顯示辨識結果"""
    text = result.get("text", "")
    confidence = result.get("confidence", 0.0)
    proc_time = result.get("processing_time", 0.0)
    error = result.get("error")

    if error:
        console.print(f"  [red]✗ {error}[/]")
        return
    if not text or text.isspace():
        console.print("  [dim]🔇 No speech detected.[/dim]")
        return

    conf_color = "green" if confidence >= 0.8 else "yellow" if confidence >= 0.5 else "red"
    console.print(Panel(
        f"[bold]{text}[/]",
        title=f"[{conf_color}]Confidence: {confidence:.2f}[/{conf_color}] | Time: {proc_time:.2f}s",
        border_style=conf_color,
    ))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主 CLI 類
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class EchoSTTCLI:
    """Echo STT System 互動式 Rich CLI"""

    def __init__(self, device: Optional[int] = None, model: Optional[str] = None,
                 task: Optional[str] = None, mc: bool = False):
        self.device = device
        self.model = model
        self.task = task
        self.mc_flag = mc
        self.config: dict = {}
        self.engine: Optional[STTEngine] = None
        self.mc_engine = None
        self.history: list = []

    def initialize(self) -> bool:
        """初始化 STT 引擎"""
        _require_rich()
        console.print(Panel(
            "[bold cyan]Echo STT System[/]\n"
            "[dim]Whisper + VAD + Speaker ID + Multi-Channel — Speech recognition playground[/]",
            border_style="cyan",
        ))

        self.config = load_config()

        # CLI 參數覆蓋
        if self.device is not None:
            self.config.setdefault("audio", {})["device_index"] = self.device
        if self.model:
            wcfg = self.config.setdefault("whisper", {})
            wcfg["model_id"] = self.model
            wcfg["use_local_model"] = False
            m = self.model
            if "/" in m:
                name = m.split("/")[-1]
                for prefix in ("whisper-", "faster-whisper-"):
                    if name.startswith(prefix):
                        name = name[len(prefix):]
                        break
                wcfg["model_size"] = name
            else:
                wcfg["model_size"] = m
        if self.task:
            self.config.setdefault("recognition", {})["task"] = self.task

        # 載入 STTEngine
        try:
            with console.status("[cyan]Loading Whisper model...[/]", spinner="dots"):
                self.engine = STTEngine(config=self.config)
                success = self.engine.initialize()
        except Exception as e:
            console.print(f"  [red]✗ Engine init failed: {e}[/]")
            return False

        if not success:
            console.print("  [red]✗ Engine initialization failed![/]")
            return False

        backend = getattr(self.engine, "_backend_type", "?")
        model_name = getattr(self.engine, "_fw_model_size", self.engine.whisper_model_id)
        console.print(f"  [green]✓[/] Backend: [bold]{backend}[/] ({model_name})")
        console.print(f"  [green]✓[/] Device: [bold]{self.engine.device}[/]")
        console.print(f"  [green]✓[/] Task: [bold]{self.engine._recognition_task}[/]")

        # 多通道引擎
        mc_enabled = get("multi_channel.enabled", False, config=self.config)
        if self.mc_flag or mc_enabled:
            self.mc_engine = _mc_init(self.engine, self.config)

        return True

    def show_menu(self):
        """顯示主選單"""
        grid = Table(show_header=False, box=None, padding=(0, 2), show_edge=False)
        grid.add_column(width=22)
        grid.add_column(ratio=1)

        commands = [
            ("record [SEC]",        "🎤 Record & transcribe (default 5s)",      "green"),
            ("listen [SEC]",        "👂 Continuous listening (default 30s)",     "green"),
            ("text <TEXT>",         "📝 Text input (bypass STT)",               "green"),
            ("", "", ""),
            ("dashboard / d",       "📊 Engine status & stats",                 "cyan"),
            ("history / h",         "📜 Recognition history",                   "cyan"),
            ("vad",                 "🔊 VAD & module status",                   "cyan"),
            ("devices",             "🎧 List audio input devices",              "cyan"),
            ("device [idx|auto]",   "🎧 Show/switch input device",             "cyan"),
            ("", "", ""),
            ("config",              "⚙️  Show/set config (set/reload)",          "yellow"),
            ("", "", ""),
        ]

        if self.mc_engine is not None:
            commands += [
                ("mc",              "🔀 Multi-channel status",                  "magenta"),
                ("mc start/stop",   "🔀 Control engine lifecycle",              "magenta"),
                ("mc add/remove",   "🔀 Manage channels",                       "magenta"),
                ("mc transcript",   "🔀 Show transcript",                       "magenta"),
                ("", "", ""),
            ]
        else:
            commands += [
                ("mc init",         "🔀 Initialize multi-channel engine",       "dim"),
                ("", "", ""),
            ]

        commands += [
            ("help",                "📋 Show this menu",                        "dim"),
            ("exit / q",            "👋 Exit",                                  "dim"),
        ]

        for cmd, desc, style in commands:
            if not cmd:
                grid.add_row("", "")
            else:
                grid.add_row(f"[bold {style}]{cmd}[/]", f"[{style}]{desc}[/]")

        console.print()
        console.print(grid)
        console.print()

    def show_dashboard(self):
        """完整狀態儀表板"""
        console.print()
        console.print(Columns([render_engine_panel(self.engine), render_stats_panel(self.engine)], expand=True))

        if self.mc_engine is not None and self.mc_engine.is_initialized:
            console.print(Columns([render_mc_panel(self.mc_engine), render_channel_table(self.mc_engine)], expand=True))

        console.print(render_history_panel(self.history))

    def run(self):
        """啟動 CLI"""
        if not self.initialize():
            return

        self.show_menu()

        try:
            self._main_loop()
        finally:
            self._cleanup()

    def _main_loop(self):
        """主迴圈"""
        while True:
            try:
                command = Prompt.ask("\n[bold cyan]stt[/]")
            except (EOFError, KeyboardInterrupt):
                console.print()
                break

            command = command.strip()
            if not command:
                continue

            try:
                parts = shlex.split(command)
            except ValueError as exc:
                console.print(f"  [red]Parse error: {exc}[/]")
                continue

            if not parts:
                continue

            verb = parts[0].lower()
            args = parts[1:]

            try:
                match verb:
                    case "record" | "rec" | "r":
                        _cmd_record(self.engine, args, self.history)

                    case "listen" | "l":
                        _cmd_listen(self.engine, args, self.history)

                    case "text":
                        text = " ".join(args).strip()
                        if not text:
                            console.print("  [dim]Usage: text <message>[/]")
                        else:
                            result = self.engine.handle_text_input(text)
                            _display_result(result)
                            self.history.append(result)

                    case "dashboard" | "d" | "status":
                        self.show_dashboard()

                    case "history" | "h":
                        console.print(render_history_panel(self.history, last_n=20))

                    case "vad":
                        console.print(render_vad_panel(self.engine))

                    case "devices":
                        _cmd_devices(self.engine)

                    case "device" | "dev":
                        _cmd_device(self.engine, args)

                    case "config":
                        self.config = _cmd_config(self.config, args)

                    case "mc":
                        result = _cmd_mc(self.mc_engine, self.engine, self.config, args)
                        if result is not None:
                            self.mc_engine = result

                    case "help" | "?" | "menu":
                        self.show_menu()

                    case "exit" | "quit" | "q":
                        break

                    case _:
                        console.print(f"  [dim]Unknown: {verb}. Type 'help' for commands.[/]")

            except KeyboardInterrupt:
                console.print("\n  [yellow]Interrupted. Type 'exit' to quit.[/]")
            except Exception as e:
                console.print(f"  [red]Error: {e}[/]")

    def _cleanup(self):
        """釋放資源"""
        if self.mc_engine is not None:
            try:
                self.mc_engine._stt_engine = None  # 不讓 mc 關掉共用引擎
                self.mc_engine.shutdown()
            except Exception:
                pass

        if self.engine:
            try:
                self.engine.shutdown()
                console.print("[dim]Engine unloaded. Bye![/]")
            except Exception:
                console.print("[dim]Bye![/]")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 非互動指令
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def smoke() -> int:
    """環境驗證"""
    config = load_config()

    if _HAS_RICH:
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("key", style="dim")
        table.add_column("value", style="bold cyan")

        table.add_row("Config", "loaded" if config else "empty")
        table.add_row("Backend", get("whisper.backend", "?", config=config))
        table.add_row("Model", get("whisper.model_size", "?", config=config))
        table.add_row("Multi-Channel", str(get("multi_channel.enabled", False, config=config)))

        console.print(Panel(table, title="[bold]STT Scaffold[/]", border_style="green"))
    else:
        print(f"Config: {'loaded' if config else 'empty'}")
        print(f"Backend: {get('whisper.backend', '?', config=config)}")

    return 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Argparse 入口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def main() -> int:
    parser = argparse.ArgumentParser(description="Echo STT System — Interactive Rich CLI + tools")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("smoke", help="Verify config and environment")

    shell_cmd = subparsers.add_parser("shell", help="Start the interactive Rich CLI")
    shell_cmd.add_argument("--device", type=int, help="Microphone device index")
    shell_cmd.add_argument("--model", type=str, help="Override Whisper model (e.g. base, large-v3)")
    shell_cmd.add_argument("--task", choices=["transcribe", "translate"], help="Recognition task")
    shell_cmd.add_argument("--mc", action="store_true", help="Enable multi-channel engine")
    shell_cmd.add_argument("--debug", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    # 設定 logging
    import logging
    debug = getattr(args, "debug", False)
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.WARNING,
        format="%(name)s | %(message)s",
    )

    # 無參數或 shell → 互動式 CLI
    if args.command is None or args.command == "shell":
        cli = EchoSTTCLI(
            device=getattr(args, "device", None),
            model=getattr(args, "model", None),
            task=getattr(args, "task", None),
            mc=getattr(args, "mc", False),
        )
        cli.run()
        return 0

    if args.command == "smoke":
        return smoke()

    raise ValueError(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
