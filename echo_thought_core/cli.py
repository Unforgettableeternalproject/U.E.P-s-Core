"""
Session Control — Rich CLI 互動式測試介面

仿照 TestSeperateMemorySystem 的 EchoMemoryCLI 風格，
提供 token 使用率視覺化、壓縮層級燈號、手動 /compact、
對話歷史瀏覽（含壓縮前後對比）、成本追蹤等功能。

Usage:
    python Entry.py                    # 預設使用 llm_config.yaml 設定
    python Entry.py --model gemini-2.5-pro  # 覆蓋模型
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time


def _run_async(coro):
    """安全執行 async coroutine（相容已存在 event loop 的上下文）"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # 已有 event loop（例如在 pytest-asyncio 或整合層中）
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    else:
        return asyncio.run(coro)

try:
    from rich.columns import Columns
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress_bar import ProgressBar
    from rich.prompt import Confirm, Prompt
    from rich.table import Table
    from rich.text import Text

    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False

    # 定義佔位類別，讓函數簽名不報 NameError（實際呼叫時 CLI 入口會檢查 _HAS_RICH）
    class _Stub:
        def __init__(self, *a, **kw): pass
        def __call__(self, *a, **kw): return self
        def __getattr__(self, _): return self
    Console = Panel = Table = Text = ProgressBar = Columns = Confirm = Prompt = _Stub  # type: ignore[misc,assignment]

from echo_thought_core import (  # noqa: E402
    CompactionConfig,
    ContextManager,
    LLMClient,
)
from echo_thought_core.persona import (  # noqa: E402
    build_persona_from_config,
    load_llm_config,
)
from echo_thought_core.schemas.session import CompressionTier  # noqa: E402

logger = logging.getLogger(__name__)

# ── 壓縮層級配色 ────────────────────────────────────────────────────────────

TIER_STYLES = {
    CompressionTier.TIER_0_NORMAL: ("●", "green", "Normal"),
    CompressionTier.TIER_1_MICRO_PRUNE: ("●", "yellow", "Micro-Prune"),
    CompressionTier.TIER_2_ROLLING_SUMMARY: ("●", "dark_orange", "Rolling Summary"),
    CompressionTier.TIER_3_FULL_COMPACTION: ("●", "red", "Full Compaction"),
    CompressionTier.TIER_4_EMERGENCY: ("●", "bold red", "EMERGENCY"),
}

# ── 使用率進度條配色 ────────────────────────────────────────────────────────

def _usage_color(pct: float) -> str:
    """根據使用率回傳顏色"""
    if pct < 0.60:
        return "green"
    elif pct < 0.80:
        return "yellow"
    elif pct < 0.92:
        return "dark_orange"
    return "red"


# ═══════════════════════════════════════════════════════════════════════════
# Rich Panels
# ═══════════════════════════════════════════════════════════════════════════


def render_status_panel(mgr: ContextManager) -> Panel:
    """Token 使用率 + 壓縮狀態面板"""
    state = mgr.state
    pct = state.usage_percentage
    color = _usage_color(pct)

    # 壓縮層級燈號
    dot, tier_color, tier_label = TIER_STYLES.get(
        state.current_tier,
        ("○", "dim", "Unknown"),
    )

    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("Key", style="dim", width=18)
    table.add_column("Value")

    # Token 使用率
    bar_filled = int(pct * 20)
    bar_empty = 20 - bar_filled
    bar = f"[{color}]{'█' * bar_filled}[/{color}][dim]{'░' * bar_empty}[/dim]"
    table.add_row("Token Usage", f"{bar} {pct:.1%}")
    table.add_row("", f"[dim]{state.current_tokens:,} / {state.max_tokens:,}[/dim]")

    # 壓縮層級
    table.add_row("Compression Tier", f"[{tier_color}]{dot} {tier_label}[/{tier_color}]")

    # 統計
    table.add_row("Messages", str(state.message_count))
    table.add_row("Compressions", str(state.compression_count))

    # Session
    table.add_row("Session", f"[dim]{state.session_id}[/dim]")

    return Panel(table, title="[bold cyan]Session Status[/]", border_style="cyan", width=42)


def render_model_panel(mgr: ContextManager) -> Panel:
    """模型資訊面板"""
    config = mgr.config
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("Key", style="dim", width=14)
    table.add_column("Value")

    table.add_row("Backend", config.backend)
    table.add_row("Model", config.model)
    table.add_row("Context Window", f"{config.context_window_size:,}")

    # 閾值
    thresholds = config.thresholds
    table.add_row("", "")
    table.add_row("[dim]Thresholds[/]", "")
    table.add_row("  Micro-Prune", f"{thresholds.get('micro_prune', 0):.0%}")
    table.add_row("  Rolling Sum.", f"{thresholds.get('rolling_summary', 0):.0%}")
    table.add_row("  Full Compact", f"{thresholds.get('full_compact', 0):.0%}")

    # 功能
    table.add_row("", "")
    table.add_row("LLMLingua", "✓" if config.use_llmlingua else "✗")
    table.add_row("Echo Memory", "✓" if config.echo_memory_enabled else "✗")
    has_backend = mgr._backend is not None
    table.add_row("LLM Backend", "[green]Connected[/]" if has_backend else "[red]None[/]")

    return Panel(table, title="[bold blue]Model & Config[/]", border_style="blue", width=38)


def render_history_panel(mgr: ContextManager, last_n: int = 8) -> Panel:
    """對話歷史面板"""
    messages = mgr.history.messages
    if not messages:
        return Panel("[dim]No messages yet.[/dim]", title="[bold]History[/]", border_style="dim")

    table = Table(show_header=True, box=None, padding=(0, 1))
    table.add_column("#", style="dim", width=3)
    table.add_column("Role", width=10)
    table.add_column("Content", overflow="ellipsis", max_width=60)
    table.add_column("Tokens", style="dim", width=7, justify="right")

    display_msgs = messages[-last_n:]
    start_idx = len(messages) - len(display_msgs)

    for i, msg in enumerate(display_msgs):
        idx = start_idx + i + 1
        role_style = {
            "user": "bold green",
            "assistant": "bold cyan",
            "system": "bold yellow",
            "tool": "bold magenta",
        }.get(msg.role, "dim")

        content = msg.content[:80].replace("\n", " ")
        if len(msg.content) > 80:
            content += "..."

        # 壓縮訊息標記
        if msg.is_compressed:
            content = f"[yellow]◆ COMPRESSED[/] {content}"

        tokens = str(msg.token_count or "?")
        table.add_row(str(idx), f"[{role_style}]{msg.role}[/{role_style}]", content, tokens)

    total = len(messages)
    showing = len(display_msgs)
    title = f"[bold]History[/] [dim]({showing}/{total})[/dim]"

    return Panel(table, title=title, border_style="white")


def render_sawtooth_panel(mgr: ContextManager) -> Panel:
    """Sawtooth 模式視覺化 — token 上升 → 壓縮重置的循環"""
    # 簡易文字版 sawtooth：用 message 的累計 token 做迷你圖
    messages = mgr.history.messages
    if len(messages) < 2:
        return Panel("[dim]Not enough data for sawtooth visualization.[/dim]",
                     title="[bold]Sawtooth Pattern[/]", border_style="dim")

    # 累計 token 序列
    cumulative = []
    running = 0
    for msg in messages:
        if msg.is_compressed:
            # 壓縮點：token 重置
            running = msg.token_count or 0
        else:
            running += msg.token_count or 0
        cumulative.append(running)

    # 正規化到 0-20 的高度
    max_val = max(cumulative) if cumulative else 1
    heights = [int(v / max_val * 15) for v in cumulative]

    # 取最近 40 個點
    display = heights[-40:]
    lines = []
    for row in range(15, -1, -1):
        chars = []
        for h in display:
            if h >= row:
                chars.append("█")
            else:
                chars.append(" ")
        lines.append("".join(chars))

    chart = "\n".join(lines)
    footer = f"[dim]← oldest{'─' * (len(display) - 16)}newest →  peak: {max_val:,} tokens[/dim]"

    return Panel(f"{chart}\n{footer}",
                 title="[bold magenta]Sawtooth Pattern[/]", border_style="magenta")


def show_dashboard(mgr: ContextManager, console: Console) -> None:
    """完整儀表板"""
    console.print()
    console.print(Columns([
        render_status_panel(mgr),
        render_model_panel(mgr),
    ], expand=True))
    console.print(render_history_panel(mgr))
    console.print(render_sawtooth_panel(mgr))


# ═══════════════════════════════════════════════════════════════════════════
# CLI 主類別
# ═══════════════════════════════════════════════════════════════════════════


class SessionControlCLI:
    """Session Control Rich CLI — 互動式壓縮測試介面"""

    def __init__(self, args: argparse.Namespace | None = None):
        self.console = Console()
        self.args = args
        self.mgr: ContextManager | None = None
        self.llm: LLMClient | None = None
        self.llm_config: dict = {}
        self._start_time = time.time()

    def initialize(self) -> bool:
        """初始化 LLM Backend + ContextManager"""
        self.console.print(Panel(
            "[bold cyan]Session Control System[/]\n"
            "[dim]LLM 會話控制子系統 — Prompt / Token / Context 優化引擎[/]",
            border_style="cyan",
        ))

        with self.console.status("[cyan]Loading LLM config..."):
            self.llm_config = load_llm_config()

        # 後端選擇（先決定 backend，再決定 model 預設值）
        backend_type = "gemini"
        if self.args and self.args.backend:
            backend_type = self.args.backend

        # Model: CLI 明確指定 > llm_config.yaml > 按 backend 預設
        backend_defaults = {
            "gemini": "gemini-2.5-flash-lite",
            "ollama": "gemma4",
        }
        if self.args and self.args.model:
            model = self.args.model
        elif backend_type == "gemini":
            model = self.llm_config.get("model", backend_defaults["gemini"])
        else:
            model = backend_defaults.get(backend_type, "default")

        persona = build_persona_from_config(self.llm_config)

        context_window = 200_000
        if self.args and self.args.window:
            context_window = self.args.window

        config = CompactionConfig(
            backend=backend_type,
            model=model,
            context_window_size=context_window,
            compaction_cooldown_seconds=0,  # 測試用：無冷卻
        )

        with self.console.status(f"[cyan]Initializing {backend_type} backend + ContextManager..."):
            try:
                if backend_type == "ollama":
                    from echo_thought_core.backends.ollama import OllamaBackend

                    self.llm = OllamaBackend(
                        model=model,
                        temperature=self.llm_config.get("temperature", 0.8),
                        max_tokens=self.llm_config.get("max_output_tokens", 4096),
                        character=self.llm_config.get("character", ""),
                        config={
                            "base_url": (
                                self.args.ollama_url
                                if self.args and hasattr(self.args, "ollama_url") and self.args.ollama_url
                                else "http://localhost:11434"
                            ),
                        },
                    )
                    # 立即驗證連線（fail-fast）
                    self.llm._ensure_connection()
                else:
                    # Gemini（預設）
                    self.llm = LLMClient(
                        model=model,
                        temperature=self.llm_config.get("temperature", 0.8),
                        max_tokens=self.llm_config.get("max_output_tokens", 4096),
                        character=self.llm_config.get("character", ""),
                        config={
                            "top_p": self.llm_config.get("top_p", 1.0),
                        },
                    )
                # ContextManager 注入 Backend 做壓縮
                self.mgr = ContextManager(
                    config=config,
                    persona=persona,
                    backend=self.llm,
                )

                # 用 backend 的真實 context window 校正
                real_ctx = self.llm.get_context_window_size()
                if real_ctx != context_window:
                    self.mgr.update_model(model, real_ctx)
                    context_window = real_ctx

            except Exception as e:
                # Backend 初始化失敗，降級為無 backend 模式
                self.console.print(f"[yellow]⚠ Backend ({backend_type}) init failed: {e}[/]")
                self.console.print("[yellow]  Running in offline mode (Layer 1/4 only)[/]")
                self.llm = None
                self.mgr = ContextManager(config=config, persona=persona, backend=None)

        self.console.print(f"[green]✓[/] Backend: [bold]{backend_type}[/]")
        self.console.print(f"[green]✓[/] Model: [bold]{model}[/]")
        self.console.print(f"[green]✓[/] Context Window: [bold]{context_window:,}[/]")
        self.console.print(
            f"[green]✓[/] LLM: "
            f"[bold]{'Connected' if self.llm else 'Offline'}[/]"
        )
        self.console.print()

        return True

    def show_menu(self) -> None:
        """顯示選單"""
        # 動態顯示當前 backend
        cur_backend = self.mgr.config.backend if self.mgr else "?"
        cur_model = self.mgr.config.model if self.mgr else "?"

        commands = [
            ("chat", "💬 Interactive chat (with compression)", "green"),
            ("dashboard", "📊 Full status dashboard", "cyan"),
            ("", "", ""),
            ("fill <N>", "🧪 Fill N dummy messages (stress test)", "blue"),
            ("compact", "🗜  Force full compaction (Layer 3)", "yellow"),
            ("history", "📜 Browse message history", "blue"),
            ("usage", "📈 Token usage report", "blue"),
            ("", "", ""),
            ("backend", f"🔄 Switch LLM backend [dim](now: {cur_backend}/{cur_model})[/dim]", "magenta"),
            ("clear", "⚠️  Reset session (clear all history)", "red"),
            ("help", "📋 Show this menu", "dim"),
            ("exit", "👋 Exit", "dim"),
        ]

        grid = Table(show_header=False, box=None, padding=(0, 2), show_edge=False)
        grid.add_column(width=14)
        grid.add_column(ratio=1)

        for cmd, desc, style in commands:
            if not cmd:
                grid.add_row("", "")
            else:
                grid.add_row(f"[bold {style}]{cmd}[/]", f"[{style}]{desc}[/]")

        self.console.print()
        self.console.print(grid)
        self.console.print()

    def run(self) -> None:
        """主迴圈"""
        if not self.initialize():
            return

        assert self.mgr is not None, "initialize() succeeded but mgr is None"
        self.show_menu()

        while True:
            try:
                cmd = Prompt.ask("[bold]session[/]").strip().lower()
            except (EOFError, KeyboardInterrupt):
                break

            if not cmd or cmd == "help":
                self.show_menu()
                continue

            if cmd in ("exit", "quit", "q"):
                break

            if cmd == "chat":
                self._run_chat()
            elif cmd == "dashboard":
                show_dashboard(self.mgr, self.console)
            elif cmd.startswith("fill"):
                self._cmd_fill(cmd)
            elif cmd == "compact":
                self._cmd_compact()
            elif cmd == "history":
                self.console.print(render_history_panel(self.mgr, last_n=20))
            elif cmd == "usage":
                self._cmd_usage()
            elif cmd == "backend" or cmd.startswith("backend "):
                self._cmd_backend(cmd)
            elif cmd == "clear":
                self._cmd_clear()
            else:
                self.console.print(f"[red]Unknown command: {cmd}[/]")

            self.console.rule("[dim]─[/]", style="dim")
            self.show_menu()

        self.console.print("[dim]Session ended.[/]")

    # ── 對話模式 ──────────────────────────────────────────────────────────

    def _run_chat(self) -> None:
        """互動對話模式（含壓縮觸發）"""
        self.console.print(Panel(
            "[bold cyan]Session Control Chat[/]\n"
            f"[dim]Session: {self.mgr.state.session_id} | "
            f"Model: {self.mgr.config.model} | "
            f"Commands: /quit /status /compact /backend /history /clear[/]",
            border_style="cyan", width=70,
        ))

        while True:
            # 狀態列
            pct = self.mgr.state.usage_percentage
            color = _usage_color(pct)
            dot, tier_color, tier_label = TIER_STYLES.get(
                self.mgr.state.current_tier,
                ("○", "dim", "?"),
            )
            status_line = (
                f"[dim]turn {self.mgr.state.message_count // 2 + 1} | "
                f"[{color}]{pct:.0%}[/{color}] tokens | "
                f"[{tier_color}]{dot} {tier_label}[/{tier_color}][/dim]"
            )
            self.console.print(status_line)

            try:
                user_input = Prompt.ask("[bold green]You[/]")
            except (EOFError, KeyboardInterrupt):
                break

            if not user_input:
                continue

            # 斜線命令
            if user_input.startswith("/"):
                cmd = user_input[1:].strip().lower()
                if cmd in ("quit", "exit", "q"):
                    break
                elif cmd in ("status", "dashboard"):
                    show_dashboard(self.mgr, self.console)
                    continue
                elif cmd == "compact":
                    self._cmd_compact()
                    continue
                elif cmd.startswith("backend"):
                    self._cmd_backend(cmd)
                    continue
                elif cmd == "history":
                    self.console.print(render_history_panel(self.mgr, last_n=20))
                    continue
                elif cmd == "usage":
                    self._cmd_usage()
                    continue
                elif cmd == "clear":
                    self._cmd_clear()
                    continue
                else:
                    self.console.print(f"[red]Unknown command: /{cmd}[/]")
                    continue

            # 加入使用者訊息
            self.mgr.add_message("user", user_input)

            # 嘗試用 LLM Backend 生成回覆
            if self.llm:
                with self.console.status("[cyan]Thinking...", spinner="dots"):
                    try:
                        response = self.llm.chat(user_input)
                    except Exception as e:
                        response = f"[LLM error: {e}]"

                # 用 API 回傳的精確 token 數校正
                api_usage = getattr(self.llm, "_last_usage", {})
                if api_usage:
                    self.mgr.update_from_response({
                        "input_tokens": api_usage.get("prompt_eval_count", 0),
                        "output_tokens": api_usage.get("eval_count", 0),
                    })
            else:
                response = "[dim](Offline mode — no LLM connected)[/dim]"

            # 加入助理回應
            self.mgr.add_message("assistant", response)

            # 顯示回應
            self.console.print(f"[bold cyan]U.E.P[/]: {response}")

            # 檢查是否需要壓縮
            tier = self.mgr.budget.check_tier()
            if tier >= CompressionTier.TIER_2_ROLLING_SUMMARY:
                self.console.print(
                    f"[yellow]⚠ Compression triggered: {TIER_STYLES[tier][2]}[/]"
                )
                with self.console.status("[yellow]Compressing..."):
                    try:
                        _run_async(self.mgr.get_messages_async())
                        self.console.print(
                            f"[green]✓ Compressed. "
                            f"Tokens: {self.mgr.state.current_tokens:,}[/]"
                        )
                    except Exception as e:
                        self.console.print(f"[red]Compression failed: {e}[/]")

                # 檢查是否發生了 auto session rotate
                rotate_result = self.mgr.last_rotate_result
                if rotate_result and rotate_result.session_rotated:
                    self.console.print(
                        f"[dim]↻ session rotated: "
                        f"{rotate_result.previous_session_id} → "
                        f"{rotate_result.new_session_id} "
                        f"(saved {rotate_result.tokens_saved:,} tokens)[/dim]"
                    )

    # ── 指令處理 ──────────────────────────────────────────────────────────

    def _cmd_fill(self, cmd: str) -> None:
        """灌入 N 條測試訊息（壓力測試）"""
        parts = cmd.split()
        n = 50
        if len(parts) > 1:
            try:
                n = int(parts[1])
            except ValueError:
                self.console.print("[red]Usage: fill <number>[/]")
                return

        with self.console.status(f"[cyan]Filling {n} message pairs..."):
            for i in range(n):
                self.mgr.add_message("user", f"Test message #{i + 1}: " + "lorem ipsum " * 10)
                self.mgr.add_message("assistant", f"Response #{i + 1}: " + "dolor sit amet " * 10)

        pct = self.mgr.state.usage_percentage
        self.console.print(
            f"[green]✓ Added {n * 2} messages. "
            f"Token usage: {pct:.1%} ({self.mgr.state.current_tokens:,})[/]"
        )

        # 自動顯示當前 tier
        tier = self.mgr.state.current_tier
        dot, tier_color, tier_label = TIER_STYLES.get(tier, ("○", "dim", "?"))
        self.console.print(
            f"  Tier: [{tier_color}]{dot} {tier_label}[/{tier_color}]"
        )

    def _cmd_compact(self) -> None:
        """手動觸發 Layer 3 完整壓縮"""
        if self.mgr.state.message_count == 0:
            self.console.print("[yellow]Nothing to compact (no messages).[/]")
            return

        with self.console.status("[yellow]Compacting (Layer 3)..."):
            try:
                result = _run_async(self.mgr.compact())
            except Exception as e:
                self.console.print(f"[red]Compaction failed: {e}[/]")
                return

        saved = result.tokens_saved

        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("Key", style="dim", width=16)
        table.add_column("Value")

        table.add_row("Tier", f"{result.tier.name}")
        table.add_row("Before", f"{result.tokens_before:,} tokens")
        table.add_row("After", f"{result.tokens_after:,} tokens")
        table.add_row("Saved", f"[green]{saved:,} tokens ({result.savings_percentage:.1%})[/]")
        table.add_row("Duration", f"{result.duration_ms:.0f} ms")

        if result.summary_text:
            preview = result.summary_text[:200].replace("\n", " ")
            if len(result.summary_text) > 200:
                preview += "..."
            table.add_row("Summary", f"[dim]{preview}[/dim]")

        self.console.print(Panel(table, title="[bold yellow]Compaction Result[/]", border_style="yellow"))

    def _cmd_backend(self, cmd: str) -> None:
        """切換 LLM 後端（runtime hot-swap）"""
        from echo_thought_core.backends.registry import BackendRegistry

        registry = BackendRegistry.default()
        providers = registry.list_providers()

        parts = cmd.split()

        # backend（無參數）：顯示當前狀態 + 可用列表
        if len(parts) == 1:
            table = Table(show_header=False, box=None, padding=(0, 1))
            table.add_column("Key", style="dim", width=16)
            table.add_column("Value")

            table.add_row("Current Backend", f"[bold]{self.mgr.config.backend}[/]")
            table.add_row("Current Model", f"[bold]{self.mgr.config.model}[/]")
            table.add_row("Context Window", f"{self.mgr.config.context_window_size:,}")
            table.add_row("", "")
            table.add_row("Available", ", ".join(
                f"[bold green]{p}[/]" if p == self.mgr.config.backend else p
                for p in providers
            ))
            table.add_row("", "")
            table.add_row("[dim]Usage[/]", "[dim]backend <name> [model][/]")
            table.add_row("", "[dim]e.g. backend ollama gemma4[/]")

            self.console.print(Panel(table, title="[bold magenta]Backend[/]", border_style="magenta"))
            return

        # backend <type> [model]
        new_type = parts[1]
        new_model = parts[2] if len(parts) > 2 else None

        if not registry.has(new_type):
            self.console.print(
                f"[red]Unknown backend: {new_type!r}. "
                f"Available: {', '.join(providers)}[/]"
            )
            return

        # 組裝 config（保留當前 character/persona）
        config = {}
        if new_model:
            config["model"] = new_model
        # 從當前 backend 繼承 character
        current_char = getattr(self.llm, "character", "") if self.llm else ""
        if not current_char:
            current_char = self.llm_config.get("character", "")
        if current_char:
            config["character"] = current_char

        old_backend = self.mgr.config.backend
        old_model = self.mgr.config.model
        msg_count = self.mgr.state.message_count

        with self.console.status(f"[cyan]Switching to {new_type}...", spinner="dots"):
            try:
                self.mgr.switch_backend(new_type, config)
                self.llm = self.mgr._backend

                # Ollama 需要立即驗證連線
                if new_type == "ollama" and hasattr(self.llm, "_ensure_connection"):
                    self.llm._ensure_connection()

            except Exception as e:
                self.console.print(f"[red]✗ Switch failed: {e}[/]")
                return

        new_model_name = self.mgr.config.model
        self.console.print(
            f"[green]✓[/] Backend switched: "
            f"[dim]{old_backend}/{old_model}[/] → "
            f"[bold]{new_type}/{new_model_name}[/]"
        )
        if msg_count > 0:
            self.console.print(
                f"[dim]  {msg_count} messages synced to new backend[/]"
            )
        self.console.print(
            f"[dim]  Context window: {self.mgr.config.context_window_size:,}[/]"
        )

    def _cmd_clear(self) -> None:
        """重置 session（清除所有歷史與狀態）"""
        confirm = Confirm.ask("[yellow]Clear all history and reset session?[/]", default=False)
        if not confirm:
            self.console.print("[dim]Cancelled.[/dim]")
            return

        self.mgr.reset_session()
        self.console.print(
            f"[green]✓ Session reset.[/] New session: [bold]{self.mgr.state.session_id}[/]"
        )

    def _cmd_usage(self) -> None:
        """顯示 token 用量報告"""
        usage = self.mgr.get_usage()
        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("Key", style="dim", width=18)
        table.add_column("Value")

        for key, value in usage.items():
            if isinstance(value, float):
                table.add_row(key, f"{value:.4f}")
            else:
                table.add_row(key, str(value))

        self.console.print(Panel(table, title="[bold]Token Usage Report[/]", border_style="green"))


# ═══════════════════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════════════════


def main():
    if not _HAS_RICH:
        print("ERROR: Rich is required for CLI mode.")
        print("Install with: pip install rich")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Session Control — Rich CLI")
    parser.add_argument("--model", type=str, help="Override model name")
    parser.add_argument("--window", type=int, help="Override context window size")
    parser.add_argument(
        "--backend",
        type=str,
        choices=["gemini", "ollama"],
        default="gemini",
        help="LLM backend to use (default: gemini)",
    )
    parser.add_argument(
        "--ollama-url",
        type=str,
        default="http://localhost:11434",
        help="Ollama service URL (default: http://localhost:11434)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s | %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING)

    cli = SessionControlCLI(args)
    try:
        cli.run()
    except KeyboardInterrupt:
        Console().print("\n[dim]Interrupted.[/dim]")


if __name__ == "__main__":
    main()
