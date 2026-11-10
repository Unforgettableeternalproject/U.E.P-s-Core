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
   （D67；``UEP_SUPERVISOR_ENABLED`` 預設開）。``shutdown`` 在關閉入口之前同步 ``supervisor.shutdown()``（SV-1）。
4. 其後才啟動 relay（``relay_starters``，例如 outbox relay）與全域停止本機 API。
5. Runtime 軸 READY。

``DeliveryManager`` 與 relay 在步驟 1 完成前不會被啟動；即使有人提前呼叫 delivery，Gate 也因 epoch
未就緒而拒絕（REJECT_NO_PRESENCE）。
"""
from __future__ import annotations

import logging
import os
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
from uep.contracts.presence import PresenceStatus
from uep.control.channel import (
    ControlChannel,
    ControlHandler,
    ControlSendResult,
    ControlSubscription,
)
from uep.control.socket_transport import SocketControlServer
from uep.decision.capability import ROUTE_DEFAULT_CAPABILITIES
from uep.decision.db import DbSink
from uep.decision.log import DecisionLogWriter
from uep.decision.policy_publish import register_policy_publish
from uep.echo.agent_wiring import attach_agent_tools
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
from uep.tasks.manager import IllegalTransition, TaskManager, TransitionRequest
from uep.tasks.presence_bridge import (
    DEFAULT_MAINTENANCE_INTERVAL,
    PresenceMaintenance,
    PresenceTaskBridge,
)
from uep.tasks.states import RunStatus
from uep.tasks.supervisor import Supervisor, SupervisorLoop
from uep.tools.cancel import ActionCanceller
from uep.tools.catalog.memory import MEMORY_EXECUTOR

if TYPE_CHECKING:  # 組裝層不得硬依賴 uep.stream：只有 uep/echo/host 會 import 它
    from uep.echo.host import EchoPipelineHost

logger = logging.getLogger("uep.assembly")

DECISION_LOG_CLOSE_TIMEOUT = 5.0  # 關閉時等待決策日誌排空的上限（秒）；sink 卡住時 shutdown 不會被拖住


def _decision_log_enabled(flag: bool | None) -> bool:
    """決策日誌是否啟用：明示 True／False 優先；None 時依 ``UEP_DECISION_LOG``（0／off／false／no 停用，預設啟用）。"""
    if flag is not None:
        return flag
    return os.environ.get("UEP_DECISION_LOG", "").strip().lower() not in {"0", "off", "false", "no"}


# Presence Manager 會發出、且帶 status 的事件（Presence 軸投影用）
_PRESENCE_PROJECTED = (
    "presence.granted", "presence.activated", "presence.revoked", "presence.recovering", "presence.recovered",
)


# 預設的轉換請求提議者（S-1：fail-closed，只有名單內的已認證提議者能提請求）
DEFAULT_PROPOSERS: dict[Axis, frozenset[str]] = {
    Axis.INTERACTION: frozenset({
        "core.nlp", "core.proactive", "core.workflow", "core.turn", "core.decision",
    }),
    Axis.BEHAVIOR: frozenset({"core.policy", "core.proactive"}),
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
    supervisor_loop: SupervisorLoop | None = None  # 僅在維護迴圈啟用（maintenance_interval 不為 None）時建立

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
        """關閉入口、訂閱與 Bus（不寫 DB）；可重複呼叫。

        停止序反向於啟動：ES Host 最先停（停 feed、cancel_all、等 commit 與事件寫完；它的 sink 與事件都還要
        經 Bus／Gateway，也還要用 memory 做 store），其次記憶 Host（它的 sink 還要經 Bus 發布），之後才關
        endpoint socket 等入口。每一步獨立容錯。"""
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
        if self.decision_log is not None:
            # 最後才關：echo／memory（含其擁有的 dream）停止前仍可能送出決策；排空佇列後才釋放連線
            try:
                self.decision_log.close(timeout=DECISION_LOG_CLOSE_TIMEOUT)
            except Exception:
                logger.exception("決策日誌寫入器關閉失敗")
            self.start_log.append("decision_log.close")
        if self.stop_api is not None:
            self.stop_api.stop()
        if self.endpoint_server is not None:
            self.endpoint_server.close()
        if self.maintenance is not None:
            self.maintenance.stop()
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
        if self.supervisor is not None and self.started:
            # SV-1：在 bus.stop() 之前同步暫停所有未終態 Task／Run（PAUSED(core_shutdown)），不依賴事件被訂閱
            try:
                self.supervisor.shutdown()
            except Exception:
                logger.exception("Supervisor 關機暫停失敗")
            self.start_log.append("supervisor.shutdown")
        self._close_inputs()
        self.started = False


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
    """D69／RA-5：宣告 interruptible／checkpoint_only 或 cancellable 的工具，其 executor 必須實作 ``cancel``；fail-closed。
    executor 不在映射內的情形由 TG-5 另行回報，這裡略過。"""
    entries = resolver.entries() if callable(getattr(resolver, "entries", None)) else {}
    bad: list[str] = []
    for name, spec in specs.items():
        executor = executors.get(spec.executor)
        if executor is None or supports_cancel(executor):
            continue
        entry = entries.get(name)
        if spec.interruptible != "non_interruptible" or (entry is not None and entry.cancellable):
            bad.append(name)
    if bad:
        raise AssemblyError(f"工具宣告可中斷或 cancellable，但其 executor 未實作 cancel：{', '.join(sorted(bad))}（D69、RA-5）")


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
    decision_log: bool | None = None,
    supervisor: bool | None = None,
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
    ``decision_log``：None 依 ``UEP_DECISION_LOG``（預設啟用）；False 停用；True 啟用（覆寫 env 的停用）。僅在有 ``echo``
    且啟用時，用本函式的 ``session_factory`` 建 DB sink 與寫入器並掛到 ``echo.decision_hub``（連同政策發布記錄）；
    於所有驗證之後才建立，任何 ``AssemblyError`` 都不會留下寫入器，關閉由 ``CoreAssembly.shutdown`` 負責。
    ``supervisor``：None 依 ``UEP_SUPERVISOR_ENABLED``（預設啟用）；False 不建立。建立時以 ``ActionCanceller.prepare`` 作為
    ``cancel_actions``；間隔、``shutdown_grace``、``deadline_grace``、cancel 逾時取自設定（``UEP_SUPERVISOR_*``）。
    背景 tick 迴圈與維護迴圈同條件（``maintenance_interval`` 不為 None）；關機暫停則不論是否有迴圈都會執行。
    ``strict`` 為 True 時，缺少（或過短的）``UEP_PAYLOAD_HASH_KEY`` 直接拒絕組裝；預設只告警。
    ``maintenance_interval`` 為維護迴圈的間隔秒數，None 則不建立迴圈（測試或由外部排程時）。
    ``endpoint_socket_port`` 為 None 時不起端點 socket；給值時，``endpoint_secrets``（預設讀
    ``UEP_ENDPOINT_SECRETS``）為空仍不起 socket（沒有可驗證的端點身分就不開口）。
    """
    _probe_payload_hash_key(strict)
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
    tool_path = tool_path or get_settings().tool_path  # 工具路徑只在這裡決定一次；Host 與 think stage 必須與之一致
    _check_tool_path_consistency(tool_path, echo)
    if loop_entry is not None and (tool_path != "agent" or echo is None):
        raise AssemblyError("loop_entry 只能在 UEP_TOOL_PATH=agent 且有 echo 時使用（legacy 下 agent_loop 不可達）")
    if tool_path == "agent":
        memory_executor, agent_cache = _prepare_agent_tools(echo, model_port)
        executors = {**executors, MEMORY_EXECUTOR: memory_executor}
    _check_registry_executors(tool_resolver, executors, internal_executors)
    assert turn_generation_source is not None and presence_audience is not None

    local_control = control
    fanout = _FanoutControl(local_control)
    control = fanout
    clock_kw = {"clock": clock} if clock is not None else {}
    flag_blocker = make_flag_blocker(persona_id)
    presence = PresenceManager(
        session_factory, persona_id=persona_id, epoch_store=epoch_store, publisher=publisher, control=control,
        acquire_guard=flag_blocker, **clock_kw,
    )
    gate = OutputGate(
        session_factory, presence, turn_generation_source=turn_generation_source,
        presence_audience=presence_audience, publisher=publisher, **clock_kw,
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
    if agent_cache is not None:
        assert echo is not None
        attach_agent_tools(echo.think, tasks, agent_cache)
        if loop_entry is not None:
            echo.orchestrator.bind_loop_entry(loop_entry)
    if echo is not None:
        # ES 端點以行程內專用的隨機 secret 經 EndpointService 走既有 acquire／renew 流程（沿用 P-21／P-22，
        # 不離開 loopback）；secret 不落地、不離開行程
        secret = token_bytes(32)
        service = EndpointService(presence, build_request_verifier({echo.endpoint_id: secret}), **clock_kw)
        core_api = RpcCoreApi(
            echo.endpoint_id, secret, lambda wire: service.handle(SignedRequest.from_wire(wire))
        )
        echo.bind(EchoServices(
            core_api=core_api, state=state, control=control, session_factory=session_factory,
            delivery=delivery, gate=gate,
            presence_audience=None if presence_audience is ALLOW_ALL else presence_audience,
        ))
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
        canceller = ActionCanceller(tasks, executors, tool_resolver, timeout_s=settings.supervisor_cancel_timeout_s)
        supervisor_obj = Supervisor(
            tasks, deadline_grace=timedelta(seconds=settings.supervisor_deadline_grace_s),
            shutdown_grace=timedelta(seconds=settings.supervisor_shutdown_grace_s),
            cancel_actions=canceller.prepare, **clock_kw,
        )
        if maintenance_interval is not None:
            supervisor_loop = SupervisorLoop(supervisor_obj, interval=settings.supervisor_interval_s)

    for event_type in _PRESENCE_PROJECTED + ("core.component_health_changed",):
        bus.subscribe(event_type, state.project_event)
    api = LocalStopServer(stop, port=stop_api_port) if stop_api_port is not None else None
    endpoint_server: SocketControlServer | None = None
    try:
        if endpoint_socket_port is not None:
            secrets = endpoint_secrets if endpoint_secrets is not None else load_secrets_from_env()
            if secrets:
                service = EndpointService(presence, build_request_verifier(secrets), **clock_kw)
                endpoint_server = SocketControlServer(service, port=endpoint_socket_port)
                fanout.remote = endpoint_server
    except BaseException:
        if api is not None:
            api.stop()  # 已 bind 的 stop API 不能留著
        raise
    writer: DecisionLogWriter | None = None
    if echo is not None and _decision_log_enabled(decision_log):
        writer = DecisionLogWriter(DbSink(session_factory))
        writer.attach(echo.decision_hub)
        # 政策發布記錄只在日誌啟用時登記（停用時零成本）；須在 Host 開始發布前（組裝階段）完成
        policy = getattr(echo, "policy", None)
        if policy is not None:
            register_policy_publish(echo.decision_hub, policy, run_id=getattr(echo, "run_id", None))
    return CoreAssembly(
        persona_id=persona_id, bus=bus, publisher=publisher, control=control, presence=presence, gate=gate,
        delivery=delivery, tasks=tasks, state=state, stop=stop, bridge=bridge, maintenance=maintenance,
        stop_api=api,
        endpoint_server=endpoint_server, relay_starters=tuple(relay_starters), memory=memory, echo=echo,
        decision_log=writer, supervisor=supervisor_obj, supervisor_loop=supervisor_loop,
    )
