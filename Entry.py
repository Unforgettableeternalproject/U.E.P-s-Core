"""
Echo Memory System — 互動式 CLI + 記憶儀表板

Usage:
    python Entry.py              # 互動式 CLI
    python Entry.py chat         # 直接進入對話模式
    python Entry.py dashboard    # 顯示記憶儀表板
"""

import sys
import uuid
import logging
from pathlib import Path
from datetime import datetime, timezone

# 添加專案路徑
sys.path.insert(0, str(Path(__file__).parent))

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.columns import Columns
from rich.text import Text
from rich.prompt import Prompt, Confirm
from rich.markdown import Markdown
from rich import box

from echo_memory import MemoryEngine
from echo_memory.config import get_config
from llm_integration.llm_client import LLMClient

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 設定
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("entry")

console = Console()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 記憶儀表板
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def render_stats_panel(engine: MemoryEngine) -> Panel:
    """渲染記憶統計面板"""
    stats = engine.get_stats()

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("key", style="dim")
    table.add_column("value", style="bold cyan")

    table.add_row("Episodes", str(stats.get("total_episodes", 0)))
    table.add_row("Concepts", str(stats.get("total_concepts", 0)))
    table.add_row("Procedures", str(stats.get("total_procedures", 0)))
    table.add_row("Entities", str(stats.get("total_entities", 0)))
    table.add_row("Working Cache", str(stats.get("working_cache_size", 0)))

    # 圖譜統計
    try:
        graph_stats = engine.episodic.graph.get_stats()
        table.add_row("Graph Nodes", str(graph_stats.get("nodes", 0)))
        table.add_row("Graph Edges", str(graph_stats.get("edges", 0)))
        avg_w = graph_stats.get("avg_edge_weight", 0)
        table.add_row("Avg Weight", f"{avg_w:.3f}" if avg_w else "—")
    except Exception:
        pass

    return Panel(table, title="[bold]Memory Stats[/]", border_style="cyan", width=36)


def render_recent_episodes(engine: MemoryEngine, limit: int = 5) -> Panel:
    """渲染最近情節面板"""
    table = Table(box=box.SIMPLE_HEAVY, show_lines=False, padding=(0, 1))
    table.add_column("#", style="dim", width=3)
    table.add_column("Time", style="cyan", width=16)
    table.add_column("User", style="white", ratio=1)
    table.add_column("Sal.", style="yellow", width=5, justify="right")

    try:
        episodes = engine.episodic.list_episodes(
            limit=limit, offset=0, sort_by="timestamp", ascending=False
        )
        for i, ep in enumerate(episodes, 1):
            ts = ep.timestamp.strftime("%m-%d %H:%M")
            user_text = ep.user_input[:50] + ("…" if len(ep.user_input) > 50 else "")
            sal_style = (
                "green" if ep.salience_score >= 0.7
                else "yellow" if ep.salience_score >= 0.4
                else "dim"
            )
            table.add_row(
                str(i), ts, user_text,
                Text(f"{ep.salience_score:.2f}", style=sal_style),
            )

        if not episodes:
            table.add_row("—", "—", "[dim]No episodes yet[/]", "—")
    except Exception as e:
        table.add_row("!", "—", f"[red]Error: {e}[/]", "—")

    return Panel(table, title="[bold]Recent Episodes[/]", border_style="blue")


def render_concepts(engine: MemoryEngine, limit: int = 8) -> Panel:
    """渲染概念面板"""
    table = Table(box=box.SIMPLE, show_lines=False, padding=(0, 1))
    table.add_column("Concept", style="magenta", ratio=1)
    table.add_column("Conf.", style="cyan", width=5, justify="right")
    table.add_column("Sources", style="dim", width=7, justify="right")

    try:
        concepts = engine.semantic.list_concepts()
        for concept in concepts[:limit]:
            table.add_row(
                concept.name[:40],
                f"{concept.confidence:.2f}",
                str(len(concept.source_episode_ids)),
            )
        if not concepts:
            table.add_row("[dim]No concepts yet[/]", "—", "—")
        elif len(concepts) > limit:
            table.add_row(f"[dim]... +{len(concepts) - limit} more[/]", "", "")
    except Exception:
        table.add_row("[dim]Semantic layer empty[/]", "—", "—")

    return Panel(table, title="[bold]Concepts[/]", border_style="magenta")


def show_dashboard(engine: MemoryEngine):
    """顯示完整記憶儀表板"""
    console.print()

    # 第一行：統計 + 最近情節
    stats_panel = render_stats_panel(engine)
    episodes_panel = render_recent_episodes(engine, limit=6)
    console.print(Columns([stats_panel, episodes_panel], expand=True))

    # 第二行：概念
    concepts_panel = render_concepts(engine, limit=10)
    console.print(concepts_panel)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 對話模式
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def run_chat(engine: MemoryEngine, llm: LLMClient, config):
    """互動式對話模式"""
    session_id = f"chat_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    turn = 0

    console.print(Panel(
        "[bold cyan]Echo Memory Chat[/]\n"
        f"Session: [dim]{session_id}[/]\n"
        f"Model: [dim]{llm.model_name}[/]\n\n"
        "Commands: [dim]/quit  /stats  /dream  /clear  /history[/]",
        border_style="cyan",
        width=70,
    ))

    while True:
        console.print()
        try:
            user_input = Prompt.ask("[bold green]You[/]")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Bye![/]")
            break

        if not user_input.strip():
            continue

        # ── 斜線命令 ──
        cmd = user_input.strip().lower()
        if cmd in ("/quit", "/exit", "/q"):
            console.print("[dim]Session ended.[/]")
            break
        elif cmd == "/stats":
            show_dashboard(engine)
            continue
        elif cmd == "/dream":
            _cmd_dream(engine, llm)
            continue
        elif cmd == "/clear":
            llm.clear_history()
            console.print("[yellow]Conversation history cleared.[/]")
            continue
        elif cmd == "/history":
            _show_history(llm)
            continue

        # ── 1. 檢索記憶 ──
        try:
            context = engine.retrieve(
                query=user_input,
                dopamine=0.5,
                serotonin=0.5,
                top_k=5,
                min_similarity=config.retrieval_min_similarity,
            )
            memory_text = "" if context.is_empty() else context.to_text()

            if not context.is_empty():
                ep_count = len(context.episodes)
                concept_count = len(context.concepts) if hasattr(context, "concepts") else 0
                proc_count = len(context.procedures) if hasattr(context, "procedures") else 0

                parts = []
                if ep_count:
                    parts.append(f"{ep_count} episodes")
                if concept_count:
                    parts.append(f"{concept_count} concepts")
                if proc_count:
                    parts.append(f"{proc_count} procedures")

                console.print(
                    f"  [dim]📡 Retrieved: {', '.join(parts)}[/]"
                )

        except Exception as e:
            logger.warning(f"Retrieval error: {e}")
            memory_text = ""

        # ── 2. LLM 生成 ──
        try:
            with console.status("[cyan]Thinking...[/]", spinner="dots"):
                response = llm.chat(
                    message=user_input,
                    memory_context=memory_text,
                )

            console.print(f"[bold cyan]U[/]  {response}")

        except Exception as e:
            console.print(f"[red]LLM Error: {e}[/]")
            logger.exception("LLM generation failed")
            continue

        # ── 3. 存儲情節 ──
        try:
            turn += 1
            episode_id = engine.store_episode(
                user_input=user_input,
                agent_response=response,
                session_id=session_id,
                salience=0.5,
            )
            console.print(f"  [dim]💾 Stored: {episode_id[:12]}… (turn {turn})[/]")

        except Exception as e:
            logger.warning(f"Store error: {e}")


def _show_history(llm: LLMClient):
    """顯示對話歷史"""
    history = llm.get_history()
    if not history:
        console.print("[dim]No conversation history.[/]")
        return

    table = Table(box=box.ROUNDED, show_lines=True, padding=(0, 1))
    table.add_column("Role", style="bold", width=6)
    table.add_column("Message", ratio=1)

    for turn in history:
        role = turn["role"]
        style = "green" if role == "user" else "cyan"
        text = turn["text"]
        if len(text) > 200:
            text = text[:200] + "…"
        table.add_row(Text(role, style=style), text)

    console.print(Panel(table, title=f"[bold]Conversation ({llm.turn_count} turns)[/]"))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 指令處理
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _cmd_store(engine: MemoryEngine, session_id: str):
    """手動存儲對話"""
    user_input = Prompt.ask("[cyan]User input[/]")
    if not user_input:
        return
    agent_response = Prompt.ask("[cyan]Agent response[/]")
    if not agent_response:
        return
    salience = float(Prompt.ask("[cyan]Salience (0-1)[/]", default="0.5"))
    salience = max(0.0, min(1.0, salience))

    episode_id = engine.store_episode(
        user_input=user_input,
        agent_response=agent_response,
        session_id=session_id,
        salience=salience,
    )
    console.print(f"[green]✓ Stored: {episode_id}[/]")


def _cmd_retrieve(engine: MemoryEngine, config):
    """檢索記憶"""
    query = Prompt.ask("[cyan]Query[/]")
    if not query:
        return

    context = engine.retrieve(
        query=query,
        dopamine=0.5,
        serotonin=0.5,
        top_k=5,
        min_similarity=config.retrieval_min_similarity,
    )

    if context.is_empty():
        console.print("[yellow]No relevant memories found.[/]")
    else:
        console.print(Panel(
            context.to_text(),
            title="[bold]Retrieved Memories[/]",
            border_style="blue",
        ))


def _cmd_list(engine: MemoryEngine):
    """列出情節"""
    page = int(Prompt.ask("[cyan]Page[/]", default="1"))
    limit = 10
    offset = (max(1, page) - 1) * limit

    total = engine.episodic.count()
    episodes = engine.episodic.list_episodes(
        limit=limit, offset=offset, sort_by="timestamp", ascending=False
    )

    if not episodes:
        console.print("[yellow]No episodes found.[/]")
        return

    table = Table(title=f"Episodes (page {page}, total {total})", box=box.ROUNDED)
    table.add_column("#", style="dim", width=4)
    table.add_column("ID", style="cyan", width=14)
    table.add_column("Time", width=16)
    table.add_column("User", ratio=1)
    table.add_column("Agent", ratio=1)
    table.add_column("Sal.", style="yellow", width=5)

    for i, ep in enumerate(episodes, offset + 1):
        table.add_row(
            str(i),
            ep.id[:12] + "…",
            ep.timestamp.strftime("%m-%d %H:%M"),
            ep.user_input[:40] + ("…" if len(ep.user_input) > 40 else ""),
            ep.agent_response[:40] + ("…" if len(ep.agent_response) > 40 else ""),
            f"{ep.salience_score:.2f}",
        )

    console.print(table)


def _cmd_dream(engine: MemoryEngine, llm: LLMClient | None = None):
    """執行 Dream Engine"""
    if not Confirm.ask("[yellow]Run Dream Engine?[/]", default=False):
        return

    with console.status("[cyan]Dreaming...[/]", spinner="moon"):
        llm_fn = llm.as_llm_fn() if llm else None

        # 如果 engine 還沒有 llm_fn 就嘗試注入
        if llm_fn and not getattr(engine, '_llm_fn', None):
            engine._llm_fn = llm_fn

        report = engine.dream(triggered_by="manual")

    if report.get("error"):
        console.print(f"[red]Dream failed: {report['error']}[/]")
        return

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("key", style="dim")
    table.add_column("value", style="bold green")

    table.add_row("Duration", f"{report.get('duration_seconds', 0):.1f}s")
    table.add_row("Edges Updated", str(report.get("edges_updated", 0)))
    table.add_row("Edges Pruned", str(report.get("edges_pruned", 0)))

    # 蒸餾報告
    distill = report.get("distillation", {})
    if distill:
        table.add_row("Concepts Created", str(distill.get("concepts_created", 0)))
        table.add_row("Episodes Processed", str(distill.get("episodes_processed", 0)))

    # 重播報告
    replay = report.get("replay", {})
    if replay:
        table.add_row("Replayed", str(replay.get("episodes_replayed", 0)))

    console.print(Panel(table, title="[bold]Dream Report[/]", border_style="yellow"))


def _cmd_export(engine: MemoryEngine):
    """匯出記憶"""
    fmt = Prompt.ask("[cyan]Format[/]", choices=["json", "csv"], default="json")
    path = Prompt.ask("[cyan]Output path (empty = screen)[/]", default="")

    content = engine.episodic.export_episodes(format=fmt)

    if path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        console.print(f"[green]✓ Exported to {p.resolve()}[/]")
    else:
        lines = content.splitlines()
        preview = "\n".join(lines[:40])
        if len(lines) > 40:
            preview += f"\n\n[dim]... {len(lines) - 40} more lines[/]"
        console.print(Panel(preview, title=f"Export ({fmt.upper()})"))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主程式
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class EchoMemoryCLI:
    """Echo Memory System 互動式 CLI"""

    def __init__(self):
        self.config = get_config()
        self.engine: MemoryEngine | None = None
        self.llm: LLMClient | None = None
        self.session_id = f"cli_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

    def initialize(self) -> bool:
        """初始化記憶引擎"""
        console.print(Panel(
            "[bold cyan]Echo Memory System[/]\n"
            "[dim]Autonomous memory subsystem — inspired by OpenSoul[/]",
            border_style="cyan",
        ))

        try:
            with console.status("[cyan]Loading memory engine...[/]"):
                self.engine = MemoryEngine(
                    storage_path=self.config.memory_storage_path,
                    embedding_model=self.config.embedding_model,
                    device=self.config.embedding_device,
                    query_prefix=getattr(self.config, "embedding_query_prefix", ""),
                    max_episodes=self.config.memory_max_episodes,
                )

            stats = self.engine.get_stats()
            console.print(
                f"[green]✓[/] Engine ready — "
                f"{stats['total_episodes']} episodes, "
                f"{stats['total_concepts']} concepts"
            )

        except Exception as e:
            console.print(f"[red]✗ Engine init failed: {e}[/]")
            return False

        # LLM（非必要）
        is_valid, missing = self.config.validate_llm()
        if is_valid:
            try:
                self.llm = LLMClient()
                console.print(f"[green]✓[/] LLM ready — {self.llm.model_name}")
            except Exception as e:
                console.print(f"[yellow]⚠ LLM init failed: {e}[/]")
                console.print("[dim]  Chat mode unavailable, memory ops still work.[/]")
        else:
            console.print(f"[yellow]⚠ LLM not configured (missing: {', '.join(missing)})[/]")

        return True

    def show_menu(self):
        """顯示主選單"""
        commands = [
            ("chat", "💬 Chat with memory", "green" if self.llm else "dim"),
            ("dashboard", "📊 Memory dashboard", "cyan"),
            ("store", "📝 Store episode", "blue"),
            ("retrieve", "🔍 Retrieve memories", "blue"),
            ("list", "📋 List episodes", "blue"),
            ("dream", "🌙 Run Dream Engine", "yellow"),
            ("export", "📦 Export data", "blue"),
            ("clear", "🗑  Clear all", "red"),
            ("exit", "👋 Exit", "dim"),
        ]

        grid = Table(show_header=False, box=None, padding=(0, 2), show_edge=False)
        grid.add_column(width=12)
        grid.add_column(ratio=1)

        for cmd, desc, style in commands:
            grid.add_row(f"[bold {style}]{cmd}[/]", f"[{style}]{desc}[/]")

        console.print()
        console.print(grid)
        console.print()

    def run(self):
        """主迴圈"""
        if not self.initialize():
            return

        self.show_menu()

        while True:
            try:
                command = Prompt.ask("[bold]echo[/]").strip().lower()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Bye![/]")
                break

            if not command:
                continue

            try:
                match command:
                    case "chat" | "c":
                        if self.llm:
                            run_chat(self.engine, self.llm, self.config)
                        else:
                            console.print("[red]LLM not available. Configure API keys in .env[/]")
                    case "dashboard" | "d":
                        show_dashboard(self.engine)
                    case "store" | "s":
                        _cmd_store(self.engine, self.session_id)
                    case "retrieve" | "r":
                        _cmd_retrieve(self.engine, self.config)
                    case "list" | "l":
                        _cmd_list(self.engine)
                    case "dream":
                        _cmd_dream(self.engine, self.llm)
                    case "export":
                        _cmd_export(self.engine)
                    case "clear":
                        if Confirm.ask("[red]Delete ALL memory data?[/]", default=False):
                            confirm = Prompt.ask("[red]Type DELETE to confirm[/]")
                            if confirm == "DELETE":
                                self.engine.clear_all()
                                console.print("[green]✓ All memory cleared.[/]")
                    case "stats":
                        show_dashboard(self.engine)
                    case "exit" | "quit" | "q":
                        console.print("[dim]Bye![/]")
                        break
                    case "help" | "?" | "menu":
                        self.show_menu()
                    case _:
                        console.print(f"[dim]Unknown: {command}. Type 'help' for commands.[/]")

            except KeyboardInterrupt:
                console.print("\n[yellow]Interrupted. Type 'exit' to quit.[/]")
            except Exception as e:
                console.print(f"[red]Error: {e}[/]")
                logger.exception("Command error")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    """主程式入口"""
    args = sys.argv[1:]

    cli = EchoMemoryCLI()

    if not args:
        cli.run()
    elif args[0] in ("chat", "c"):
        if cli.initialize() and cli.llm:
            run_chat(cli.engine, cli.llm, cli.config)
        else:
            console.print("[red]Cannot start chat mode.[/]")
    elif args[0] in ("dashboard", "d"):
        if cli.initialize():
            show_dashboard(cli.engine)
    else:
        console.print(f"[dim]Unknown arg: {args[0]}. Usage: python Entry.py [chat|dashboard][/]")


if __name__ == "__main__":
    main()
