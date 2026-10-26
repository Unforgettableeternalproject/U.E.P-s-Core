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
from echo_tts.benchmarks.metrics import LatencySample, ProfileReport, summarize_latency, summarize_profiles
from echo_tts.cli.interactive import (
    EMOTION_LABELS,
    EMOTION_PRESETS,
    SUPPORTED_LANGUAGES,
    _detect_language,
    _translate_text,
)
from echo_tts.config.emotion_presets import (
    USER_STRENGTH_DEFAULT,
    get_preset_overrides,
    get_preset_vector,
    scale_to_user_strength,
    user_to_strength,
)
from echo_tts.runtime.audio import NullAudioPlayer, StreamingAudioPlayer
from echo_tts.runtime.backend_contract import indextts_backend_info
from echo_tts.runtime.indextts_backend import IndexTTS2Backend
from echo_tts.runtime.indextts_generate import default_generation_paths, generate_with_indextts2
from echo_tts.runtime.session import TTSInteractiveSession

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
    for name, entry in EMOTION_PRESETS.items():
        vec = get_preset_vector(entry)
        if all(abs(a - b) < 0.001 for a, b in zip(current, vec)):
            matched_preset = name
            break

    title = "[bold]Emotion Vector[/]"
    if matched_preset:
        title += f"  [dim]({matched_preset})[/]"

    # 可用預設列表
    preset_names = list(EMOTION_PRESETS.keys())
    preset_line = Text()
    for i, name in enumerate(preset_names):
        style = "bold magenta" if name == matched_preset else "dim"
        preset_line.append(name, style=style)
        if i < len(preset_names) - 1:
            preset_line.append("  ", style="dim")

    from rich.console import Group
    return Panel(
        Group(table, Text(""), Text.assemble(("Presets: ", "dim"), preset_line)),
        title=title, border_style="magenta", width=50,
    )


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


def render_speed_panel(session: TTSInteractiveSession) -> Panel:
    """渲染語速控制面板"""
    info = session.get_speed_info()
    speed = info["speed"]
    stretch = info["stretch_ratio"]
    config_stretch = info["config_stretch_ratio"]

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("key", style="dim")
    table.add_column("value", style="bold")

    # 速度指示
    if abs(speed) < 0.01:
        speed_label = Text("Default", style="cyan")
    elif speed > 0:
        speed_label = Text(f"+{speed:.2f} (faster)", style="green")
    else:
        speed_label = Text(f"{speed:.2f} (slower)", style="yellow")

    table.add_row("Speed", speed_label)
    table.add_row("Stretch Ratio", f"[cyan]{stretch:.3f}[/]")
    table.add_row("Config Default", f"[dim]{config_stretch:.3f}[/]")

    # 視覺化 bar
    bar_pos = int((speed + 1.0) * 10)  # 0~20 range
    bar = "░" * bar_pos + "█" + "░" * (20 - bar_pos)
    bar_text = f"[dim]-1.0[/] {bar} [dim]+1.0[/]"
    table.add_row("", Text.from_markup(bar_text))

    return Panel(table, title="[bold]🎚️ Speed[/]", border_style="blue", width=50)


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

    - 支援語言（SUPPORTED_LANGUAGES：zh/en/ja）→ 自動切換 session language，原文直接通過
    - 不支援語言 → 翻譯成當前 session language
    """
    detected = _detect_language(text)

    if detected in SUPPORTED_LANGUAGES:
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
    """顯示或設定情緒。

    Preset 與手動向量都只定義「情緒配方」（比例），強度用直覺的 0~1 刻度，
    內部線性映射到定案區間 [0.57, 0.73]（0.5 → 甜蜜點 0.65）：
        emotion sad                  → 0.5（預設，= 0.65）
        emotion sad 0                → 最弱（= 0.57）
        emotion sad 1                → 最強（= 0.73）
        emotion 0 0 1 0 0 0 0 0 0.8  → 手動向量 + 強度 0.8
    """
    if not args:
        console.print(render_emotion_panel(session))
        return

    name = args[0].lower()
    if name in EMOTION_PRESETS:
        user_strength = USER_STRENGTH_DEFAULT
        if len(args) > 2:
            console.print(f"  [yellow]Too many arguments. Use: emotion <preset> [strength 0~1][/]")
            return
        if len(args) == 2:
            try:
                user_strength = float(args[1])
            except ValueError:
                console.print(f"  [yellow]Invalid strength '{args[1]}'. Expected a number in 0~1.[/]")
                return

        entry = EMOTION_PRESETS[name]
        overrides = get_preset_overrides(entry)
        vec = scale_to_user_strength(list(get_preset_vector(entry)), user_strength)
        session.set_emotion(vec)
        session.set_emotion_strength(user_to_strength(user_strength))
        session.set_emotion_overrides(overrides)
        pairs = [f"{l}={v:.2f}" for l, v in zip(EMOTION_LABELS, vec)]
        override_info = f"  [dim](overrides: {overrides})[/]" if overrides else ""
        console.print(f"  [green]✓[/] Emotion set to [magenta]{name}[/] "
                      f"@ {max(0.0, min(1.0, user_strength)):.2f} "
                      f"[dim](→ {user_to_strength(user_strength):.3f})[/]: "
                      f"[{', '.join(pairs)}]{override_info}")
        return

    # 嘗試 8 個浮點數（可再帶第 9 個當強度）
    try:
        values = [float(a) for a in args]
    except ValueError:
        console.print(f"  [yellow]Unknown preset '{name}'. Available: {', '.join(EMOTION_PRESETS.keys())}[/]")
        return

    user_strength = USER_STRENGTH_DEFAULT
    if len(values) == 9:
        values, user_strength = values[:8], values[8]
    if len(values) != 8:
        console.print(f"  [yellow]Expected 8 values, got {len(values)}. "
                      f"Use: emotion <preset> [strength 0~1] or emotion H A S F D M U C [strength 0~1][/]")
        return

    vec = scale_to_user_strength(values, user_strength)
    session.set_emotion(vec)
    session.set_emotion_strength(user_to_strength(user_strength))
    session.set_emotion_overrides(None)  # 手動向量清除 overrides
    pairs = [f"{l}={v:.2f}" for l, v in zip(EMOTION_LABELS, vec)]
    console.print(f"  [green]✓[/] Emotion set @ {max(0.0, min(1.0, user_strength)):.2f} "
                  f"[dim](→ {user_to_strength(user_strength):.3f})[/]: [{', '.join(pairs)}]")


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


def _cmd_speed(session: TTSInteractiveSession, args: list[str]) -> None:
    """語速控制"""
    if not args:
        # 無參數：顯示狀態
        console.print(render_speed_panel(session))
        return

    subcmd = args[0].lower()

    if subcmd == "status":
        console.print(render_speed_panel(session))
        return

    if subcmd == "reset":
        result = session.set_speed(0.0)
        console.print(f"  [green]✓[/] 🎚️ Speed reset → [cyan]{result:+.1f}[/]")
        return

    if subcmd == "set" and len(args) > 1:
        try:
            value = float(args[1])
            result = session.set_speed(value)
            info = session.get_speed_info()
            console.print(f"  [green]✓[/] 🎚️ Speed set to [cyan]{result:+.2f}[/]  (stretch={info['stretch_ratio']:.3f})")
        except ValueError:
            console.print(f"  [yellow]Invalid value: {args[1]}[/]")
        return

    # 嘗試解析為微調值：speed +0.3 / speed -0.2
    try:
        delta = float(subcmd)
        result = session.adjust_speed(delta)
        info = session.get_speed_info()
        console.print(f"  [green]✓[/] 🎚️ Speed adjusted to [cyan]{result:+.2f}[/]  (stretch={info['stretch_ratio']:.3f})")
        return
    except ValueError:
        pass

    console.print(
        "  [dim]Usage: speed [+/-N | set <N> | reset | status][/]\n"
        "  [dim]  speed +0.3    — 基於目前加快 0.3[/]\n"
        "  [dim]  speed -0.2    — 基於目前減慢 0.2[/]\n"
        "  [dim]  speed set 0.5 — 設定為 0.5[/]\n"
        "  [dim]  speed reset   — 回到預設 (0)[/]\n"
        "  [dim]  Range: [-1.0, +1.0]  正=快 負=慢[/]"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 解析工具
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _extract_batch_flag(args: list[str]) -> tuple[bool, list[str]]:
    """Extract ``--batch`` flag from args, return (is_batch, remaining_args)."""
    if "--batch" in args:
        remaining = [a for a in args if a != "--batch"]
        return True, remaining
    return False, args


def _collect_multiline(delimiter: str = "EOF") -> str:
    """在互動 shell 中收集多行輸入，直到遇到 delimiter 行。"""
    lines: list[str] = []
    console.print(f"  [dim]（多行輸入模式 — 輸入 '{delimiter}' 結束）[/]")
    while True:
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            console.print("\n  [yellow]⚠ 輸入中斷[/]")
            break
        if line.strip() == delimiter:
            break
        lines.append(line)
    text = "\n".join(lines)
    if text:
        console.print(f"  [dim]📝 已讀取 {len(lines)} 行，{len(text)} 字元[/]")
    return text


def _read_text_file(path: str) -> str:
    """讀取文字檔內容。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"找不到檔案: {path}")
    if not p.is_file():
        raise ValueError(f"不是檔案: {path}")
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"檔案為空: {path}")
    line_count = text.count("\n") + 1
    console.print(f"  [dim]📄 已讀取 {p.name}（{line_count} 行，{len(text)} 字元）[/]")
    return text


def _resolve_text_input(args: list[str]) -> list[str]:
    """處理 --file 和 <<DELIMITER 語法，回傳替換後的 args。

    --file <path>  → 讀取檔案內容取代這兩個 token
    <<DELIMITER    → 進入多行輸入模式，收集到 DELIMITER 為止
    <<             → 同上，DELIMITER 預設為 EOF
    """
    resolved: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]

        if arg == "--file":
            if i + 1 >= len(args):
                raise ValueError("--file 需要指定檔案路徑")
            text = _read_text_file(args[i + 1])
            resolved.append(text)
            i += 2
            continue

        if arg.startswith("<<"):
            delimiter = arg[2:].strip() or "EOF"
            text = _collect_multiline(delimiter)
            if not text:
                raise ValueError("多行輸入為空")
            resolved.append(text)
            i += 1
            continue

        resolved.append(arg)
        i += 1

    return resolved


def _cmd_emotion_batch(
    session: TTSInteractiveSession,
    text: str,
    mode: str = "save",
    output_stem: str | None = None,
) -> None:
    """Generate the same text with every emotion preset (Rich version)."""
    from datetime import datetime

    original_emotion = session.get_emotion()
    original_overrides = session.get_emotion_overrides()
    presets = list(EMOTION_PRESETS.items())
    total = len(presets)

    console.print(f"\n  [cyan]⏳[/] Generating [bold]{total}[/] emotion variants...")

    # Determine output directory and stem
    if output_stem is not None:
        p = Path(output_stem)
        stem = p.stem
        suffix = p.suffix if p.suffix.lower() in (".wav", ".mp3", ".flac", ".ogg") else ".wav"
        out_dir = p.parent if p.is_absolute() else session.output_dir / p.parent
    else:
        stem = f"batch-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        suffix = ".wav"
        out_dir = session.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[tuple[str, Path]] = []
    for i, (preset_name, entry) in enumerate(presets):
        vec = get_preset_vector(entry)
        overrides = get_preset_overrides(entry)
        session.set_emotion(vec)
        session.set_emotion_overrides(overrides)
        out_path = out_dir / f"{stem}_{preset_name}{suffix}"

        try:
            with console.status(f"  [cyan][{i + 1}/{total}] {preset_name}...[/]", spinner="dots"):
                result = session.save(text, output_path=out_path)
            if mode == "say":
                session.player.play(result.output_path)
            results.append((preset_name, result.output_path))
            console.print(f"  [green]✓[/] [{i + 1}/{total}] [magenta]{preset_name}[/] → [dim]{result.output_path}[/]")
        except Exception as exc:
            console.print(f"  [red]✗[/] [{i + 1}/{total}] [magenta]{preset_name}[/] — {exc}")

    # Restore original emotion + overrides
    session.set_emotion(original_emotion)
    session.set_emotion_overrides(original_overrides)

    console.print(f"\n  [green]✓[/] Batch done — [bold]{len(results)}/{total}[/] files generated")
    if _HAS_RICH:
        result_table = Table(show_header=False, box=None, padding=(0, 2))
        result_table.add_column("preset", style="magenta", width=14)
        result_table.add_column("path", style="dim")
        for name, path in results:
            result_table.add_row(name, str(path))
        console.print(result_table)


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
                self.session = build_session(
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
            ("say/save --batch <text>", "🎭 Generate all emotion presets at once", "green"),
            ("say/save --file <path>", "📄 Read text from file", "green"),
            ("say/save <<[MARKER]", "📝 Multiline input (default: EOF)", "green"),
            ("profile <text>", "⏱  Synthesis with timing report", "green"),
            ("", "", ""),
            ("emotion [name] [0-1]", "🎭 Show/set emotion preset or 8D vector (strength 0~1, default 0.5)", "magenta"),
            ("language [zh|en|ja]", "🌐 Show/set synthesis language", "blue"),
            ("lora", "🧬 Toggle/configure LoRA adapter", "yellow"),
            ("speed", "🎚️ Adjust speech speed (+fast / -slow)", "blue"),
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

        # 第三行：Speed + Poise
        speed_panel = render_speed_panel(self.session)
        poise_panel = render_poise_panel(self.session)
        console.print(Columns([speed_panel, poise_panel], expand=True))

    def run(self):
        """主迴圈"""
        if not self.initialize():
            return

        self.show_menu()

        try:
            self._main_loop()
        finally:
            self._cleanup()

    def _cleanup(self):
        """釋放引擎資源"""
        if self.session is not None:
            try:
                status = self.session.status()
                if status.get("loaded"):
                    with console.status("[dim]Releasing engine...[/]", spinner="dots"):
                        self.session.unload()
                    console.print("[dim]Engine unloaded. Bye![/]")
                else:
                    console.print("[dim]Bye![/]")
            except Exception:
                console.print("[dim]Bye![/]")

    def _main_loop(self):
        """主迴圈（由 run() 呼叫）"""
        while True:
            try:
                command = Prompt.ask("\n[bold cyan]tts[/]")
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
                    case "say" | "s":
                        args = _resolve_text_input(args)
                        batch, remaining = _extract_batch_flag(args)
                        text = _parse_text_args(remaining, command_name="say")
                        if batch:
                            text = auto_translate(text, self.session)
                            _cmd_emotion_batch(self.session, text, mode="say")
                        else:
                            _cmd_say(self.session, text)

                    case "save":
                        args = _resolve_text_input(args)
                        batch, remaining = _extract_batch_flag(args)
                        if batch:
                            text, output = _parse_save_args(remaining)
                            text = auto_translate(text, self.session)
                            _cmd_emotion_batch(self.session, text, mode="save", output_stem=output)
                        else:
                            _cmd_save(self.session, remaining)

                    case "profile" | "p":
                        args = _resolve_text_input(args)
                        text = _parse_text_args(args, command_name="profile")
                        _cmd_profile(self.session, text)

                    case "emotion" | "emo":
                        _cmd_emotion(self.session, args)

                    case "language" | "lang":
                        _cmd_language(self.session, args)

                    case "lora":
                        _cmd_lora(self.session, args)

                    case "speed":
                        _cmd_speed(self.session, args)

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
    info = indextts_backend_info(Path(__file__).resolve().parent)

    if _HAS_RICH:
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("key", style="dim")
        table.add_column("value", style="bold cyan")

        table.add_row("Backend", info.name)
        table.add_row("Engine source", str(info.source_path))
        table.add_row("Emotion vectors", "[green]yes[/]" if info.supports_emotion_vector else "[dim]no[/]")

        console.print(Panel(table, title="[bold]TTS Scaffold[/]", border_style="green"))
    else:
        console.print("TTS experiment scaffold is available.")
        console.print(f"Backend: {info.name}")
        console.print(f"Engine source: {info.source_path}")
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
        output_path = generate_with_indextts2(
            text=text,
            output_path=output,
            project_root=Path(__file__).resolve().parent,
            device=device,
            use_fp16=use_fp16,
            use_cuda_kernel=use_cuda_kernel,
        )
    console.print(f"[green]✓[/] Generated audio: [cyan]{output_path}[/]")
    return 0


def build_session(
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
    backend = IndexTTS2Backend(
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


# 向後相容別名
build_legacy_session = build_session  # noqa: N816


def run_profile(text: str, device: str | None, use_fp16: bool, use_cuda_kernel: bool, use_accel: bool, emotion_vector: list[float] | None, repeat: int) -> int:
    session = build_session(
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

    subparsers.add_parser("smoke", help="Verify scaffold and engine source paths")

    bench = subparsers.add_parser("benchmark", help="Run a placeholder benchmark")
    bench.add_argument("--text", default="Hello from the TTS experiment project.")

    gen = subparsers.add_parser("generate", help="Generate speech through IndexTTS2 engine")
    gen_text = gen.add_mutually_exclusive_group(required=True)
    gen_text.add_argument("--text", help="Text to synthesize")
    gen_text.add_argument("--file", help="Read text from .txt file")
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
    prof_text = profile_cmd.add_mutually_exclusive_group(required=True)
    prof_text.add_argument("--text", help="Text to synthesize")
    prof_text.add_argument("--file", help="Read text from .txt file")
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
        text = args.text if args.text else _read_text_file(args.file)
        return generate(
            text=text,
            output=args.output,
            device=args.device,
            use_fp16=not args.no_fp16,
            use_cuda_kernel=not args.no_cuda_kernel,
        )
    if args.command == "profile":
        text = args.text if args.text else _read_text_file(args.file)
        return run_profile(
            text=text,
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
