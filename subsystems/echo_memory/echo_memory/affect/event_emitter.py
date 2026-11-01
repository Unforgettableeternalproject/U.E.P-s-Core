"""
MemoryEventEmitter — 在 commit 之後把「已發生的事」發給 Core（M6）

單向、fire-and-forget：
  - 只在 Gate 狀態轉移已落地、或圖譜結構已變化之後呼叫；
  - 不讀回傳值、不依賴對方成功；對方丟例外或卡住（逾時）都不影響寫入與 Dream；
  - 開關關閉、沒有 sink、共享 namespace 時一律不發；
  - evidence_refs 為空不發；以 event_id（內容雜湊）去重；
  - weight 只填值供排序，不是門檻；本模組不做任何關鍵字／規則判斷。

本模組不 import 任何 storage 寫入端，也不碰網路或子行程。
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from echo_memory.schemas.events import (
    KIND_CONTRADICTION_FOUND,
    KIND_DREAM_COMPLETED,
    KIND_EPISODE_FLAGGED,
    KIND_HYPOTHESIS_CONTRADICTED,
    KIND_HYPOTHESIS_SUPPORTED,
    KIND_REFLECTION_COMPLETED,
    KIND_SELF_OBSERVATION_PROPOSED,
    CandidateSink,
    EventDraft,
    build_candidate_draft,
    build_memory_draft,
)
from echo_memory.schemas.sharing import is_scope_namespace_id

logger = logging.getLogger("echo_memory.affect.event_emitter")

# 候選存活時間、同時未結束的投遞執行緒上限、去重記憶容量
_CANDIDATE_TTL = timedelta(days=7)
_MAX_INFLIGHT = 4
_SEEN_CAPACITY = 2048


def _default_enabled() -> bool:
    from echo_memory.config import get_config

    return bool(getattr(get_config(), "self_model_events_enabled", False))


class MemoryEventEmitter:
    """組裝 EventDraft（memory.*／proactive.trigger_observed） 並 fire-and-forget 投遞。所有 emit_* 都回傳 None。"""

    def __init__(
        self,
        namespace_id: str,
        *,
        enabled_fn: Callable[[], bool] | None = None,
    ) -> None:
        self.namespace_id = namespace_id
        self._enabled_fn = enabled_fn or _default_enabled
        self._sink: CandidateSink | None = None
        self._seen: OrderedDict[str, None] = OrderedDict()  # 已成功投遞的 key
        self._pending: set[str] = set()  # 在途（已預約、尚未成功）的 key，避免同事件並行重送
        self._lock = threading.Lock()
        self._inflight = 0

    # ── 注入 ────────────────────────────────

    def set_sink(self, sink: CandidateSink | None) -> None:
        self._sink = sink

    def set_enabled_fn(self, fn: Callable[[], bool] | None) -> None:
        """宿主注入的開關：設定後取代全域 self_model_events_enabled 判斷；None 還原為全域設定。"""
        self._enabled_fn = fn or _default_enabled

    def drain(self, timeout: float = 2.0) -> bool:
        """等所有在途投遞結束（僅供測試／關閉時使用；hook 路徑不呼叫）。回傳是否已清空。"""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._inflight == 0:
                    return True
            time.sleep(0.005)
        return False

    @property
    def sink(self) -> CandidateSink | None:
        return self._sink

    def _active(self) -> bool:
        """開關、sink、namespace 三者都成立才會發。共享 namespace 不發 Self Model 事件。"""
        if self._sink is None:
            return False
        if is_scope_namespace_id(self.namespace_id):
            return False
        try:
            return bool(self._enabled_fn())
        except Exception:  # noqa: BLE001 - 判斷開關失敗視為關閉
            return False

    # ── 各觸發點（都是 commit 之後呼叫） ───────────

    def emit_dream_completed(self, cycle_id: str, weight: float = 0.0) -> None:
        """Dream 週期結束：只帶 cycle id（patch 與 audit 以此回溯），不含內容摘要。"""
        refs = (f"dream_cycle:{cycle_id}",) if cycle_id else ()
        self._emit(KIND_DREAM_COMPLETED, refs, derived_from=(), weight=weight)

    def emit_reflection_completed(self, action_ids: list[str], weight: float = 0.0) -> None:
        """Reflection 的行動已通過 Gate 並執行：evidence_refs 為這些 action id。"""
        refs = tuple(str(a) for a in action_ids if a)
        self._emit(KIND_REFLECTION_COMPLETED, refs, derived_from=(), weight=weight)

    def emit_hypothesis_transition(
        self,
        hypothesis_id: str,
        supported: bool,
        cited_evidence_ids: list[str],
        support_strength: float = 0.0,
        record_version: int | str = "",
    ) -> None:
        """Hypothesis 經 Gate 變為 supported／refuted。evidence_refs 等於 Gate 驗證後的引用。

        supported 時另產生一個最小路徑的 ProactiveCandidate（純描述，不帶證據原文）。
        """
        kind = KIND_HYPOTHESIS_SUPPORTED if supported else KIND_HYPOTHESIS_CONTRADICTED
        refs = tuple(str(x) for x in cited_evidence_ids if x)
        # 轉移識別：同一假說 supported→refuted→supported 每次真實轉移的紀錄版本不同，key 隨之不同
        derived = (f"hypothesis:{hypothesis_id}", f"version:{record_version}")
        draft = self._emit(kind, refs, derived_from=derived, weight=support_strength)
        if draft is not None and supported:
            self._emit_candidate_from(kind, draft, hypothesis_id, support_strength)

    def emit_contradiction_found(self, concept_a_id: str, concept_b_id: str, strength: float = 0.0) -> None:
        """圖譜新增 CONTRADICTS 邊之後。引用為兩端概念 id。"""
        refs = tuple(sorted({str(concept_a_id), str(concept_b_id)} - {""}))
        self._emit(KIND_CONTRADICTION_FOUND, refs, derived_from=(), weight=strength)

    def emit_episode_flagged(
        self, episode_id: str, via: str, weight: float = 0.0, action_id: str = "",
    ) -> None:
        """對話 LLM 主動呼叫 force_remember／adjust_importance 之後（via 為工具名）。

        action_id 是該次工具呼叫的識別：同一 episode 多次 adjust_importance 是不同的事件。
        """
        refs = (str(episode_id),) if episode_id else ()
        self._emit(
            KIND_EPISODE_FLAGGED, refs,
            derived_from=(f"tool:{via}", f"action:{action_id}"), weight=weight,
        )

    def emit_self_observation_proposed(self, concept_id: str, evidence_refs: list[str]) -> None:
        """subject=agent 的 Self Model 條目經 Gate 套用之後。"""
        refs = tuple(str(x) for x in evidence_refs if x)
        self._emit(
            KIND_SELF_OBSERVATION_PROPOSED, refs, derived_from=(f"concept:{concept_id}",), weight=0.0,
        )

    # ── 內部 ────────────────────────────────

    def _emit(
        self,
        kind: str,
        evidence_refs: tuple[str, ...],
        *,
        derived_from: tuple[str, ...],
        weight: float,
    ) -> EventDraft | None:
        """組裝、預約 key、投遞。回傳草稿僅供本模組內組候選，hook 不使用。

        key 只在投遞成功後才記入已送清單；被丟棄或接收端失敗會釋放預約，之後同一事件可再送。
        """
        try:
            if not evidence_refs or not self._active():
                return None
            draft = build_memory_draft(
                kind, self.namespace_id, evidence_refs, derived_from, weight,
            )
            if not self._reserve(draft.idempotency_key):
                return None
            self._deliver(draft)
            return draft
        except Exception as e:  # noqa: BLE001 - 事件層永遠不影響寫入路徑
            logger.warning("[EventEmitter] 組裝事件失敗（已忽略）: %s", e)
            return None

    def _reserve(self, key: str) -> bool:
        """預約 key：已成功送過或正在送就回 False。"""
        with self._lock:
            if key in self._seen or key in self._pending:
                return False
            self._pending.add(key)
            return True

    def _release(self, key: str) -> None:
        with self._lock:
            self._pending.discard(key)

    def _commit(self, key: str) -> None:
        """投遞成功：由預約轉入已送清單。"""
        with self._lock:
            self._pending.discard(key)
            self._seen[key] = None
            while len(self._seen) > _SEEN_CAPACITY:
                self._seen.popitem(last=False)

    def _emit_candidate_from(
        self, kind: str, source: EventDraft, hypothesis_id: str, confidence: float,
    ) -> None:
        """hypothesis_supported → 一個候選（proactive.trigger_observed）。summary 只含 id。"""
        try:
            draft = build_candidate_draft(
                kind,
                self.namespace_id,
                source_keys=(source.idempotency_key,),
                dedupe_key=f"hypothesis:{hypothesis_id}",
                evidence_refs=tuple(source.payload["evidence_refs"]),
                memory_weight=source.payload["weight"],
                confidence=confidence,
                expires_at=datetime.now(timezone.utc) + _CANDIDATE_TTL,
                summary=f"假說 {hypothesis_id} 已通過證據審核",
                transition_ref=source.idempotency_key,
            )
            if self._reserve(draft.idempotency_key):
                self._deliver(draft)
        except Exception as e:  # noqa: BLE001
            logger.warning("[EventEmitter] 組裝候選失敗（已忽略）: %s", e)

    def _deliver(self, draft: EventDraft) -> None:
        """在 daemon 執行緒呼叫 sink.emit，不等待、不讀回傳值；並行達上限就丟棄（不排隊）。

        成功才記入已送清單；丟棄或接收端例外都釋放預約，同一事件之後再觸發可再送。
        """
        sink = self._sink
        key = draft.idempotency_key
        if sink is None:
            self._release(key)
            return
        with self._lock:
            if self._inflight >= _MAX_INFLIGHT:
                self._pending.discard(key)
                logger.warning(
                    "[EventEmitter] 投遞執行緒已滿（接收端可能卡住），丟棄 %s key=%s",
                    draft.event_type, key,
                )
                return
            self._inflight += 1

        def _run() -> None:
            ok = False
            try:
                sink.emit(draft)
                ok = True
            except Exception as e:  # noqa: BLE001 - 接收端例外一律吞掉
                logger.warning("[EventEmitter] 接收端 emit 失敗（已忽略）: %s", e)
            finally:
                if ok:
                    self._commit(key)
                else:
                    self._release(key)
                with self._lock:
                    self._inflight -= 1

        # 完全不等待：不 join，呼叫端（可能持有 storage_lock）耗時接近 0
        threading.Thread(target=_run, name="echo-event-emit", daemon=True).start()
