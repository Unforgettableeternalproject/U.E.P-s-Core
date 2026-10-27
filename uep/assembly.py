"""Core 組裝層（T-17、R1）：把各 Manager 接成一個 Core，並把安全前提寫成「缺就拒絕組裝」。

必填（缺任何一項即 ``AssemblyError``，不降級成預設放行）：
- Output Gate 的 ``turn_generation_source`` 與 ``presence_audience``；要關閉須顯式 ``ALLOW_ALL`` 並同時
  傳 ``allow_gate_bypass=True``（僅供測試或尚未接線的階段）。組裝層自己建 Gate 並傳給 DeliveryManager，
  不存在「沒有 Gate 的 DeliveryManager」。
- TaskManager 的 ``presence_check``（組裝層以 Presence 權威狀態提供）與 CC-7 ``transition_guard``。
- ``publisher``、``bus``、``control``（全域停止要用控制通道廣播）。

啟動順序（``CoreAssembly.start``，契約 4.1 core_epoch 列）：

1. ``presence.start()``：Host epoch +1、舊 Lease 進 RECOVERING；完成前 Output Gate 一律拒絕。
   它失敗（例如 Host epoch 檔落後 DB）時其後任何元件都不啟動。
2. Bus 啟動、Runtime 軸 STARTING、Presence 初始投影、presence_bound Task 補暫停（reconcile）。
3. 維護迴圈（``maintenance_interval`` 不為 None 時）：定期 ``presence.expire_due()`` 與 ``bridge.reconcile()``，
   讓到期與橋接失敗不依賴外部觸發而補上（T-23）。
4. 其後才啟動 relay（``relay_starters``，例如 outbox relay）與全域停止本機 API。
5. Runtime 軸 READY。

``DeliveryManager`` 與 relay 在步驟 1 完成前不會被啟動；即使有人提前呼叫 delivery，Gate 也因 epoch
未就緒而拒絕（REJECT_NO_PRESENCE）。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from uep.adapters.output import OutputAdapter
from uep.adapters.tool import ToolExecutor
from uep.config import get_settings
from uep.contracts.presence import PresenceStatus
from uep.control.channel import (
    ControlChannel,
    ControlHandler,
    ControlSendResult,
    ControlSubscription,
)
from uep.control.socket_transport import SocketControlServer
from uep.endpoint.auth import (
    DEFAULT_SKEW_SECONDS,
    RequestVerifier,
    load_secrets_from_env,
)
from uep.endpoint.service import EndpointService
from uep.events.bus import EventBus
from uep.events.payload_hash import MIN_KEY_BYTES
from uep.events.publisher import EventPublisher
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

logger = logging.getLogger("uep.assembly")

# Presence Manager 會發出、且帶 status 的事件（Presence 軸投影用）
_PRESENCE_PROJECTED = (
    "presence.granted", "presence.activated", "presence.revoked", "presence.recovering", "presence.recovered",
)


# 預設的轉換請求提議者（S-1：fail-closed，只有名單內的已認證提議者能提請求）
DEFAULT_PROPOSERS: dict[Axis, frozenset[str]] = {
    Axis.INTERACTION: frozenset({"core.nlp", "core.proactive", "core.workflow"}),
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
        """關閉入口、訂閱與 Bus（不寫 DB）；可重複呼叫。"""
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


def build_request_verifier(
    secrets: Mapping[str, bytes], *, clock: Callable[[], float] = time.time
) -> RequestVerifier:
    """端點請求驗證器。啟動下限取 ``clock() - skew``：時鐘落後 δ（< skew）的端點在 Core 重啟後立即的請求
    仍被接受；早於「啟動 - skew」的舊請求被拒。代價：重啟前最後 skew 秒內被截取的請求，nonce 表已清空，
    在窗口內可被重放一次（跨機器 Part H 前須配每則 MAC／通道綁定）。"""
    return RequestVerifier(
        secrets, clock=clock, skew_seconds=DEFAULT_SKEW_SECONDS, startup_grace=DEFAULT_SKEW_SECONDS
    )


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
) -> CoreAssembly:
    """組裝 Core。缺必填前提即 ``AssemblyError``；不啟動任何東西（``start`` 才啟動）。

    ``stop_api_port`` 為 None 時不建立本機 HTTP API；給 0 由系統配置埠。
    ``strict`` 為 True 時，缺少（或過短的）``UEP_PAYLOAD_HASH_KEY`` 直接拒絕組裝；預設只告警。
    ``maintenance_interval`` 為維護迴圈的間隔秒數，None 則不建立迴圈（測試或由外部排程時）。
    ``endpoint_socket_port`` 為 None 時不起端點 socket；給值時，``endpoint_secrets``（預設讀
    ``UEP_ENDPOINT_SECRETS``）為空仍不起 socket（沒有可驗證的端點身分就不開口）。
    """
    _probe_payload_hash_key(strict)
    missing = [
        name for name, value in (
            ("bus", bus), ("publisher", publisher), ("control", control), ("adapter", adapter),
            ("executors", executors),
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
    assert adapter is not None and executors is not None
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
        session_factory, publisher, executors, transition_guard=transition_guard,
        presence_check=presence_check, action_guard=state.action_entry_blocked, **clock_kw,
    )
    _assert_wired(tasks, delivery)
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
    return CoreAssembly(
        persona_id=persona_id, bus=bus, publisher=publisher, control=control, presence=presence, gate=gate,
        delivery=delivery, tasks=tasks, state=state, stop=stop, bridge=bridge, maintenance=maintenance,
        stop_api=api,
        endpoint_server=endpoint_server, relay_starters=tuple(relay_starters),
    )
