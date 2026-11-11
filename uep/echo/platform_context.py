"""PlatformContextProvider 與 shadow 觀察者（平台宣告注入端契約草案 §3、§6 第 2～4 步；組裝層）。

provider 每個 turn 輪詢一次 Presence（唯讀 ``peek``）、executors 來源與 ``session.platform``，算出
``PlatformContext`` 快照；版本以 ``status + row_version`` 等判定（``generation`` 只在核發 Lease 時遞增）。
shadow 只記錄與比對，**不改任何消費點的輸入**：``ProjectionContext``、``ProactiveContext`` 的實際輸入與先前相同
（恆為 None／空集合）。``loop_consult`` 的 placement 事實自第 5 步（決策層側）起由 assembly 以同一個 provider 注入
（見 ``uep.decision.loop_consult.placement_facts``）；shadow 的比對基準 ``ProductionInputs`` 仍代表「未注入」的舊值。
任何失敗都吞掉並計數，不影響 turn。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from uep.contracts.platform import (
    ORIGIN_COARSE_FALLBACK,
    ExecutorDescriptor,
    PlatformContext,
    PlatformId,
    is_safe_ref,
    platform_from_presence_location,
    platform_from_session,
)
from uep.contracts.presence import PresenceStatus
from uep.echo.metrics import Metrics
from uep.presence.manager import PresenceNotStarted, PresenceView

logger = logging.getLogger("uep.echo.platform_context")


class PresenceViewSource(Protocol):
    """唯讀的 Presence 來源；不得進寫入交易（``PresenceManager.peek``）。"""

    def peek(self) -> PresenceView: ...


class ExecutorSource(Protocol):
    """executors 來源。工具層的 ``ToolRegistry.executors_for`` 接上後只需換掉這個來源。"""

    def executors_for(self, platform: PlatformId) -> Sequence[ExecutorDescriptor]: ...


class CoarseExecutorSource:
    """預設來源（F-1 退路）：只回 current_platform 的粗粒度結果，即 ``(platform,)``「只能留在原 executor」。"""

    def executors_for(self, platform: PlatformId) -> Sequence[ExecutorDescriptor]:
        return (ExecutorDescriptor(executor_ref=platform, tool="*", platform=platform, origin=ORIGIN_COARSE_FALLBACK),)


@dataclass(frozen=True)
class ProductionInputs:
    """目前 production 實際傳入三個消費點的值（全部是「無場域」）。shadow 用它當比對基準。"""

    projection_platform: str | None = None
    presence_active_platforms: frozenset[str] = frozenset()
    placement_current_platform: str | None = None
    placement_available_executors: tuple[str, ...] = ()
    proactive_current_platform: str | None = None
    monitor_platforms: frozenset[str] = frozenset()


def diff_against_production(ctx: PlatformContext, production: ProductionInputs | None = None) -> tuple[str, ...]:
    """快照算出的值與 production 實際傳入值的差異欄位名稱（純函式）。"""
    prod = production or ProductionInputs()
    diffs: list[str] = []
    if ctx.current_platform != prod.projection_platform:
        diffs.append("projection.platform")
    if ctx.presence_active_platforms != prod.presence_active_platforms:
        diffs.append("projection.presence_active_platforms")
    if ctx.current_platform != prod.placement_current_platform:
        diffs.append("placement.current_platform")
    if ctx.available_executors != prod.placement_available_executors:
        diffs.append("placement.available_executors")
    if ctx.current_platform != prod.proactive_current_platform:
        diffs.append("proactive.current_platform")
    if ctx.monitor_platforms != prod.monitor_platforms:
        diffs.append("monitor_platforms")
    return tuple(diffs)


class PlatformContextProvider:
    """每個 turn 呼叫一次 ``snapshot``。version 在「判定欄位」與上一份不同時 +1，重啟歸零。"""

    def __init__(
        self,
        presence: PresenceViewSource,
        executors: ExecutorSource | None = None,
        *,
        registry_revision: Callable[[], int] | None = None,
        declaration_revision: Callable[[], int] | None = None,
        monitor_platforms: Callable[[], Iterable[PlatformId]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._presence = presence
        self._executors: ExecutorSource = executors or CoarseExecutorSource()
        self._fallback = CoarseExecutorSource()
        self._registry_revision = registry_revision or (lambda: 0)
        self._declaration_revision = declaration_revision or (lambda: 0)
        self._monitor_platforms = monitor_platforms or frozenset
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._version = 0
        self._last_key: tuple | None = None

    def _read_presence(self) -> tuple[PresenceStatus, PlatformId | None, int, int]:
        """-> (有效 status, current_platform, generation, row_version)；失敗 fail-closed 為 NONE（NONE = 不在任何平台、存在於主機中，是正常狀態而非故障；此處讀不到 Presence 時保守以 NONE 處理，不誤報任何平台）。"""
        try:
            view = self._presence.peek()
        except PresenceNotStarted:
            logger.warning("platform_context：Presence 尚未 start，保守以 NONE（不在任何平台）處理")
            return PresenceStatus.NONE, None, 0, 0
        except Exception:
            logger.warning("platform_context：讀取 Presence 失敗，保守以 NONE（不在任何平台）處理", exc_info=True)
            return PresenceStatus.NONE, None, 0, 0
        status = view.status
        # peek 不處理過期轉換：ACTIVE 但 Lease 已過期，視同 NONE（回到主機、不在任何平台）（不把過期的位置當成 current_platform）
        if status is PresenceStatus.ACTIVE and view.expires_at is not None and view.expires_at <= self._clock():
            status = PresenceStatus.NONE
        platform = platform_from_presence_location(view.location) if status is PresenceStatus.ACTIVE else None
        return status, platform, view.generation, view.row_version

    def _executor_refs(self, platform: PlatformId | None) -> tuple[str, ...]:
        if platform is None:
            return ()
        try:
            descriptors = self._executors.executors_for(platform)
        except Exception:
            logger.warning("platform_context：executors 來源失敗，退回粗粒度（F-1）", exc_info=True)
            descriptors = self._fallback.executors_for(platform)
        return tuple(sorted({d.executor_ref for d in descriptors if d.platform == platform}))

    def _read_monitor_platforms(self) -> frozenset[PlatformId]:
        """監控面在線的平台（EC-3：與 Presence 完全獨立）；來源失敗或內容不合法一律以空集合／丟棄處理，不影響快照。"""
        try:
            return frozenset(ref for ref in self._monitor_platforms() if is_safe_ref(ref))
        except Exception:
            logger.warning("platform_context：讀取 monitor_platforms 失敗，以空集合計", exc_info=True)
            return frozenset()

    def snapshot(self, session_platform: str | None = None) -> PlatformContext:
        status, platform, generation, row_version = self._read_presence()
        refs = self._executor_refs(platform)
        registry_rev = self._safe_int(self._registry_revision)
        declaration_rev = self._safe_int(self._declaration_revision)
        monitors = self._read_monitor_platforms()
        key = (generation, row_version, status, platform, refs, registry_rev, declaration_rev, monitors)
        with self._lock:
            if key != self._last_key:
                self._version += 1
                self._last_key = key
            version = self._version
        return PlatformContext(
            version=version, presence_generation=generation, presence_row_version=row_version,
            presence_status=status, current_platform=platform,
            presence_active_platforms=frozenset({platform}) if platform is not None else frozenset(),
            session_platform=platform_from_session(session_platform), available_executors=refs,
            registry_revision=registry_rev, declaration_revision=declaration_rev, monitor_platforms=monitors,
        )

    @staticmethod
    def _safe_int(source: Callable[[], int]) -> int:
        try:
            return int(source())
        except Exception:
            logger.warning("platform_context：讀取版本來源失敗，以 0 計", exc_info=True)
            return 0


class PlatformShadow:
    """shadow 觀察者：每個 turn 取一份快照，記錄版本與摘要、計數與 production 實際值的差異；永不拋出。

    記錄落點：``logging``（logger ``uep.echo.platform_context``）與 ``Metrics`` 計數器（``platform_shadow.*``）。
    decision_log 沒有可用的 payload 欄位（0014 只加 sub_kind／trigger／namespace／snapshot_version），故不寫表。"""

    def __init__(
        self, provider: PlatformContextProvider, metrics: Metrics | None = None,
        *, production: ProductionInputs | None = None,
    ) -> None:
        self._provider = provider
        self.metrics = metrics if metrics is not None else Metrics()
        self._production = production or ProductionInputs()
        self.last: PlatformContext | None = None

    def observe(self, turn_ref: str, session_platform: str | None = None) -> PlatformContext | None:
        try:
            ctx = self._provider.snapshot(session_platform)
            diffs = diff_against_production(ctx, self._production)
            m = self.metrics
            m.inc("platform_shadow.turns")
            if ctx.current_platform is None:
                m.inc("platform_shadow.current_platform_none")
            if diffs:
                m.inc("platform_shadow.diff_turns")
                for name in diffs:
                    m.inc(f"platform_shadow.diff.{name}")
            if self.last is None or ctx.version != self.last.version:
                m.inc("platform_shadow.version_changes")
            self.last = ctx
            logger.info(
                "platform_shadow turn=%s platform_context_version=%d %s diff=%s",
                turn_ref, ctx.version, ctx.summary(), ",".join(diffs) or "-",
            )
            return ctx
        except Exception:
            self.metrics.inc("platform_shadow.errors")
            logger.exception("platform_shadow 失敗（已忽略，不影響 turn）")
            return None
