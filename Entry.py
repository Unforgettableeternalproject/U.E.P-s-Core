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
from llm_integration.tools import to_gemini_tools
from llm_integration.tools.executor import ToolExecutor

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


def run_chat(engine: MemoryEngine, llm: LLMClient, config, tools_enabled: bool = True):
    """互動式對話模式"""
    session_id = f"chat_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    turn = 0

    gemini_tools = to_gemini_tools() if tools_enabled else None
    executor = ToolExecutor(engine=engine, session_id=session_id) if tools_enabled else None

    def _show_session_banner():
        tools_status = "[green]on[/]" if tools_enabled else "[dim]off[/]"
        console.print(Panel(
            "[bold cyan]Echo Memory Chat[/]\n"
            f"Session: [dim]{session_id}[/]\n"
            f"Model: [dim]{llm.model_name}[/]\n"
            f"Tools: {tools_status}\n\n"
            "Commands: [dim]/quit /stats /dream /clear /history /tools /session[/]",
            border_style="cyan",
            width=70,
        ))

    _show_session_banner()

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
        elif cmd == "/tools":
            tools_enabled = not tools_enabled
            if tools_enabled:
                gemini_tools = to_gemini_tools()
                executor = ToolExecutor(engine=engine, session_id=session_id)
                console.print("[green]Tools enabled.[/]")
            else:
                gemini_tools = None
                executor = None
                console.print("[yellow]Tools disabled.[/]")
            continue
        elif cmd.startswith("/session"):
            parts_cmd = user_input.strip().split(maxsplit=1)
            sub = parts_cmd[1].strip().lower() if len(parts_cmd) > 1 else ""

            if sub == "" or sub == "info":
                # 顯示當前 session
                console.print(f"  Current: [cyan]{session_id}[/] ({turn} turns, {llm.turn_count} in LLM history)")
                continue

            elif sub == "list":
                # 列出所有 sessions
                all_eps = engine.episodic.list_episodes(
                    limit=9999, offset=0, sort_by="timestamp", ascending=True
                )
                sessions_map: dict[str, list] = {}
                for ep in all_eps:
                    sessions_map.setdefault(ep.session_id, []).append(ep)

                if not sessions_map:
                    console.print("[yellow]No sessions found.[/]")
                    continue

                s_table = Table(box=box.SIMPLE, show_lines=False)
                s_table.add_column("#", style="dim", width=3)
                s_table.add_column("Session ID", style="cyan", ratio=1)
                s_table.add_column("Episodes", width=8, justify="right")
                s_table.add_column("Last", width=16)

                active_marker = " [green]◀ current[/]"
                for i, (sid, eps) in enumerate(
                    sorted(sessions_map.items(), key=lambda x: x[1][-1].timestamp, reverse=True), 1
                ):
                    marker = active_marker if sid == session_id else ""
                    s_table.add_row(
                        str(i), sid + marker, str(len(eps)),
                        eps[-1].timestamp.strftime("%m-%d %H:%M"),
                    )

                console.print(s_table)
                continue

            elif sub == "new":
                # 建立新 session
                session_id = f"chat_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
                turn = 0
                llm.clear_history()
                if executor:
                    executor = ToolExecutor(engine=engine, session_id=session_id)
                console.print(f"[green]✓ New session: {session_id}[/]")
                continue

            else:
                # 嘗試切換到指定 session
                target_sid = sub
                # 前綴匹配
                all_eps = engine.episodic.list_episodes(
                    limit=9999, offset=0, sort_by="timestamp", ascending=True
                )
                sessions_map = {}
                for ep in all_eps:
                    sessions_map.setdefault(ep.session_id, []).append(ep)

                matched_sids = [s for s in sessions_map if s.startswith(target_sid) or target_sid in s]

                if not matched_sids:
                    console.print(f"[yellow]No session matching '{target_sid}'[/]")
                    continue
                if len(matched_sids) > 1:
                    console.print(f"[yellow]Ambiguous — {len(matched_sids)} matches:[/]")
                    for s in matched_sids[:5]:
                        console.print(f"  [dim]{s}[/]")
                    continue

                target_sid = matched_sids[0]
                target_eps = sessions_map[target_sid]

                # 載入歷史到 LLM
                llm.clear_history()
                from google.genai import types as _types
                for ep in target_eps:
                    llm._history.append(_types.Content(
                        role="user", parts=[_types.Part(text=ep.user_input)]
                    ))
                    llm._history.append(_types.Content(
                        role="model", parts=[_types.Part(text=ep.agent_response)]
                    ))

                session_id = target_sid
                turn = len(target_eps)
                if executor:
                    executor = ToolExecutor(engine=engine, session_id=session_id)

                console.print(f"[green]✓ Switched to: {session_id} ({len(target_eps)} episodes loaded)[/]")
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

        # ── 2. LLM 生成（含可能的 tool-use loop）──
        try:
            if executor:
                executor.update_context_whitelist(
                    [ep.id for ep in context.episodes] if not context.is_empty() else []
                )
                executor.increment_turn()

            with console.status("[cyan]Thinking...[/]", spinner="dots"):
                response = llm.chat(
                    message=user_input,
                    memory_context=memory_text,
                    tools=gemini_tools,
                    tool_executor=executor,
                )

            # 顯示工具呼叫提示（如果有的話）
            if tools_enabled:
                _show_tool_usage_hint(llm)

            console.print(f"[bold cyan]U[/]  {response}")

        except Exception as e:
            console.print(f"[red]LLM Error: {e}[/]")
            logger.exception("LLM generation failed")
            continue

        # ── 3. 存儲情節 ──
        try:
            turn += 1
            salience = _estimate_salience(user_input, response)
            episode_id = engine.store_episode(
                user_input=user_input,
                agent_response=response,
                session_id=session_id,
                salience=salience,
            )
            sal_indicator = "🔴" if salience >= 0.75 else "🟡" if salience >= 0.5 else "⚪"
            console.print(f"  [dim]💾 Stored: {episode_id[:12]}… (turn {turn}) {sal_indicator} {salience:.2f}[/]")

        except Exception as e:
            logger.warning(f"Store error: {e}")


def _estimate_salience(user_input: str, agent_response: str) -> float:
    """
    啟發式估算對話的顯著性分數。

    基於對話特徵自動評分，避免所有 episode 都是 0.5。
    """
    score = 0.35  # 基準分（一般閒聊）
    text = (user_input + " " + agent_response).lower()

    # 個人資訊（名字、身份、偏好）→ 高顯著性
    personal_signals = [
        "my name", "i am", "i'm", "我叫", "我是", "remember",
        "don't forget", "important", "記住", "別忘",
    ]
    if any(sig in text for sig in personal_signals):
        score += 0.25

    # 情感/深度對話 → 中高
    emotional_signals = [
        "feel", "love", "hate", "worry", "afraid", "happy", "sad",
        "thank", "sorry", "miss", "dream", "hope", "wish",
        "喜歡", "討厭", "擔心", "開心", "難過", "謝謝",
    ]
    if any(sig in text for sig in emotional_signals):
        score += 0.15

    # 提問/知識交換 → 中
    if "?" in user_input or "？" in user_input:
        score += 0.1

    # 長對話通常更有內容
    if len(user_input) > 100:
        score += 0.1
    if len(agent_response) > 200:
        score += 0.05

    return min(1.0, round(score, 2))


def _show_tool_usage_hint(llm: LLMClient):
    """顯示最近一輪的工具呼叫提示（簡潔版）"""
    history = llm.get_history()
    if not history:
        return

    # 從歷史末尾往回掃，找到最後一個 user 文字訊息後的所有工具條目
    last_user_idx = -1
    for i in range(len(history) - 1, -1, -1):
        item = history[i]
        if item["role"] == "user" and not item["text"].startswith("[tool"):
            last_user_idx = i
            break

    if last_user_idx < 0:
        return

    for item in history[last_user_idx + 1:]:
        if item["text"].startswith("[tool_call]"):
            tool_part = item["text"][len("[tool_call] "):]
            tool_name = tool_part.split("(")[0]
            console.print(f"  [dim]🔧 Tool: {tool_name}[/]")


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


def _cmd_sessions(engine: MemoryEngine):
    """列出所有 session"""
    try:
        all_episodes = engine.episodic.list_episodes(
            limit=9999, offset=0, sort_by="timestamp", ascending=True
        )
    except Exception as e:
        console.print(f"[red]Error: {e}[/]")
        return

    if not all_episodes:
        console.print("[yellow]No episodes.[/]")
        return

    # 按 session 分組
    sessions: dict[str, list] = {}
    for ep in all_episodes:
        sessions.setdefault(ep.session_id, []).append(ep)

    table = Table(title=f"Sessions ({len(sessions)} total)", box=box.ROUNDED)
    table.add_column("Session ID", style="cyan", ratio=1)
    table.add_column("Episodes", style="bold", width=8, justify="right")
    table.add_column("First", width=16)
    table.add_column("Last", width=16)

    for sid, eps in sorted(sessions.items(), key=lambda x: x[1][-1].timestamp, reverse=True):
        table.add_row(
            sid,
            str(len(eps)),
            eps[0].timestamp.strftime("%m-%d %H:%M"),
            eps[-1].timestamp.strftime("%m-%d %H:%M"),
        )

    console.print(table)


def _cmd_inspect(engine: MemoryEngine):
    """檢視單一 episode 完整內容"""
    ep_id = Prompt.ask("[cyan]Episode ID (prefix OK, min 6 chars)[/]")
    if not ep_id or len(ep_id) < 6:
        console.print("[yellow]ID too short.[/]")
        return

    # 前綴搜尋
    matched = [
        (eid, ep) for eid, ep in engine.episodic._episodes.items()
        if eid.startswith(ep_id)
    ]

    if not matched:
        console.print(f"[yellow]No episode matching '{ep_id}'[/]")
        return
    if len(matched) > 1:
        console.print(f"[yellow]Ambiguous — {len(matched)} matches. Provide more chars.[/]")
        for eid, ep in matched[:5]:
            console.print(f"  [dim]{eid}[/]")
        return

    eid, ep = matched[0]

    # 基本資訊
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("key", style="dim", width=16)
    table.add_column("value", ratio=1)

    table.add_row("ID", f"[cyan]{eid}[/]")
    table.add_row("Session", ep.session_id)
    table.add_row("Timestamp", str(ep.timestamp))
    table.add_row("Salience", f"[yellow]{ep.salience_score:.3f}[/]")
    table.add_row("Summary", ep.content_summary or "—")
    table.add_row("Dreamed", str(getattr(ep, "is_dreamed", False)))

    # Metadata
    meta = getattr(ep, "metadata", {})
    if meta:
        table.add_row("Metadata", str(meta))

    console.print(Panel(table, title="[bold]Episode Info[/]", border_style="cyan"))

    # 對話內容
    console.print(Panel(
        f"[green bold]User:[/]\n{ep.user_input}\n\n[cyan bold]Agent:[/]\n{ep.agent_response}",
        title="[bold]Conversation[/]",
        border_style="blue",
    ))

    # 圖譜連結
    try:
        graph = engine.episodic.graph
        neighbors = list(graph.graph.neighbors(eid)) if graph.graph.has_node(eid) else []
        predecessors = list(graph.graph.predecessors(eid)) if graph.graph.has_node(eid) else []

        if neighbors or predecessors:
            edges_table = Table(box=box.SIMPLE, show_lines=False)
            edges_table.add_column("Direction", width=4)
            edges_table.add_column("Target", style="cyan", ratio=1)
            edges_table.add_column("Type", style="magenta", width=16)
            edges_table.add_column("Weight", width=8)

            for n in predecessors:
                edge_data = graph.graph.edges[n, eid]
                edges_table.add_row(
                    "←", n[:16] + "…",
                    edge_data.get("edge_type", "?"),
                    f"{edge_data.get('weight', 0):.3f}",
                )
            for n in neighbors:
                edge_data = graph.graph.edges[eid, n]
                edges_table.add_row(
                    "→", n[:16] + "…",
                    edge_data.get("edge_type", "?"),
                    f"{edge_data.get('weight', 0):.3f}",
                )

            console.print(Panel(edges_table, title="[bold]Graph Edges[/]", border_style="magenta"))
        else:
            console.print("[dim]No graph edges for this episode.[/]")
    except Exception:
        pass


def _cmd_search(engine: MemoryEngine, config):
    """搜尋記憶（語義 + 文字）"""
    query = Prompt.ask("[cyan]Search query[/]")
    if not query:
        return

    mode = Prompt.ask("[cyan]Mode[/]", choices=["semantic", "text", "both"], default="semantic")
    results = []

    # 語義搜尋
    if mode in ("semantic", "both"):
        context = engine.retrieve(
            query=query, dopamine=0.6, serotonin=0.4,
            top_k=10, min_similarity=0.2,
        )
        if not context.is_empty():
            for i, ep in enumerate(context.episodes):
                sim = context.episode_similarities[i] if i < len(context.episode_similarities) else 0
                results.append(("semantic", ep, sim))

    # 文字搜尋（簡單 substring match）
    if mode in ("text", "both"):
        q_lower = query.lower()
        for eid, ep in engine.episodic._episodes.items():
            if q_lower in ep.user_input.lower() or q_lower in ep.agent_response.lower():
                # 避免重複
                if not any(r[1].id == ep.id for r in results):
                    results.append(("text", ep, 0.0))

    if not results:
        console.print("[yellow]No results found.[/]")
        return

    table = Table(title=f"Search Results ({len(results)})", box=box.ROUNDED)
    table.add_column("#", style="dim", width=3)
    table.add_column("Mode", width=8)
    table.add_column("ID", style="cyan", width=14)
    table.add_column("Sim.", width=6)
    table.add_column("User", ratio=1)
    table.add_column("Sal.", style="yellow", width=5)

    for i, (src, ep, sim) in enumerate(results[:15], 1):
        mode_style = "green" if src == "semantic" else "blue"
        table.add_row(
            str(i),
            Text(src, style=mode_style),
            ep.id[:12] + "…",
            f"{sim:.2f}" if sim > 0 else "—",
            ep.user_input[:45] + ("…" if len(ep.user_input) > 45 else ""),
            f"{ep.salience_score:.2f}",
        )

    console.print(table)


def _cmd_edit(engine: MemoryEngine):
    """修改 episode 資訊"""
    ep_id = Prompt.ask("[cyan]Episode ID (prefix OK)[/]")
    if not ep_id or len(ep_id) < 6:
        console.print("[yellow]ID too short.[/]")
        return

    matched = [
        (eid, ep) for eid, ep in engine.episodic._episodes.items()
        if eid.startswith(ep_id)
    ]

    if not matched:
        console.print(f"[yellow]No episode matching '{ep_id}'[/]")
        return
    if len(matched) > 1:
        console.print(f"[yellow]Ambiguous — {len(matched)} matches.[/]")
        return

    eid, ep = matched[0]
    console.print(f"[cyan]Editing: {eid}[/]")
    console.print(f"  User: {ep.user_input[:60]}…")
    console.print(f"  Current salience: [yellow]{ep.salience_score:.3f}[/]")
    console.print()

    field = Prompt.ask("[cyan]What to edit[/]", choices=["salience", "summary", "cancel"], default="salience")

    if field == "cancel":
        return
    elif field == "salience":
        new_val = Prompt.ask("[cyan]New salience (0-1)[/]", default=str(ep.salience_score))
        try:
            new_salience = max(0.0, min(1.0, float(new_val)))
            engine.episodic.update_salience(eid, new_salience)
            console.print(f"[green]✓ Salience updated: {ep.salience_score:.3f} → {new_salience:.3f}[/]")
        except Exception as e:
            console.print(f"[red]Failed: {e}[/]")
    elif field == "summary":
        new_summary = Prompt.ask("[cyan]New summary[/]", default=ep.content_summary or "")
        if new_summary:
            ep.content_summary = new_summary
            engine.episodic._save()
            console.print(f"[green]✓ Summary updated.[/]")


def _cmd_delete(engine: MemoryEngine):
    """刪除 episode 或整個 session"""
    mode = Prompt.ask("[cyan]Delete[/]", choices=["episode", "session", "cancel"], default="episode")

    if mode == "cancel":
        return
    elif mode == "episode":
        ep_id = Prompt.ask("[cyan]Episode ID (prefix OK)[/]")
        if not ep_id or len(ep_id) < 6:
            return

        matched = [
            (eid, ep) for eid, ep in engine.episodic._episodes.items()
            if eid.startswith(ep_id)
        ]

        if not matched:
            console.print(f"[yellow]No match for '{ep_id}'[/]")
            return
        if len(matched) > 1:
            console.print(f"[yellow]Ambiguous — {len(matched)} matches.[/]")
            return

        eid, ep = matched[0]
        console.print(f"  [dim]{eid} | {ep.user_input[:50]}…[/]")
        if Confirm.ask("[red]Delete?[/]", default=False):
            engine.episodic.delete_episode(eid)
            console.print(f"[green]✓ Deleted.[/]")

    elif mode == "session":
        sid = Prompt.ask("[cyan]Session ID[/]")
        if not sid:
            return

        eps = engine.episodic.list_episodes(session_id=sid, limit=999, offset=0, sort_by="timestamp", ascending=True)
        if not eps:
            console.print(f"[yellow]No episodes in session '{sid}'[/]")
            return

        console.print(f"[yellow]Will delete {len(eps)} episodes from session '{sid}'[/]")
        if Confirm.ask("[red]Confirm?[/]", default=False):
            count = engine.episodic.delete_session(sid)
            console.print(f"[green]✓ Deleted {count} episodes.[/]")


def _cmd_graph(engine: MemoryEngine):
    """顯示圖結構詳細資訊"""
    try:
        graph = engine.episodic.graph
        stats = graph.get_stats()
    except Exception as e:
        console.print(f"[red]Error: {e}[/]")
        return

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("key", style="dim")
    table.add_column("value", style="bold cyan")

    table.add_row("Nodes", str(stats.get("nodes", 0)))
    table.add_row("Edges", str(stats.get("edges", 0)))
    avg_w = stats.get("avg_edge_weight", 0)
    table.add_row("Avg Weight", f"{avg_w:.4f}" if avg_w else "—")

    # 邊類型分布
    edge_types: dict[str, int] = {}
    for u, v, data in graph.graph.edges(data=True):
        et = data.get("edge_type", "unknown")
        edge_types[et] = edge_types.get(et, 0) + 1

    if edge_types:
        table.add_row("", "")
        table.add_row("[bold]Edge Types[/]", "")
        for et, count in sorted(edge_types.items(), key=lambda x: -x[1]):
            table.add_row(f"  {et}", str(count))

    # 孤立節點
    isolated = list(graph.graph.nodes() - set(
        n for e in graph.graph.edges() for n in e
    ))
    table.add_row("", "")
    table.add_row("Isolated Nodes", str(len(isolated)))

    console.print(Panel(table, title="[bold]Memory Graph[/]", border_style="magenta"))


def _cmd_concepts(engine: MemoryEngine):
    """列出和檢視概念"""
    try:
        concepts = engine.semantic.list_concepts()
    except Exception:
        console.print("[dim]Semantic layer not initialized or empty.[/]")
        return

    if not concepts:
        console.print("[yellow]No concepts yet. Run Dream Engine to distill episodes into concepts.[/]")
        return

    table = Table(title=f"Concepts ({len(concepts)})", box=box.ROUNDED)
    table.add_column("#", style="dim", width=3)
    table.add_column("ID", style="cyan", width=14)
    table.add_column("Name", style="magenta", ratio=1)
    table.add_column("Conf.", width=6)
    table.add_column("Sources", width=7)
    table.add_column("Access", width=6)

    for i, c in enumerate(concepts, 1):
        table.add_row(
            str(i),
            c.id[:12] + "…",
            c.name,
            f"{c.confidence:.2f}",
            str(len(c.source_episode_ids)),
            str(c.access_count),
        )

    console.print(table)


def _cmd_reset(engine: MemoryEngine):
    """重置所有記憶資料（保留設定）"""
    stats = engine.get_stats()
    console.print(f"[yellow]Current data: {stats['total_episodes']} episodes, "
                  f"{stats['total_concepts']} concepts[/]")

    if not Confirm.ask("[red]Reset ALL memory data?[/]", default=False):
        return
    confirm = Prompt.ask("[red]Type RESET to confirm[/]")
    if confirm != "RESET":
        console.print("[dim]Cancelled.[/]")
        return

    engine.clear_all()
    console.print("[green]✓ All memory data reset. Fresh start.[/]")


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
        self.tools_enabled: bool = True

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
            ("", "", ""),
            ("search", "🔍 Search memories (semantic + text)", "blue"),
            ("retrieve", "🔎 Retrieve with neurochem params", "blue"),
            ("inspect", "🔬 Inspect episode details + graph", "blue"),
            ("", "", ""),
            ("store", "📝 Store episode manually", "blue"),
            ("edit", "✏️  Edit episode (salience, summary)", "blue"),
            ("delete", "🗑  Delete episode or session", "red"),
            ("", "", ""),
            ("list", "📋 List episodes", "blue"),
            ("sessions", "📂 List all sessions", "blue"),
            ("concepts", "💡 List concepts", "magenta"),
            ("graph", "🕸  Graph structure info", "magenta"),
            ("", "", ""),
            ("dream", "🌙 Run Dream Engine", "yellow"),
            ("export", "📦 Export data", "blue"),
            ("reset", "⚠️  Reset all memory", "red"),
            ("exit", "👋 Exit", "dim"),
        ]

        grid = Table(show_header=False, box=None, padding=(0, 2), show_edge=False)
        grid.add_column(width=12)
        grid.add_column(ratio=1)

        for cmd, desc, style in commands:
            if not cmd:  # separator
                grid.add_row("", "")
            else:
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
                            run_chat(self.engine, self.llm, self.config, self.tools_enabled)
                        else:
                            console.print("[red]LLM not available. Configure API keys in .env[/]")
                    case "dashboard" | "d":
                        show_dashboard(self.engine)
                    case "search":
                        _cmd_search(self.engine, self.config)
                    case "retrieve" | "r":
                        _cmd_retrieve(self.engine, self.config)
                    case "inspect" | "i":
                        _cmd_inspect(self.engine)
                    case "store" | "s":
                        _cmd_store(self.engine, self.session_id)
                    case "edit" | "e":
                        _cmd_edit(self.engine)
                    case "delete" | "del":
                        _cmd_delete(self.engine)
                    case "list" | "l":
                        _cmd_list(self.engine)
                    case "sessions":
                        _cmd_sessions(self.engine)
                    case "concepts":
                        _cmd_concepts(self.engine)
                    case "graph" | "g":
                        _cmd_graph(self.engine)
                    case "dream":
                        _cmd_dream(self.engine, self.llm)
                    case "export":
                        _cmd_export(self.engine)
                    case "reset":
                        _cmd_reset(self.engine)
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
            run_chat(cli.engine, cli.llm, cli.config, cli.tools_enabled)
        else:
            console.print("[red]Cannot start chat mode.[/]")
    elif args[0] in ("dashboard", "d"):
        if cli.initialize():
            show_dashboard(cli.engine)
    else:
        console.print(f"[dim]Unknown arg: {args[0]}. Usage: python Entry.py [chat|dashboard][/]")


if __name__ == "__main__":
    main()
