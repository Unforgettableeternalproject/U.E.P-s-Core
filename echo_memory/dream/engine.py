"""
Dream Engine - 離線記憶鞏固主協調器

對應大腦分區：預設模式網路（Default Mode Network, DMN）

職責：
  1. 管理 APScheduler 排程（閒置觸發 + 定時 Cron）
  2. 執行記憶鞏固流程（簡化版）：
     - Graph Pruning: 清理低權重邊
     - Weight Update: 更新時間衰減
  3. 生成夢境報告

觸發條件（雙觸發機制）：
  - 閒置觸發：距離上次對話超過配置的分鐘數（默認 30 分鐘）
  - Cron 觸發：每日定時（默認凌晨 3 點）
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

# 延遲導入型別（避免循環依賴）
from typing import Any

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
    HAS_APSCHEDULER = True
except ImportError:
    BackgroundScheduler = None  # type: ignore
    CronTrigger = None  # type: ignore
    IntervalTrigger = None  # type: ignore
    HAS_APSCHEDULER = False

from echo_memory.dream.distillation import DistillationReport, KnowledgeDistillation
from echo_memory.dream.pruning import GraphPruning, PruningReport
from echo_memory.dream.replay import LiDERReplay, ReplayReport
from echo_memory.storage.episodic import EpisodicMemory
from echo_memory.storage.graph import MemoryGraph
from echo_memory.storage.semantic import SemanticMemory

logger = logging.getLogger("echo_memory.dream")


@dataclass
class DreamReport:
    """單次夢境週期的綜合報告"""

    started_at: str = ""
    finished_at: str = ""
    triggered_by: str = "manual"  # "idle" | "cron" | "manual"
    pruning: PruningReport | None = None
    replay: ReplayReport | None = None
    distillation: DistillationReport | None = None
    procedure_extraction: Any | None = None  # ProcedureExtractionReport
    edges_updated: int = 0
    error: str | None = None

    @property
    def duration_seconds(self) -> float:
        """計算執行時長"""
        try:
            s = datetime.fromisoformat(self.started_at)
            f = datetime.fromisoformat(self.finished_at)
            return (f - s).total_seconds()
        except Exception:
            return 0.0

    def summary(self) -> str:
        """生成摘要報告"""
        lines = [
            f"🌙 夢境週期完成 [{self.triggered_by}]",
            f"  耗時: {self.duration_seconds:.1f}s",
        ]

        if self.pruning:
            lines.append(f"  {self.pruning.summary()}")

        if self.replay:
            lines.append(f"  {self.replay.summary()}")

        if self.distillation:
            lines.append(f"  {self.distillation.summary()}")

        if self.edges_updated > 0:
            lines.append(f"  權重更新: {self.edges_updated} 條邊")

        if self.error:
            lines.append(f"  ⚠️ 錯誤: {self.error}")

        return "\n".join(lines)


class DreamEngine:
    """
    Echo Memory Dream Engine - 簡化版。

    設計為單例模式，使用 APScheduler BackgroundScheduler，
    不佔用主執行緒，對即時對話完全透明。

    Usage:
        engine = DreamEngine(graph)
        engine.start()  # 啟動排程器

        # 手動觸發（測試用）
        report = engine.dream_now(triggered_by="manual")
        print(report.summary())

        engine.stop()  # 關閉排程器
    """

    def __init__(
        self,
        graph: MemoryGraph,
        episodic: EpisodicMemory | None = None,
        semantic: SemanticMemory | None = None,
        procedural: Any | None = None,  # ProceduralMemory（延遲導入避免循環）
        llm_fn: Callable[[str], str] | None = None,
        concept_inhibitor: Any | None = None,  # ConceptInhibitor（GPT #4 patch pipeline）
        idle_minutes: int = 30,
        cron_schedule: str = "0 3 * * *",  # 每日凌晨 3 點
        enable_idle: bool = True,
        enable_cron: bool = True,
    ):
        """
        初始化 Dream Engine。

        Args:
            graph: MemoryGraph 實例
            episodic: EpisodicMemory 實例（蒸餾需要）
            semantic: SemanticMemory 實例（蒸餾需要）
            llm_fn: LLM 呼叫函式（蒸餾需要，None 則跳過蒸餾）
            idle_minutes: 閒置觸發時間（分鐘）
            cron_schedule: Cron 表達式（分 時 日 月 週）
            enable_idle: 是否啟用閒置觸發
            enable_cron: 是否啟用定時觸發
        """
        self.graph = graph
        self.idle_minutes = idle_minutes
        self.cron_schedule = cron_schedule
        self.enable_idle = enable_idle
        self.enable_cron = enable_cron

        # APScheduler（可選，缺少時排程功能停用，手動觸發仍可用）
        self._scheduler = BackgroundScheduler(timezone="UTC") if HAS_APSCHEDULER else None
        self._lock = threading.Lock()
        self._is_dreaming = False
        self._last_interaction: datetime = datetime.now(timezone.utc)
        self._last_dream_report: DreamReport | None = None

        # 保留 episodic 引用（給 stage 5 程序提取用）
        self._episodic_ref = episodic

        # DreamPatchCoordinator（GPT #4: 統一 audit pipeline）
        self._patch_coordinator = None
        if concept_inhibitor is not None or semantic is not None:
            from echo_memory.dream.patch_coordinator import DreamPatchCoordinator
            audit_path = None
            if semantic is not None and hasattr(semantic, 'storage_path'):
                audit_path = semantic.storage_path / "dream_audit_log.jsonl"
            self._patch_coordinator = DreamPatchCoordinator(
                inhibitor=concept_inhibitor,
                audit_log_path=audit_path,
            )

        # 子模組（傳入 semantic 供 LATENT_BRIDGE 使用）
        self._pruning = GraphPruning(
            graph, semantic=semantic,
            patch_coordinator=self._patch_coordinator,
        )

        # LiDER 反思重播（需要 episodic，llm_fn=None 時自動跳過）
        self._replay: LiDERReplay | None = None
        if episodic is not None:
            self._replay = LiDERReplay(
                episodic=episodic,
                graph=graph,
                llm_fn=llm_fn,
                patch_coordinator=self._patch_coordinator,
            )

        # 矛盾偵測器（需要 semantic + llm_fn）+ ConflictLedger
        contradiction_detector = None
        if semantic is not None:
            try:
                from echo_memory.dream.contradiction import ContradictionDetector
                from echo_memory.storage.conflict_ledger import ConflictLedger

                # ConflictLedger 與 SemanticMemory 同層持久化
                conflict_ledger = None
                if graph and hasattr(graph, 'storage_path') and graph.storage_path:
                    conflict_ledger = ConflictLedger(graph.storage_path)

                contradiction_detector = ContradictionDetector(
                    semantic=semantic,
                    graph=graph,
                    llm_fn=llm_fn,
                    conflict_ledger=conflict_ledger,
                )
            except ImportError:
                pass

        # 程序提取（需要 procedural + episodic）
        self._procedure_extractor = None
        if procedural is not None and episodic is not None:
            try:
                from echo_memory.dream.procedure_extractor import ProcedureExtractor
                self._procedure_extractor = ProcedureExtractor(
                    procedural=procedural,
                    semantic=semantic,
                    graph=graph,
                    llm_fn=llm_fn,
                )
            except ImportError:
                pass

        # 知識蒸餾（需要 episodic + semantic，任一缺失則降級跳過）
        self._distillation: KnowledgeDistillation | None = None
        if episodic is not None and semantic is not None:
            self._distillation = KnowledgeDistillation(
                episodic=episodic,
                semantic=semantic,
                graph=graph,
                llm_fn=llm_fn,
                contradiction_detector=contradiction_detector,
                patch_coordinator=self._patch_coordinator,
            )

        logger.info(
            f"[DreamEngine] 初始化完成 - "
            f"閒置觸發: {idle_minutes}min, "
            f"Cron: {cron_schedule}, "
            f"蒸餾: {'✓' if self._distillation else '✗'}"
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 排程控制
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def start(self):
        """啟動 APScheduler 排程器"""
        if self._scheduler is None:
            logger.warning("[DreamEngine] apscheduler 未安裝，排程功能停用（手動觸發仍可用）")
            return
        if self._scheduler.running:
            logger.warning("[DreamEngine] 排程器已在運行")
            return

        # 閒置檢查（每分鐘檢查一次）
        if self.enable_idle:
            self._scheduler.add_job(
                func=self._idle_check,
                trigger=IntervalTrigger(minutes=1),
                id="idle_check",
                replace_existing=True,
                misfire_grace_time=30,
            )
            logger.info(f"[DreamEngine] 已啟用閒置觸發（{self.idle_minutes} 分鐘）")

        # Cron 定時觸發
        if self.enable_cron:
            try:
                cron_parts = self.cron_schedule.split()
                if len(cron_parts) == 5:
                    minute, hour, day, month, day_of_week = cron_parts
                    self._scheduler.add_job(
                        func=lambda: self._trigger_dream("cron"),
                        trigger=CronTrigger(
                            minute=minute,
                            hour=hour,
                            day=day,
                            month=month,
                            day_of_week=day_of_week,
                        ),
                        id="cron_dream",
                        replace_existing=True,
                        misfire_grace_time=300,
                    )
                    logger.info(f"[DreamEngine] 已啟用 Cron 觸發（{self.cron_schedule}）")
            except Exception as e:
                logger.error(f"[DreamEngine] Cron 設定失敗: {e}")

        self._scheduler.start()
        logger.info("[DreamEngine] 排程器已啟動")

    def stop(self):
        """關閉排程器"""
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("[DreamEngine] 排程器已關閉")

    def notify_interaction(self):
        """通知有新的互動（重置閒置計時器）"""
        self._last_interaction = datetime.now(timezone.utc)

    def _idle_check(self):
        """檢查是否達到閒置觸發條件"""
        now = datetime.now(timezone.utc)
        idle_seconds = (now - self._last_interaction).total_seconds()
        idle_threshold = self.idle_minutes * 60

        if idle_seconds >= idle_threshold:
            logger.info(
                f"[DreamEngine] 閒置觸發條件滿足 "
                f"({idle_seconds / 60:.1f} 分鐘)"
            )
            self._trigger_dream("idle")

    def _trigger_dream(self, triggered_by: str):
        """觸發夢境週期（內部調用）"""
        try:
            report = self.dream_now(triggered_by=triggered_by)
            self._last_dream_report = report
            logger.info(f"[DreamEngine] {report.summary()}")
        except Exception as e:
            logger.error(f"[DreamEngine] 夢境週期失敗: {e}", exc_info=True)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 夢境執行
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def dream_now(self, triggered_by: str = "manual") -> DreamReport:
        """
        立即執行一個完整的夢境週期（簡化版）。

        流程：
          1. 更新邊權重（時間衰減）
          2. 圖譜修剪（移除低權重邊）

        Args:
            triggered_by: 觸發來源標籤（"manual" / "idle" / "cron"）

        Returns:
            DreamReport: 執行報告
        """
        with self._lock:
            if self._is_dreaming:
                # 防止並發執行
                report = DreamReport()
                report.error = "夢境週期已在執行中，跳過本次觸發"
                return report
            self._is_dreaming = True

        report = DreamReport(
            started_at=datetime.now(timezone.utc).isoformat(),
            triggered_by=triggered_by,
        )

        try:
            logger.info(f"[DreamEngine] 開始夢境週期 [{triggered_by}]")

            # 通知 DreamPatchCoordinator 開始新週期
            if self._patch_coordinator is not None:
                self._patch_coordinator.begin_cycle(triggered_by)

            # ── 階段 1：更新邊權重（簡化版 LiDER）──────────────────────
            report.edges_updated = self._pruning.update_edge_weights()

            # ── 階段 2：圖譜修剪 ──────────────────────────────────────
            report.pruning = self._pruning.run()

            # ── 階段 3：LiDER 反思重播（若有 LLM）──────────────────
            if self._replay is not None:
                report.replay = self._replay.run()

            # ── 階段 4：知識蒸餾（若有 LLM）─────────────────────────
            if self._distillation is not None:
                report.distillation = self._distillation.run()
                # 蒸餾會在 MemoryGraph 新增 Concept 節點和 DERIVED_FROM 邊，
                # 需要持久化以免重啟後遺失（造成 SemanticMemory 和 Graph 不一致）
                self.graph.save()

            # ── 階段 5：程序提取（若有 ProceduralMemory + LLM）────────
            if self._procedure_extractor is not None and self._episodic_ref is not None:
                try:
                    candidates = self._episodic_ref.list_episodes(
                        limit=10, sort_by="salience", ascending=False,
                    )
                    if candidates:
                        report.procedure_extraction = self._procedure_extractor.run(candidates)
                except Exception as e:
                    logger.warning(f"[DreamEngine] 程序提取失敗（不影響夢境週期）: {e}")

            # ── 圖譜持久化（階段 5 的 Procedure 節點 + APPLIES_TO 邊）────
            # Critical #2 修復：Stage 5 建立的圖譜關聯也需要 save
            self.graph.save()

            # ── 統一標記 is_dreamed（所有階段結束後）──────────────────
            #
            # 標記策略（防止 Replay 成功但蒸餾空結果時永久鎖死 Episode）：
            #
            #   1. 蒸餾有產出 → 標記蒸餾處理過的 + Replay 處理過的
            #   2. 蒸餾未啟用（self._distillation is None）→ 標記 Replay 的
            #   3. 蒸餾啟用但空結果 → **都不標記**，下一輪 Replay+蒸餾都會重試
            #
            dreamed_ids: set[str] = set()

            distillation_produced = (
                report.distillation is not None
                and (report.distillation.concepts_created > 0
                     or report.distillation.concepts_updated > 0)
            )

            if distillation_produced:
                # 蒸餾成功 → 標記蒸餾和重播處理過的 Episode
                if report.distillation and report.distillation._distilled_episode_ids:
                    dreamed_ids.update(report.distillation._distilled_episode_ids)
                if report.replay and report.replay._replayed_episode_ids:
                    dreamed_ids.update(report.replay._replayed_episode_ids)
            elif self._distillation is None:
                # 蒸餾不適用（沒有 distillation 子模組）→ Replay 可以標記
                if report.replay and report.replay._replayed_episode_ids:
                    dreamed_ids.update(report.replay._replayed_episode_ids)
            # else: 蒸餾啟用但空結果 → 不標記，下一輪重試

            if dreamed_ids and self._episodic_ref is not None:
                self._episodic_ref.mark_as_dreamed(list(dreamed_ids))
                logger.info(
                    f"[DreamEngine] 統一標記 {len(dreamed_ids)} 個 Episode 為 is_dreamed"
                )

            # 重置閒置計時器（防止立即重複觸發）
            self.notify_interaction()

            logger.info("[DreamEngine] 夢境週期完成")

        except Exception as e:
            report.error = str(e)
            logger.error(f"[DreamEngine] 夢境週期失敗: {e}", exc_info=True)

        finally:
            report.finished_at = datetime.now(timezone.utc).isoformat()
            self._is_dreaming = False

        return report

    def get_last_report(self) -> DreamReport | None:
        """獲取最後一次夢境報告"""
        return self._last_dream_report

    def is_dreaming(self) -> bool:
        """檢查是否正在執行夢境週期"""
        return self._is_dreaming
