"""
Echo TTS System — 互動式 Rich CLI + 合成工具

Usage:
    python Entry.py              # 互動式 Rich CLI（預設進入 shell 模式）
    python Entry.py shell        # 同上
    python Entry.py generate     # 單次生成
    python Entry.py profile      # 單次 profiling
    python Entry.py smoke        # 驗證 scaffold
"""
from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# ── Rich imports（graceful fallback for test environments without rich） ──
try:
    from rich.console import Console as _RichConsole
    from rich.panel import Panel
    from rich.table import Table
    from rich.columns import Columns
    from rich.text import Text
    from rich.prompt import Prompt
    from rich import box

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

# ── Project imports（always available） ──
from test_tts.benchmarks.metrics import LatencySample, ProfileReport, summarize_latency, summarize_profiles
from test_tts.cli.interactive import (
    EMOTION_LABELS,
    EMOTION_PRESETS,
    SUPPORTED_LANGUAGES,
    _detect_language,
    _translate_text,
)
from test_tts.runtime.audio import NullAudioPlayer, StreamingAudioPlayer
from test_tts.runtime.backend_contract import legacy_backend_info
from test_tts.runtime.legacy_backend import LegacyIndexTTS2Backend
from test_tts.runtime.legacy_generate import default_generation_paths, generate_with_legacy_indextts2
from test_tts.runtime.session import TTSInteractiveSession

console = _RichConsole()


def _require_rich():
    """Guard for features that need the full Rich library."""
    if not _HAS_RICH:
        print("Error: 'rich' package is required for the interactive CLI.")
        print("Install it with: pip install rich>=13.7.0")
        raise SystemExit(1)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Rich UI 元件
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def render_status_panel(session: TTSInteractiveSession) -> Panel:
    """渲染引擎狀態面板"""
    status = session.status()

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("key", style="dim")
    table.add_column("value", style="bold cyan")

    loaded_style = "green" if status["loaded"] else "red"
    table.add_row("Engine", Text("loaded" if status["loaded"] else "not loaded", style=loaded_style))
    table.add_row("Say Count", str(status["say_count"]))
    table.add_row("Save Count", str(status["save_count"]))
    table.add_row("Play Count", str(status["play_count"]))
    table.add_row("Output Dir", str(status["output_dir"]))

    if status["last_output_path"]:
        table.add_row("Last Output", str(status["last_output_path"]))

    return Panel(table, title="[bold]Engine Status[/]", border_style="cyan", width=50)


def render_emotion_panel(session: TTSInteractiveSession) -> Panel:
    """渲染情緒向量面板"""
    current = session.get_emotion()

    table = Table(box=box.SIMPLE, show_lines=False, padding=(0, 1))
    table.add_column("Dimension", style="bold", width=14)
    table.add_column("Value", style="cyan", width=6, justify="right")
    table.add_column("Bar", ratio=1)

    for label, value in zip(EMOTION_LABELS, current):
        bar_len = int(value * 20)
        bar_color = "green" if value < 0.15 else "yellow" if value < 0.25 else "red"
        bar = Text("█" * bar_len + "░" * (20 - bar_len), style=bar_color)
        table.add_row(label.capitalize(), f"{value:.2f}", bar)

    # 偵測是否匹配某個 preset
    matched_preset = None
    for name, vec in EMOTION_PRESETS.items():
        if all(abs(a - b) < 0.001 for a, b in zip(current, vec)):
            matched_preset = name
            break

    title = "[bold]Emotion Vector[/]"
    if matched_preset:
        title += f"  [dim]({matched_preset})[/]"

    return Panel(table, title=title, border_style="magenta", width=50)


def render_lora_panel(session: TTSInteractiveSession) -> Panel:
    """渲染 LoRA 狀態面板"""
    status = session.get_lora_status()

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("key", style="dim")
    table.add_column("value", style="bold")

    active = status.get("active", False)
    table.add_row("Active", Text("ON" if active else "OFF", style="green" if active else "dim"))
    if active:
        table.add_row("Scale", f"[cyan]{status.get('scale', 0):.0%}[/]")
        table.add_row("Delta Keys", str(status.get("delta_keys", 0)))
        if status.get("path"):
            path_str = str(status["path"])
            if len(path_str) > 45:
                path_str = "…" + path_str[-44:]
            table.add_row("Path", f"[dim]{path_str}[/]")

    return Panel(table, title="[bold]🧬 LoRA[/]", border_style="yellow", width=50)


def render_language_panel(session: TTSInteractiveSession) -> Panel:
    """渲染語言設定面板"""
    current = session.get_language()
    name = SUPPORTED_LANGUAGES.get(current, current)

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("key", style="dim")
    table.add_column("value", style="bold cyan")

    table.add_row("Current", f"{current} ({name})")
    others = [f"{k} ({v})" for k, v in SUPPORTED_LANGUAGES.items() if k != current]
    table.add_row("Available", ", ".join(others))
    table.add_row("Auto-translate", "[green]enabled[/]")

    return Panel(table, title="[bold]🌐 Language[/]", border_style="blue", width=50)


def render_poise_panel(session: TTSInteractiveSession) -> Panel:
    """渲染 Poise 呼吸法面板"""
    cfg = session.get_poise_config()

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("key", style="dim")
    table.add_column("value", style="bold")

    enabled = cfg.get("enabled", False)
    table.add_row("Enabled", Text("ON" if enabled else "OFF", style="green" if enabled else "dim"))
    table.add_row("Potency", f"[cyan]{cfg.get('potency', 0):.2f}[/]")
    table.add_row("Count", f"[cyan]{cfg.get('count', 0):.1f}[/] [dim](every N segments)[/]")
    table.add_row("Count Jitter", f"[dim]{cfg.get('count_jitter', 0):.2f}[/]")
    table.add_row("Min Segments", f"[dim]{cfg.get('min_segments_to_trigger', 3)}[/]")
    table.add_row("Emotion Correction", "[green]on[/]" if cfg.get("emotion_correction_enabled", True) else "[dim]off[/]")

    return Panel(table, title="[bold]🫁 Poise (Breathing)[/]", border_style="green", width=50)


def render_profile_panel(report: ProfileReport) -> Panel:
    """將 ProfileReport 渲染為 Rich 面板"""
    s = report.synthesis
    i = report.init

    sections: list[Table | Text] = []

    # Cold start section
    if i.total_init_ms > 0:
        init_table = Table(show_header=False, box=None, padding=(0, 2))
        init_table.add_column("stage", style="dim", width=24)
        init_table.add_column("ms", style="cyan", width=12, justify="right")

        init_table.add_row("GPT model load", f"{i.gpt_ms:.1f} ms")
        init_table.add_row("Semantic Codec load", f"{i.semantic_codec_ms:.1f} ms")
        init_table.add_row("S2Mel model load", f"{i.s2mel_ms:.1f} ms")
        init_table.add_row("BigVGAN vocoder load", f"{i.bigvgan_ms:.1f} ms")
        init_table.add_row("Emo/Spk matrices", f"{i.emo_matrix_ms:.1f} ms")
        init_table.add_row("Text processor", f"{i.text_processor_ms:.1f} ms")
        init_table.add_row("Character load", f"{i.char_load_ms:.1f} ms")
        init_table.add_row("Warmup", f"{i.warmup_ms:.1f} ms")
        init_table.add_row(
            Text("Total cold start", style="bold"), Text(f"{i.total_cold_start_ms:.1f} ms", style="bold cyan")
        )
        sections.append(
            Panel(init_table, title="[bold]◆ Cold Start[/]", border_style="yellow", padding=(0, 1))
        )

    # Synthesis section
    seg_label = f", {s.segment_count} segments" if s.segment_count > 1 else ""
    synth_table = Table(show_header=False, box=None, padding=(0, 2))
    synth_table.add_column("stage", style="dim", width=24)
    synth_table.add_column("ms", style="cyan", width=12, justify="right")

    synth_table.add_row("Text normalization", f"{s.text_norm_ms:.1f} ms")
    synth_table.add_row("Emotion processing", f"{s.emotion_ms:.1f} ms")
    synth_table.add_row("GPT semantic gen", f"{s.gpt_semantic_ms:.1f} ms")
    synth_table.add_row("GPT latent forward", f"{s.gpt_latent_ms:.1f} ms")
    synth_table.add_row("S2Mel / CFM", f"{s.s2mel_cfm_ms:.1f} ms")
    synth_table.add_row("BigVGAN vocoder", f"{s.vocoder_ms:.1f} ms")
    synth_table.add_row("WAV save", f"{s.wav_save_ms:.1f} ms")
    synth_table.add_row(
        Text("Total wall time", style="bold"), Text(f"{s.total_wall_ms:.1f} ms", style="bold cyan")
    )
    sections.append(
        Panel(synth_table, title=f"[bold]◆ Synthesis ({s.text_chars} chars{seg_label})[/]", border_style="blue", padding=(0, 1))
    )

    # Summary section
    summary_table = Table(show_header=False, box=None, padding=(0, 2))
    summary_table.add_column("key", style="dim", width=24)
    summary_table.add_column("value", style="bold cyan", width=12, justify="right")

    summary_table.add_row("Audio duration", f"{s.audio_duration_ms:.1f} ms")
    rtf_style = "bold green" if s.rtf < 1.0 else "bold red"
    summary_table.add_row("RTF", Text(f"{s.rtf:.3f}", style=rtf_style))
    if s.rtf > 0:
        rtf_label = "faster" if s.rtf < 1.0 else "slower"
        summary_table.add_row("Speed", f"{1 / s.rtf:.1f}x {rtf_label} than real-time")
    summary_table.add_row("First audio proxy", f"{s.first_audio_ms:.1f} ms")
    sections.append(
        Panel(summary_table, title="[bold]◆ Summary[/]", border_style="green", padding=(0, 1))
    )

    # Combine all sections
    from rich.console import Group
    return Panel(Group(*sections), title="[bold]⏱  TTS Profiling Report[/]", border_style="cyan")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 語言 / 翻譯 工具
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def auto_translate(text: str, session: TTSInteractiveSession) -> str:
    """自動偵測語言並路由。

    - 支援語言 (zh/en) → 自動切換 session language，原文直接通過
    - 不支援語言 → 翻譯成當前 session language
    """
    detected = _detect_language(text)

    if detected in ("zh", "en"):
        current = session.get_language()
        if detected != current:
            session.set_language(detected)
            det_name = SUPPORTED_LANGUAGES.get(detected, detected)
            console.print(f"  [dim]🌐 Auto-language: {det_name}[/]")
        return text

    # 不支援的語言：翻譯
    target_lang = session.get_language()
    translated = _translate_text(text, target_lang, source_lang="auto")
    if translated:
        tgt_name = SUPPORTED_LANGUAGES.get(target_lang, target_lang)
        console.print(f"  [dim]🔄 Auto-translate → {tgt_name}: {translated}[/]")
        return translated

    console.print("  [yellow]⚠ Translation failed, using original text[/]")
    return text


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 指令處理
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _cmd_say(session: TTSInteractiveSession, text: str) -> None:
    """生成並播放"""
    text = auto_translate(text, session)
    with console.status("[cyan]Generating...[/]", spinner="dots"):
        result = session.say(text)
    console.print(f"  [green]✓[/] played: [dim]{result.output_path}[/]")


def _cmd_save(session: TTSInteractiveSession, args: list[str]) -> None:
    """生成並保存"""
    text, output = _parse_save_args(args)
    text = auto_translate(text, session)

    with console.status("[cyan]Generating...[/]", spinner="dots"):
        if output is not None:
            result = session.save(text, output_path=output)
        else:
            result = session.save_auto(text)
    console.print(f"  [green]✓[/] saved: [cyan]{result.output_path}[/]")


def _cmd_profile(session: TTSInteractiveSession, text: str) -> None:
    """帶 profiling 的合成"""
    text = auto_translate(text, session)
    with console.status("[cyan]Profiling...[/]", spinner="dots"):
        result = session.profile(text)
    console.print(render_profile_panel(result.report))


def _cmd_emotion(session: TTSInteractiveSession, args: list[str]) -> None:
    """顯示或設定情緒"""
    if not args:
        console.print(render_emotion_panel(session))
        return

    name = args[0].lower()
    if name in EMOTION_PRESETS:
        vec = EMOTION_PRESETS[name]
        session.set_emotion(list(vec))
        pairs = [f"{l}={v:.2f}" for l, v in zip(EMOTION_LABELS, vec)]
        console.print(f"  [green]✓[/] Emotion set to [magenta]{name}[/]: [{', '.join(pairs)}]")
        return

    # 嘗試 8 個浮點數
    try:
        values = [float(a) for a in args]
        if len(values) != 8:
            console.print(f"  [yellow]Expected 8 values, got {len(values)}. Use: emotion <preset> or emotion H A S F D M U C[/]")
            return
        session.set_emotion(values)
        pairs = [f"{l}={v:.2f}" for l, v in zip(EMOTION_LABELS, values)]
        console.print(f"  [green]✓[/] Emotion set: [{', '.join(pairs)}]")
    except ValueError:
        console.print(f"  [yellow]Unknown preset '{name}'. Available: {', '.join(EMOTION_PRESETS.keys())}[/]")


def _cmd_language(session: TTSInteractiveSession, args: list[str]) -> None:
    """顯示或設定語言"""
    if not args:
        console.print(render_language_panel(session))
        return

    lang = args[0].lower()
    if lang not in SUPPORTED_LANGUAGES:
        console.print(f"  [yellow]Unsupported language '{lang}'. Available: {', '.join(SUPPORTED_LANGUAGES.keys())}[/]")
        return

    session.set_language(lang)
    name = SUPPORTED_LANGUAGES[lang]
    console.print(f"  [green]✓[/] Language set to: [cyan]{lang} ({name})[/]")


def _cmd_lora(session: TTSInteractiveSession, args: list[str]) -> None:
    """LoRA 控制"""
    status = session.get_lora_status()

    if not args:
        # 無參數：toggle
        if status["active"]:
            try:
                session.unload_lora()
                console.print("  [green]✓[/] 🧬 LoRA [red]OFF[/] — 原始權重已還原")
            except Exception as e:
                console.print(f"  [red]Error: {e}[/]")
        else:
            try:
                session.load_lora()
                new = session.get_lora_status()
                console.print(f"  [green]✓[/] 🧬 LoRA [green]ON[/]  scale={new['scale']:.0%}")
            except Exception as e:
                console.print(f"  [red]LoRA load failed: {e}[/]")
        return

    subcmd = args[0].lower()

    if subcmd == "status":
        console.print(render_lora_panel(session))
        return

    if subcmd == "on":
        scale = float(args[1]) if len(args) > 1 else None
        try:
            if scale is not None:
                session.load_lora(scale=scale)
            else:
                session.load_lora()
            new = session.get_lora_status()
            console.print(f"  [green]✓[/] 🧬 LoRA [green]ON[/]  scale={new['scale']:.0%}")
        except Exception as e:
            console.print(f"  [red]LoRA load failed: {e}[/]")
        return

    if subcmd == "off":
        try:
            session.unload_lora()
            console.print("  [green]✓[/] 🧬 LoRA [red]OFF[/] — 原始權重已還原")
        except Exception as e:
            console.print(f"  [red]Error: {e}[/]")
        return

    if subcmd == "scale" and len(args) > 1:
        try:
            scale = float(args[1])
            if not status["active"]:
                session.load_lora(scale=scale)
            else:
                session.set_lora_scale(scale)
            console.print(f"  [green]✓[/] 🧬 LoRA [green]ON[/]  scale={scale:.0%}")
        except ValueError:
            console.print(f"  [yellow]Invalid scale: {args[1]}[/]")
        except RuntimeError as e:
            console.print(f"  [red]Error: {e}[/]")
        return

    # 直接給 scale 值: "lora 0.85"
    try:
        scale = float(subcmd)
        if not status["active"]:
            session.load_lora(scale=scale)
        else:
            session.set_lora_scale(scale)
        console.print(f"  [green]✓[/] 🧬 LoRA [green]ON[/]  scale={scale:.0%}")
        return
    except ValueError:
        pass

    console.print("  [dim]Usage: lora [on|off|status|scale <0-1>|<0-1>][/]")


def _cmd_poise(session: TTSInteractiveSession, args: list[str]) -> None:
    """Poise 呼吸法控制"""
    if not args:
        # 無參數：顯示狀態
        console.print(render_poise_panel(session))
        return

    subcmd = args[0].lower()

    if subcmd == "status":
        console.print(render_poise_panel(session))
        return

    if subcmd == "on":
        session.set_poise_enabled(True)
        console.print("  [green]✓[/] 🫁 Poise [green]ON[/]")
        return

    if subcmd == "off":
        session.set_poise_enabled(False)
        console.print("  [green]✓[/] 🫁 Poise [dim]OFF[/]")
        return

    if subcmd in ("potency", "pot", "p") and len(args) > 1:
        try:
            value = float(args[1])
            session.set_poise_potency(value)
            console.print(f"  [green]✓[/] 🫁 Potency set to [cyan]{value:.2f}[/]")
        except ValueError:
            console.print(f"  [yellow]Invalid potency: {args[1]}[/]")
        return

    if subcmd in ("count", "cnt", "c") and len(args) > 1:
        try:
            value = float(args[1])
            session.set_poise_count(value)
            console.print(f"  [green]✓[/] 🫁 Count set to [cyan]{value:.1f}[/]")
        except ValueError:
            console.print(f"  [yellow]Invalid count: {args[1]}[/]")
        return

    console.print(
        "  [dim]Usage: poise [on|off|status|potency <0-1>|count <N>][/]\n"
        "  [dim]  potency — rest intensity (0=silence only, 1=full action)[/]\n"
        "  [dim]  count   — trigger every N segments (min 2)[/]"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 解析工具
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _parse_text_args(args: list[str], command_name: str) -> str:
    text = " ".join(args).strip()
    if not text:
        raise ValueError(f"{command_name} requires text")
    return text


def _parse_save_args(args: list[str]) -> tuple[str, str | None]:
    output: str | None = None
    text_parts: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--output":
            if index + 1 >= len(args):
                raise ValueError("--output requires a path")
            output = args[index + 1]
            index += 2
            continue
        text_parts.append(arg)
        index += 1

    text = " ".join(text_parts).strip()
    if not text:
        raise ValueError("save requires text")

    if output is not None:
        p = Path(output)
        if p.suffix.lower() not in (".wav", ".mp3", ".flac", ".ogg"):
            output = str(p.with_suffix(".wav"))

    return text, output


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主 CLI 類
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class EchoTTSCLI:
    """Echo TTS System 互動式 Rich CLI"""

    def __init__(
        self,
        device: str | None = "cuda",
        use_fp16: bool = True,
        use_cuda_kernel: bool = True,
        use_accel: bool = True,
        emotion_vector: list[float] | None = None,
        playback: bool = True,
    ):
        self.device = device
        self.use_fp16 = use_fp16
        self.use_cuda_kernel = use_cuda_kernel
        self.use_accel = use_accel
        self.emotion_vector = emotion_vector
        self.playback = playback
        self.session: TTSInteractiveSession | None = None

    def initialize(self) -> bool:
        """初始化 TTS 引擎"""
        _require_rich()
        console.print(Panel(
            "[bold cyan]Echo TTS System[/]\n"
            "[dim]IndexTTS2 + AccelEngine + Poise — Voice synthesis playground[/]",
            border_style="cyan",
        ))

        try:
            with console.status("[cyan]Building session...[/]", spinner="dots"):
                self.session = build_legacy_session(
                    project_root=Path(__file__).resolve().parent,
                    device=self.device,
                    use_fp16=self.use_fp16,
                    use_cuda_kernel=self.use_cuda_kernel,
                    use_accel=self.use_accel,
                    emotion_vector=self.emotion_vector,
                    playback=self.playback,
                )

            console.print(f"[green]✓[/] Session created — device: [cyan]{self.device}[/]")

        except Exception as e:
            console.print(f"[red]✗ Session init failed: {e}[/]")
            return False

        # 自動載入 engine + LoRA
        try:
            with console.status("[cyan]Loading TTS engine...[/]", spinner="dots"):
                self.session.load()
            console.print("[green]✓[/] Engine loaded")

            try:
                self.session.load_lora()
                lora_status = self.session.get_lora_status()
                console.print(f"[green]✓[/] 🧬 LoRA auto-loaded (scale={lora_status['scale']:.0%})")
            except Exception:
                console.print("[dim]  🧬 LoRA: not available (adapter not found)[/]")

        except Exception as e:
            console.print(f"[yellow]⚠ Engine load failed: {e}[/]")
            console.print("[dim]  Use 'load' to retry[/]")

        return True

    def show_menu(self):
        """顯示主選單"""
        commands = [
            ("say <text>", "🔊 Generate and play (streaming)", "green"),
            ("save [--output f] <text>", "💾 Generate and save (auto-name or specified)", "green"),
            ("profile <text>", "⏱  Synthesis with timing report", "green"),
            ("", "", ""),
            ("emotion", "🎭 Show/set emotion preset or 8D vector", "magenta"),
            ("language", "🌐 Show/set synthesis language", "blue"),
            ("lora", "🧬 Toggle/configure LoRA adapter", "yellow"),
            ("poise", "🫁 Poise breathing system control", "green"),
            ("", "", ""),
            ("status", "📊 Engine status dashboard", "cyan"),
            ("load", "🔧 Load TTS engine", "blue"),
            ("unload", "🔧 Unload TTS engine", "blue"),
            ("", "", ""),
            ("help", "📋 Show this menu", "dim"),
            ("exit", "👋 Exit", "dim"),
        ]

        grid = Table(show_header=False, box=None, padding=(0, 2), show_edge=False)
        grid.add_column(width=18)
        grid.add_column(ratio=1)

        for cmd, desc, style in commands:
            if not cmd:
                grid.add_row("", "")
            else:
                grid.add_row(f"[bold {style}]{cmd}[/]", f"[{style}]{desc}[/]")

        console.print()
        console.print(grid)
        console.print()

    def show_dashboard(self):
        """顯示完整狀態儀表板"""
        console.print()

        # 第一行：Engine + Emotion
        status_panel = render_status_panel(self.session)
        emotion_panel = render_emotion_panel(self.session)
        console.print(Columns([status_panel, emotion_panel], expand=True))

        # 第二行：LoRA + Language
        lora_panel = render_lora_panel(self.session)
        lang_panel = render_language_panel(self.session)
        console.print(Columns([lora_panel, lang_panel], expand=True))

        # 第三行：Poise
        poise_panel = render_poise_panel(self.session)
        console.print(poise_panel)

    def run(self):
        """主迴圈"""
        if not self.initialize():
            return

        self.show_menu()

        while True:
            try:
                command = Prompt.ask("\n[bold cyan]tts[/]")
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Bye![/]")
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
                    case "say" | "s":
                        text = _parse_text_args(args, command_name="say")
                        _cmd_say(self.session, text)

                    case "save":
                        _cmd_save(self.session, args)

                    case "profile" | "p":
                        text = _parse_text_args(args, command_name="profile")
                        _cmd_profile(self.session, text)

                    case "emotion" | "emo":
                        _cmd_emotion(self.session, args)

                    case "language" | "lang":
                        _cmd_language(self.session, args)

                    case "lora":
                        _cmd_lora(self.session, args)

                    case "poise":
                        _cmd_poise(self.session, args)

                    case "status" | "dashboard" | "d":
                        self.show_dashboard()

                    case "load":
                        with console.status("[cyan]Loading...[/]", spinner="dots"):
                            self.session.load()
                        console.print("  [green]✓[/] loaded")

                    case "unload":
                        self.session.unload()
                        console.print("  [green]✓[/] unloaded")

                    case "help" | "?" | "menu":
                        self.show_menu()

                    case "exit" | "quit" | "q":
                        console.print("[dim]Bye![/]")
                        break

                    case _:
                        console.print(f"  [dim]Unknown: {verb}. Type 'help' for commands.[/]")

            except KeyboardInterrupt:
                console.print("\n  [yellow]Interrupted. Type 'exit' to quit.[/]")
            except Exception as e:
                console.print(f"  [red]Error: {e}[/]")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 非互動指令
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def smoke() -> int:
    info = legacy_backend_info(Path(__file__).resolve().parent)

    if _HAS_RICH:
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("key", style="dim")
        table.add_column("value", style="bold cyan")

        table.add_row("Backend", info.name)
        table.add_row("Legacy source", str(info.source_path))
        table.add_row("Emotion vectors", "[green]yes[/]" if info.supports_emotion_vector else "[dim]no[/]")

        console.print(Panel(table, title="[bold]TTS Scaffold[/]", border_style="green"))
    else:
        console.print("TTS experiment scaffold is available.")
        console.print(f"Backend: {info.name}")
        console.print(f"Legacy source: {info.source_path}")
        console.print(f"Emotion vectors: {info.supports_emotion_vector}")
    return 0


def benchmark(text: str) -> int:
    sample = LatencySample(
        text_chars=len(text),
        audio_duration_ms=1000.0,
        gpt_ms=0.0,
        s2mel_ms=0.0,
        vocoder_ms=0.0,
        first_audio_ms=0.0,
    )
    result = summarize_latency([sample])

    if _HAS_RICH:
        table = Table(title="Benchmark Results", box=box.ROUNDED)
        table.add_column("Metric", style="dim")
        table.add_column("Value", style="cyan", justify="right")

        for key, value in result.items():
            if isinstance(value, float):
                table.add_row(key, f"{value:.1f}")
            else:
                table.add_row(key, str(value))

        console.print(table)
    else:
        console.print(result)
    return 0


def generate(text: str, output: str, device: str | None, use_fp16: bool, use_cuda_kernel: bool) -> int:
    with console.status("[cyan]Generating speech...[/]", spinner="dots"):
        output_path = generate_with_legacy_indextts2(
            text=text,
            output_path=output,
            project_root=Path(__file__).resolve().parent,
            device=device,
            use_fp16=use_fp16,
            use_cuda_kernel=use_cuda_kernel,
        )
    console.print(f"[green]✓[/] Generated audio: [cyan]{output_path}[/]")
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
            console.print(f"  [dim]Run {i + 1}/{repeat}...[/]")
        with console.status("[cyan]Profiling...[/]", spinner="dots"):
            result = session.profile(text)
        profiles.append(result.report.synthesis)

        # 第一次顯示完整 Rich report
        if i == 0:
            console.print(render_profile_panel(result.report))

    if repeat > 1:
        console.print()
        summary = summarize_profiles(profiles)

        table = Table(title=f"Summary over {repeat} runs", box=box.ROUNDED)
        table.add_column("Metric", style="dim")
        table.add_column("Value", style="cyan", justify="right")

        for key, value in summary.items():
            if isinstance(value, float):
                table.add_row(key, f"{value:.1f}")
            else:
                table.add_row(key, str(value))

        console.print(table)

    return 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Argparse 入口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def main() -> int:
    parser = argparse.ArgumentParser(description="Echo TTS System — Interactive Rich CLI + tools")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("smoke", help="Verify scaffold and copied legacy source paths")

    bench = subparsers.add_parser("benchmark", help="Run a placeholder benchmark")
    bench.add_argument("--text", default="Hello from the TTS experiment project.")

    gen = subparsers.add_parser("generate", help="Generate speech through copied legacy IndexTTS2")
    gen.add_argument("--text", required=True)
    gen.add_argument("--output", default="outputs/tts/generated.wav")
    gen.add_argument("--device", default="cuda")
    gen.add_argument("--no-fp16", action="store_true")
    gen.add_argument("--no-cuda-kernel", action="store_true")

    shell_cmd = subparsers.add_parser("shell", help="Start the interactive Rich CLI")
    shell_cmd.add_argument("--device", default="cuda")
    shell_cmd.add_argument("--no-fp16", action="store_true")
    shell_cmd.add_argument("--no-cuda-kernel", action="store_true")
    shell_cmd.add_argument("--no-playback", action="store_true", help="Generate on say without audio playback")
    shell_cmd.add_argument("--no-accel", action="store_true", help="Disable AccelInferenceEngine")
    shell_cmd.add_argument("--emotion", type=float, nargs=8, metavar=("H", "A", "S", "F", "D", "M", "U", "C"),
                           help="8D emotion vector (0.0-1.0 each)")

    profile_cmd = subparsers.add_parser("profile", help="Run profiled synthesis with stage-level timing")
    profile_cmd.add_argument("--text", required=True, help="Text to synthesize")
    profile_cmd.add_argument("--device", default="cuda")
    profile_cmd.add_argument("--no-fp16", action="store_true")
    profile_cmd.add_argument("--no-cuda-kernel", action="store_true")
    profile_cmd.add_argument("--no-accel", action="store_true")
    profile_cmd.add_argument("--emotion", type=float, nargs=8, metavar=("H", "A", "S", "F", "D", "M", "U", "C"),
                           help="8D emotion vector (0.0-1.0 each)")
    profile_cmd.add_argument("--repeat", type=int, default=1, help="Number of synthesis runs")

    args = parser.parse_args()

    # 無參數 → 預設進入 Rich CLI
    if args.command is None or args.command == "shell":
        # 從 shell 子命令取參數，或用預設值
        device = getattr(args, "device", "cuda")
        use_fp16 = not getattr(args, "no_fp16", False)
        use_cuda_kernel = not getattr(args, "no_cuda_kernel", False)
        use_accel = not getattr(args, "no_accel", False)
        emotion = getattr(args, "emotion", None)
        playback = not getattr(args, "no_playback", False)

        cli = EchoTTSCLI(
            device=device,
            use_fp16=use_fp16,
            use_cuda_kernel=use_cuda_kernel,
            use_accel=use_accel,
            emotion_vector=emotion,
            playback=playback,
        )
        cli.run()
        return 0

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
