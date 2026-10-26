"""本機 CLI 端點（T-18）：實作 ``OutputAdapter``，持有 Lease、續約、二次接納、模擬播放 buffer。

重點（皆為 T-16 審查轉入的必做項）：
- 控制 handler（``on_control``）只記錄訊號並喚醒，不判斷、不停止；冪等（control_id 去重）、執行緒安全
  （不同 kind 的 lane 可並行呼叫）。是否生效在「處理時」（``process_controls``）才以
  ``signal.presence_generation >= 本地 generation`` 判斷；缺 generation 的訊號採 fail-safe（照停）。
- 本地單調時鐘 watchdog：期限 = 續約請求送出前的單調時間 + Core 回報的剩餘 ttl（偏保守），
  不依賴 revoke 送達；期限到即停止並清空 buffer。
- 二次接納：``send`` 前與每個 chunk 播放前，自檢授權六元組與本地剩餘 TTL（Gate -> send 之間的 TOCTOU
  由端點在此關閉）。
- Lease 丟棄（``_drop_locked``）與播放取消在同一把鎖內完成，之後任何 chunk 都過不了二次接納；
  網路回報在鎖外。停止證據的 playing／buffer_flushed 等播放執行緒真的離開 chunk 後如實記錄。
- ``start()`` 起兩條執行緒：本地執行緒（控制訊號＋watchdog，不碰網路）與網路執行緒（續約＋證據回報），
  Core RPC 卡住不會拖慢 watchdog。
- 重連（``reconnect``）先取權威快照，授權仍是自己的才續約，否則停止。
- 停止後以 ``confirm_quiesced(old_generation, evidence)`` 回報；回報失敗（網路隔離）會保留並於
  tick／重連時重試。若 Core 仍認為自己持有 Lease（本地期限早於 Core 期限），改以 release 結束。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from uep.adapters.output import SendFailed, SendLookup, SendResult
from uep.contracts.presence import (
    AcquireResult,
    AuthorizationTuple,
    ConfirmResult,
    GateDecision,
    ReleaseResult,
    RenewResult,
)
from uep.control.channel import ControlChannel, ControlSubscription
from uep.endpoint.auth import EndpointAuthError
from uep.endpoint.protocol import CoreApi, CoreApiError
from uep.endpoint.watchdog import LeaseWatchdog
from uep.output.delivery import DeliveryEvidence
from uep.output.intent import ControlKind, ControlScope, ControlSignal, OutputIntent

logger = logging.getLogger("uep.endpoint")

_SEEN_LIMIT = 4096
_ENDED_LIMIT = 256
_RESULT_LIMIT = 1024
_MARK_LIMIT = 16
_IDLE_WAIT = 1.0  # 等播放執行緒離開 chunk 的上限（秒）
# Core 暫時不可達或拒絕時的錯誤；端點不因此放寬任何限制，只保留待重試
_CORE_ERRORS = (OSError, CoreApiError, EndpointAuthError)


class EndpointRejected(SendFailed):
    """端點二次接納拒絕；``decision`` 為與 Output Gate 同一份結果碼。"""

    def __init__(self, decision: GateDecision) -> None:
        super().__init__(decision.value)
        self.decision = decision


class PlaybackCancelled(SendFailed):
    """播放在輸出任何內容前被取消。"""


class ReconnectOutcome(StrEnum):
    RESUMED = "RESUMED"  # 授權仍屬自己，已續約
    LOST = "LOST"  # 授權已不屬自己，已停止
    RETRY = "RETRY"  # Core 暫時不可達：維持原狀，稍後再試
    IDLE = "IDLE"  # 本來就沒有持有 Lease


@dataclass(frozen=True)
class LocalLease:
    auth: AuthorizationTuple
    deadline: float  # 本地單調時鐘的絕對期限


@dataclass
class _Play:
    intent: OutputIntent
    buffer: deque[int]
    total: int
    played: int = 0
    cancelled: bool = False
    reject: GateDecision | None = None
    idle: threading.Event = field(default_factory=threading.Event)  # 設起＝播放執行緒不在 chunk 內

    def __post_init__(self) -> None:
        self.idle.set()


@dataclass(frozen=True)
class _Dropped:
    auth: AuthorizationTuple
    reason: str
    play: _Play | None


@dataclass
class _Report:
    auth: AuthorizationTuple
    evidence: dict[str, Any] = field(default_factory=dict)
    play: _Play | None = None  # 停止當下仍在 chunk 內的播放；送出證據前必須確認它已離開 chunk


def _default_chunk_count(intent: OutputIntent) -> int:
    return 4 if intent.kind.value == "audio_stream" else 1


def _wall_now() -> datetime:
    return datetime.now(UTC)


class CliEndpoint:
    lookup_authoritative = False  # 記憶體內紀錄不持久，不足以「確認未送出」

    def __init__(
        self, endpoint_id: str, core: CoreApi, *, location: str = "desktop",
        clock: Callable[[], float] = time.monotonic, wall_clock: Callable[[], datetime] = _wall_now,
        chunk_count: Callable[[OutputIntent], int] = _default_chunk_count,
        on_chunk: Callable[[OutputIntent, int], None] | None = None,
        sink: Callable[[str], None] | None = None,
        renew_fraction: float = 1 / 3, retry_seconds: float = 1.0, tick_interval: float = 0.2,
    ) -> None:
        self.endpoint_id = endpoint_id
        self._core = core
        self._location = location
        self._clock = clock
        self._wall = wall_clock
        self._chunk_count = chunk_count
        self._on_chunk = on_chunk
        self._sink = sink
        self._renew_fraction = renew_fraction
        self._retry = retry_seconds
        self._tick_interval = tick_interval
        self._lock = threading.RLock()
        self._play_lock = threading.Lock()  # 一次只播一個輸出
        self._lease: LocalLease | None = None
        self._renew_at: float | None = None
        self._current: _Play | None = None
        self._pending: deque[ControlSignal] = deque()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._ended: OrderedDict[str, GateDecision] = OrderedDict()  # lease_id -> 停止原因對應結果碼
        self._reports: list[_Report] = []
        self._report_retry_at: float | None = None
        self._revoke_mark: dict[int, int] = {}  # core_epoch -> 收過的最大 revoke generation（含無 Lease 時）
        self._stopped_turn: int | None = None  # 已被 stop 的最大 turn_generation
        self._threaded = False
        self._net_wake = threading.Event()
        self._net_thread: threading.Thread | None = None
        self._results: OrderedDict[str, SendResult] = OrderedDict()
        self._watchdog = LeaseWatchdog(self._on_watchdog_expired, clock=clock)
        self._wake = threading.Event()
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None
        self.ignored_controls = 0  # 因 generation／epoch 落後而不生效的訊號數

    # ---- 觀測 ----

    @property
    def lease(self) -> LocalLease | None:
        with self._lock:
            return self._lease

    @property
    def playing(self) -> bool:
        with self._lock:
            return self._current is not None

    @property
    def pending_report_count(self) -> int:
        with self._lock:
            return len(self._reports)

    def remaining_ttl(self) -> float | None:
        return self._watchdog.remaining()

    def _say(self, text: str) -> None:
        if self._sink is not None:
            self._sink(text)

    # ---- 控制通道 ----

    def attach_control(self, channel: ControlChannel) -> ControlSubscription:
        return channel.subscribe(self.endpoint_id, self.on_control)

    def on_control(self, signal: ControlSignal) -> None:
        """控制 handler：只記錄與喚醒（不阻塞、不判斷、不停止）；以 control_id 冪等。"""
        with self._lock:
            if signal.control_id in self._seen:
                return
            self._seen[signal.control_id] = None
            if len(self._seen) > _SEEN_LIMIT:
                self._seen.popitem(last=False)
            self._pending.append(signal)
        self._wake.set()

    def control(self, signal: ControlSignal) -> None:
        """OutputAdapter 協定的控制入口，等同 ``on_control``。"""
        self.on_control(signal)

    def process_controls(self) -> None:
        """處理已收到的訊號；在此時才以 generation／epoch 判斷是否生效。"""
        dropped: _Dropped | None = None
        with self._lock:
            signals, self._pending = list(self._pending), deque()
            for signal in signals:
                if signal.kind is ControlKind.REVOKE:
                    self._remember_revoke(signal)
                if not self._applies(signal):
                    self.ignored_controls += 1
                    continue
                if signal.kind is ControlKind.REVOKE:
                    dropped = self._drop_locked("revoked", GateDecision.REJECT_REVOKED) or dropped
                else:
                    self._cancel_for(signal)
        if dropped is not None:
            self._finish_drop(dropped)

    def _remember_revoke(self, signal: ControlSignal) -> None:
        """記下 revoke 高水位：Lease 尚未建立完成時到達的 revoke 不能被丟掉（呼叫端須持有 ``_lock``）。"""
        generation = signal.target_fence.get("presence_generation")
        if generation is None:
            return
        epoch = signal.target_fence["core_epoch"]
        self._revoke_mark[epoch] = max(self._revoke_mark.get(epoch, generation), generation)
        while len(self._revoke_mark) > _MARK_LIMIT:
            del self._revoke_mark[min(self._revoke_mark)]

    def _marked_revoked(self, auth: AuthorizationTuple) -> bool:
        return any(
            epoch > auth.core_epoch or (epoch == auth.core_epoch and generation >= auth.presence_generation)
            for epoch, generation in self._revoke_mark.items()
        )

    def _applies(self, signal: ControlSignal) -> bool:
        lease = self._lease
        if lease is None:
            return False  # 沒有授權也就沒有可停止的輸出（revoke 另由高水位保存）
        fence = signal.target_fence
        if fence["core_epoch"] > lease.auth.core_epoch:
            return True  # 較新的 Core epoch：無條件生效
        if fence["core_epoch"] < lease.auth.core_epoch:
            return False
        generation = fence.get("presence_generation")
        return generation is None or generation >= lease.auth.presence_generation  # 缺值：fail-safe 照停

    def _cancel_for(self, signal: ControlSignal) -> None:
        play = self._current
        if signal.scope is ControlScope.TURN:
            turn = signal.target_fence["turn_generation"]
            if self._stopped_turn is None or turn > self._stopped_turn:
                self._stopped_turn = turn  # 之後 send 到的舊回合輸出也要擋
            current_turn = play.intent.turn_generation if play is not None else None
            if current_turn is not None and current_turn > turn:
                return  # 較新的回合不受舊回合的 stop 影響
        if play is not None:
            self._cancel_current()

    def _cancel_current(self) -> None:
        play = self._current
        if play is not None:
            play.cancelled = True
            play.buffer.clear()

    # ---- Lease 生命週期 ----

    def start_lease(self) -> AcquireResult:
        """acquire -> 確認到場（confirm_activated）-> 武裝 watchdog；只有真的啟用才回 GRANTED。"""
        reply = self._core.acquire(self._location)
        if reply.result is not AcquireResult.GRANTED or reply.auth is None:
            return reply.result
        started = self._clock()  # 取請求送出前的時間：期限偏保守
        confirmed = self._core.confirm_activated(reply.auth.lease_id, reply.auth.presence_generation)
        if confirmed.result is not ConfirmResult.ACCEPTED or confirmed.ttl_seconds is None:
            return AcquireResult.DENIED
        with self._lock:
            self._install(reply.auth, started, confirmed.ttl_seconds)
            self._stopped_turn = None  # 新 Lease：回合 generation 的停止紀錄重算
            dropped = (
                self._drop_locked("revoke_before_activation", GateDecision.REJECT_REVOKED)
                if self._marked_revoked(reply.auth) else None
            )
        if dropped is not None:  # 確認啟用與安裝之間已有對應的 revoke
            self._finish_drop(dropped)
            return AcquireResult.DENIED
        self._say(f"已持有 Lease（generation={reply.auth.presence_generation}）")
        return AcquireResult.GRANTED

    def _install(self, auth: AuthorizationTuple, started: float, ttl: float) -> None:
        deadline = started + ttl
        with self._lock:
            self._lease = LocalLease(auth, deadline)
            self._renew_at = started + ttl * self._renew_fraction
            self._watchdog.arm(deadline)

    def renew_once(self) -> RenewResult | None:
        """續約一次；Core 不可達回 None（維持原期限，由 watchdog 把關）。"""
        with self._lock:
            lease = self._lease
        if lease is None:
            return None
        started = self._clock()
        try:
            reply = self._core.renew(
                lease.auth.lease_id, lease.auth.core_epoch, lease.auth.presence_generation,
                lease.auth.fencing_epoch,
            )
        except _CORE_ERRORS:
            with self._lock:
                self._renew_at = self._clock() + self._retry
            return None
        if reply.result is RenewResult.RENEWED and reply.ttl_seconds is not None:
            with self._lock:
                if self._lease is lease:  # 續約期間未被停止才延後期限
                    self._install(lease.auth, started, reply.ttl_seconds)
            return reply.result
        if reply.result is RenewResult.EXPIRED:
            self._quiesce("renew_expired", GateDecision.REJECT_EXPIRED)
        else:
            self._quiesce("renew_stale", GateDecision.REJECT_STALE)
        return reply.result

    def reconnect(self) -> ReconnectOutcome:
        """重連：先取權威快照；授權仍屬自己才續約，否則停止。Core 不可達則例外往外拋。"""
        snap = self._core.snapshot()
        with self._lock:
            lease = self._lease
        outcome = ReconnectOutcome.IDLE
        if lease is not None:
            a = lease.auth
            same = (
                snap.owns and snap.lease_id == a.lease_id and snap.generation == a.presence_generation
                and snap.core_epoch == a.core_epoch and snap.fencing_epoch == a.fencing_epoch
            )
            if not same:
                self._quiesce("lost_authority_on_reconnect", GateDecision.REJECT_STALE)
                outcome = ReconnectOutcome.LOST
            else:
                renewed = self.renew_once()
                if renewed is RenewResult.RENEWED:
                    outcome = ReconnectOutcome.RESUMED
                elif renewed is None and self.lease is not None:
                    outcome = ReconnectOutcome.RETRY  # Core 又不可達：不當作失去授權
                else:
                    outcome = ReconnectOutcome.LOST  # renew 被拒（renew_once 已停止）
        self._flush_reports(force=True)
        return outcome

    def release(self) -> None:
        """主動釋放：先停止本地輸出，再通知 Core（失敗時保留待重試）。"""
        self._quiesce("released", GateDecision.REJECT_NO_PRESENCE)

    # ---- 停止與證據 ----

    def _on_watchdog_expired(self) -> None:
        self._quiesce("lease_expired_local", GateDecision.REJECT_EXPIRED)

    def _drop_locked(self, reason: str, code: GateDecision) -> _Dropped | None:
        """鎖內原子地丟棄 Lease 並取消播放（之後任何 chunk 都過不了接納）；呼叫端須持有 ``_lock``。"""
        lease = self._lease
        if lease is None:
            return None
        self._lease, self._renew_at = None, None
        self._watchdog.disarm()
        self._ended[lease.auth.lease_id] = code
        if len(self._ended) > _ENDED_LIMIT:
            self._ended.popitem(last=False)
        play = self._current
        self._cancel_current()
        return _Dropped(lease.auth, reason, play)

    def _quiesce(self, reason: str, code: GateDecision) -> None:
        """停止：丟棄 Lease、清空 buffer、取消播放，並回報停止證據。冪等。"""
        with self._lock:
            dropped = self._drop_locked(reason, code)
        if dropped is not None:
            self._finish_drop(dropped)

    def _finish_drop(self, dropped: _Dropped) -> None:
        """等播放執行緒真正離開 chunk，再如實記錄證據並回報（網路在鎖外）。"""
        playing = False
        if dropped.play is not None:
            playing = not dropped.play.idle.wait(_IDLE_WAIT)
        buffer_flushed = dropped.play is None or not dropped.play.buffer
        report = _Report(dropped.auth, {
            "kind": "adapter_stop_report", "reason": dropped.reason, "endpoint_id": self.endpoint_id,
            "buffer_flushed": buffer_flushed, "playing": playing,
        }, dropped.play)
        with self._lock:
            self._reports.append(report)
        self._say(f"已停止（{dropped.reason}）")
        if self._threaded:
            self._net_wake.set()  # 交給網路執行緒，本地執行緒不碰網路
        else:
            self._flush_reports()

    def _flush_reports(self, *, force: bool = False) -> None:
        with self._lock:
            if not self._reports or (
                not force and self._report_retry_at is not None and self._clock() < self._report_retry_at
            ):
                return
            reports, self._reports = self._reports, []
        remaining: list[_Report] = []
        failed = False
        for index, report in enumerate(reports):
            if report.play is not None and not report.play.idle.is_set():
                remaining.append(report)  # sink 還在 chunk 內：不能證明已停止，不送；不算失敗、不退避（下個 tick 再看）
                continue
            try:
                done = self._deliver_report(report)
            except _CORE_ERRORS:
                remaining.extend(reports[index:])  # Core 不可達：全部保留
                failed = True
                break
            if not done:
                remaining.append(report)
                failed = True
        with self._lock:
            if remaining:
                self._reports = remaining + self._reports
                self._report_retry_at = self._clock() + self._retry if failed else None  # 失敗才退避
            else:
                self._report_retry_at = None

    def _deliver_report(self, report: _Report) -> bool:
        """回 True 表示已結案（Core 已接受或已不需要）；False 表示稍後重試。"""
        auth = report.auth
        if report.play is not None:  # 呼叫端已確認播放執行緒離開 chunk：如實更新證據
            report.evidence["playing"] = False
            report.evidence["buffer_flushed"] = not report.play.buffer
        result = self._core.confirm_quiesced(auth.presence_generation, report.evidence)
        if result is ConfirmResult.ACCEPTED:
            return True
        snap = self._core.snapshot()  # STALE：看 Core 是否仍認為自己持有這份 Lease
        if snap.owns and snap.lease_id == auth.lease_id:
            released = self._core.release(
                auth.lease_id, auth.core_epoch, auth.presence_generation, auth.fencing_epoch
            )
            return released is ReleaseResult.RELEASED
        return not snap.unquiesced  # 未證實停止的標記仍在則重試

    # ---- tick／背景執行 ----

    def local_tick(self) -> None:
        """純本地檢查（不碰網路）：處理控制訊號與 watchdog。"""
        self.process_controls()
        self._watchdog.check()

    def net_tick(self) -> None:
        """會碰網路的工作：到期續約與停止證據回報。"""
        with self._lock:
            due = self._lease is not None and self._renew_at is not None and self._clock() >= self._renew_at
        if due:
            self.renew_once()
        self._flush_reports()

    def tick(self) -> None:
        self.local_tick()
        self.net_tick()

    def start(self) -> None:
        """啟動本地執行緒（控制＋watchdog）與網路執行緒（續約＋證據）；兩者互不阻塞。"""
        if self._thread is not None:
            return
        self._closed.clear()
        self._threaded = True
        self._thread = threading.Thread(
            target=self._run, args=(self._wake, self.local_tick), daemon=True,
            name=f"UepEndpoint[{self.endpoint_id}]",
        )
        self._net_thread = threading.Thread(
            target=self._run, args=(self._net_wake, self.net_tick), daemon=True,
            name=f"UepEndpointNet[{self.endpoint_id}]",
        )
        self._thread.start()
        self._net_thread.start()

    def close(self) -> None:
        self._closed.set()
        self._wake.set()
        self._net_wake.set()
        threads = [self._thread, self._net_thread]
        self._thread = self._net_thread = None
        self._threaded = False
        for thread in threads:
            if thread is not None:
                thread.join(timeout=2.0)

    def _run(self, wake: threading.Event, work: Callable[[], None]) -> None:
        while not self._closed.is_set():
            wake.wait(timeout=self._tick_interval)
            wake.clear()
            if self._closed.is_set():
                return
            try:
                work()
            except Exception:
                logger.exception("端點背景工作錯誤")

    # ---- 二次接納與播放（OutputAdapter）----

    def _admit(self, intent: OutputIntent) -> GateDecision:
        """端點自檢：六元組與本地剩餘 TTL；結果碼與 Output Gate 同源（呼叫端須持有 ``_lock``）。"""
        lease = self._lease
        if lease is None:
            return self._ended.get(intent.lease_id, GateDecision.REJECT_NO_PRESENCE)
        a = lease.auth
        if (
            intent.persona_id, intent.endpoint_id, intent.lease_id, intent.fencing_epoch,
            intent.core_epoch, intent.presence_generation,
        ) != (a.persona_id, a.endpoint_id, a.lease_id, a.fencing_epoch, a.core_epoch, a.presence_generation):
            return GateDecision.REJECT_STALE
        if (
            self._stopped_turn is not None and intent.turn_generation is not None
            and intent.turn_generation <= self._stopped_turn
        ):
            return GateDecision.REJECT_STALE  # 該回合（或更舊）已被 stop，晚到的輸出不得播
        if self._clock() >= lease.deadline or intent.valid_until <= self._wall():
            return GateDecision.REJECT_EXPIRED
        return GateDecision.ADMIT

    def send(self, intent: OutputIntent, dispatch_id: str) -> SendResult:
        with self._lock:
            previous = self._results.get(dispatch_id)
        if previous is not None:
            return previous  # 同一 dispatch_id 不重複播放
        with self._play_lock:
            with self._lock:
                previous = self._results.get(dispatch_id)  # 等鎖期間別的執行緒可能已播完
            if previous is not None:
                return previous
            return self._play(intent, dispatch_id)

    def _play(self, intent: OutputIntent, dispatch_id: str) -> SendResult:
        self.local_tick()
        total = max(1, self._chunk_count(intent))
        with self._lock:
            decision = self._admit(intent)
            if decision is not GateDecision.ADMIT:
                raise EndpointRejected(decision)
            play = _Play(intent, deque(range(total)), total)
            self._current = play
        try:
            while True:
                self.local_tick()
                with self._lock:
                    if not play.cancelled:
                        decision = self._admit(intent)  # 每個 chunk 播放前再接納一次
                        if decision is not GateDecision.ADMIT:
                            play.reject = decision
                            self._cancel_current()
                    if play.cancelled or not play.buffer:
                        break
                    chunk = play.buffer.popleft()
                    play.idle.clear()
                done = False
                try:
                    if self._on_chunk is not None:
                        self._on_chunk(intent, chunk)  # 模擬播放一塊
                    done = True
                finally:
                    with self._lock:
                        if done and not play.cancelled:
                            play.played += 1
                        play.idle.set()
                    self._net_wake.set()  # 若有因播放中而保留的停止證據，讓網路執行緒立刻重看
        finally:
            with self._lock:
                if self._current is play:
                    self._current = None
        return self._finish(play, dispatch_id)

    def _finish(self, play: _Play, dispatch_id: str) -> SendResult:
        if play.played == 0:
            if play.reject is not None:
                raise EndpointRejected(play.reject)
            raise PlaybackCancelled("播放在輸出前被取消")
        complete = play.played == play.total
        result = SendResult(
            dispatch_id, DeliveryEvidence.LOCAL_PLAYBACK, self._wall(),
            played_range={"from_chunk": 0, "to_chunk": play.played, "complete": complete},
        )
        with self._lock:
            self._results[dispatch_id] = result
            if len(self._results) > _RESULT_LIMIT:
                self._results.popitem(last=False)
        self._say(f"播放{'完成' if complete else '中斷'}：{play.played}/{play.total}")
        return result

    def lookup(self, dispatch_id: str) -> SendLookup:
        with self._lock:
            found = self._results.get(dispatch_id)
        if found is not None:
            return SendLookup("found", found)
        return SendLookup("undetermined")
