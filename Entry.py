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
from typing import Optional


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

from echo_thought_core import (
    CompactionConfig,
    ContextManager,
    create_context_manager,
)
from echo_thought_core.persona import (
    build_persona_from_config,
    build_system_instruction,
    load_llm_config,
)
from echo_thought_core.schemas.session import CompressionTier

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

    def __init__(self, args: Optional[argparse.Namespace] = None):
        self.console = Console()
        self.args = args
        self.mgr: Optional[ContextManager] = None
        self.llm_config: dict = {}
        self._start_time = time.time()

    def initialize(self) -> bool:
        """初始化 ContextManager + Backend"""
        self.console.print(Panel(
            "[bold cyan]Session Control System[/]\n"
            "[dim]LLM 會話控制子系統 — Prompt / Token / Context 優化引擎[/]",
            border_style="cyan",
        ))

        with self.console.status("[cyan]Loading LLM config..."):
            self.llm_config = load_llm_config()

        model = self.llm_config.get("model", "gemini-2.5-flash-lite")
        if self.args and self.args.model:
            model = self.args.model

        persona = build_persona_from_config(self.llm_config)

        context_window = 200_000
        if self.args and self.args.window:
            context_window = self.args.window

        config = CompactionConfig(
            backend="gemini",
            model=model,
            context_window_size=context_window,
            compaction_cooldown_seconds=0,  # 測試用：無冷卻
        )

        with self.console.status("[cyan]Initializing ContextManager..."):
            try:
                self.mgr = create_context_manager(
                    backend_config={
                        "model": model,
                        "temperature": self.llm_config.get("temperature", 0.8),
                        "top_p": self.llm_config.get("top_p", 1.0),
                        "max_output_tokens": self.llm_config.get("max_output_tokens", 4096),
                    },
                    compaction_config=config,
                    persona=persona,
                )
            except Exception as e:
                # Backend 初始化失敗（無 API key 等），降級為無 backend 模式
                self.console.print(f"[yellow]⚠ Backend init failed: {e}[/]")
                self.console.print("[yellow]  Running in offline mode (Layer 1/4 only)[/]")
                self.mgr = ContextManager(config=config, persona=persona, backend=None)

        self.console.print(f"[green]✓[/] Model: [bold]{model}[/]")
        self.console.print(f"[green]✓[/] Context Window: [bold]{context_window:,}[/]")
        self.console.print(
            f"[green]✓[/] Backend: "
            f"[bold]{'Connected' if self.mgr._backend else 'Offline'}[/]"
        )
        self.console.print()

        return True

    def show_menu(self) -> None:
        """顯示選單"""
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Cmd", style="bold cyan", width=12)
        table.add_column("Description")

        table.add_row("chat", "Start interactive chat (with compression)")
        table.add_row("dashboard", "Show full status dashboard")
        table.add_row("fill <N>", "Fill N dummy messages (stress test)")
        table.add_row("compact", "Force full compaction (Layer 3)")
        table.add_row("history", "Browse message history")
        table.add_row("usage", "Show token usage report")
        table.add_row("help", "Show this menu")
        table.add_row("exit", "Exit")

        self.console.print(Panel(table, title="[bold]Commands[/]", border_style="dim"))

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
            else:
                self.console.print(f"[red]Unknown command: {cmd}[/]")

            self.console.rule()
            self.show_menu()

        self.console.print("[dim]Session ended.[/]")

    # ── 對話模式 ──────────────────────────────────────────────────────────

    def _run_chat(self) -> None:
        """互動對話模式（含壓縮觸發）"""
        self.console.print(Panel(
            "[bold cyan]Session Control Chat[/]\n"
            f"[dim]Session: {self.mgr.state.session_id} | "
            f"Model: {self.mgr.config.model} | "
            f"Commands: /quit /status /compact /history[/]",
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
                elif cmd == "history":
                    self.console.print(render_history_panel(self.mgr, last_n=20))
                    continue
                elif cmd == "usage":
                    self._cmd_usage()
                    continue
                else:
                    self.console.print(f"[red]Unknown command: /{cmd}[/]")
                    continue

            # 加入使用者訊息
            self.mgr.add_message("user", user_input)

            # 嘗試用 backend 生成回覆
            if self.mgr._backend:
                with self.console.status("[cyan]Thinking..."):
                    try:
                        system_instruction = build_system_instruction(self.llm_config)
                        result = self.mgr._backend.query(
                            prompt=user_input,
                            mode="chat",
                            system_instruction=system_instruction,
                        )
                        response = result.get("text", "(no response)")
                    except Exception as e:
                        response = f"[Backend error: {e}]"
            else:
                response = "[dim](Offline mode — no LLM backend connected)[/dim]"

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
                        result = _run_async(self.mgr.get_messages_async())
                        self.console.print(
                            f"[green]✓ Compressed. "
                            f"Tokens: {self.mgr.state.current_tokens:,}[/]"
                        )
                    except Exception as e:
                        self.console.print(f"[red]Compression failed: {e}[/]")

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
