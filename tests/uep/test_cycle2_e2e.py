"""週期 2 端到端組裝情境（需 PostgreSQL；未設 UEP_TEST_DATABASE_URL 時 skip）。

用 ``assemble_core`` 組出真實 Core（PresenceManager、OutputGate、DeliveryManager、TaskManager、
StateCoordinator、GlobalStop、stop API、端點 socket），Runtime／Tool executor 用 fake；端點是
``CliEndpoint`` ＋ ``SocketControlClient`` 經真實 loopback socket 連入，停止走真實 HTTP（``python -m uep.stop``
子行程，Bearer token 由環境變數注入）。時間用可注入時鐘；Host epoch 檔、端點密鑰與 stop token 全為測試常數。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from pydantic import SecretStr

from tests.uep.endpoint_support import Mono
from tests.uep.presence_support import FakeClock, intent_for
from uep.assembly import CoreAssembly, assemble_core
from uep.config import get_settings
from uep.contracts.presence import AcquireResult, GateDecision, PresenceStatus
from uep.control.channel import InProcessControlChannel
from uep.control.socket_transport import SocketControlClient
from uep.endpoint.endpoint import CliEndpoint, ReconnectOutcome
from uep.events.bus import EventBus
from uep.events.publisher import EventPublisher
from uep.fakes.tool import FakeToolExecutor
from uep.output.delivery import DeliveryEvidence
from uep.output.delivery import DeliveryStatus as S
from uep.presence.epoch import HostEpochStore
from uep.tasks.manager import ActionBlocked, TaskPolicy
from uep.tasks.states import InterruptPolicy, RunStatus

pytestmark = pytest.mark.usefixtures("session_factory")

REPO_ROOT = Path(__file__).resolve().parents[2]
EP_ID = "ep_cli"
EP_SECRET = "e2e-endpoint-secret-0123456789abcdef"  # 測試常數，非真實密鑰
STOP_TOKEN = "e2e-stop-token-for-tests"
TURN_GEN = 5
BOUND = TaskPolicy(presence_bound=True, interrupt_policy=InterruptPolicy.INTERRUPTIBLE, policy_ref="pol_e2e")
FREE = TaskPolicy(presence_bound=False, interrupt_policy=InterruptPolicy.INTERRUPTIBLE, policy_ref="pol_e2e_free")
WAIT = 5.0


def wait_until(predicate, timeout: float = WAIT) -> bool:
    ticker = threading.Event()
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return True
        ticker.wait(0.02)
    return predicate()


class LateAdapter:
    """Core 先組好、端點後建（要等 socket 埠）：OutputAdapter 轉接，端點建好後才綁定。"""

    lookup_authoritative = False

    def __init__(self) -> None:
        self.target: CliEndpoint | None = None
        self.sends: list[str] = []

    def send(self, intent, dispatch_id):
        self.sends.append(dispatch_id)
        assert self.target is not None
        return self.target.send(intent, dispatch_id)

    def control(self, signal) -> None:
        assert self.target is not None
        self.target.control(signal)

    def lookup(self, dispatch_id):
        assert self.target is not None
        return self.target.lookup(dispatch_id)


class SwapApi:
    """端點的 CoreApi：Core 重啟後換成連到新 Core 的 client（真實重連的等價做法）。"""

    def __init__(self, inner) -> None:
        self.inner = inner

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


def stop_cli(action: str, url: str, token: str | None = STOP_TOKEN) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("UEP_STOP_TOKEN", "UEP_STOP_URL")}
    if token is not None:
        env["UEP_STOP_TOKEN"] = token
    env["UEP_STOP_URL"] = url
    return subprocess.run(
        [sys.executable, "-m", "uep.stop", action, "--url", url],
        check=False, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30,
    )


@pytest.fixture
def epoch_env(tmp_path, monkeypatch):
    """Host epoch 檔與端點密鑰走環境變數（測試用 tmp 檔，不碰真實 %LOCALAPPDATA%）。"""
    path = tmp_path / "host_epoch.json"
    monkeypatch.setenv("UEP_HOST_EPOCH_FILE", str(path))
    monkeypatch.setenv("UEP_ENDPOINT_SECRETS", f"{EP_ID}:{EP_SECRET}")
    get_settings.cache_clear()
    yield path
    monkeypatch.undo()
    get_settings.cache_clear()


def _build_core(session_factory, adapter, executor, clock) -> CoreAssembly:
    bus = EventBus()
    return assemble_core(
        session_factory, bus=bus, publisher=EventPublisher(session_factory, bus), control=InProcessControlChannel(),
        adapter=adapter, executors={"fake": executor},
        turn_generation_source=lambda turn_id: TURN_GEN, presence_audience=lambda: "scope_private_owner",
        epoch_store=HostEpochStore(), clock=clock, stop_token=SecretStr(STOP_TOKEN),
        stop_api_port=0, endpoint_socket_port=0,  # secrets 取自 UEP_ENDPOINT_SECRETS；epoch 檔取自 UEP_HOST_EPOCH_FILE
    )


def test_cycle2_end_to_end_assembled_core(session_factory, epoch_env):
    clock, mono = FakeClock(), Mono()
    adapter, executor = LateAdapter(), FakeToolExecutor()
    cores: list[CoreAssembly] = []
    clients: list[SocketControlClient] = []
    holder: dict[str, CliEndpoint] = {}
    api = SwapApi(None)
    try:
        core = _build_core(session_factory, adapter, executor, clock)
        cores.append(core)
        assert core.stop_api is not None and core.endpoint_server is not None
        assert HostEpochStore().path == epoch_env  # 確認用的是 tmp 檔
        core.start()
        assert core.start_log[:3] == ["presence.start", "bus.start", "stop_api.start"]

        def connect(c: CoreAssembly) -> SocketControlClient:
            client = SocketControlClient(
                *c.endpoint_server.address, EP_ID, EP_SECRET.encode(), on_control=lambda s: holder["ep"].on_control(s)
            )
            clients.append(client)
            return client

        client = connect(core)
        api.inner = client.api
        ep = CliEndpoint(EP_ID, api, clock=mono, wall_clock=clock)
        holder["ep"] = ep
        adapter.target = ep
        client.connect()
        ep.start()

        # ---- 1. acquire -> Task／工具 action（receipt）-> 輸出經 Gate -> Delivery -> 端點播放 -> 交付證據 ----
        assert ep.start_lease() is AcquireResult.GRANTED
        auth = core.presence.authorization()
        assert auth is not None and auth == ep.lease.auth
        assert core.presence.current().status is PresenceStatus.ACTIVE

        # presence_bound Task 隨 Presence 暫停／恢復；非 bound 的背景 Task 才受「全域停止擋新工具」約束
        task = core.tasks.create_task(
            persona_id="uep", goal="bound", owner_actor_id="owner", scope_ref="scope_private_owner", policy=BOUND
        )
        core.tasks.start_run(task.task_id, persona_version=1, policy_version=1)
        free = core.tasks.create_task(
            persona_id="uep", goal="e2e", owner_actor_id="owner", scope_ref="scope_private_owner", policy=FREE
        )
        run = core.tasks.start_run(free.task_id, persona_version=1, policy_version=1)
        step = core.tasks.begin_step(run.run_id, "work", worker_epoch=run.worker_epoch)

        def new_action(params: dict):
            return core.tasks.create_action(
                run_id=run.run_id, step_id=step.step_id, tool="files.read", executor="fake", params=params,
                side_effect_class="internal_read", idempotency="via_action_id", worker_epoch=run.worker_epoch,
            )

        params = {"path": "a"}
        action = new_action(params)
        outcome = core.tasks.execute_action(action.action_id, params, worker_epoch=run.worker_epoch)
        assert outcome.executed and outcome.status.value == "SUCCEEDED"
        receipt = core.tasks.get_receipt(action.action_id)
        assert receipt is not None and executor.execution_count(action.action_id) == 1

        record = core.delivery.dispatch(intent_for(auth))
        assert record.status is S.RENDERED_PLAYED
        assert record.delivery_evidence is DeliveryEvidence.LOCAL_PLAYBACK
        assert len(adapter.sends) == 1

        # ---- 2. 全域停止（HTTP + Bearer，env 注入 token）----
        bad = stop_cli("stop", core.stop_api.url, token="wrong-token")
        assert bad.returncode == 1  # 錯 token 被拒，旗標不得生效
        assert core.presence.current().status is PresenceStatus.ACTIVE and ep.lease is not None
        stopped = stop_cli("stop", core.stop_api.url)
        assert stopped.returncode == 0, stopped.stderr
        assert wait_until(lambda: ep.lease is None and ep.pending_report_count == 0)  # 端點收到 revoke 並停止、證據已回報
        assert core.presence.current().status is PresenceStatus.NONE
        assert core.tasks.get_task(task.task_id).status == RunStatus.PAUSED.value  # bound Task 隨 revoke 暫停
        assert stop_cli("status", core.stop_api.url).returncode == 0

        # 新工具不再開始；Task 不得轉回 RUNNING；acquire 被拒
        params2 = {"path": "b"}
        blocked = new_action(params2)
        with pytest.raises(ActionBlocked, match="global_stop_engaged"):
            core.tasks.execute_action(blocked.action_id, params2, worker_epoch=run.worker_epoch)
        assert executor.execution_count(blocked.action_id) == 0
        assert ep.start_lease() is AcquireResult.DENIED
        stale = core.delivery.dispatch(intent_for(auth))  # 舊授權的輸出：Gate 擋下，沒到端點
        assert stale.status is S.REJECTED and len(adapter.sends) == 1

        # release 後恢復
        assert stop_cli("release", core.stop_api.url).returncode == 0
        assert ep.start_lease() is AcquireResult.GRANTED
        auth2 = core.presence.authorization()
        assert auth2 is not None and auth2.presence_generation == auth.presence_generation + 1
        outcome2 = core.tasks.execute_action(blocked.action_id, params2, worker_epoch=run.worker_epoch)
        assert outcome2.executed
        assert core.delivery.dispatch(intent_for(auth2)).status is S.RENDERED_PLAYED
        bound2 = core.tasks.create_task(
            persona_id="uep", goal="bound2", owner_actor_id="owner", scope_ref="scope_private_owner", policy=BOUND
        )
        core.tasks.start_run(bound2.task_id, persona_version=1, policy_version=1)  # 重啟前處於 RUNNING
        assert core.tasks.get_task(bound2.task_id).status == RunStatus.RUNNING.value

        # ---- 3. Core 重啟（新 epoch）：舊端點輸出被拒、重連後證據回報並恢復 ----
        core.shutdown()  # 關入口與訂閱，不撤銷 Presence（Core 關機不終止 Task）
        client.close()
        core2 = _build_core(session_factory, adapter, executor, clock)
        cores.append(core2)
        core2.start()
        assert core2.presence.core_epoch == auth2.core_epoch + 1
        view = core2.presence.current()
        assert view.status is PresenceStatus.RECOVERING and view.unquiesced_endpoint_id == EP_ID
        assert core2.tasks.get_task(bound2.task_id).status == RunStatus.PAUSED.value  # presence_bound Task 補暫停

        sends_before = len(adapter.sends)
        old_output = core2.delivery.dispatch(intent_for(auth2))  # 端點還以為自己有 Lease，但 Gate 先擋
        assert old_output.status is S.REJECTED and len(adapter.sends) == sends_before
        assert core2.gate.admit_intent(intent_for(auth2)) is GateDecision.REJECT_STALE
        # 舊 Core（已 shutdown 的 core1）的 Gate 綁舊 Manager，也不得放行舊授權
        assert core.gate.admit_intent(intent_for(auth2)) is not GateDecision.ADMIT

        # 端點以新 client 重連：權威快照已不是 owner -> 停止並回報證據 -> Core 解除未證實停止標記
        new_client = connect(core2)
        api.inner = new_client.api
        new_client.connect()
        assert ep.reconnect() is ReconnectOutcome.LOST
        assert ep.lease is None and ep.pending_report_count == 0
        view = core2.presence.current()
        assert view.status is PresenceStatus.NONE and view.unquiesced_endpoint_id is None
        assert view.generation == auth2.presence_generation  # generation 不回退

        # 恢復：新 epoch 取得 Lease，輸出可播；舊 epoch 的授權仍永遠過不了
        assert ep.start_lease() is AcquireResult.GRANTED
        fresh = core2.presence.authorization()
        assert fresh is not None and fresh.core_epoch == auth2.core_epoch + 1
        assert fresh.presence_generation == auth2.presence_generation + 1
        assert core2.delivery.dispatch(intent_for(fresh)).status is S.RENDERED_PLAYED
        assert core2.delivery.dispatch(intent_for(auth2)).status is S.REJECTED
    finally:
        ep_obj = holder.get("ep")
        if ep_obj is not None:
            ep_obj.close()
        for c in clients:
            c.close()
        for c in cores:
            c.shutdown()
