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
from pathlib import Path

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
from echo_memory.dream.staleness import Guard, resolve_guard
from echo_memory.storage.episodic import EpisodicMemory
from echo_memory.storage.graph import MemoryGraph
from echo_memory.storage.locking import storage_lock
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
    evidence_review: Any | None = None  # ReviewBatchReport（證據審核 phase）
    hypothesis: Any | None = None  # HypothesisReport（假說產生與維護 phase）
    hypothesis_review: Any | None = None  # HypothesisReviewReport（假說審核 phase）
    sharing_reconcile: Any | None = None  # SharingReport（共享層對帳，Dream 開頭）
    sharing: Any | None = None  # SharingReport（分享 phase）
    edges_reevaluated: int = 0  # 既有 CONTRADICTS 邊本週期重判的配對數
    edges_superseded: int = 0  # 其中被改判為取代（邊已移除）的數量
    edges_updated: int = 0
    error: str | None = None
    skipped_reason: str | None = None  # 未執行的原因（租約被占用、cron 同窗口已完成等）

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

        if self.evidence_review is not None:
            lines.append(f"  {self.evidence_review.summary()}")

        if self.hypothesis is not None:
            lines.append(f"  {self.hypothesis.summary()}")

        if self.hypothesis_review is not None:
            lines.append(f"  {self.hypothesis_review.summary()}")

        if self.sharing_reconcile is not None and (
            self.sharing_reconcile.revoked or self.sharing_reconcile.corroborations_invalidated
        ):
            lines.append(f"  對帳 {self.sharing_reconcile.summary()}")

        if self.sharing is not None:
            lines.append(f"  {self.sharing.summary()}")

        if self.edges_reevaluated > 0:
            lines.append(
                f"  矛盾重判: {self.edges_reevaluated} 對, 改判取代 {self.edges_superseded}"
            )

        if self.edges_updated > 0:
            lines.append(f"  權重更新: {self.edges_updated} 條邊")

        if self.error:
            lines.append(f"  ⚠️ 錯誤: {self.error}")

        return "\n".join(lines)


class _LeaseLost(Exception):
    """Dream 週期進行中失去跨行程租約（逾期被他行程接手），須中止後續階段。"""


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
        lock_factory: Guard | None = None,
        evidence_store: Any | None = None,
        share_layer: Any | None = None,
        privacy_ledger: Any | None = None,
        owner_uid: str | None = None,
        lease: Any | None = None,
        cron_window_seconds: float = 900.0,
        hypothesis_enabled: bool = True,
        namespace_id: str | None = None,
        event_emitter: Any | None = None,
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
            evidence_store: EvidenceStore 實例（可選）。提供時蒸餾／replay／程序提取的 prompt
                依 evidence 標示來源（使用者陳述／agent 已說出口／未交付／工具記錄／來源未確認）。
            share_layer: ScopeShareLayer（M10）。提供且有 owner_uid 時啟用分享 phase 與共享層對帳；
                缺省則完全不分享。
            privacy_ledger: 擁有者的 PrivacyLedger；缺省取 Gate 的 privacy_ledger。分享判定必要，
                缺少一律不分享（fail-closed）。
            owner_uid: 本 namespace 擁有者 uid（投影的 owner_uid；通常等於 namespace_id）。
            lease: 跨行程租約（FileLease）。提供時所有觸發來源執行前都要先取得，取不到即跳過；
                None 則不做跨行程互斥。
            cron_window_seconds: cron 去重窗口；窗口內已有行程完成 cron 週期則跳過。
            hypothesis_enabled: False 時不產生也不審核假說（共享 namespace 由 engine 傳 False）。
                refuted 假說的 pair 仍會抑制 LATENT_BRIDGE 的建立。
            namespace_id: 本 engine 的 namespace id；注入 Gate／假說 phase／審核器，共享 namespace
                判斷不靠儲存路徑推得。
            lock_factory: 取得 storage_lock 的工廠（呼叫後回傳 context manager）。
                手動 dream_now 與排程（idle / cron）觸發走同一條路徑，鎖語意一致：
                只在讀快照與套用結果的短臨界區持鎖，LLM 階段一律在鎖外。
                None 時若 episodic 有 lock_path 則用其 storage_lock，否則不加鎖。
        """
        if lock_factory is None and episodic is not None:
            ep_lock_path = getattr(episodic, "lock_path", None)
            if isinstance(ep_lock_path, (str, Path)):
                lock_factory = lambda: storage_lock(ep_lock_path)  # noqa: E731
        self._guard = resolve_guard(lock_factory)
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
        self._lease = lease
        self.cron_window_seconds = cron_window_seconds

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
                semantic=semantic,
            )

        # Hypothesis：唯讀 store 取自 Gate（寫入只有 Gate 能做）；refuted 的 pair 抑制 LATENT_BRIDGE
        hypothesis_gate = self._patch_coordinator.inhibitor if self._patch_coordinator else None
        if hypothesis_gate is not None and not hasattr(hypothesis_gate, "process_hypothesis_patch"):
            hypothesis_gate = None
        if hypothesis_gate is not None and namespace_id and getattr(hypothesis_gate, "namespace_id", None) is None:
            hypothesis_gate.namespace_id = namespace_id
        self._hypothesis_store = (
            hypothesis_gate.hypothesis_store if hypothesis_gate is not None else None
        )
        self._hypothesis_phase = None
        self._hypothesis_reviewer = None

        # 子模組（傳入 semantic 供 LATENT_BRIDGE 使用）
        self._pruning = GraphPruning(
            graph, semantic=semantic,
            patch_coordinator=self._patch_coordinator,
            bridge_suppressed=(
                self._hypothesis_store.is_pair_refuted if self._hypothesis_store is not None else None
            ),
        )

        # LiDER 反思重播（需要 episodic，llm_fn=None 時自動跳過）
        self._replay: LiDERReplay | None = None
        if episodic is not None:
            self._replay = LiDERReplay(
                episodic=episodic,
                graph=graph,
                llm_fn=llm_fn,
                patch_coordinator=self._patch_coordinator,
                guard=self._guard,
                evidence_store=evidence_store,
            )

        # 證據審核（需要 semantic + evidence_store + Gate；無 LLM 時 phase 自動 skipped）
        provenance = None
        self._evidence_reviewer = None
        if semantic is not None and evidence_store is not None:
            try:
                from echo_memory.storage.provenance import ProvenanceResolver

                provenance = ProvenanceResolver(
                    semantic=semantic,
                    get_episode=(episodic.get if episodic is not None else (lambda _eid: None)),
                    evidence=evidence_store,
                )
                gate = self._patch_coordinator.inhibitor if self._patch_coordinator else None
                if gate is not None:
                    from echo_memory.affect.evidence_reviewer import EvidenceReviewer

                    self._evidence_reviewer = EvidenceReviewer(
                        llm_fn=llm_fn,
                        guard=gate,
                        semantic=semantic,
                        provenance=provenance,
                        evidence=evidence_store,
                        lock_factory=self._guard,
                        privacy_ledger=(
                            (privacy_ledger or getattr(gate, "privacy_ledger", None))
                            if share_layer is not None else None
                        ),
                    )
            except ImportError:
                pass

        # 假說 phase / 審核器（M5-D）：需要 Gate；審核另需 provenance（evidence_store）
        if hypothesis_gate is not None and semantic is not None:
            from echo_memory.dream.hypothesis_phase import HypothesisPhase

            self._hypothesis_phase = HypothesisPhase(
                semantic=semantic,
                graph=graph,
                store=self._hypothesis_store,
                gate=hypothesis_gate,
                provenance=provenance,
                lock_factory=self._guard,
                patch_coordinator=self._patch_coordinator,
                enabled=hypothesis_enabled,
                namespace_id=namespace_id,
            )
            if provenance is not None and hypothesis_enabled:
                from echo_memory.affect.hypothesis_reviewer import HypothesisReviewer

                self._hypothesis_reviewer = HypothesisReviewer(
                    llm_fn=llm_fn,
                    guard=hypothesis_gate,
                    semantic=semantic,
                    provenance=provenance,
                    store=self._hypothesis_store,
                    lock_factory=self._guard,
                    namespace_id=namespace_id,
                )

        # 分享 phase（M10）：需要審核器 + 共享層 + owner uid；條件不齊備時 phase 自行 skipped
        self._sharing_phase = None
        if (
            share_layer is not None and self._evidence_reviewer is not None
            and semantic is not None and self._patch_coordinator is not None
        ):
            from echo_memory.dream.sharing_phase import SharingPhase

            self._sharing_phase = SharingPhase(
                owner_uid=owner_uid,
                semantic=semantic,
                reviewer=self._evidence_reviewer,
                share_layer=share_layer,
                patch_coordinator=self._patch_coordinator,
                lock_factory=self._guard,
            )

        # 矛盾偵測器（需要 semantic + llm_fn）+ ConflictLedger
        contradiction_detector = None
        if semantic is not None:
            try:
                from echo_memory.dream.contradiction import ContradictionDetector
                from echo_memory.storage.conflict_ledger import ConflictLedger

                # ConflictLedger 使用 graph.storage_path（圖譜根目錄），與圖譜同層，非 SemanticMemory 子目錄
                conflict_ledger = None
                if graph and hasattr(graph, 'storage_path') and graph.storage_path:
                    conflict_ledger = ConflictLedger(graph.storage_path)

                if self._patch_coordinator is not None:
                    self._patch_coordinator.conflict_ledger = conflict_ledger
                contradiction_detector = ContradictionDetector(
                    semantic=semantic,
                    graph=graph,
                    llm_fn=llm_fn,
                    conflict_ledger=conflict_ledger,
                    guard=self._guard,
                    patch_coordinator=self._patch_coordinator,
                    provenance=provenance,
                )
            except ImportError:
                pass

        self._contradiction_detector = contradiction_detector
        # M6：事件發射器（None＝不發）；矛盾偵測器共用同一個
        self.event_emitter = event_emitter
        if contradiction_detector is not None:
            contradiction_detector.event_emitter = event_emitter

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
                    guard=self._guard,
                    episodic=episodic,
                    evidence_store=evidence_store,
                    patch_coordinator=self._patch_coordinator,
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
                guard=self._guard,
                evidence_store=evidence_store,
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

    @property
    def is_running(self) -> bool:
        """排程器是否正在運行。"""
        return self._scheduler is not None and bool(self._scheduler.running)

    def start(self) -> bool:
        """啟動 APScheduler 排程器；實際啟動回傳 True，停用或已在運行回傳 False。"""
        if self._scheduler is None:
            logger.warning("[DreamEngine] apscheduler 未安裝，排程功能停用（手動觸發仍可用）")
            return False
        if self._scheduler.running:
            logger.warning("[DreamEngine] 排程器已在運行")
            return False

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
        return True

    def stop(self):
        """關閉排程器"""
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("[DreamEngine] 排程器已關閉")

    def notify_interaction(self, at: datetime | None = None):
        """通知有新的互動（重置閒置計時器）；at 供宿主補入先前累積的互動時間。"""
        self._last_interaction = at or datetime.now(timezone.utc)

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
            if report.skipped_reason:
                # 被租約擋下：只對 idle 觸發重置閒置計時（避免每分鐘重試洗版）；cron 不重置
                if triggered_by == "idle":
                    self.notify_interaction()
                return
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

        # 跨行程租約：取不到（他行程在跑、cron 同窗口已完成）就跳過，不阻塞
        lease_held = False
        if self._lease is not None:
            window = self.cron_window_seconds if triggered_by == "cron" else None
            ok, reason = self._lease.try_acquire(
                owner=triggered_by,
                skip_if_completed_within=window,
                completion_key="last_cron_completed_at",
            )
            if not ok:
                report.skipped_reason = reason
                report.finished_at = datetime.now(timezone.utc).isoformat()
                with self._lock:
                    self._is_dreaming = False
                logger.info(f"[DreamEngine] 跳過夢境週期 [{triggered_by}]: {reason}")
                return report
            lease_held = True

        try:
            logger.info(f"[DreamEngine] 開始夢境週期 [{triggered_by}]")

            # 鎖語意（手動 / idle / cron 共用）：整個週期不持鎖，
            # 各階段只在讀快照與套用結果的短臨界區持 storage_lock，LLM 在鎖外。

            # 通知 DreamPatchCoordinator 開始新週期
            if self._patch_coordinator is not None:
                with self._guard():
                    self._patch_coordinator.begin_cycle(triggered_by)
                # 重審先前 held 的 concept patch（LLM 在鎖外，apply 在鎖內）
                self._patch_coordinator.review_held(guard=self._guard)

            # ── 共享層對帳（不需 LLM）：孤兒投影、已撤回未刪、失效佐證 ──────
            if self._sharing_phase is not None:
                try:
                    report.sharing_reconcile = self._sharing_phase.reconcile()
                except Exception as e:
                    logger.warning(f"[DreamEngine] 共享層對帳失敗（不影響夢境週期）: {e}")

            self._renew_lease(lease_held, "共享層對帳")
            # ── 階段 1、2：邊權重更新 + 圖譜修剪（無 LLM，單一短臨界區）──
            with self._guard():
                report.edges_updated = self._pruning.update_edge_weights()
                report.pruning = self._pruning.run()

            self._renew_lease(lease_held, "邊權重更新與修剪")
            # ── 階段 3：LiDER 反思重播（若有 LLM）──────────────────
            if self._replay is not None:
                report.replay = self._replay.run()

            self._renew_lease(lease_held, "LiDER 重播")
            # ── 階段 4：知識蒸餾（若有 LLM）─────────────────────────
            if self._distillation is not None:
                report.distillation = self._distillation.run()
                # 蒸餾會在 MemoryGraph 新增 Concept 節點和 DERIVED_FROM 邊，
                # 需要持久化以免重啟後遺失（造成 SemanticMemory 和 Graph 不一致）
                with self._guard():
                    self.graph.save()

            self._renew_lease(lease_held, "知識蒸餾")
            # ── 階段 4.5：證據審核（若有 LLM；LLM 在鎖外，寫回走 Gate）────
            # 優先序：新概念 > digest 失效 > held 待審；單次上限 evidence_review_batch
            if self._evidence_reviewer is not None:
                try:
                    report.evidence_review = self._evidence_reviewer.review_batch()
                except Exception as e:
                    logger.warning(f"[DreamEngine] 證據審核失敗（不影響夢境週期）: {e}")

            self._renew_lease(lease_held, "證據審核")
            # ── 階段 4.52：假說（先維護與產生，不需 LLM；再審核，LLM 在鎖外，寫回走 Gate）────
            if self._hypothesis_phase is not None:
                try:
                    report.hypothesis = self._hypothesis_phase.run()
                except Exception as e:
                    logger.warning(f"[DreamEngine] 假說 phase 失敗（不影響夢境週期）: {e}")
            if self._hypothesis_reviewer is not None and (
                report.hypothesis is None or report.hypothesis.skipped_reason is None
                or report.hypothesis.skipped_reason == "no_provenance"
            ):
                try:
                    report.hypothesis_review = self._hypothesis_reviewer.review_batch()
                except Exception as e:
                    logger.warning(f"[DreamEngine] 假說審核失敗（不影響夢境週期）: {e}")

            self._renew_lease(lease_held, "假說")
            # ── 階段 4.55：分享（LLM 在鎖外；投影走 Gate share/project；無 LLM 不投影）────
            if self._sharing_phase is not None:
                try:
                    report.sharing = self._sharing_phase.run()
                except Exception as e:
                    logger.warning(f"[DreamEngine] 分享 phase 失敗（不影響夢境週期）: {e}")

            self._renew_lease(lease_held, "分享")
            # ── 階段 4.6：既有 CONTRADICTS 邊重判（LLM 在鎖外；批次受 config 限制）────
            if self._contradiction_detector is not None and self._contradiction_detector.llm_fn:
                try:
                    results = self._contradiction_detector.reevaluate_batch()
                    report.edges_reevaluated = len(results)
                    report.edges_superseded = sum(1 for r in results if r.action == "superseded")
                    if results:
                        with self._guard():
                            self.graph.save()
                except Exception as e:
                    logger.warning(f"[DreamEngine] 矛盾重判失敗（不影響夢境週期）: {e}")

            self._renew_lease(lease_held, "矛盾重判")
            # ── 階段 5：程序提取（若有 ProceduralMemory + LLM）────────
            if self._procedure_extractor is not None and self._episodic_ref is not None:
                try:
                    # 逐條 audit 由 write_procedure 負責；此處只在短臨界區讀候選
                    with self._guard():
                        candidates = self._episodic_ref.list_episodes(
                            limit=10, sort_by="salience", ascending=False,
                        )
                    if candidates:
                        # LLM 在鎖外；寫入由 extractor 在短臨界區內套用
                        report.procedure_extraction = self._procedure_extractor.run(candidates)
                except Exception as e:
                    logger.warning(f"[DreamEngine] 程序提取失敗（不影響夢境週期）: {e}")

            self._renew_lease(lease_held, "程序提取")
            # ── 圖譜持久化（階段 5 的 Procedure 節點 + APPLIES_TO 邊）────
            # Critical #2 修復：Stage 5 建立的圖譜關聯也需要 save
            with self._guard():
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
                with self._guard():
                    self._episodic_ref.mark_as_dreamed(list(dreamed_ids))
                logger.info(
                    f"[DreamEngine] 統一標記 {len(dreamed_ids)} 個 Episode 為 is_dreamed"
                )

            # 重置閒置計時器（防止立即重複觸發）
            self.notify_interaction()

            logger.info("[DreamEngine] 夢境週期完成")
            # M6：週期正常結束後通知 Core（fire-and-forget，例外與逾時都不影響夢境）
            if self.event_emitter is not None and self._patch_coordinator is not None:
                # cycle id 只到秒；加上 started_at 讓相鄰週期的 event_id 不會被誤判為重複
                self.event_emitter.emit_dream_completed(
                    f"{self._patch_coordinator.cycle_id}@{report.started_at}"
                )

        except _LeaseLost as e:
            lease_held = False  # 已非持有者，不必（也不該）釋放
            report.error = f"Dream 租約已失去，於「{e}」後中止後續階段"
            logger.error(f"[DreamEngine] {report.error}")

        except Exception as e:
            report.error = str(e)
            logger.error(f"[DreamEngine] 夢境週期失敗: {e}", exc_info=True)

        finally:
            report.finished_at = datetime.now(timezone.utc).isoformat()
            self._is_dreaming = False
            if lease_held:
                keys = ("last_completed_at",)
                if triggered_by == "cron":
                    keys += ("last_cron_completed_at",)
                # 釋放失敗重試一次；仍失敗只記 log（租約會自行逾期）
                for attempt in (1, 2):
                    try:
                        self._lease.release(completed=report.error is None, completion_keys=keys)
                        break
                    except Exception as exc:
                        logger.warning(f"[DreamEngine] 釋放 Dream 租約失敗（第 {attempt} 次）: {exc}")

        return report

    def _renew_lease(self, held: bool, after_stage: str) -> None:
        """階段之間續租；原持有者已失租則丟 _LeaseLost 中止後續階段。"""
        renew = getattr(self._lease, "renew", None) if held else None
        if renew is None:
            return
        if not renew():
            raise _LeaseLost(after_stage)

    def get_last_report(self) -> DreamReport | None:
        """獲取最後一次夢境報告"""
        return self._last_dream_report

    def is_dreaming(self) -> bool:
        """檢查是否正在執行夢境週期"""
        return self._is_dreaming
