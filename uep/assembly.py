"""Core 組裝層（T-17、R1）：把各 Manager 接成一個 Core，並把安全前提寫成「缺就拒絕組裝」。

必填（缺任何一項即 ``AssemblyError``，不降級成預設放行）：
- Output Gate 的 ``turn_generation_source`` 與 ``presence_audience``；要關閉須顯式 ``ALLOW_ALL`` 並同時
  傳 ``allow_gate_bypass=True``（僅供測試或尚未接線的階段）。組裝層自己建 Gate 並傳給 DeliveryManager，
  不存在「沒有 Gate 的 DeliveryManager」。
- TaskManager 的 ``presence_check``（組裝層以 Presence 權威狀態提供）與 CC-7 ``transition_guard``。
- ``publisher``、``bus``、``control``（全域停止要用控制通道廣播）。
- ``tool_resolver``（TG-1：action 的分類只能來自註冊表）。resolver 可列舉（有 ``specs()``，例如靜態註冊表）時另做
  TG-5：每項的 executor 都在 ``executors`` 映射，且每個 executor 至少被一項引用（``internal_executors`` 明示者除外）；
  動態 resolver 無法列舉，這項檢查略過。

啟動順序（``CoreAssembly.start``，契約 4.1 core_epoch 列）：

1. ``presence.start()``：Host epoch +1、舊 Lease 進 RECOVERING；完成前 Output Gate 一律拒絕。
   它失敗（例如 Host epoch 檔落後 DB）時其後任何元件都不啟動。
2. Bus 啟動、Runtime 軸 STARTING、Presence 初始投影、presence_bound Task 補暫停（reconcile）。
3. 維護迴圈（``maintenance_interval`` 不為 None 時）：定期 ``presence.expire_due()`` 與 ``bridge.reconcile()``，
   讓到期與橋接失敗不依賴外部觸發而補上（T-23）。Supervisor 迴圈（同條件）隨後啟動，定期 deadline／核准過期／孤兒回合掃描
   （D67；``UEP_SUPERVISOR_ENABLED`` 預設開）。

關閉順序（``CoreAssembly.shutdown``，SV-1）：停 Supervisor 迴圈 -> 先停產生工作的入口（ES Host、記憶 Host、endpoint socket；
進行中的回合照既有流程收尾，回合 Task 由 Agent Core ``finish_turn_task`` 結束）-> 同步 ``supervisor.shutdown()``
（``pause_all(CORE_SHUTDOWN)`` 加等 receipt，上限 ``shutdown_grace``）-> 決策日誌、stop API、維護迴圈、橋接、Bus。
理由：入口停掉之後才暫停，才不會有回合在 Task 被 PAUSED 後才收尾而被 ``finish_turn_task`` 判成 FAILED(turn_not_running)，
也不會在 ``pause_all`` 之後又建出新的回合 Task；Bus 在暫停之後才停，暫停事件才送得出去。
4. 其後才啟動 relay（``relay_starters``，例如 outbox relay）與全域停止本機 API。
5. Runtime 軸 READY。

``DeliveryManager`` 與 relay 在步驟 1 完成前不會被啟動；即使有人提前呼叫 delivery，Gate 也因 epoch
未就緒而拒絕（REJECT_NO_PRESENCE）。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from secrets import token_bytes
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from uep.adapters.output import OutputAdapter
from uep.adapters.tool import (
    CAPABILITY_CEILING,
    EnumerableToolSpecResolver,
    ToolExecutor,
    ToolSpecResolver,
    supports_cancel,
)
from uep.config import get_settings
from uep.contracts.platform import (
    InteractionCapability,
    PlatformCapabilities,
    platform_from_endpoint_location,
    platform_from_presence_location,
)
from uep.contracts.presence import PresenceStatus
from uep.control.channel import (
    ControlChannel,
    ControlHandler,
    ControlSendResult,
    ControlSubscription,
)
from uep.control.socket_transport import SocketControlServer
from uep.decision.budget_events import register_budget_events
from uep.decision.capability import ROUTE_DEFAULT_CAPABILITIES
from uep.decision.db import DbSink
from uep.decision.log import DecisionLogWriter
from uep.decision.loop_consult import (
    KIND_LOOP_CONSULT,
    placement_facts,
    register_loop_consult,
)
from uep.decision.policy_publish import register_policy_publish
from uep.echo.agent_wiring import attach_agent_tools, build_loop_entry
from uep.echo.laya_wiring import bind_laya_capabilities, resolve_capabilities_mode
from uep.echo.platform_context import (
    CoarseExecutorSource,
    PlatformContextProvider,
    PlatformShadow,
    ProductionInputs,
)
from uep.echo.proactive_wiring import ProactiveShadow, attach_proactive_shadow
from uep.echo.services import EchoServices
from uep.endpoint.auth import (
    DEFAULT_SKEW_SECONDS,
    RequestVerifier,
    SignedRequest,
    load_secrets_from_env,
)
from uep.endpoint.protocol import RpcCoreApi
from uep.endpoint.service import EndpointService
from uep.events.bus import EventBus
from uep.events.payload_hash import MIN_KEY_BYTES
from uep.events.publisher import EventPublisher
from uep.lifecycle.budget import BudgetConfig, NvmlVramProbe, ResourceBudget, VramProbe
from uep.lifecycle.coordinator import ModuleCoordinator
from uep.lifecycle.publisher import bus_sink
from uep.memory.foreground import StateBusForegroundProbe
from uep.memory.host import MemoryHost
from uep.output.manager import DeliveryManager
from uep.presence.epoch import HostEpochStore
from uep.presence.gate import (
    ALLOW_ALL,
    AudienceProvider,
    OutputGate,
    TurnGenerationSource,
)
from uep.presence.manager import PresenceManager
from uep.state.coordinator import Axis, RuntimeState, StateCoordinator
from uep.stop.api import LocalStopServer
from uep.stop.service import GlobalStop, make_flag_blocker
from uep.tasks.manager import (
    TURN_MAX_DURATION,
    IllegalTransition,
    TaskManager,
    TransitionRequest,
)
from uep.tasks.presence_bridge import (
    DEFAULT_MAINTENANCE_INTERVAL,
    PresenceMaintenance,
    PresenceTaskBridge,
)
from uep.tasks.states import RunStatus
from uep.tasks.supervisor import Supervisor, SupervisorLoop
from uep.tools.cancel import ActionCanceller
from uep.tools.catalog.memory import MEMORY_EXECUTOR, memory_registry
from uep.tools.module_events import seed_fail_closed, subscribe_module_events
from uep.tools.registry import ToolRegistry

if TYPE_CHECKING:  # 組裝層不得硬依賴 uep.stream：只有 uep/echo/host 會 import 它
    from uep.echo.host import EchoPipelineHost

logger = logging.getLogger("uep.assembly")

DECISION_LOG_CLOSE_TIMEOUT = 5.0  # 關閉時等待決策日誌排空的上限（秒）；sink 卡住時 shutdown 不會被拖住


def _decision_log_enabled(flag: bool | None) -> bool:
    """決策日誌是否啟用：明示 True／False 優先；None 時依 ``UEP_DECISION_LOG``（0／off／false／no 停用，預設啟用）。"""
    if flag is not None:
        return flag
    return get_settings().decision_log_enabled


# Presence Manager 會發出、且帶 status 的事件（Presence 軸投影用）
_PRESENCE_PROJECTED = (
    "presence.granted", "presence.activated", "presence.revoked", "presence.recovering", "presence.recovered",
)


# 預設的轉換請求提議者（S-1：fail-closed，只有名單內的已認證提議者能提請求）
DEFAULT_PROPOSERS: dict[Axis, frozenset[str]] = {
    Axis.INTERACTION: frozenset({
        "core.nlp", "core.proactive", "core.workflow", "core.turn", "core.decision",
    }),
    Axis.BEHAVIOR: frozenset({"core.policy"}),  # BH-2：Behavior 只由 BehaviorDriver（core.policy）寫入；盤點無 core.proactive 的 Behavior 請求
}


class _FanoutControl:
    """控制訊號同時送程序內通道與（若有）跨行程 socket 傳輸；socket 伺服器建好後才掛上。"""

    def __init__(self, local: ControlChannel) -> None:
        self._local = local
        self.remote: SocketControlServer | None = None

    def subscribe(self, endpoint_id: str, handler: ControlHandler) -> ControlSubscription:
        return self._local.subscribe(endpoint_id, handler)

    def send(self, signal, *, endpoint_id: str | None = None) -> ControlSendResult:
        """本地與遠端各自隔離例外：一邊失敗，另一邊仍送；兩邊都失敗才上拋。"""
        results: list[ControlSendResult] = []
        errors: list[Exception] = []
        for name, channel in (("本地通道", self._local), ("跨行程傳輸", self.remote)):
            if channel is None:
                continue
            try:
                results.append(channel.send(signal, endpoint_id=endpoint_id))
            except Exception as exc:
                logger.exception("%s控制訊號送出失敗", name)
                errors.append(exc)
        if not results:
            raise errors[0]
        delivered = tuple(dict.fromkeys(d for r in results for d in r.delivered_to))
        return ControlSendResult(
            signal.control_id, delivered, bool(delivered) and all(r.duplicate or not r.delivered_to for r in results)
        )


class AssemblyError(RuntimeError):
    """組裝前提不成立（缺 Gate、缺 presence_check 等）；拒絕組裝而不是降級。"""


@dataclass
class CoreAssembly:
    persona_id: str
    bus: EventBus
    publisher: EventPublisher
    control: ControlChannel
    presence: PresenceManager
    gate: OutputGate
    delivery: DeliveryManager
    tasks: TaskManager
    state: StateCoordinator
    stop: GlobalStop
    bridge: PresenceTaskBridge
    maintenance: PresenceMaintenance | None
    stop_api: LocalStopServer | None
    endpoint_server: SocketControlServer | None
    relay_starters: tuple[Callable[[], None], ...]
    start_log: list[str] = field(default_factory=list)  # 啟動順序紀錄（測試與診斷用）
    started: bool = False
    memory: MemoryHost | None = None  # P3-M：可選；啟動於 presence／Bus／維護迴圈之後、relay 之前
    echo: EchoPipelineHost | None = None  # P3 S3：可選；啟動於 memory 之後（ES 需要 MemoryPort）、relay 之前
    decision_log: DecisionLogWriter | None = None  # 決策日誌寫入器（有 echo 且啟用時才有）；於 echo／memory 停止後才關閉
    supervisor: Supervisor | None = None  # D67：停用（UEP_SUPERVISOR_ENABLED=0）時為 None
    supervisor_loop: SupervisorLoop | None = None  # Supervisor 啟用且 supervisor_interval 不為 0 時建立（與維護迴圈各自獨立）
    loop_launcher: Any = None  # agent_loop 的 production 啟動器（UEP_AGENT_LOOP_ENTRY）；入口停止段先關閉，早於 Supervisor 關機暫停
    module_events_unsubscribe: Callable[[], None] | None = None  # module.* 就緒事件訂閱的取消函式（UEP_TOOL_MODULE_EVENTS，subscriber）
    module_coordinator: ModuleCoordinator | None = None  # producer（UEP_MODULE_COORDINATOR，預設關閉）；關閉時為 None
    proactive_shadow: ProactiveShadow | None = None  # DL-6b：proactive.initiate 的 shadow 入口（有 echo 且決策日誌啟用時才有；不啟動 Run）
    dream_probe: StateBusForegroundProbe | None = None  # 做夢迴圈的前景 Probe（memory 啟用 dream_loop 且未自帶 Probe 時由組裝層建立）

    def start(self) -> None:
        """依上述順序啟動；冪等。失敗時關閉已綁定的入口（stop API、endpoint socket）、訂閱與 Bus 後上拋。"""
        if self.started:
            return
        try:
            self._start()
        except BaseException:
            self._close_inputs()
            raise

    def _start(self) -> None:
        self.presence.start()  # 必須第一個：Host epoch -> DB；之前 Gate 一律拒絕
        self.start_log.append("presence.start")
        self.bus.start()
        self.start_log.append("bus.start")
        if self.stop_api is not None:
            self.stop_api.start()  # 停止入口最早可用：早於 relay／endpoint／任何輸出
            self.start_log.append("stop_api.start")
        self.state.set_runtime(RuntimeState.STARTING)
        self.start_log.append("state.runtime.starting")
        view = self.presence.current()
        self.state.project(Axis.PRESENCE, view.status.value, source_version=view.row_version)
        self.start_log.append("state.presence.sync")
        self.bridge.start()
        self.bridge.reconcile()  # presence.start() 的 recovering 事件早於訂閱，這裡補暫停
        self.start_log.append("bridge.reconcile")
        if self.maintenance is not None:
            self.maintenance.start()
            self.start_log.append("maintenance.start")
        if self.supervisor_loop is not None:
            self.supervisor_loop.start()
            self.start_log.append("supervisor.start")
        if self.dream_probe is not None:
            self.dream_probe.start()  # 訂閱 Bus：須早於 memory.start（迴圈啟動後第一個 tick 就會讀 Probe）
            self.start_log.append("dream_probe.start")
        if self.module_coordinator is not None:
            # 訂閱早在組裝期完成（先訂閱再 start）；啟動快照在此發出並被 registry 套用，先於 echo／endpoint 與 Core READY
            self.module_coordinator.start()
            self.start_log.append("module_coordinator.start")
            if not self.module_coordinator.wait_settled(MODULE_COORDINATOR_SETTLE_TIMEOUT):
                logger.warning("module coordinator 在 %.0f 秒內未穩定；未就緒的工具維持 fail-closed", MODULE_COORDINATOR_SETTLE_TIMEOUT)
            self.start_log.append("module_coordinator.settled")
        if self.memory is not None:
            self.memory.start()  # 記憶 Host 在 Presence／Bus 之後（事件才有地方落）；ES Host（S3）將在它之後
            self.start_log.append("memory.start")
        if self.echo is not None:
            self.echo.start()  # ES Host 在 memory 之後（ES 需要 MemoryPort）、endpoint socket 之前
            self.start_log.append("echo.start")
        for starter in self.relay_starters:
            starter()  # 此時 presence 已啟動：relay 送出的輸出才會受 Gate 判定
        if self.relay_starters:
            self.start_log.append("relay.start")
        if self.endpoint_server is not None:
            self.endpoint_server.start()  # 端點只能在 presence 啟動後連入
            self.start_log.append("endpoint_server.start")
        self.state.set_runtime(RuntimeState.READY)
        self.start_log.append("state.runtime.ready")
        self.started = True

    def _close_inputs(self) -> None:
        """關閉入口、訂閱與 Bus（不寫 DB）；可重複呼叫。啟動失敗的收尾路徑用；正常關機由 ``shutdown`` 分兩段呼叫。"""
        self._stop_intake()
        self._stop_infrastructure()

    def _stop_intake(self) -> None:
        """第一段：停止產生工作的入口，讓進行中的回合照既有流程收尾（可重複呼叫，每一步獨立容錯）。

        停止序反向於啟動：ES Host 最先停（停 feed、cancel_all、等 commit 與事件寫完；它的 sink 與事件都還要
        經 Bus／Gateway，也還要用 memory 做 store），其次記憶 Host（它的 sink 還要經 Bus 發布），之後關 endpoint socket。"""
        if self.module_events_unsubscribe is not None and self.module_coordinator is None:
            self.module_events_unsubscribe()  # 沒有 producer：入口停止前先退訂，關機後不再改 readiness（冪等）
            self.module_events_unsubscribe = None
            self.start_log.append("module_events.unsubscribe")
        if self.loop_launcher is not None:
            # 先通知進行中的 AgentLoop 取消並限時等待（它們還要經 echo／Bus 收尾 turn），再停 echo
            try:
                self.loop_launcher.close()
            except Exception:
                logger.exception("AgentLoop 啟動器關閉失敗")
            self.start_log.append("loop_launcher.close")
        if self.echo is not None:
            try:
                self.echo.stop()
            except Exception:
                logger.exception("EchoPipelineHost 停止失敗")
            self.start_log.append("echo.stop")
        if self.memory is not None:
            try:
                self.memory.stop()
            except Exception:
                logger.exception("MemoryHost 停止失敗")
            self.start_log.append("memory.stop")
        if self.dream_probe is not None:
            self.dream_probe.close()  # 迴圈已隨 memory.stop 停止；之後才退訂 Bus（冪等）
            self.start_log.append("dream_probe.close")
        if self.endpoint_server is not None:
            self.endpoint_server.close()
        if self.module_coordinator is not None:
            # 入口都停止之後才停 coordinator（最終 shutdown 事件仍送得出去、被 registry 套用），然後退訂；Bus 在 _stop_infrastructure 才關
            try:
                self.module_coordinator.stop()
            except Exception:
                logger.exception("ModuleCoordinator 停止失敗")
            self.start_log.append("module_coordinator.stop")
            if self.module_events_unsubscribe is not None:
                self.module_events_unsubscribe()
                self.module_events_unsubscribe = None
                self.start_log.append("module_events.unsubscribe")

    def _stop_infrastructure(self) -> None:
        """第二段：決策日誌、stop API、迴圈、橋接與 Bus（在 Supervisor 關機暫停之後）。"""
        if self.decision_log is not None:
            # 最後才關：echo／memory（含其擁有的 dream）停止前仍可能送出決策；排空佇列後才釋放連線
            try:
                self.decision_log.close(timeout=DECISION_LOG_CLOSE_TIMEOUT)
            except Exception:
                logger.exception("決策日誌寫入器關閉失敗")
            self.start_log.append("decision_log.close")
        if self.stop_api is not None:
            self.stop_api.stop()
        if self.maintenance is not None:
            self.maintenance.stop()
        if self.supervisor_loop is not None:
            self.supervisor_loop.stop()
        self.bridge.stop()
        self.bus.stop()

    def shutdown(self) -> None:
        """關閉入口與訂閱；Runtime 軸 STOPPING。不撤銷 Presence（Core 關機不終止 Task，T-11）。"""
        try:
            if self.started:
                self.state.set_runtime(RuntimeState.STOPPING)
        except Exception:
            logger.exception("Runtime 軸寫入 STOPPING 失敗")
        if self.supervisor_loop is not None:
            self.supervisor_loop.stop()  # 先停 tick，避免與關機暫停競爭
        self._stop_intake()  # 入口先停：進行中的回合收尾、不再有新回合 Task（M1）
        if self.supervisor is not None and self.started:
            # SV-1：入口停止之後、bus.stop() 之前同步暫停所有未終態 Task／Run（PAUSED(core_shutdown)），不依賴事件被訂閱
            try:
                self.supervisor.shutdown()
            except Exception:
                logger.exception("Supervisor 關機暫停失敗")
            self.start_log.append("supervisor.shutdown")
        self._stop_infrastructure()
        self._log_gate_shadow_summary()
        self.started = False

    def _log_gate_shadow_summary(self) -> None:
        """E3 shadow 的可觀察出口：關機時記一次「端點未宣告 interaction」的累計摘要（log 內只有 endpoint_id 與次數）。"""
        try:
            logger.info(
                "Output Gate shadow 摘要：not_interaction=%d by_endpoint=%s",
                self.gate.not_interaction_count, self.gate.not_interaction_by_endpoint(),
            )
        except Exception:
            logger.exception("Output Gate shadow 摘要失敗")


_UNKNOWN_PLATFORM = "unknown"
MODULE_COORDINATOR_SETTLE_TIMEOUT = 10.0  # 啟動時等 coordinator 的初始載入穩定的上限（秒）；逾時不擋啟動，工具維持 fail-closed


def check_module_events_policy(subscriber: bool, producer: bool) -> None:
    """R10：subscriber（``UEP_TOOL_MODULE_EVENTS``）開而 producer（``UEP_MODULE_COORDINATOR``）缺席 => 啟動失敗。
    producer 開、subscriber 關是允許的觀測模式（見 ``assemble_core``）。純函式，可離線測試。"""
    if subscriber and not producer:
        raise AssemblyError(
            "UEP_TOOL_MODULE_EVENTS 開啟但沒有 producer（UEP_MODULE_COORDINATOR 未開）：註冊表會永遠等不到事件；"
            "請同時開啟 producer 或關閉 subscriber"
        )


# R11／設計稿 §4：v1 的 M 群（本地模型、stage）只觀測，描述子必須 pinned，不可被驅逐
_M_GROUP_KINDS = frozenset({"model", "stage"})


def check_m_group_pinned(registrations: Sequence[tuple[Any, Any]]) -> None:
    """v1 所有 M 群描述子（kind=model／stage）必須 ``pinned=True``；違反即組裝失敗。純函式，可離線測試。"""
    for descriptor, _ in registrations:
        if getattr(descriptor, "kind", None) in _M_GROUP_KINDS and not getattr(descriptor, "pinned", False):
            raise AssemblyError(f"module_registrations：{getattr(descriptor, 'module_id', '?')} 是 M 群（kind={descriptor.kind}），v1 必須 pinned=True")


def check_module_coverage(
    entries: Mapping[str, Any], known_ids: Collection[str], registrations: Sequence[tuple[Any, Any]], *, enforce: bool,
) -> tuple[list[tuple[str, str]], list[str]]:
    """雙向覆蓋檢查（設計稿 §13.3）。純函式，可離線測試。回傳 ``(缺漏, 未被引用)``：

    - 正向：所有工具 ``requires`` 都必須是已登記的 module id；缺漏 ``(工具名, module_id)``。``enforce``（coordinator 驅動註冊表）時
      有缺漏即 ``AssemblyError``（否則該工具 readiness 恆為 unavailable 卻無人知道），不驅動時只記警告。
    - 反向：``kind="service"`` 描述子必須被某個工具的 ``requires`` 引用（否則是沒人用的死登記）；M 群（kind=model／stage）豁免。只記警告。"""
    known = set(known_ids)
    missing = sorted(
        (name, module) for name, entry in entries.items() for module in getattr(entry, "requires", ()) if module not in known
    )
    referenced = {module for entry in entries.values() for module in getattr(entry, "requires", ())}
    unreferenced = sorted(
        d.module_id for d, _ in registrations
        if getattr(d, "kind", None) == "service" and d.module_id not in referenced
    )
    if missing and enforce:
        detail = "；".join(f"{name}->{module}" for name, module in missing)
        raise AssemblyError(f"工具 requires 含未登記的模組（module coordinator 驅動註冊表時不允許）：{detail}")
    if missing:
        logger.warning("工具 requires 含未登記的模組：%s", [f"{n}->{m}" for n, m in missing])
    if unreferenced:
        logger.warning("service 描述子沒有任何工具 requires 引用：%s", unreferenced)
    return missing, unreferenced


def build_module_budget(settings: Any, probe: VramProbe | None = None) -> ResourceBudget | None:
    """依設定建立資源預算；``UEP_MODULE_VRAM_HEADROOM_MB`` 未設 => None（不啟用預算管理）。
    只設 budget 沒設 headroom => ``AssemblyError``。沒給 probe 時才建 NVML probe（延遲載入 pynvml，建立本身不碰 GPU）。"""
    headroom = settings.module_vram_headroom_mb
    if headroom is None:
        if probe is not None:
            raise AssemblyError("給了 module_vram_probe 但 UEP_MODULE_VRAM_HEADROOM_MB 未設（預算管理未啟用，probe 不會被使用）")
        if settings.module_vram_budget_mb is not None:
            raise AssemblyError("UEP_MODULE_VRAM_BUDGET_MB 必須同時設定 UEP_MODULE_VRAM_HEADROOM_MB（保留水位需依機器量測，不猜預設）")
        return None
    try:
        config = BudgetConfig(
            headroom_mb=headroom, budget_mb=settings.module_vram_budget_mb, reading_max_age_s=settings.module_vram_reading_max_age_s,
            recover_margin_mb=settings.module_vram_recover_margin_mb, evict_cooldown_s=settings.module_evict_cooldown_s,
            read_timeout_s=settings.module_vram_read_timeout_s, unload_release_ratio=settings.module_unload_release_ratio,
        )
    except ValueError as exc:
        raise AssemblyError(f"模組預算設定不合法：{exc}") from exc
    return ResourceBudget(config, probe if probe is not None else NvmlVramProbe(settings.module_gpu_index))


def _declare_interaction(
    table: dict[str, PlatformCapabilities], endpoint_id: str, platform: str, facets: frozenset[str],
) -> None:
    """登記一個出站（互動）端點的能力宣告；endpoint_id 不是合法 ref 時不登記（Gate 視為查無宣告），不擋組裝。"""
    try:
        table.setdefault(endpoint_id, PlatformCapabilities(
            platform=platform, endpoint_instance_id=endpoint_id, protocol_version=1,
            interaction=InteractionCapability(facets),  # type: ignore[arg-type]
        ))
    except ValueError:
        logger.warning("端點 %r 無法產生能力宣告（id 不合法），Gate 將視為未宣告", endpoint_id)


def _assert_wired(tasks: TaskManager, delivery: DeliveryManager) -> None:
    """R1 的最終斷言：實際建出的元件確實帶著 Gate、presence_check 與 CC-7 guard，缺任一就拒絕組裝。"""
    if getattr(delivery, "_gate", None) is None:
        raise AssemblyError("DeliveryManager 未接上 Output Gate（R1）")
    if not tasks.presence_check_configured:
        raise AssemblyError("TaskManager 未接上 presence_check（R1）")
    if not tasks.transition_guard_configured:
        raise AssemblyError("TaskManager 未接上 CC-7 transition_guard")
    if not tasks.action_guard_configured:
        raise AssemblyError("TaskManager 未接上 action_guard（T-10）")
    if getattr(tasks, "tool_resolver", None) is None:
        raise AssemblyError("TaskManager 未接上 tool_resolver（TG-1）")


def _check_registry_executors(
    resolver: ToolSpecResolver, executors: Mapping[str, ToolExecutor], internal: Collection[str]
) -> None:
    """TG-5：註冊表與 executors 映射雙向一致；只對可列舉的靜態註冊表做。"""
    stray = sorted(set(internal) - set(executors))
    if stray:
        raise AssemblyError(f"internal_executors 引用了不存在的 executor：{', '.join(stray)}（TG-5）")
    if not isinstance(resolver, EnumerableToolSpecResolver):
        logger.warning("tool_resolver 不可列舉，略過 TG-5 註冊表與 executors 的雙向檢查")
        return
    specs = resolver.specs()
    _check_cancel_support(resolver, specs, executors)
    unknown = sorted({spec.executor for spec in specs.values()} - set(executors))
    if unknown:
        raise AssemblyError(f"註冊表引用了不存在的 executor：{', '.join(unknown)}（TG-5）")
    unused = sorted(set(executors) - {spec.executor for spec in specs.values()} - set(internal))
    if unused:
        raise AssemblyError(f"executors 中沒有任何註冊表項目引用：{', '.join(unused)}（TG-5）")


def _check_cancel_support(
    resolver: ToolSpecResolver, specs: Mapping[str, Any], executors: Mapping[str, ToolExecutor]
) -> None:
    """D69／RA-5／RA-11：宣告 interruptible／checkpoint_only、cancellable 或 parallel_one_shot 的工具，其 executor 必須實作 ``cancel``；fail-closed。
    executor 不在映射內的情形由 TG-5 另行回報，這裡略過。"""
    entries = resolver.entries() if callable(getattr(resolver, "entries", None)) else {}
    bad: list[str] = []
    for name, spec in specs.items():
        executor = executors.get(spec.executor)
        if executor is None or supports_cancel(executor):
            continue
        entry = entries.get(name)
        if spec.interruptible != "non_interruptible" or (
            entry is not None and (entry.cancellable or entry.concurrency == "parallel_one_shot")
        ):
            bad.append(name)
    if bad:
        raise AssemblyError(f"工具宣告可中斷或 cancellable，但其 executor 未實作 cancel：{', '.join(sorted(bad))}（D69、RA-5）")


def _check_loop_one_shot_executors(registry: Any, executors: Mapping[str, ToolExecutor]) -> None:
    """§6.4：AgentLoop 使用的註冊表若有 ``parallel_one_shot`` 條目，其 executor 必須在傳給 AgentLoop 的映射內且實作 ``cancel``，
    否則逾時／取消時無法 cancel；fail-closed。"""
    bad = sorted(
        name for name, entry in registry.entries().items()
        if entry.concurrency == "parallel_one_shot"
        and not supports_cancel(executors.get(entry.spec.executor) or object())
    )
    if bad:
        raise AssemblyError(f"parallel_one_shot 工具的 executor 不在 AgentLoop 的 executors 內或未實作 cancel：{', '.join(bad)}（RA-11、D69）")


def build_request_verifier(
    secrets: Mapping[str, bytes], *, clock: Callable[[], float] = time.time
) -> RequestVerifier:
    """端點請求驗證器。啟動下限取 ``clock() - skew``：時鐘落後 δ（< skew）的端點在 Core 重啟後立即的請求
    仍被接受；早於「啟動 - skew」的舊請求被拒。代價：重啟前最後 skew 秒內被截取的請求，nonce 表已清空，
    在窗口內可被重放一次（跨機器 Part H 前須配每則 MAC／通道綁定）。"""
    return RequestVerifier(
        secrets, clock=clock, skew_seconds=DEFAULT_SKEW_SECONDS, startup_grace=DEFAULT_SKEW_SECONDS
    )


def validate_route_capabilities(
    table: Mapping[str, Collection[str]] | None = None,
    ceiling: Mapping[str, Any] | None = None,
) -> None:
    """DL-4 PR3：route 預設表的每個標籤都必須屬於工具層閉集（``CAPABILITY_CEILING``），表內有未知標籤即 ``ValueError``。

    決策層不得 import 工具層，所以這項驗證放在組裝層。``decision.consult`` 本身就在閉集內
    （``uep/tools/catalog/decision.py`` 的 consult 工具以它為能力標籤），不需要特例。
    """
    table = ROUTE_DEFAULT_CAPABILITIES if table is None else table
    ceiling = CAPABILITY_CEILING if ceiling is None else ceiling
    unknown = {
        route: sorted(set(tags) - set(ceiling)) for route, tags in table.items() if set(tags) - set(ceiling)
    }
    if unknown:
        raise ValueError(f"route 預設表含有不在 CAPABILITY_CEILING 的未知標籤：{unknown}")


def _check_tool_path_consistency(tool_path: str, echo: EchoPipelineHost | None) -> None:
    """Host 注入的路徑與 think stage 種類都必須等於組裝層決定的路徑；不一致一律拒絕（不靜默退回另一條路徑）。"""
    if tool_path not in ("legacy", "agent"):
        raise AssemblyError(f"tool_path 只能是 legacy 或 agent：{tool_path!r}")
    if echo is None:
        return
    host_path = getattr(echo, "_tool_path", "legacy")
    if host_path != tool_path:
        raise AssemblyError(f"工具路徑不一致：組裝層為 {tool_path}，EchoPipelineHost 為 {host_path}（建構 Host 時須傳同一個 tool_path）")
    is_agent_stage = callable(getattr(echo.think, "bind_port", None))
    if is_agent_stage != (tool_path == "agent"):
        raise AssemblyError(
            f"工具路徑不一致：組裝層為 {tool_path}，但 think stage {'是' if is_agent_stage else '不是'} AgentThinkStage"
        )


# 本機 Host 的輸入位置：desktop 與測試 Console（console）；兩者的輸入都由本機 stage／Console 簽發 source_location
LOCAL_INPUT_LOCATIONS = frozenset({"desktop", "console"})


def check_source_location_policy(input_locations: Collection[str], require_source_location: bool) -> None:
    """V18 fail-closed 守門：註冊任何非本機（desktop／console）的輸入 adapter 而 ``UEP_REQUIRE_SOURCE_LOCATION`` 未開，拒絕組裝。

    觸發條件：``input_locations`` 含 desktop／console（本機）以外的值（P4 的 Discord／Website adapter 接上時）。
    目前 repo 沒有外部 adapter 註冊點；assemble_core 以 ``input_locations`` 參數呼叫本檢查，adapter 註冊點出現時沿用。
    """
    external = sorted({str(loc) for loc in input_locations} - LOCAL_INPUT_LOCATIONS)
    if external and not require_source_location:
        raise AssemblyError(
            f"註冊了非本機（desktop／console）輸入 adapter（{', '.join(external)}）時必須開啟 UEP_REQUIRE_SOURCE_LOCATION=true"
            "（缺席端輸入 fail-closed）；否則缺 source_location 的缺席端輸入會被放行"
        )


def _prepare_agent_tools(echo: EchoPipelineHost | None, model_port: Any | None) -> tuple[Any, Any]:
    """``UEP_TOOL_PATH=agent`` 的前提檢查與記憶工具 executor；任何一項不成立都拒絕組裝，不退回 legacy。"""
    from uep.agent.runtime import ModelPortBackend
    from uep.echo.agent_wiring import make_memory_executor

    if model_port is None:
        raise AssemblyError("UEP_TOOL_PATH=agent 需要 model_port（PydanticAI 埠）；不會退回 legacy 路徑")
    if echo is None:
        raise AssemblyError("UEP_TOOL_PATH=agent 需要 echo（EchoPipelineHost）")
    think = echo.think
    if not callable(getattr(think, "bind_port", None)):
        raise AssemblyError("UEP_TOOL_PATH=agent 需要 AgentThinkStage（以 tool_path=agent 組出 think stage）")
    try:
        backend = think.inner.backend
    except Exception:  # noqa: BLE001 - backend 缺失也是同一個結論
        backend = None
    if not isinstance(backend, ModelPortBackend):
        raise AssemblyError("UEP_TOOL_PATH=agent 的 think stage 必須使用 ModelPortBackend（組 stage 時傳 model_port）")
    memory = getattr(echo, "_memory", None)  # 宿主未公開 MemoryPort；組裝層與宿主同屬 Core，直接取用
    if memory is None:
        raise AssemblyError("UEP_TOOL_PATH=agent 需要 echo 具備 MemoryPort（memory=）")
    return make_memory_executor(memory, echo.roster, echo._audience_allowed)


def _probe_payload_hash_key(strict: bool = False) -> None:
    """啟動探測 payload_hash 密鑰：缺失或過短預設只告警（寫入私人 payload 時另有 fail-closed）；
    ``strict`` 時直接拒絕組裝，避免正式環境帶著壞掉的私人 payload 寫入能力啟動。"""
    key = get_settings().payload_hash_key
    if key is None or not key.get_secret_value().strip():  # 全空白視同缺失（與 PayloadHasher 一致）
        if strict:
            raise AssemblyError("strict 組裝需要 UEP_PAYLOAD_HASH_KEY，目前未設定（D42）")
        logger.warning("未設定 UEP_PAYLOAD_HASH_KEY：寫入私人 payload 會被拒絕（D42 fail-closed）")
    elif len(key.get_secret_value().encode()) < MIN_KEY_BYTES:
        if strict:
            raise AssemblyError(f"strict 組裝需要至少 {MIN_KEY_BYTES} 位元組的 UEP_PAYLOAD_HASH_KEY（D42）")
        logger.warning("UEP_PAYLOAD_HASH_KEY 少於 %d 位元組：寫入私人 payload 會被拒絕", MIN_KEY_BYTES)



def _loop_resilience_kwargs() -> dict[str, Any]:
    """AgentLoop 的 Step 逾時與重試政策（``UEP_AGENT_*``）；預算其餘欄位由 LoopLauncher 的預設決定。"""
    from uep.agent.retry import RetryPolicy
    from uep.echo.agent_wiring import task_request_budget_table

    s = get_settings()
    return {
        "step_timeout_s": s.agent_step_timeout_s or None,  # 0 = 關閉
        "budget_policies": task_request_budget_table(),  # 政策表：task_request 的緊預算組；agent_loop 查不到列，退回 launcher 預算
        "retry": RetryPolicy(
            model_max=s.agent_model_retry_max, model_backoff_s=s.agent_model_retry_backoff_s,
            tool_max=s.agent_tool_retry_max, tool_backoff_s=s.agent_tool_retry_backoff_s,
        ),
    }


def _composite_blob_kwargs(session_factory: sessionmaker[Session]) -> dict[str, Any]:
    """composite 引用 ``text`` 的持久 blob（D110）：上限為 0、沒有 ``UEP_PAYLOAD_HASH_KEY``、或密鑰不合 ``PayloadHasher`` 的長度
    規則（至少 ``MIN_KEY_BYTES``、非全空白）時不注入（行為與先前相同）；密鑰存在但不合規則時啟動記一次 warning。"""
    s = get_settings()
    key = s.payload_hash_key
    if s.composite_blob_max_bytes <= 0 or key is None:
        return {}
    from uep.events.composite_blobs import DbCompositeBlobStore
    from uep.events.payload_hash import (
        MIN_KEY_BYTES,
        PayloadHasher,
        PayloadHashKeyMissing,
    )

    try:
        PayloadHasher(key)  # 與寫入時同一套判斷：太短或全空白 -> put 一定會失敗，不如不注入
    except PayloadHashKeyMissing:
        logger.warning(
            "UEP_PAYLOAD_HASH_KEY 不合規（至少 %d 位元組且非全空白），composite blob 持久化停用：跨重啟的待決子步驟會 fail-closed",
            MIN_KEY_BYTES,
        )
        return {}

    return {
        "composite_blobs": DbCompositeBlobStore(session_factory, ttl_s=s.composite_blob_ttl_s),
        "composite_blob_max_bytes": s.composite_blob_max_bytes,
    }


def _loop_context_store() -> Any | None:
    """agent loop 的持久 child context store（echo_thought_core SessionStore）；子系統不可用時回 None（啟動器退回行程內暫存）。"""
    try:
        from pathlib import Path

        from uep.echo.loop_store_wiring import SessionLoopContextStore

        base = get_settings().agent_loop_dir or Path.home() / ".uep" / "agent_loop"
        return SessionLoopContextStore(base)
    except Exception:
        logger.warning("持久 loop context store 不可用，改用行程內暫存（park 後跨行程不能 resume）", exc_info=True)
        return None


def assemble_core(
    session_factory: sessionmaker[Session],
    *,
    bus: EventBus | None,
    publisher: EventPublisher | None,
    control: ControlChannel | None,
    adapter: OutputAdapter | None,
    executors: Mapping[str, ToolExecutor] | None,
    tool_resolver: ToolSpecResolver | None,
    turn_generation_source: TurnGenerationSource | None,
    presence_audience: AudienceProvider | None,
    epoch_store: HostEpochStore | None = None,
    persona_id: str = "uep",
    clock: Callable[[], datetime] | None = None,
    stop_token: SecretStr | None = None,
    allow_gate_bypass: bool = False,
    relay_starters: Sequence[Callable[[], None]] = (),
    stop_api_port: int | None = None,
    endpoint_socket_port: int | None = None,
    endpoint_secrets: Mapping[str, bytes] | None = None,
    authorized_proposers: Mapping[Axis, frozenset[str]] | None = None,
    maintenance_interval: float | None = DEFAULT_MAINTENANCE_INTERVAL,
    strict: bool = False,
    memory: MemoryHost | None = None,
    echo: EchoPipelineHost | None = None,
    internal_executors: Collection[str] = (),
    tool_path: str | None = None,
    model_port: Any | None = None,
    loop_entry: Callable[[Any, Any], object] | None = None,
    agent_loop_entry: bool | None = None,
    tool_testkit: bool | None = None,
    decision_log: bool | None = None,
    supervisor: bool | None = None,
    supervisor_interval: float | None = None,
    platform_shadow: bool | None = None,
    require_source_location: bool | None = None,
    input_locations: Collection[str] = ("desktop",),
    module_coordinator: bool | None = None,
    tool_module_events: bool | None = None,
    module_registrations: Sequence[tuple[Any, Any]] = (),
    module_sink: Callable[[str, Mapping[str, Any]], None] | None = None,
    module_vram_probe: VramProbe | None = None,
    allow_console_platform: bool = False,
) -> CoreAssembly:
    """組裝 Core。缺必填前提即 ``AssemblyError``；不啟動任何東西（``start`` 才啟動）。

    ``stop_api_port`` 為 None 時不建立本機 HTTP API；給 0 由系統配置埠。
    ``internal_executors`` 為明示不由任何註冊表項目引用的內部 executor（TG-5 豁免）。
    ``tool_path``（預設取設定 ``UEP_TOOL_PATH``）為 ``agent`` 時：需要 ``model_port``、``echo``（其 think 為 ``AgentThinkStage``
    且使用 ``ModelPortBackend``）與 MemoryPort；``MemoryToolExecutor`` 以 ``MEMORY_EXECUTOR`` 為鍵併入 ``executors``
    （TG-5 對併入後的映射做雙向檢查），並把回合工具 Port 綁到 think stage。任何前提不成立即 ``AssemblyError``，
    不會退回 legacy。``legacy`` 時組裝結果與先前完全相同。
    ``loop_entry``（``route=agent_loop`` -> ``LoopTrigger`` 的入口，見 ``uep.echo.agent_wiring.LoopEntry``）只在
    ``tool_path == "agent"`` 且有 ``echo`` 時可給，綁到 ``echo.orchestrator``；legacy 下給了即 ``AssemblyError``（不可達）。
    ``agent_loop_entry``：None 依 ``UEP_AGENT_LOOP_ENTRY``（預設關閉）。為 True 且 ``tool_path == "agent"``、有 ``echo``、
    未另給 ``loop_entry`` 時，組裝 production 的 ``LoopEntry``（``uep.echo.agent_wiring.build_loop_entry``）：``route=agent_loop`` 的
    回合由 ``AgentLoop`` 處理、交付只經 Output Gate，宿主不再為該回合啟動 direct_chat 回覆。legacy 下開啟即 ``AssemblyError``。
    ``tool_testkit``：None 依 ``UEP_TOOL_TESTKIT``（預設關閉）。為 True 時（只在 ``tool_path == "agent"`` 有效，legacy 下即
    ``AssemblyError``）：``tool_resolver`` 併入測試工具（必須是 ``ToolRegistry``）、executors 併入 ``TESTKIT_EXECUTOR``
    （與記憶工具共用回合暫存），``/loop`` 的 capability_tags 於決策層收窄後併入測試標籤。關閉時以上皆不存在。
    ``decision_log``：None 依 ``UEP_DECISION_LOG``（預設啟用）；False 停用；True 啟用（覆寫 env 的停用）。僅在有 ``echo``
    且啟用時，用本函式的 ``session_factory`` 建 DB sink 與寫入器並掛到 ``echo.decision_hub``（連同政策發布記錄）；
    於所有驗證之後才建立，任何 ``AssemblyError`` 都不會留下寫入器，關閉由 ``CoreAssembly.shutdown`` 負責。
    ``supervisor``：None 依 ``UEP_SUPERVISOR_ENABLED``（預設啟用）；False 不建立。建立時以 ``ActionCanceller.prepare`` 作為
    ``cancel_actions``；間隔、``shutdown_grace``、``deadline_grace``、cancel 逾時取自設定（``UEP_SUPERVISOR_*``）。
    ``supervisor_interval`` 為背景 tick 間隔秒數，與 ``maintenance_interval`` 各自獨立：None 取 ``UEP_SUPERVISOR_INTERVAL_S``，
    0 不建立迴圈（由外部排程）；關機暫停則不論是否有迴圈都會執行。孤兒掃描的 ``older_than`` 取 ``UEP_SUPERVISOR_ORPHAN_OLDER_THAN_S``，
    必須大於 ``TURN_MAX_DURATION``，否則 ``AssemblyError``。
    ``require_source_location``（None 依 ``UEP_REQUIRE_SOURCE_LOCATION``，預設 false）：缺 source_location 的輸入即否決；
    ``input_locations`` 為已註冊輸入 adapter 的場域，含非本機（desktop／console）而開關未開即 ``AssemblyError``（V18）。
    ``module_coordinator``（None 依 ``UEP_MODULE_COORDINATOR``，預設關閉）與 ``tool_module_events``（None 依
    ``UEP_TOOL_MODULE_EVENTS``，預設關閉）：兩旗標分別回滾（R10）。subscriber 開而 producer 關 => ``AssemblyError``；
    producer 開、subscriber 關 => 觀測模式（coordinator 只觀測、發事件，不 provisioning／驅逐，註冊表也不訂閱）；兩者都開 =>
    coordinator 管理模式，known modules 取自 coordinator、設定閘關閉後 probe 成功不得覆蓋，啟動快照先於 Core READY。
    ``module_registrations`` 是 ``(ModuleDescriptor, ModuleHandle)`` 序列（真實 handle 屬 PR-4，目前由呼叫端注入）：
    沒有登記的模組不在 known modules，依賴它的工具恆為 unavailable（fail-closed）。``module_sink`` 預設經 ``publisher`` 落
    journal 並同步分發到 Bus，測試可注入。兩旗標都關閉時以上皆不存在，行為與先前完全相同。
    ``strict`` 為 True 時，缺少（或過短的）``UEP_PAYLOAD_HASH_KEY`` 直接拒絕組裝；預設只告警。
    ``maintenance_interval`` 為維護迴圈的間隔秒數，None 則不建立迴圈（測試或由外部排程時）。
    ``endpoint_socket_port`` 為 None 時不起端點 socket；給值時，``endpoint_secrets``（預設讀
    ``UEP_ENDPOINT_SECRETS``）為空仍不起 socket（沒有可驗證的端點身分就不開口）。
    """
    _probe_payload_hash_key(strict)
    require_source = get_settings().require_source_location if require_source_location is None else require_source_location
    check_source_location_policy(input_locations, require_source)
    validate_route_capabilities()  # 表內有未知標籤 = 啟動失敗（DL-4 PR3）
    missing = [
        name for name, value in (
            ("bus", bus), ("publisher", publisher), ("control", control), ("adapter", adapter),
            ("executors", executors), ("tool_resolver", tool_resolver),
        ) if value is None
    ]
    if missing:
        raise AssemblyError(f"缺少必填元件：{', '.join(missing)}")
    for name, value in (
        ("turn_generation_source", turn_generation_source), ("presence_audience", presence_audience),
    ):
        if value is None:
            raise AssemblyError(f"Output Gate 必填：缺少 {name}（R1）；不可組出沒有 Gate 的 DeliveryManager")
        if value is ALLOW_ALL and not allow_gate_bypass:
            raise AssemblyError(f"Output Gate 的 {name} 為 ALLOW_ALL：須明確傳 allow_gate_bypass=True 才可組裝")
    assert bus is not None and publisher is not None and control is not None
    if memory is not None and memory.publisher is not publisher:
        raise AssemblyError("MemoryHost 必須與組裝層使用同一個 publisher（同一個 Bus）：否則記憶事件不會進同一條 journal／Bus")
    if echo is not None:
        if echo.publisher is not publisher:
            raise AssemblyError("EchoPipelineHost 必須與組裝層使用同一個 publisher（同一個 Bus）")
        if adapter is not echo.adapter:
            raise AssemblyError("EchoPipelineHost 的端點必須就是 DeliveryManager 的 adapter（echo.adapter）")
        if turn_generation_source != echo.generation_source:
            raise AssemblyError(
                "Output Gate 的 turn_generation_source 必須是 echo.generation_source，"
                "否則舊 generation 的輸出不會被 Gate 拒絕（V12）"
            )
    assert adapter is not None and executors is not None and tool_resolver is not None
    agent_cache = None
    loop_launcher = None
    tool_path = tool_path or get_settings().tool_path  # 工具路徑只在這裡決定一次；Host 與 think stage 必須與之一致
    _check_tool_path_consistency(tool_path, echo)
    if loop_entry is not None and (tool_path != "agent" or echo is None):
        raise AssemblyError("loop_entry 只能在 UEP_TOOL_PATH=agent 且有 echo 時使用（legacy 下 agent_loop 不可達）")
    loop_entry_on = get_settings().agent_loop_entry if agent_loop_entry is None else agent_loop_entry
    if loop_entry_on and loop_entry is None and (tool_path != "agent" or echo is None):
        raise AssemblyError("UEP_AGENT_LOOP_ENTRY 只能在 UEP_TOOL_PATH=agent 且有 echo 時啟用（legacy 下 agent_loop 不可達）")
    coordinator_on = get_settings().module_coordinator if module_coordinator is None else module_coordinator
    events_on = get_settings().tool_module_events if tool_module_events is None else tool_module_events
    check_module_events_policy(events_on, coordinator_on)
    # 管理模式 = producer 與 subscriber 都開（且為 agent 路徑，註冊表才存在）；只開 producer = 觀測模式
    coordinator_drives_registry = coordinator_on and events_on and tool_path == "agent"
    coordinator_obj: ModuleCoordinator | None = None
    if module_vram_probe is not None and not coordinator_on:
        raise AssemblyError("給了 module_vram_probe 但 UEP_MODULE_COORDINATOR 未開")
    if coordinator_on:
        check_m_group_pinned(module_registrations)
        coordinator_obj = ModuleCoordinator(
            module_sink if module_sink is not None else bus_sink(publisher, persona_id=persona_id),
            epoch_source=lambda: presence.core_epoch,  # presence.start() 之後才讀得到（coordinator.start() 內才呼叫）
            mode="manage" if coordinator_drives_registry else "observe",
            probe_interval_s=get_settings().module_probe_interval_s,  # 預設 0 = 不定期探活（現況）
            budget=build_module_budget(get_settings(), module_vram_probe),  # PR-3：未設 headroom => None（不啟用預算管理）
        )
        for descriptor, handle in module_registrations:
            try:
                coordinator_obj.register(descriptor, handle)
            except ValueError as exc:  # ModuleRegistryError 是 ValueError；登記錯誤統一成組裝失敗
                raise AssemblyError(f"module_registrations 登記失敗：{exc}") from exc
    elif module_registrations:
        raise AssemblyError("給了 module_registrations 但 UEP_MODULE_COORDINATOR 未開")
    gate_coordinator = coordinator_obj  # 觀測模式也套設定閘（D108 R3：未設定的服務 probe 成功也不發 ready）
    testkit_on = get_settings().tool_testkit if tool_testkit is None else tool_testkit
    if testkit_on and tool_path != "agent":
        raise AssemblyError("UEP_TOOL_TESTKIT 只能在 UEP_TOOL_PATH=agent 下啟用（測試工具只經 agent 路徑曝光）")
    if tool_path == "agent":
        memory_executor, agent_cache = _prepare_agent_tools(echo, model_port)
        executors = {**executors, MEMORY_EXECUTOR: memory_executor}
        if testkit_on:
            from uep.tools.catalog.testkit import TESTKIT_EXECUTOR, with_testkit
            from uep.tools.executors.testkit import TestkitToolExecutor

            if not isinstance(tool_resolver, ToolRegistry):
                raise AssemblyError("UEP_TOOL_TESTKIT 需要 ToolRegistry 型的 tool_resolver（才能併入測試工具）")
            tool_resolver = with_testkit(tool_resolver)
            executors = {**executors, TESTKIT_EXECUTOR: TestkitToolExecutor(cache=agent_cache)}
        from uep.tools.catalog.chatroom import CHATROOM_EXECUTOR, with_chatroom
        from uep.tools.executors.chatroom import (
            ChatroomToolExecutor,
            chatroom_config_from_settings,
        )
        from uep.tools.registry import ToolRegistry as _ToolRegistry

        if isinstance(tool_resolver, _ToolRegistry):
            # Chatroom 原生工具：一律註冊；UEP_CHATROOM_* 不齊時 readiness 恆為 unavailable（不投出），組裝不失敗
            chatroom_config = chatroom_config_from_settings(get_settings())
            tool_resolver = with_chatroom(tool_resolver, configured=chatroom_config is not None, coordinator=gate_coordinator,
                drive_registry=coordinator_drives_registry,
            )
            executors = {**executors, CHATROOM_EXECUTOR: ChatroomToolExecutor(chatroom_config, cache=agent_cache)}
        from uep.tools.registry import ToolRegistry as _Registry

        if isinstance(tool_resolver, _Registry):
            # web.search／web.fetch／image.read：條目一律註冊；未設定的工具 readiness 為 unavailable（不投出），組裝不失敗
            from uep.tools.web_image import with_web_image

            tool_resolver, web_executors = with_web_image(
                tool_resolver, cache=agent_cache, coordinator=gate_coordinator, drive_registry=coordinator_drives_registry,
            )
            executors = {**executors, **web_executors}
        from uep.tools.registry import ToolRegistry as _LoreRegistryType

        if isinstance(tool_resolver, _LoreRegistryType):
            # Lore Vault 原生工具：一律註冊；未設定時 readiness 為 unavailable（不投出、組裝不失敗）
            from uep.tools.catalog.lore import LORE_EXECUTOR, with_lore
            from uep.tools.executors.lore import LoreConfig, LoreToolExecutor

            lore_config = LoreConfig.from_settings(get_settings())
            tool_resolver = with_lore(
                tool_resolver, configured=lore_config.configured, delete_enabled=lore_config.delete_enabled,
                coordinator=gate_coordinator, drive_registry=coordinator_drives_registry,
            )
            executors = {**executors, LORE_EXECUTOR: LoreToolExecutor(lore_config, cache=agent_cache, allow_write=True)}
        if callable(getattr(tool_resolver, "entries", None)):
            from uep.models.tool_names import ToolNameError, wire_map

            try:  # provider 名稱轉譯（. -> __）的碰撞／歧義在組裝時就失敗，不等到第一次呼叫模型
                wire_map(tool_resolver.entries())
            except ToolNameError as exc:
                raise AssemblyError(f"工具名稱無法安全轉譯給 provider：{exc}") from exc
    if coordinator_obj is not None and callable(getattr(tool_resolver, "entries", None)):
        check_module_coverage(
            tool_resolver.entries(), coordinator_obj.known_ids(), module_registrations, enforce=coordinator_drives_registry,
        )
    _check_registry_executors(tool_resolver, executors, internal_executors)
    assert turn_generation_source is not None and presence_audience is not None

    local_control = control
    fanout = _FanoutControl(local_control)
    control = fanout
    clock_kw = {"clock": clock} if clock is not None else {}
    flag_blocker = make_flag_blocker(persona_id)
    presence = PresenceManager(
        session_factory, persona_id=persona_id, epoch_store=epoch_store, publisher=publisher, control=control,
        acquire_guard=flag_blocker, allow_console=allow_console_platform, **clock_kw,
    )
    # 出站端點能力宣告（IC-3、E3）：持有 Lease 的端點身分即互動端點（IC-4），所以「可 acquire 的端點」＝已宣告 interaction。
    # 來源只有兩處：ES 內建端點與 socket 端點的 secrets 鍵；其餘 endpoint_id 查無宣告（shadow 下計數）。
    # 表在端點註冊時才填入（下方 echo／endpoint socket 區段），Gate 以 ``declared_capabilities.get`` 即時查詢。
    declared_capabilities: dict[str, PlatformCapabilities] = {}
    gate = OutputGate(
        session_factory, presence, turn_generation_source=turn_generation_source,
        presence_audience=presence_audience, publisher=publisher, capability_resolver=declared_capabilities.get,
        require_interaction=get_settings().gate_require_interaction, **clock_kw,
    )
    delivery = DeliveryManager(session_factory, adapter, gate=gate)
    state = StateCoordinator(
        session_factory, publisher, persona_id=persona_id, global_stop_check=flag_blocker,
        global_stop_locked_check=make_flag_blocker(persona_id, lock=True),
        authorized_proposers=DEFAULT_PROPOSERS if authorized_proposers is None else authorized_proposers,
        **clock_kw,
    )

    def transition_guard(request: TransitionRequest) -> None:
        if request.to_status is RunStatus.RUNNING:
            reason = state.task_entry_blocked()
            if reason:
                raise IllegalTransition(f"CC-7 拒絕 {request.entity} 轉入 RUNNING：{reason}")

    def presence_check(persona: str) -> bool:
        return persona == persona_id and presence.current().status is PresenceStatus.ACTIVE

    tasks = TaskManager(
        session_factory, publisher, executors, tool_resolver=tool_resolver, transition_guard=transition_guard,
        presence_check=presence_check, action_guard=state.action_entry_blocked, **clock_kw,
    )
    _assert_wired(tasks, delivery)
    agent_registry = tool_resolver if isinstance(tool_resolver, ToolRegistry) else memory_registry()
    if gate_coordinator is not None and coordinator_drives_registry:
        seed_fail_closed(agent_registry, gate_coordinator)  # 全部 with_* 併入之後、在最終註冊表上只播一次 fail-closed 底
    if agent_cache is not None:
        assert echo is not None
        # 工具投影的平台取自 Host 本機的 Lease 與 location（授權邊界），不取 Presence peek
        platform_source = getattr(echo, "projection_platform", None)
        attach_agent_tools(echo.think, tasks, agent_cache, agent_registry, platform_source=platform_source)
        bind_registry = getattr(echo, "bind_tool_registry", None)
        if callable(bind_registry):
            bind_registry(agent_registry)  # direct_chat 投影與 TaskManager／Gateway／loop 看同一份註冊表
            laya_orchestrator = getattr(echo, "orchestrator", None)
            if laya_orchestrator is not None:
                # UEP_LAYA_CAPABILITIES=snapshot 時 Laya 的 available_capabilities 每次推論讀工具層快照；預設 static 維持靜態清單
                bind_laya_capabilities(
                    laya_orchestrator, agent_registry, resolve_capabilities_mode(get_settings().laya_capabilities),
                )
        if loop_entry is None and loop_entry_on:
            # loop、TaskManager、Gateway、投影共用組裝層組出的同一份註冊表（含測試工具與原生工具）
            loop_registry = agent_registry
            _check_loop_one_shot_executors(loop_registry, executors)
            from uep.tools.catalog.testkit import TESTKIT_TAGS

            extra_tags = TESTKIT_TAGS if testkit_on else frozenset()
            loop_store = _loop_context_store()
            loop_entry, loop_launcher = build_loop_entry(
                tool_path=tool_path, tasks=tasks, delivery=delivery, orchestrator=echo.orchestrator,
                registry=loop_registry, model_port=model_port, cache=agent_cache,
                text_store=getattr(echo, "loop_text", None), persona_id=persona_id, executors=executors,
                platform_source=platform_source, store=loop_store, memory_store=getattr(echo, "store_turn_memory", None),
                on_resumed=getattr(echo, "accept_resumed_result", None),
                resume_cancel_check=getattr(echo, "resume_should_cancel", None),
                extra_capability_tags=extra_tags, **_loop_resilience_kwargs(), **_composite_blob_kwargs(session_factory),
            )
        if loop_entry is not None:
            echo.orchestrator.bind_loop_entry(loop_entry)
    if echo is not None:
        # ES 端點以行程內專用的隨機 secret 經 EndpointService 走既有 acquire／renew 流程（沿用 P-21／P-22，
        # 不離開 loopback）；secret 不落地、不離開行程
        secret = token_bytes(32)
        service = EndpointService(presence, build_request_verifier({echo.endpoint_id: secret}), **clock_kw)
        _declare_interaction(
            declared_capabilities, echo.endpoint_id,
            platform_from_endpoint_location(getattr(echo, "_location", None)) or _UNKNOWN_PLATFORM,
            frozenset({"text", "audio_in", "audio_out"}),
        )
        core_api = RpcCoreApi(
            echo.endpoint_id, secret, lambda wire: service.handle(SignedRequest.from_wire(wire))
        )
        echo.bind(EchoServices(
            core_api=core_api, state=state, control=control, session_factory=session_factory,
            delivery=delivery, gate=gate,
            presence_audience=None if presence_audience is ALLOW_ALL else presence_audience,
        ))
    if echo is not None and hasattr(echo, "set_require_source_location"):
        echo.set_require_source_location(require_source)
    # 平台脈絡 provider：shadow 與 loop.consult 注入共用同一個（version 計數一致，decision_log 才對得上 shadow 記錄）；
    # executors 先用粗粒度來源（F-1 退路），工具層 ToolRegistry.executors_for 接上後只換這個來源
    platform_provider = PlatformContextProvider(presence, CoarseExecutorSource())
    shadow_on = get_settings().platform_shadow if platform_shadow is None else platform_shadow
    if shadow_on and echo is not None and hasattr(echo, "bind_platform_shadow"):
        # 平台脈絡 shadow（UEP_PLATFORM_SHADOW，預設關）：只記錄與比對，不改任何消費點的輸入；Presence 走唯讀 peek
        # production 的投影平台 = Host 本機 location（只有 Lease ACTIVE 才開 turn，turn 內即此值），與 shadow 比對才有意義
        host_platform = platform_from_presence_location(getattr(echo, "location", None))
        production = ProductionInputs(
            projection_platform=host_platform,
            presence_active_platforms=frozenset({host_platform}) if host_platform is not None else frozenset(),
        )
        echo.bind_platform_shadow(PlatformShadow(platform_provider, echo.metrics, production=production))
    consult_hub = getattr(echo, "decision_hub", None)
    if consult_hub is not None and consult_hub.engine(KIND_LOOP_CONSULT) is None:
        # 平台宣告注入第 5 步（決策層側）：loop.consult 的 current_platform／available_executors 由系統注入，
        # 模型自帶值一律覆蓋（Q26）；快照版本隨 facts 寫入決策日誌（平台快照版本欄）。
        # 工具層的 ConsultToolExecutor 直接用這個 hub，不要再 register_loop_consult。
        register_loop_consult(consult_hub, platform_facts=lambda: placement_facts(platform_provider.snapshot()))
    stop = GlobalStop(session_factory, presence, control, publisher=publisher, token=stop_token, **clock_kw)
    bridge = PresenceTaskBridge(bus, tasks, lambda: presence.current().status, persona_id=persona_id)

    def reproject_presence() -> bool:
        """以 Presence 權威狀態補投影 State 的 Presence 軸（Bus 丟事件時的補償；source_version 去重）。"""
        view = presence.current()
        return state.project(Axis.PRESENCE, view.status.value, source_version=view.row_version)

    maintenance = (
        PresenceMaintenance(
            presence.expire_due, bridge.reconcile, project=reproject_presence, interval=maintenance_interval
        )
        if maintenance_interval is not None else None
    )

    supervisor_obj: Supervisor | None = None
    supervisor_loop: SupervisorLoop | None = None
    settings = get_settings()
    if settings.supervisor_enabled if supervisor is None else supervisor:
        orphan_older_than = timedelta(seconds=settings.supervisor_orphan_older_than_s)
        if orphan_older_than <= TURN_MAX_DURATION:
            raise AssemblyError(
                f"UEP_SUPERVISOR_ORPHAN_OLDER_THAN_S 必須大於 TURN_MAX_DURATION（{TURN_MAX_DURATION}），否則會誤收仍在進行的回合"
            )
        interval = settings.supervisor_interval_s if supervisor_interval is None else supervisor_interval
        if interval < 0:
            raise AssemblyError("supervisor_interval 不可為負數")
        canceller = ActionCanceller(tasks, executors, tool_resolver, timeout_s=settings.supervisor_cancel_timeout_s)
        supervisor_obj = Supervisor(
            tasks, deadline_grace=timedelta(seconds=settings.supervisor_deadline_grace_s),
            older_than=orphan_older_than,
            shutdown_grace=timedelta(seconds=settings.supervisor_shutdown_grace_s),
            cancel_actions=canceller.prepare, **clock_kw,
        )
        if interval > 0:
            supervisor_loop = SupervisorLoop(supervisor_obj, interval=interval)

    for event_type in _PRESENCE_PROJECTED + ("core.component_health_changed",):
        bus.subscribe(event_type, state.project_event)
    module_events_unsubscribe: Callable[[], None] | None = None
    if tool_path == "agent" and events_on:
        # 把 agent 路徑共用的註冊表接到 Bus 的 module.* 就緒事件；先訂閱，coordinator.start() 的快照才收得到
        module_events_unsubscribe = subscribe_module_events(bus, agent_registry)
    api = LocalStopServer(stop, port=stop_api_port) if stop_api_port is not None else None
    endpoint_server: SocketControlServer | None = None
    try:
        if endpoint_socket_port is not None:
            secrets = endpoint_secrets if endpoint_secrets is not None else load_secrets_from_env()
            if secrets:
                for socket_endpoint_id in secrets:  # 場域在 acquire 時才決定，宣告階段只能記 unknown
                    _declare_interaction(declared_capabilities, socket_endpoint_id, _UNKNOWN_PLATFORM, frozenset({"text"}))
                service = EndpointService(presence, build_request_verifier(secrets), **clock_kw)
                endpoint_server = SocketControlServer(service, port=endpoint_socket_port)
                fanout.remote = endpoint_server
    except BaseException:
        if api is not None:
            api.stop()  # 已 bind 的 stop API 不能留著
        raise
    dream_probe: StateBusForegroundProbe | None = None
    if memory is not None and memory.dream_enabled and memory.dream_probe is None:
        # 沒有 Probe 的迴圈永遠是 runtime_not_ready（planner 不會被呼叫）；Probe 需要 state／bus／GlobalStop，只能在這裡建
        dream_probe = StateBusForegroundProbe(state, bus, global_stop_fn=stop.is_engaged)
        memory.attach_dream_probe(dream_probe)
    writer: DecisionLogWriter | None = None
    proactive_shadow: ProactiveShadow | None = None
    if echo is not None and _decision_log_enabled(decision_log):
        # DL-6b：proactive.initiate 只在日誌啟用時登記（停用時零成本），須早於 attach 讓預設名單串到此 kind；
        # shadow-only：沒有 producer、沒有計時器，不建立 Run、不投遞；capability_lookup 待工具層 S-H 標籤表接上（暫回 None）
        proactive_shadow = attach_proactive_shadow(
            echo.decision_hub, presence=presence, state=state, stop=stop, audience=presence_audience, clock=clock,
        )
        writer = DecisionLogWriter(DbSink(session_factory))
        writer.attach(echo.decision_hub)
        # 政策發布記錄只在日誌啟用時登記（停用時零成本）；須在 Host 開始發布前（組裝階段）完成
        policy = getattr(echo, "policy", None)
        if policy is not None:
            register_policy_publish(echo.decision_hub, policy, run_id=getattr(echo, "run_id", None))
        if loop_launcher is not None:
            # 預算門檻事件（budget_warn／budget_notice／task_budget_exceeded）寫進決策日誌
            loop_launcher.set_budget_recorder(register_budget_events(echo.decision_hub))
    return CoreAssembly(
        persona_id=persona_id, bus=bus, publisher=publisher, control=control, presence=presence, gate=gate,
        delivery=delivery, tasks=tasks, state=state, stop=stop, bridge=bridge, maintenance=maintenance,
        stop_api=api,
        endpoint_server=endpoint_server, relay_starters=tuple(relay_starters), memory=memory, echo=echo,
        decision_log=writer, supervisor=supervisor_obj, supervisor_loop=supervisor_loop, loop_launcher=loop_launcher,
        module_events_unsubscribe=module_events_unsubscribe, module_coordinator=coordinator_obj, dream_probe=dream_probe,
        proactive_shadow=proactive_shadow,
    )
