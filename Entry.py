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
from typing import Any

# 添加專案路徑
sys.path.insert(0, str(Path(__file__).parent))

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.columns import Columns
from rich.text import Text
from rich.prompt import Prompt, Confirm
from rich import box

from echo_memory import MemoryEngine
from echo_memory.config import get_config
from echo_memory.schemas.memory_context import MemoryContext
from echo_memory.affect.signals import SalienceSignals, SubconsciousAssessment
from echo_memory.affect.salience import SalienceEvaluator
from echo_memory.affect.subconscious import SubconsciousAssessor
from echo_memory.affect.concept_verifier import ConceptVerifier
from echo_memory.affect.concept_inhibitor import ConceptInhibitor
from echo_memory.affect.verifier import ResponseVerifier
from echo_memory.affect.inhibitor import ResponseInhibitor, InhibitionAction
from echo_memory.affect.judge import JUDGE_FORCE_REMEMBER_KEY, StorageJudge
from echo_memory.affect.concept_extractor import ConceptExtractor
from echo_memory.affect.soul_note import SoulNote, SoulNoteManager
from echo_memory.affect.reflection import ReflectionModule
from echo_memory.affect.reflection_executor import ReflectionExecutor, parse_reflection_to_actions
from echo_memory.affect.action_queue import ReflectionActionQueue
from echo_memory.utils.sanitizer import sanitize_llm_output
from llm_integration.llm_client import LLMClient
from llm_integration.router import LLMRouter, LLMRole
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
        concepts = engine.semantic.list_concepts(limit=limit)
        total_count = engine.semantic.count()
        for concept in concepts:
            table.add_row(
                concept.name[:40],
                f"{concept.confidence:.2f}",
                str(len(concept.source_episode_ids)),
            )
        if not concepts:
            table.add_row("[dim]No concepts yet[/]", "—", "—")
        elif total_count > limit:
            table.add_row(f"[dim]... +{total_count - limit} more[/]", "", "")
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

    # ── LLMRouter 初始化 ──
    # 本專案只使用 CHAT（LLMClient）和 EVAL（Router）兩個角色。
    # WORK 角色保留給未來 U.E.P 回聲系統整合，目前不使用。
    # 詳見 docs/design/llm-client-vs-router.md
    router = LLMRouter()
    eval_fn = router.get_llm_fn(LLMRole.EVAL)

    # 同步更新 MemoryEngine 的 llm_fn（Dream Engine 等子模組使用）
    engine.set_llm_fn(eval_fn)

    # ── 結構化 LLM 函式（Gemini response_schema 保證合法 JSON）──
    from echo_memory.affect.subconscious import ASSESSMENT_RESPONSE_SCHEMA
    from echo_memory.affect.judge import STORAGE_DECISION_SCHEMA
    from echo_memory.affect.reflection import REFLECTION_RESPONSE_SCHEMA
    from echo_memory.affect.concept_verifier import CONCEPT_VERIFY_RESPONSE_SCHEMA
    from echo_memory.affect.concept_extractor import CONCEPT_EXTRACT_RESPONSE_SCHEMA

    structured_assessment_fn = router.get_structured_llm_fn(
        LLMRole.EVAL, response_schema=ASSESSMENT_RESPONSE_SCHEMA,
    )
    structured_reflection_fn = router.get_structured_llm_fn(
        LLMRole.EVAL, response_schema=REFLECTION_RESPONSE_SCHEMA,
    )
    structured_judge_storage_fn = router.get_structured_llm_fn(
        LLMRole.EVAL, response_schema=STORAGE_DECISION_SCHEMA,
    )
    structured_concept_verify_fn = router.get_structured_llm_fn(
        LLMRole.EVAL, response_schema=CONCEPT_VERIFY_RESPONSE_SCHEMA,
    )
    structured_concept_extract_fn = router.get_structured_llm_fn(
        LLMRole.EVAL, response_schema=CONCEPT_EXTRACT_RESPONSE_SCHEMA,
    )

    # ── 情感/潛意識模組初始化（session 級別，複用整個對話）──
    concept_verifier = ConceptVerifier(
        llm_fn=eval_fn,
        embed_fn=engine.episodic.generate_query_embedding,
        structured_llm_fn=structured_concept_verify_fn,
    )
    concept_inhibitor = ConceptInhibitor(
        semantic=engine.semantic,
        llm_fn=eval_fn,
    )
    # 注入 ConceptInhibitor 到 MemoryEngine，供 DreamEngine 使用
    engine.set_concept_inhibitor(concept_inhibitor)
    # llm_fn 與 inhibitor 皆已注入，明確啟動 Dream 自動排程（閒置 + cron）
    dream_start = engine.start_dream_scheduler()
    if not dream_start["started"]:
        logger.info(f"[run_chat] Dream 排程未啟動: {dream_start['reason']}")
    subconscious_assessor = SubconsciousAssessor(
        llm_fn=eval_fn,
        structured_llm_fn=structured_assessment_fn,
    )
    judge = StorageJudge(
        llm_fn=eval_fn,
        structured_llm_fn=structured_judge_storage_fn,
    )
    soul_notes = SoulNoteManager(engine.storage_path)
    reflection_module = ReflectionModule(
        llm_fn=eval_fn,
        soul_notes=soul_notes,
        reflect_interval=10,
        structured_llm_fn=structured_reflection_fn,
    )
    action_queue = ReflectionActionQueue(
        audit_path=engine.storage_path / "reflection_audit.jsonl",
    )
    action_queue.event_emitter = engine.event_emitter  # M6：預設關閉，無 sink 時不發
    reflection_executor = ReflectionExecutor(
        engine=engine,
        soul_notes=soul_notes,
        concept_inhibitor=concept_inhibitor,
    )
    executor_fn = reflection_executor.build_executor_fn()
    rollback_fn = reflection_executor.build_rollback_fn()
    concept_extractor = ConceptExtractor(
        llm_fn=eval_fn,
        semantic=engine.semantic,
        graph=engine.episodic.graph,
        embed_fn=lambda text: engine.episodic._generate_embedding(text),
        inhibitor=concept_inhibitor,
        structured_llm_fn=structured_concept_extract_fn,
        evidence_store=engine.evidence,
    )

    def _show_session_banner():
        tools_status = "[green]on[/]" if tools_enabled else "[dim]off[/]"
        eval_cfg = router.get_role_config(LLMRole.EVAL)
        console.print(Panel(
            "[bold cyan]Echo Memory Chat[/]\n"
            f"Session: [dim]{session_id}[/]\n"
            f"Chat: [dim]{llm.model_name}[/]  "
            f"Eval: [dim]{eval_cfg.model}[/]\n"
            f"Tools: {tools_status}\n\n"
            "Commands: [dim]/quit /stats /dream /clear /history /tools /session /review-actions /review-concept /rollback[/]",
            border_style="cyan",
            width=70,
        ))

    _show_session_banner()

    while True:
        # 緊湊狀態列
        tools_tag = "[green]tools[/]" if tools_enabled else "[dim]no-tools[/]"
        stats_line = engine.get_stats()
        ep_count = stats_line.get("total_episodes", 0)
        console.print(
            f"\n[dim]──── {session_id} │ turn {turn} │ {ep_count} eps │ {tools_tag} ────[/]"
        )
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
        elif cmd.split(maxsplit=1)[0] == "/review-concept":
            _cmd_review_concept(engine, user_input, console)
            continue
        elif cmd == "/review-actions":
            _cmd_review_actions(action_queue, executor_fn, console)
            continue
        elif cmd.startswith("/rollback"):
            _cmd_rollback(action_queue, rollback_fn, console)
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
                engine.working.clear()  # 清除查詢向量快取，確保 session 邊界乾淨
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

        # ── 防禦性預設值（避免 LLM 錯誤路徑中變數未定義）──
        from echo_memory.affect.verifier import VerificationResult
        verify_result = VerificationResult(
            score=1.0, threshold=0.7, passed=True, details={}
        )
        gate_action = InhibitionAction.PASS
        assessment = SubconsciousAssessment()  # 預設五維度

        # ── 1. 檢索記憶（使用 neurochem 動態參數）──
        context = MemoryContext()  # 預設空 context，避免 retrieve 失敗時 UnboundLocalError
        try:
            context = engine.retrieve(
                query=user_input,
                top_k=10,
                min_similarity=config.retrieval_min_similarity,
                exclude_session_id=session_id,
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

                nc = engine.neurochem
                console.print(
                    f"  [dim]📡 Retrieved: {', '.join(parts)} "
                    f"│ DA={nc.dopamine:.2f} 5-HT={nc.serotonin:.2f} [{nc.mode.value}][/]"
                )

        except Exception as e:
            logger.warning(f"Retrieval error: {e}")
            memory_text = ""

        # ── 1.5 概念前驗證 + 潛意識評估 + 工具過濾 ──

        # 概念前驗證：檢查檢索到的概念是否需要 expand/modify/deprecate
        concept_verification = concept_verifier.verify(
            user_input=user_input,
            memory_ctx=context if not context.is_empty() else None,
        )
        # 持久化 Concept Health 更新（record_verification 在 verify 內呼叫）
        if context and context.concepts:
            engine.semantic._save()
        if concept_verification.has_changes:
            console.print(
                f"  [dim]🔍 {concept_verification.summary()}[/]"
            )

        # 潛意識評估（啟發式模式下很快，LLM 模式下可能需要時間）
        assessment = subconscious_assessor.assess(
            user_input=user_input,
            memory_ctx=context if not context.is_empty() else None,
        )

        # 工具過濾 + 推薦
        filtered_tools = gemini_tools
        if tools_enabled and gemini_tools:
            total_eps = engine.get_stats().get("total_episodes", 0)
            filtered_tools = judge.filter_tools(
                context if not context.is_empty() else None,
                gemini_tools,
                total_episodes=total_eps,
            )

            # StorageJudge 工具推薦（LLM 語意排序）
            recommendation = judge.recommend_tool(
                user_input,
                context if not context.is_empty() else None,
                filtered_tools,
            )
            if recommendation.recommended_tool != "none":
                # 將推薦工具排到最前面，讓 LLM 更容易看到
                rec_name = recommendation.recommended_tool
                filtered_tools = sorted(
                    filtered_tools,
                    key=lambda t: t.get("name") != rec_name,
                )
                console.print(
                    f"  [dim]🎯 Tool hint: {rec_name} "
                    f"({recommendation.confidence:.0%})[/]"
                )

        # ── 2. LLM 生成（含品質閘門 + 可能的重試）──
        try:
            if executor:
                executor.update_context_whitelist(
                    [ep.id for ep in context.episodes] if not context.is_empty() else []
                )
                executor.increment_turn()

            # 構建 neurochem + 概念驗證注入段落
            nc = engine.neurochem
            neurochem_prompt = (
                f"\n## Internal State\n"
                f"Dopamine: {nc.dopamine:.2f} | Serotonin: {nc.serotonin:.2f} | "
                f"Mode: {nc.mode.value}\n"
            )
            # 若有概念變更，注入提示讓 LLM 注意
            concept_hint = concept_verification.to_context_text()
            if concept_hint:
                neurochem_prompt += f"\n{concept_hint}\n"

            response = None
            gate_action = InhibitionAction.PASS

            # 注入 embed_fn 啟用 Grounded Verification（語意一致性比對）
            embed_fn = engine.episodic.generate_query_embedding
            verifier = ResponseVerifier(nc, embed_fn=embed_fn, llm_fn=eval_fn)
            inhibitor = ResponseInhibitor(nc, max_retries=1)

            for attempt in range(2):  # 最多嘗試 2 次（首次 + 1 重試）
                with console.status("[cyan]Thinking...[/]", spinner="dots"):
                    raw_response = llm.chat(
                        message=user_input,
                        memory_context=(memory_text + neurochem_prompt) if memory_text else neurochem_prompt,
                        tools=filtered_tools,
                        tool_executor=executor,
                    )

                # 內容消殺（清除偽造工具標記）
                raw_response = sanitize_llm_output(raw_response)

                # 品質閘門
                verify_result = verifier.verify(raw_response, context if not context.is_empty() else None)
                gate_decision = inhibitor.gate(raw_response, verify_result, attempt=attempt)
                gate_action = gate_decision.action

                if gate_decision.should_retry:
                    # SUPPRESS retry：移除被抑制的回覆，避免歷史污染
                    llm.undo_last_exchange()
                    console.print(f"  [dim yellow]⚡ {gate_decision.reason}[/]")
                    continue

                response = gate_decision.text
                break

            if response is None:
                response = raw_response  # 防禦性：所有重試都失敗時使用最後一次回覆

            # 顯示工具呼叫提示（如果有的話）
            if tools_enabled:
                _show_tool_usage_hint(llm)

            # 顯示閘門狀態
            gate_icon = {"pass": "✅", "revise": "⚠️", "suppress": "🚫"}.get(gate_action.value, "")
            console.print(f"  [dim]{gate_icon} Gate: {gate_action.value.upper()} "
                         f"(score={verify_result.score:.2f}/{verify_result.threshold:.2f})[/]")

            console.print(f"[bold cyan]U[/]  {response}")

        except Exception as e:
            console.print(f"[red]LLM Error: {e}[/]")
            logger.exception("LLM generation failed")
            continue

        # ── 3. 存儲情節（使用 SalienceEvaluator 三值輸出）──
        try:
            turn += 1

            # 從 SubconsciousAssessment 建立 SalienceSignals
            # verify_result 和 gate_action 在迴圈開頭已設定預設值
            signals = SalienceSignals.from_subconscious(
                assessment,
                verification_score=verify_result.score,
                error_occurred=gate_action == InhibitionAction.SUPPRESS,
            )

            # SalienceEvaluator 三值計算（baseline）
            evaluator = SalienceEvaluator(engine.neurochem)
            baseline_salience, da_weight, ht_weight = evaluator.evaluate(
                signals, user_input=user_input, agent_response=response,
            )

            # 更新 neurochem（基於本輪信號）
            evaluator.update_neurochem(signals)

            # ── 3.2 StorageJudge 存儲決策（後處理）──
            # Judge 決定「怎麼記住」：salience 微調、concept seeds、
            # entity/reflection triggers、metadata tags
            storage_dec = judge.storage_decision(
                user_input=user_input,
                agent_response=response,
                baseline_salience=baseline_salience,
                assessment=assessment,
                context=context if not context.is_empty() else None,
                concept_verification=concept_verification,
            )

            # 合併 baseline + judge delta → final salience
            salience = max(0.0, min(1.0, baseline_salience + storage_dec.salience_delta))

            # 收集 episode metadata（包含 judge 決策）
            ep_metadata: dict = {}
            if storage_dec.metadata_tags:
                ep_metadata["judge_tags"] = storage_dec.metadata_tags
            if storage_dec.force_remember:
                ep_metadata[JUDGE_FORCE_REMEMBER_KEY] = True
            if storage_dec.entity_triggers:
                ep_metadata["entity_triggers"] = True
            if storage_dec.source != "baseline":
                ep_metadata["judge_source"] = storage_dec.source

            episode_id = engine.store_episode(
                user_input=user_input,
                agent_response=response,
                session_id=session_id,
                salience=salience,
                da_weight=da_weight,
                ht_weight=ht_weight,
                **ep_metadata,
            )

            # 顯示存儲資訊
            sal_indicator = "🔴" if salience >= 0.75 else "🟡" if salience >= 0.5 else "⚪"
            delta_str = ""
            if storage_dec.salience_delta != 0.0:
                sign = "+" if storage_dec.salience_delta > 0 else ""
                delta_str = f" Δ{sign}{storage_dec.salience_delta:.2f}"
            console.print(
                f"  [dim]💾 Stored: {episode_id[:12]}… (turn {turn}) "
                f"{sal_indicator} sal={salience:.2f}{delta_str} "
                f"da={da_weight:.2f} ht={ht_weight:.2f}[/]"
            )

            # 顯示 StorageJudge 語意增補（僅在有增補時）
            if storage_dec.has_semantic_enrichment:
                parts = []
                if storage_dec.concept_seeds:
                    parts.append(f"{len(storage_dec.concept_seeds)} seeds")
                if storage_dec.metadata_tags:
                    parts.append(f"tags={storage_dec.metadata_tags}")
                if storage_dec.force_remember:
                    parts.append("force_remember")
                console.print(
                    f"  [dim]⚖️ Judge [{storage_dec.source}]: "
                    f"{', '.join(parts)}[/]"
                )

            # ── 3.5 概念 Patch 閘門（Inhibitor）──
            # 根據 Verifier 判定 + LLM 回覆，決定是否對 SemanticMemory 提交變更
            if concept_verification.has_changes:
                try:
                    patch_tx = concept_inhibitor.process(
                        verification=concept_verification,
                        user_input=user_input,
                        llm_response=response,
                        session_id=session_id,
                        turn_id=episode_id,
                    )
                    applied = len(patch_tx.applied_patches)
                    held = len(patch_tx.held_patches)
                    if applied > 0 or held > 0:
                        parts = []
                        if applied:
                            parts.append(f"{applied} applied")
                        if held:
                            parts.append(f"{held} held")
                        console.print(
                            f"  [dim]🔧 Concept patches: {', '.join(parts)}[/]"
                        )
                except Exception as e:
                    logger.warning(f"Concept inhibitor error: {e}")

            # ── 4. 背景概念提取（daemon Thread，不阻塞）──
            try:
                stored_ep = engine.episodic._episodes.get(episode_id)
                if stored_ep:
                    # 傳入 StorageJudge 的 concept_seeds 作為優先提取建議
                    concept_extractor.extract_in_background(
                        stored_ep,
                        salience,
                        concept_seeds=(
                            storage_dec.concept_seeds
                            if storage_dec.concept_seeds else None
                        ),
                    )
            except Exception as e:
                logger.debug(f"Concept extraction launch error: {e}")

            # entity_triggers 透過 ep_metadata 傳入 store_episode()，
            # engine 內部在 salience < 0.4 時跳過 entity extraction，
            # 但 entity_triggers=True 會強制執行。

            # ── 5. 潛意識日記（Soul Note）──
            try:
                soul_notes.add_note(SoulNote(
                    category="reflection",
                    content=(
                        f"[tone={assessment.emotional_tone:.2f}] "
                        f"[novelty={assessment.novelty:.2f}] "
                        f"[complexity={assessment.complexity:.2f}] "
                        f"{user_input[:60]}"
                    ),
                    metadata={"episode_id": episode_id, "salience": salience},
                    tags=["auto", "dialogue"],
                ))
            except Exception as e:
                logger.debug(f"Soul note error: {e}")

            # ── 6. 定時反思 + 行動閉環 ──
            should_reflect = (
                reflection_module.should_reflect(uncertainty=assessment.uncertainty)
                or storage_dec.reflection_trigger
            )
            if should_reflect:
                try:
                    recent = engine.episodic.list_episodes(
                        limit=5, offset=0, sort_by="timestamp", ascending=False
                    )
                    # 來源感知摘要：只用 user 文字，不用混合 user／agent 原文的 content_summary
                    from echo_memory.utils.source_view import recent_user_summaries
                    summaries = recent_user_summaries(engine.evidence, recent)
                    nc = engine.neurochem

                    # 收集已知概念名稱供反思參考
                    known_concepts = [
                        c.name for c in engine.semantic.list_concepts(limit=10)
                    ]

                    reflection = reflection_module.reflect(
                        summaries, nc.dopamine, nc.serotonin, nc.mode.value,
                        known_concepts=known_concepts,
                    )
                    console.print(
                        f"  [dim magenta]💭 反思 [{reflection.action}]: "
                        f"{reflection.content[:60]}…[/]"
                    )

                    # ── 6.1 解析 memory_ops → ActionQueue ──
                    ops = parse_reflection_to_actions(
                        reflection,
                        cycle_id=f"refl_{uuid.uuid4().hex[:8]}",
                        source_episode_ids=[ep.id for ep in recent],
                    )
                    for op_dict in ops:
                        # 捕捉 pre_state
                        pre_state = reflection_executor.capture_pre_state(
                            op_dict["action_type"],
                            op_dict["target_id"],
                            op_dict["params"],
                        )
                        action = action_queue.enqueue(
                            action_type=op_dict["action_type"],
                            target_id=op_dict["target_id"],
                            params=op_dict["params"],
                            reasoning=op_dict["reasoning"],
                            source="reflection",
                        )
                        action.pre_state = pre_state
                        reflection_executor.bind_action(action)
                        # 更新 audit snapshot（確保 pre_state 被持久化）
                        action_queue.persist_action(action)

                    # ── 6.2 自動執行 AUTO 行動 ──
                    executed = action_queue.process_auto_actions(executor_fn)
                    for act in executed:
                        console.print(
                            f"  [dim green]  ⚡ 自動執行: "
                            f"{act.action_type} on {act.target_id}[/]"
                        )

                    # ── 6.3 顯示待確認行動（不阻塞）──
                    pending_confirms = action_queue.get_confirm_actions()
                    if pending_confirms:
                        console.print(
                            f"  [dim yellow]  📋 {len(pending_confirms)} 個行動"
                            f"待確認（使用 /review-actions 查看）[/]"
                        )

                except Exception as e:
                    logger.warning(f"Reflection error: {e}", exc_info=True)

        except Exception as e:
            logger.warning(f"Store error: {e}")

    # ── 清理：停止 Dream 排程 ──
    try:
        engine.stop_dream_scheduler()
    except Exception as e:
        logger.debug(f"[run_chat] Dream scheduler stop: {e}")

    # ── 清理：等待背景概念提取完成並關閉執行緒池 ──
    try:
        flushed = concept_extractor.flush(timeout=15)
        if flushed:
            logger.info(f"[run_chat] 等待 {flushed} 個背景概念提取完成")
        concept_extractor.shutdown(wait=False)
    except Exception as e:
        logger.debug(f"[run_chat] ConceptExtractor cleanup: {e}")


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


def _cmd_review_concept(engine: MemoryEngine, user_input: str, con: Console) -> None:
    """/review-concept <概念 id 或名稱>：手動對單一概念做證據審核（結果只經 Gate 寫回）。"""
    parts = user_input.strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        con.print("[yellow]用法：/review-concept <概念 id 或名稱>[/]")
        return
    ref = parts[1].strip()
    concept = engine.semantic.get(ref) or engine.semantic.get_by_name(ref)
    if concept is None:
        con.print(f"[red]找不到概念：{ref}[/]")
        return
    result = engine.review_concept(concept.id)
    detail = f"（{result['reason']}）" if result.get("reason") else ""
    con.print(f"概念審核 [cyan]{concept.name}[/] ({concept.id}): {result.get('outcome')}{detail}")


def _cmd_review_actions(
    queue: ReflectionActionQueue,
    exec_fn: Any,
    con: Console,
) -> None:
    """審閱並處理待確認的反思行動。"""
    pending = queue.get_confirm_actions()
    if not pending:
        con.print("[dim]沒有待確認的行動。[/]")
        return

    con.print(Panel(
        f"[bold yellow]📋 待確認行動 ({len(pending)} 個)[/]",
        border_style="yellow",
    ))

    for action in pending:
        con.print(
            f"\n  [bold]{action.action_type}[/] on [cyan]{action.target_id}[/]\n"
            f"  理由: {action.reasoning}\n"
            f"  參數: {action.params}"
        )
        choice = Prompt.ask(
            "  [yellow]確認執行？[/]",
            choices=["y", "n", "skip"],
            default="n",
        )
        if choice == "y":
            queue.confirm(action.id)
            ok = queue.execute(action, executor_fn=exec_fn)
            if ok:
                con.print(f"  [green]✅ 已執行: {action.action_type}[/]")
            else:
                con.print(f"  [red]❌ 執行失敗: {action.error}[/]")
        elif choice == "n":
            queue.reject(action.id)
            con.print(f"  [dim]已拒絕: {action.id}[/]")
        else:
            con.print("  [dim]跳過（保留待處理）[/]")


def _cmd_rollback(
    queue: ReflectionActionQueue,
    rb_fn: Any,
    con: Console,
) -> None:
    """回滾最近執行的反思行動。"""
    history = queue.get_history()
    executed = [a for a in history if a.status.value == "executed"]
    if not executed:
        con.print("[dim]沒有可回滾的已執行行動。[/]")
        return

    # 顯示最近 5 個已執行行動
    recent = executed[-5:]
    con.print(Panel(
        f"[bold yellow]⏪ 最近已執行行動 ({len(recent)} 個)[/]",
        border_style="yellow",
    ))

    for i, action in enumerate(recent):
        has_pre = "有" if action.pre_state else "無"
        con.print(
            f"\n  [{i}] [bold]{action.action_type}[/] on "
            f"[cyan]{action.target_id}[/]  "
            f"(pre_state: {has_pre})\n"
            f"      ID: {action.id}"
        )

    target_id = Prompt.ask("  輸入要回滾的行動 ID（或 cancel 取消）")
    if target_id.lower() == "cancel":
        return

    ok = queue.rollback(target_id, rollback_fn=rb_fn)
    if ok:
        con.print(f"  [green]✅ 已回滾: {target_id}[/]")
    else:
        con.print("  [red]❌ 回滾失敗（行動不存在或未處於 EXECUTED 狀態）[/]")


def _cmd_dream(engine: MemoryEngine, llm: LLMClient | None = None):
    """執行 Dream Engine"""
    if not Confirm.ask("[yellow]Run Dream Engine?[/]", default=False):
        return

    with console.status("[cyan]Dreaming...[/]", spinner="moon"):
        # 保底注入：若 engine 在 init 時沒拿到 llm_fn（例如 LLM 後來才可用）
        if not engine._llm_fn:
            try:
                router = LLMRouter()
                engine.set_llm_fn(router.get_llm_fn(LLMRole.EVAL))
            except Exception:
                if llm:
                    engine.set_llm_fn(llm.as_llm_fn())

        report = engine.dream(triggered_by="manual")

    if report.get("skipped_reason"):
        detail = report.get("skipped_detail")
        suffix = f" ({detail})" if detail else ""
        console.print(f"[yellow]Dream skipped: {report['skipped_reason']}{suffix}[/]")
        return
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
        table.add_row("Episodes Scanned", str(distill.get("episodes_scanned", 0)))

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
            top_k=10, min_similarity=config.retrieval_min_similarity,
        )
        if not context.is_empty():
            for ep in context.episodes:
                sim = context.similarities.get(ep.id, 0.0)
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
            if engine.episodic.update_summary(eid, new_summary):
                console.print("[green]✓ Summary updated.[/]")
            else:
                console.print("[red]Episode no longer exists.[/]")


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
            engine.delete_episode(eid)
            console.print("[green]✓ Deleted.[/]")

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
            count = engine.delete_session(sid)
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
        concepts = engine.semantic.list_concepts(limit=None)
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
                    embedding_backend=getattr(self.config, "embedding_backend", "sentence-transformers"),
                    ollama_base_url=getattr(self.config, "ollama_base_url", "http://localhost:11434"),
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

                # 透過 LLMRouter 注入 llm_fn（EVAL 角色，用於所有記憶系統內部模組）
                try:
                    self.router = LLMRouter()
                    self.engine.set_llm_fn(self.router.get_llm_fn(LLMRole.EVAL))
                    eval_cfg = self.router.get_role_config(LLMRole.EVAL)
                    console.print(f"[green]✓[/] LLM Router ready — EVAL: {eval_cfg.model}")
                except Exception as e:
                    # Router 初始化失敗時回退到 LLMClient
                    self.engine.set_llm_fn(self.llm.as_llm_fn())
                    console.print(f"[yellow]⚠ Router fallback: {e}[/]")

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

            # 指令結束後重新顯示選單
            if command not in ("exit", "quit", "q", "help", "?", "menu", ""):
                console.print()
                console.rule("[dim]─[/]", style="dim")
                self.show_menu()


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
