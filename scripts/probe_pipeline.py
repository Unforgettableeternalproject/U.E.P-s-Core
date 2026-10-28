"""
T-04 端到端驗證 Probe 腳本。

自動執行 dialogue_script.py 中的 15 輪對話劇本，
每輪印出 Rich Panel 推理地圖，同步寫入 probe_output.jsonl。

Usage:
    python scripts/probe_pipeline.py                # 非阻塞自動模式
    python scripts/probe_pipeline.py --interactive   # 互動模式（每輪暫停）
    python scripts/probe_pipeline.py --turns 5       # 只跑前 5 輪
"""

import os
import sys
import json
import time
import uuid
import logging
import argparse
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

# ── 環境設定（必須在 import echo_memory 之前）──
os.environ["DREAM_ENABLE_IDLE"] = "false"
os.environ["DREAM_ENABLE_CRON"] = "false"

# 專案路徑
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from rich.console import Console
from rich.panel import Panel

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
from echo_memory.affect.judge import StorageJudge
from echo_memory.affect.concept_extractor import ConceptExtractor
from echo_memory.affect.soul_note import SoulNote, SoulNoteManager
from echo_memory.affect.reflection import ReflectionModule
from echo_memory.affect.reflection_executor import ReflectionExecutor, parse_reflection_to_actions
from echo_memory.affect.action_queue import ReflectionActionQueue
from echo_memory.utils.sanitizer import sanitize_llm_output
from llm_integration.llm_client import LLMClient
from llm_integration.router import LLMRouter, LLMRole

from scripts.dialogue_script import DIALOGUE_SCRIPT, DREAM_TRIGGERS, DialogueTurn

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 設定
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("probe")

console = Console(force_terminal=True)

# Probe 專用存儲路徑（隔離於正式資料）
PROBE_STORAGE = PROJECT_ROOT / "probe_data"
PROBE_OUTPUT = PROJECT_ROOT / "probe_output.jsonl"


def build_pipeline():
    """建立完整 pipeline 元件，回傳所有需要的物件。"""
    config = get_config()

    # 使用 probe 專用存儲路徑
    engine = MemoryEngine(
        storage_path=PROBE_STORAGE,
        embedding_model=config.embedding_model,
        device=config.embedding_device,
        query_prefix=getattr(config, "embedding_query_prefix", ""),
        max_episodes=config.memory_max_episodes,
        embedding_backend=getattr(config, "embedding_backend", "sentence-transformers"),
        ollama_base_url=getattr(config, "ollama_base_url", "http://localhost:11434"),
        namespace_id="probe-e2e",
    )

    # LLM 初始化
    llm = LLMClient()
    router = LLMRouter()
    eval_fn = router.get_llm_fn(LLMRole.EVAL)
    engine.set_llm_fn(eval_fn)

    session_id = f"probe_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

    # 情感/潛意識模組
    concept_verifier = ConceptVerifier(
        llm_fn=eval_fn,
        embed_fn=engine.episodic.generate_query_embedding,
    )
    concept_inhibitor = ConceptInhibitor(
        semantic=engine.semantic,
        llm_fn=eval_fn,
    )
    engine._concept_inhibitor = concept_inhibitor
    subconscious_assessor = SubconsciousAssessor(llm_fn=eval_fn)
    judge = StorageJudge(llm_fn=eval_fn)
    soul_notes = SoulNoteManager(engine.storage_path)
    reflection_module = ReflectionModule(
        llm_fn=eval_fn,
        soul_notes=soul_notes,
        reflect_interval=10,
    )
    action_queue = ReflectionActionQueue(
        audit_path=engine.storage_path / "reflection_audit.jsonl",
    )
    reflection_executor = ReflectionExecutor(
        engine=engine,
        soul_notes=soul_notes,
        concept_inhibitor=concept_inhibitor,
    )
    executor_fn = reflection_executor.build_executor_fn()
    concept_extractor = ConceptExtractor(
        llm_fn=eval_fn,
        semantic=engine.semantic,
        graph=engine.episodic.graph,
        embed_fn=lambda text: engine.episodic._generate_embedding(text),
        inhibitor=concept_inhibitor,
    )

    return {
        "engine": engine,
        "llm": llm,
        "router": router,
        "eval_fn": eval_fn,
        "config": config,
        "session_id": session_id,
        "concept_verifier": concept_verifier,
        "concept_inhibitor": concept_inhibitor,
        "subconscious_assessor": subconscious_assessor,
        "judge": judge,
        "soul_notes": soul_notes,
        "reflection_module": reflection_module,
        "action_queue": action_queue,
        "reflection_executor": reflection_executor,
        "executor_fn": executor_fn,
        "concept_extractor": concept_extractor,
    }


def run_single_turn(
    turn_def: DialogueTurn,
    pipeline: dict,
    turn_counter: int,
) -> dict:
    """
    執行單輪對話，回傳推理地圖 dict。

    完全對齊 Entry.py run_chat 的 Step 1~6 流程。
    """
    engine: MemoryEngine = pipeline["engine"]
    llm: LLMClient = pipeline["llm"]
    config = pipeline["config"]
    session_id: str = pipeline["session_id"]
    concept_verifier: ConceptVerifier = pipeline["concept_verifier"]
    concept_inhibitor: ConceptInhibitor = pipeline["concept_inhibitor"]
    subconscious_assessor: SubconsciousAssessor = pipeline["subconscious_assessor"]
    judge: StorageJudge = pipeline["judge"]
    soul_notes: SoulNoteManager = pipeline["soul_notes"]
    reflection_module: ReflectionModule = pipeline["reflection_module"]
    action_queue: ReflectionActionQueue = pipeline["action_queue"]
    reflection_executor: ReflectionExecutor = pipeline["reflection_executor"]
    executor_fn = pipeline["executor_fn"]
    concept_extractor: ConceptExtractor = pipeline["concept_extractor"]
    eval_fn = pipeline["eval_fn"]

    user_input = turn_def.user_input
    trace: dict[str, Any] = {
        "turn": turn_def.turn,
        "scenario": turn_def.scenario,
        "user_input": user_input,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # ── 空白輸入防護：不觸發 LLM，不存儲 episode ──
    if not user_input or not user_input.strip():
        trace["skipped"] = "empty_input"
        trace["step1"] = {"episodes_retrieved": 0, "concepts_retrieved": 0, "procedures_retrieved": 0, "is_empty": True, "da": 0, "ht": 0, "mode": "balanced"}
        trace["step2"] = {"attempts": [], "final_gate": "skip", "response_preview": None}
        trace["step3"] = {"episode_id": None, "skipped_reason": "empty_input"}
        trace["stats"] = {
            "total_episodes": engine.get_stats().get("total_episodes", 0),
            "total_concepts": engine.get_stats().get("total_concepts", 0),
            "total_entities": engine.get_stats().get("total_entities", 0),
            "total_procedures": engine.get_stats().get("total_procedures", 0),
        }
        trace["neurochem"] = {
            "dopamine": round(engine.neurochem.dopamine, 4),
            "serotonin": round(engine.neurochem.serotonin, 4),
            "mode": engine.neurochem.mode.value,
        }
        trace["procedures_retrieved"] = 0
        return trace

    # ── 防禦性預設值 ──
    from echo_memory.affect.verifier import VerificationResult
    verify_result = VerificationResult(score=1.0, threshold=0.7, passed=True, details={})
    gate_action = InhibitionAction.PASS
    assessment = SubconsciousAssessment()

    # ── Step 1: EcphoryRAG 檢索 ──
    context = MemoryContext()
    try:
        context = engine.retrieve(
            query=user_input,
            top_k=10,
            min_similarity=config.retrieval_min_similarity,
            exclude_session_id=session_id,
        )
    except Exception as e:
        trace["step1_error"] = str(e)

    nc = engine.neurochem
    trace["step1"] = {
        "episodes_retrieved": len(context.episodes),
        "concepts_retrieved": len(context.concepts) if hasattr(context, "concepts") else 0,
        "procedures_retrieved": len(context.procedures) if hasattr(context, "procedures") else 0,
        "is_empty": context.is_empty(),
        "da": round(nc.dopamine, 4),
        "ht": round(nc.serotonin, 4),
        "mode": nc.mode.value,
    }
    memory_text = "" if context.is_empty() else context.to_text()

    # ── Step 1.5: 概念前驗證 + 潛意識評估 + 工具過濾 ──
    concept_verification = concept_verifier.verify(
        user_input=user_input,
        memory_ctx=context if not context.is_empty() else None,
    )
    if context and context.concepts:
        engine.semantic._save()

    assessment = subconscious_assessor.assess(
        user_input=user_input,
        memory_ctx=context if not context.is_empty() else None,
    )

    # StorageJudge 工具推薦（無 tools 模式，只要 assessment）
    engine.get_stats().get("total_episodes", 0)

    trace["step1_5"] = {
        "concept_verification_has_changes": concept_verification.has_changes,
        "concept_verification_summary": concept_verification.summary() if concept_verification.has_changes else None,
        "assessment": {
            "emotional_tone": round(assessment.emotional_tone, 4),
            "novelty": round(assessment.novelty, 4),
            "complexity": round(assessment.complexity, 4),
            "uncertainty": round(assessment.uncertainty, 4),
            "dream_resonance": round(assessment.dream_resonance, 4),
            "dreamed_topics": assessment.dreamed_topics,
        },
    }

    # ── Step 2: LLM 生成 + 品質閘門 ──
    response = None
    nc = engine.neurochem
    neurochem_prompt = (
        f"\n## Internal State\n"
        f"Dopamine: {nc.dopamine:.2f} | Serotonin: {nc.serotonin:.2f} | "
        f"Mode: {nc.mode.value}\n"
    )
    concept_hint = concept_verification.to_context_text()
    if concept_hint:
        neurochem_prompt += f"\n{concept_hint}\n"

    embed_fn = engine.episodic.generate_query_embedding
    verifier = ResponseVerifier(nc, embed_fn=embed_fn, llm_fn=eval_fn)
    inhibitor = ResponseInhibitor(nc, max_retries=1)

    step2_attempts = []
    for attempt in range(2):
        try:
            raw_response = llm.chat(
                message=user_input,
                memory_context=(memory_text + neurochem_prompt) if memory_text else neurochem_prompt,
            )
            raw_response = sanitize_llm_output(raw_response)

            verify_result = verifier.verify(raw_response, context if not context.is_empty() else None)
            gate_decision = inhibitor.gate(raw_response, verify_result, attempt=attempt)
            gate_action = gate_decision.action

            step2_attempts.append({
                "attempt": attempt,
                "verify_score": round(verify_result.score, 4),
                "verify_threshold": round(verify_result.threshold, 4),
                "verify_passed": verify_result.passed,
                "gate_action": gate_action.value,
                "should_retry": gate_decision.should_retry,
            })

            if gate_decision.should_retry:
                llm.undo_last_exchange()
                continue

            response = gate_decision.text
            break
        except Exception as e:
            step2_attempts.append({"attempt": attempt, "error": str(e)})
            break

    if response is None:
        response = raw_response if "raw_response" in dir() else "(no response)"

    trace["step2"] = {
        "attempts": step2_attempts,
        "final_gate": gate_action.value,
        "response_preview": response[:100] if response else None,
    }

    # ── Step 3: Episode 存儲 ──
    signals = SalienceSignals.from_subconscious(
        assessment,
        verification_score=verify_result.score,
        error_occurred=gate_action == InhibitionAction.SUPPRESS,
    )
    evaluator = SalienceEvaluator(engine.neurochem)
    baseline_salience, da_weight, ht_weight = evaluator.evaluate(
        signals, user_input=user_input, agent_response=response,
    )
    evaluator.update_neurochem(signals)

    storage_dec = judge.storage_decision(
        user_input=user_input,
        agent_response=response,
        baseline_salience=baseline_salience,
        assessment=assessment,
        context=context if not context.is_empty() else None,
        concept_verification=concept_verification,
    )
    salience = max(0.0, min(1.0, baseline_salience + storage_dec.salience_delta))

    ep_metadata: dict = {}
    if storage_dec.metadata_tags:
        ep_metadata["judge_tags"] = storage_dec.metadata_tags
    if storage_dec.force_remember:
        ep_metadata["judge_force_remember"] = True
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

    trace["step3"] = {
        "episode_id": episode_id,
        "baseline_salience": round(baseline_salience, 4),
        "salience_delta": round(storage_dec.salience_delta, 4),
        "final_salience": round(salience, 4),
        "da_weight": round(da_weight, 4),
        "ht_weight": round(ht_weight, 4),
        "judge_source": storage_dec.source,
        "concept_seeds_count": len(storage_dec.concept_seeds) if storage_dec.concept_seeds else 0,
        "entity_triggers": storage_dec.entity_triggers,
        "reflection_trigger": storage_dec.reflection_trigger,
        "force_remember": storage_dec.force_remember,
        "metadata_tags": storage_dec.metadata_tags,
    }

    # ── Step 3.5: ConceptInhibitor Patch ──
    patch_info = None
    if concept_verification.has_changes:
        try:
            patch_tx = concept_inhibitor.process(
                verification=concept_verification,
                user_input=user_input,
                llm_response=response,
                session_id=session_id,
                turn_id=episode_id,
            )
            patch_info = {
                "applied": len(patch_tx.applied_patches),
                "held": len(patch_tx.held_patches),
            }
        except Exception as e:
            patch_info = {"error": str(e)}
    trace["step3_5"] = patch_info

    # ── Step 4: 背景概念提取 ──
    concept_future = None
    try:
        stored_ep = engine.episodic._episodes.get(episode_id)
        if stored_ep:
            concept_future = concept_extractor.extract_in_background(
                stored_ep,
                salience,
                concept_seeds=(
                    storage_dec.concept_seeds if storage_dec.concept_seeds else None
                ),
            )
    except Exception as e:
        trace["step4_error"] = str(e)

    # 同步等待概念提取完成（probe 模式需要即時觀察結果）
    if concept_future:
        try:
            concept_future.result(timeout=30)
        except Exception as e:
            trace["step4_flush_error"] = str(e)

    concepts_after = engine.semantic.list_concepts(limit=10)
    trace["step4"] = {
        "total_concepts": engine.semantic.count(),
        "concept_names": [c.name for c in concepts_after],
    }

    # ── Step 5: Soul Note ──
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
        trace["step5"] = {"written": True}
    except Exception as e:
        trace["step5"] = {"written": False, "error": str(e)}

    # ── Step 6: 反思 ──
    should_reflect = (
        reflection_module.should_reflect(uncertainty=assessment.uncertainty)
        or storage_dec.reflection_trigger
    )
    trace["step6"] = {"should_reflect": should_reflect}

    if should_reflect:
        try:
            recent = engine.episodic.list_episodes(
                limit=5, offset=0, sort_by="timestamp", ascending=False
            )
            # 來源感知摘要：只用 user 文字，不用混合 user／agent 原文的 content_summary
            from echo_memory.utils.source_view import recent_user_summaries
            summaries = recent_user_summaries(engine.evidence, recent)
            nc = engine.neurochem
            known_concepts = [c.name for c in engine.semantic.list_concepts(limit=10)]

            reflection = reflection_module.reflect(
                summaries, nc.dopamine, nc.serotonin, nc.mode.value,
                known_concepts=known_concepts,
            )
            trace["step6"]["reflection"] = {
                "action": reflection.action,
                "content_preview": reflection.content[:80] if reflection.content else None,
                "reasoning_preview": reflection.reasoning[:80] if reflection.reasoning else None,
            }

            # 解析 + 排入 action queue
            ops = parse_reflection_to_actions(
                reflection,
                cycle_id=f"refl_{uuid.uuid4().hex[:8]}",
                source_episode_ids=[ep.id for ep in recent],
            )
            trace["step6"]["action_ops"] = len(ops)

            for op_dict in ops:
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
                action_queue.persist_action(action)

            executed = action_queue.process_auto_actions(executor_fn)
            trace["step6"]["auto_executed"] = len(executed)

        except Exception as e:
            trace["step6"]["error"] = str(e)

    # ── Neurochem 最終快照 ──
    nc = engine.neurochem
    trace["neurochem"] = {
        "dopamine": round(nc.dopamine, 4),
        "serotonin": round(nc.serotonin, 4),
        "mode": nc.mode.value,
    }

    # ── 統計快照 ──
    stats = engine.get_stats()
    trace["stats"] = {
        "total_episodes": stats.get("total_episodes", 0),
        "total_concepts": stats.get("total_concepts", 0),
        "total_entities": stats.get("total_entities", 0),
        "total_procedures": stats.get("total_procedures", 0),
    }

    # ── Procedure 檢索追蹤 ──
    proc_retrieved = len(context.procedures) if hasattr(context, "procedures") and context.procedures else 0
    trace["procedures_retrieved"] = proc_retrieved
    if proc_retrieved > 0:
        trace["procedure_names"] = [p.name for p in context.procedures]

    return trace


def render_trace_panel(trace: dict) -> Panel:
    """將推理地圖渲染成 Rich Panel。"""
    lines = []

    # Step 1
    s1 = trace.get("step1", {})
    lines.append(
        f"[cyan][STEP 1][/] EcphoryRAG: "
        f"episodes={s1.get('episodes_retrieved', 0)}, "
        f"concepts={s1.get('concepts_retrieved', 0)}, "
        f"DA={s1.get('da', 0):.2f}, 5-HT={s1.get('ht', 0):.2f} "
        f"[dim][{s1.get('mode', '?')}][/]"
    )

    # Step 1.5
    s15 = trace.get("step1_5", {})
    a = s15.get("assessment", {})
    lines.append(
        f"[cyan][STEP 1.5][/] Assessment: "
        f"tone={a.get('emotional_tone', 0):.2f}, "
        f"novelty={a.get('novelty', 0):.2f}, "
        f"complexity={a.get('complexity', 0):.2f}, "
        f"uncertainty={a.get('uncertainty', 0):.2f}, "
        f"dream_res={a.get('dream_resonance', 0):.2f}"
    )
    if s15.get("concept_verification_has_changes"):
        lines.append(f"           Verifier: [yellow]{s15.get('concept_verification_summary')}[/]")

    # Step 2
    s2 = trace.get("step2", {})
    gate = s2.get("final_gate", "?")
    gate_icon = {"pass": "✅", "revise": "⚠️", "suppress": "🚫"}.get(gate, "❓")
    attempts = s2.get("attempts", [])
    if attempts:
        last = attempts[-1]
        lines.append(
            f"[cyan][STEP 2][/] Gate: {gate_icon} {gate.upper()}, "
            f"verify_score={last.get('verify_score', 0):.2f}, "
            f"threshold={last.get('verify_threshold', 0):.2f}, "
            f"retries={len(attempts) - 1}"
        )
    else:
        lines.append(f"[cyan][STEP 2][/] Gate: {gate_icon} {gate.upper()} (no attempts recorded)")

    # Step 3
    s3 = trace.get("step3", {})
    sal = s3.get("final_salience", 0)
    sal_icon = "🔴" if sal >= 0.75 else "🟡" if sal >= 0.5 else "⚪"
    delta = s3.get("salience_delta", 0)
    delta_str = f" Δ{'+' if delta > 0 else ''}{delta:.2f}" if delta != 0 else ""
    lines.append(
        f"[cyan][STEP 3][/] Episode {s3.get('episode_id', '?')[:12]}…: "
        f"{sal_icon} sal={sal:.2f}{delta_str}, "
        f"da={s3.get('da_weight', 0):.2f}, ht={s3.get('ht_weight', 0):.2f} "
        f"[dim][{s3.get('judge_source', 'baseline')}][/]"
    )
    if s3.get("concept_seeds_count", 0) > 0 or s3.get("metadata_tags"):
        parts = []
        if s3.get("concept_seeds_count", 0):
            parts.append(f"{s3['concept_seeds_count']} seeds")
        if s3.get("metadata_tags"):
            parts.append(f"tags={s3['metadata_tags']}")
        if s3.get("force_remember"):
            parts.append("force_remember")
        lines.append(f"           Judge: {', '.join(parts)}")

    # Step 3.5
    s35 = trace.get("step3_5")
    if s35:
        if "error" in s35:
            lines.append(f"[cyan][STEP 3.5][/] Patch: [red]error: {s35['error'][:50]}[/]")
        else:
            lines.append(
                f"[cyan][STEP 3.5][/] Patch: "
                f"{s35.get('applied', 0)} applied, {s35.get('held', 0)} held"
            )

    # Step 4
    s4 = trace.get("step4", {})
    lines.append(
        f"[cyan][STEP 4][/] Concepts: "
        f"total={s4.get('total_concepts', 0)}"
    )
    if s4.get("concept_names"):
        names_preview = ", ".join(s4["concept_names"][:5])
        if len(s4["concept_names"]) > 5:
            names_preview += f" +{len(s4['concept_names']) - 5}"
        lines.append(f"           [{names_preview}]")

    # Step 5
    s5 = trace.get("step5", {})
    lines.append(
        f"[cyan][STEP 5][/] SoulNote: "
        f"{'written ✓' if s5.get('written') else 'failed ✗'}"
    )

    # Step 6
    s6 = trace.get("step6", {})
    if s6.get("should_reflect"):
        ref = s6.get("reflection", {})
        lines.append(
            f"[cyan][STEP 6][/] Reflection: [magenta][{ref.get('action', '?')}][/] "
            f"{(ref.get('content_preview') or '')[:50]}…"
        )
        if s6.get("auto_executed", 0) > 0:
            lines.append(f"           Auto-executed: {s6['auto_executed']} actions")
    else:
        lines.append("[cyan][STEP 6][/] Reflection: not triggered")

    # Neurochem
    nc = trace.get("neurochem", {})
    lines.append(
        f"[cyan][NEURO][/]  DA={nc.get('dopamine', 0):.2f} "
        f"5-HT={nc.get('serotonin', 0):.2f} "
        f"[dim][{nc.get('mode', '?')}][/]"
    )

    # Stats
    # Procedures retrieved
    proc_retrieved = trace.get("procedures_retrieved", 0)
    if proc_retrieved > 0:
        proc_names = trace.get("procedure_names", [])
        lines.append(
            f"[cyan][PROC][/]  [bold magenta]{proc_retrieved} procedures retrieved: "
            f"{', '.join(proc_names)}[/]"
        )

    st = trace.get("stats", {})
    lines.append(
        f"[cyan][STATS][/]  episodes={st.get('total_episodes', 0)} "
        f"concepts={st.get('total_concepts', 0)} "
        f"entities={st.get('total_entities', 0)} "
        f"procedures={st.get('total_procedures', 0)}"
    )

    body = "\n".join(lines)
    title = f"Turn {trace['turn']} │ {trace['scenario']}"
    return Panel(body, title=f"[bold]{title}[/]", border_style="cyan", width=100)


def run_dream(engine: MemoryEngine) -> dict:
    """手動觸發 Dream Engine，回傳 report。"""
    console.print("\n[bold magenta]═══ Triggering Dream Engine ═══[/]")
    try:
        report = engine.dream(triggered_by="manual")
        if report is None:
            return {"error": "dream() returned None"}
        return report
    except Exception as e:
        return {"error": str(e)}


def render_dream_panel(report: dict) -> Panel:
    """渲染 Dream Report。"""
    lines = []

    if report.get("error"):
        lines.append(f"[red]Error: {report['error']}[/]")
    else:
        lines.append(f"Duration: {report.get('duration_seconds', 0):.2f}s")
        lines.append(f"Edges updated: {report.get('edges_updated', 0)}")

        pruning = report.get("pruning")
        if pruning:
            lines.append(
                f"[cyan]Pruning:[/] {pruning.get('edges_before', 0)} → "
                f"{pruning.get('edges_after', 0)} edges "
                f"(-{pruning.get('edges_pruned', 0)} pruned, "
                f"+{pruning.get('latent_bridges_created', 0)} bridges, "
                f"{pruning.get('nodes_archived', 0)} archived)"
            )

        replay = report.get("replay")
        if replay:
            if replay.get("skipped_reason"):
                lines.append(f"[cyan]Replay:[/] skipped ({replay['skipped_reason']})")
            else:
                lines.append(
                    f"[cyan]Replay:[/] {replay.get('episodes_replayed', 0)} replayed, "
                    f"+{replay.get('edges_strengthened', 0)} strengthened, "
                    f"+{replay.get('edges_created', 0)} created"
                )

        distillation = report.get("distillation")
        if distillation:
            if distillation.get("skipped_reason"):
                lines.append(f"[cyan]Distillation:[/] skipped ({distillation['skipped_reason']})")
            else:
                lines.append(
                    f"[cyan]Distillation:[/] scanned {distillation.get('episodes_scanned', 0)}, "
                    f"+{distillation.get('concepts_created', 0)} concepts, "
                    f"~{distillation.get('concepts_updated', 0)} updated"
                )

    return Panel("\n".join(lines), title="[bold magenta]Dream Report[/]", border_style="magenta")


def main():
    parser = argparse.ArgumentParser(description="Echo Memory Pipeline Probe")
    parser.add_argument("--interactive", action="store_true", help="每輪暫停等待 Enter")
    parser.add_argument("--turns", type=int, default=None, help="只跑前 N 輪")
    parser.add_argument("--no-dream", action="store_true", help="跳過最後的 Dream 觸發")
    args = parser.parse_args()

    script = DIALOGUE_SCRIPT
    if args.turns:
        script = script[:args.turns]

    # 建立 Dream 觸發點查表
    dream_after_turns = {d.after_turn: d for d in DREAM_TRIGGERS}

    console.print(Panel(
        f"[bold cyan]Echo Memory Pipeline Probe v2[/]\n"
        f"對話劇本：{len(script)} 輪\n"
        f"Dream 觸發：{len(DREAM_TRIGGERS)} 次（after turns {', '.join(str(d.after_turn) for d in DREAM_TRIGGERS)}）\n"
        f"模式：{'互動' if args.interactive else '自動'}\n"
        f"存儲路徑：{PROBE_STORAGE}\n"
        f"輸出檔案：{PROBE_OUTPUT}",
        border_style="cyan",
    ))

    # 建立 pipeline
    console.print("\n[dim]初始化 pipeline...[/]")
    pipeline = build_pipeline()
    engine = pipeline["engine"]
    concept_extractor = pipeline["concept_extractor"]

    console.print(f"[green]✓[/] Pipeline ready -- session: {pipeline['session_id']}")
    console.print(f"[green]✓[/] LLM: {pipeline['llm'].model_name}")

    # 開啟 JSONL 輸出
    jsonl_file = open(PROBE_OUTPUT, "w", encoding="utf-8")
    dream_count = 0

    def _write_jsonl(data: dict):
        line = json.dumps(data, ensure_ascii=False, default=str)
        jsonl_file.write(line + "\n")
        jsonl_file.flush()

    def _do_dream(label: str):
        nonlocal dream_count
        dream_count += 1
        console.print(f"\n[bold magenta]{'═' * 80}[/]")
        console.print(f"[bold magenta]>>> {label}[/]")

        # 先 flush 概念提取，確保所有概念已寫入
        try:
            flushed = concept_extractor.flush(timeout=30)
            if flushed:
                console.print(f"[dim]  Flushed {flushed} pending concept extractions[/]")
        except Exception:
            pass

        dream_report = run_dream(engine)
        dream_panel = render_dream_panel(dream_report)
        console.print(dream_panel)

        # Procedure 狀態
        proc_stats = engine.get_stats()
        proc_count = proc_stats.get("total_procedures", 0)
        console.print(
            f"[bold magenta]  Procedures after dream: {proc_count}[/]"
        )
        if proc_count > 0:
            try:
                procs = engine.procedural.list_procedures()
                for p in procs:
                    console.print(
                        f"    [magenta]- {p.name}[/] ({p.domain}) "
                        f"[dim]{len(p.steps)} steps, v{p.version}[/]"
                    )
            except Exception:
                pass

        _write_jsonl({
            "turn": f"dream_{dream_count}",
            "scenario": label,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "dream_report": dream_report,
            "procedures_total": proc_count,
        })

    try:
        for i, turn_def in enumerate(script):
            console.print(f"\n[bold]{'═' * 80}[/]")
            console.print(f"[bold green]Turn {turn_def.turn}[/] | {turn_def.scenario}")
            console.print(f"[dim]User:[/] {turn_def.user_input[:120]}{'...' if len(turn_def.user_input) > 120 else ''}")
            console.print()

            # 執行單輪
            trace = run_single_turn(turn_def, pipeline, i)

            # 渲染 Rich Panel
            panel = render_trace_panel(trace)
            console.print(panel)

            # 寫 JSONL
            _write_jsonl(trace)

            # 互動模式暫停
            if args.interactive and i < len(script) - 1:
                input("\n[Press Enter to continue...]")

            # API rate limit 退避
            time.sleep(1)

            # ── 檢查是否需要觸發 Dream ──
            if not args.no_dream and turn_def.turn in dream_after_turns:
                trigger = dream_after_turns[turn_def.turn]
                _do_dream(trigger.label)

                # ── Session 切換（跨 session episode 檢索驗證）──
                if trigger.switch_session:
                    old_sid = pipeline["session_id"]
                    new_sid = f"probe_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
                    pipeline["session_id"] = new_sid
                    # 清除 LLM chat history（新 session 不帶舊對話）
                    pipeline["llm"].clear_history()
                    console.print(
                        f"\n[bold yellow]>>> Session switch: {old_sid[:20]}… → {new_sid[:20]}…[/]"
                    )
                    _write_jsonl({
                        "turn": "session_switch",
                        "scenario": f"Session switch after {trigger.label}",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "old_session": old_sid,
                        "new_session": new_sid,
                    })

                time.sleep(2)  # Dream 後多等一下

        # ── 最終 Dream（如果最後一輪沒有觸發 Dream）──
        last_turn = script[-1].turn if script else 0
        if not args.no_dream and last_turn not in dream_after_turns:
            _do_dream("Final Dream")

        # ── 最終統計 ──
        stats = engine.get_stats()
        proc_count = stats.get("total_procedures", 0)
        console.print(Panel(
            f"Episodes: {stats.get('total_episodes', 0)}\n"
            f"Concepts: {stats.get('total_concepts', 0)}\n"
            f"Entities: {stats.get('total_entities', 0)}\n"
            f"[bold]Procedures: {proc_count}[/]\n"
            f"Graph nodes: {engine.episodic.graph.get_stats().get('nodes', 0)}\n"
            f"Graph edges: {engine.episodic.graph.get_stats().get('edges', 0)}\n"
            f"Dreams triggered: {dream_count}",
            title="[bold]Final Stats[/]",
            border_style="green",
        ))

        # 列出所有 Procedure
        if proc_count > 0:
            try:
                procs = engine.procedural.list_procedures()
                proc_table = []
                for p in procs:
                    proc_table.append(
                        f"  [magenta]{p.name}[/] ({p.domain}) -- "
                        f"{len(p.steps)} steps, v{p.version}, "
                        f"sources={len(p.source_episode_ids)}"
                    )
                console.print(Panel(
                    "\n".join(proc_table),
                    title="[bold magenta]Procedures[/]",
                    border_style="magenta",
                ))
            except Exception:
                pass

    finally:
        # 清理
        try:
            flushed = concept_extractor.flush(timeout=15)
            if flushed:
                console.print(f"[dim]Flushed {flushed} concept extraction tasks[/]")
            concept_extractor.shutdown(wait=False)
        except Exception:
            pass

        jsonl_file.close()
        console.print(f"\n[green]✓[/] Output: {PROBE_OUTPUT}")


if __name__ == "__main__":
    main()
