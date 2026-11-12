"""在場請求仲裁的觀察與（旗標 on 時）有限落地（AR-2／AR-3a）。

模式（``UEP_PRESENCE_ARBITER``）：
- ``shadow``：只記錄判決（log＋指標），不發事件、**不呼叫任何 Presence 寫入、不寫 override／mark**。
- ``on``：另發 ``schedule.arbitration_decided``（durable），並對 ``mark_abandon`` 判決真的寫 abandon mark
  （``SlotMarkWriter`` 已有寫入端）。begin／supersede／end 與所有需要移轉的判決一律 ``executed=false``、
  ``exec_reason=transfer_not_implemented``：不寫 override、不呼叫 Presence 寫入（Presence Manager 還沒有位置對位置的移轉）。

輸入來源（全部經佇列，由本物件自己的背景執行緒消化；呼叫端的執行緒不做 DB、不發事件——審查 m7）：
- bus 事件 ``schedule.directive_issued``：事件只帶 directive_id，收到後以當下的行程格與 Presence 重算同一個 directive，
  id 對得上才仲裁，對不上記 ``stale``。
- bus 事件 ``turn.skipped``（payload 的 presence_request 觀察；目前沒有 target，只計數，日後帶 ``target_location`` 才判決）。
- 決策層注入的 sink：``on_presence_request(proposal, ...)``、``on_abandon_signal(signal)``。
背景執行緒尚未啟動時（單元測試）改為同步處理。
"""
from __future__ import annotations

import logging
import queue
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from uep.contracts.schedule import (
    PLAN_LOCATIONS,
    ArbiterRequest,
    ArbitrationDecision,
    InterruptFacts,
    PlanSlot,
)
from uep.schedule import arbiter
from uep.schedule.planner import PresenceReading
from uep.schedule.resolver import reconcile_directive

logger = logging.getLogger(__name__)

_METRIC = "schedule.arbiter.shadow"
MODES = ("shadow", "on")
EXEC_TRANSFER_NOT_IMPLEMENTED = "transfer_not_implemented"
EXEC_NOT_ACCEPTED = "not_accepted"
EXEC_MARK_WRITTEN = "mark_written"
EXEC_MARK_WRITTEN_NO_TRANSFER = "mark_written_transfer_not_implemented"
EXEC_NO_MARKABLE_SLOT = "no_markable_slot"
EXEC_MARK_WRITE_FAILED = "mark_write_failed"


@dataclass(frozen=True)
class ShadowJudgment:
    source: str  # directive｜request
    ref: str
    decision: ArbitrationDecision
    executed: bool = False
    exec_reason: str = ""


class PresenceArbiterShadow:
    def __init__(self, reader: Any, presence_fn: Callable[[], PresenceReading], override_fn: Callable[[], Any] | None,
                 probe_fn: Callable[[], InterruptFacts], *,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 metrics_inc: Callable[[str], None] | None = None, max_records: int = 100, mode: str = "shadow",
                 events: Any | None = None, mark_writer: Any | None = None, abandon_executor: Any | None = None,
                 poke: Callable[[], None] | None = None, queue_max: int = 256) -> None:
        if mode not in MODES:
            raise ValueError(f"未知的仲裁模式：{mode}")
        if mode == "on" and (events is None or mark_writer is None):
            raise ValueError("on 模式需要 events 與 mark_writer")
        self._reader = reader
        self._presence_fn = presence_fn
        self._override_fn = override_fn
        self._probe_fn = probe_fn
        self._clock = clock
        self._metrics_inc = metrics_inc
        self._mode = mode
        self._events = events
        self._marks = mark_writer
        self._abandon = abandon_executor
        self._poke = poke
        self._seq = 0
        self.judgments: deque[ShadowJudgment] = deque(maxlen=max_records)
        self._cleanups: list[Callable[[], None]] = []
        self._queue: queue.Queue[tuple[str, tuple, dict]] = queue.Queue(maxsize=queue_max)
        self._thread: threading.Thread | None = None
        self._stop_flag = threading.Event()

    @property
    def mode(self) -> str:
        return self._mode

    def _count(self, name: str) -> None:
        if self._metrics_inc is not None:
            try:
                self._metrics_inc(name)
            except Exception:
                logger.debug("指標寫入失敗：%s", name, exc_info=True)

    # ---- 判決記錄與（on）落地 ----

    def _record(self, source: str, ref: str, decision: ArbitrationDecision, slot: PlanSlot, now: datetime) -> None:
        executed, exec_reason = False, ""
        if self._mode == "on":
            executed, exec_reason = self._execute(ref, decision, slot, now)
            self._events.arbitration_decided(source=source, ref=ref, decision=decision, executed=executed,
                                             exec_reason=exec_reason)
        self.judgments.append(ShadowJudgment(source, ref, decision, executed, exec_reason))
        self._count(f"{_METRIC}.{source}.{decision.outcome.lower()}")
        self._count(f"{_METRIC}.{source}.reason.{decision.reason_code}")
        logger.info("仲裁判決：%s %s → %s/%s（override=%s target=%s executed=%s mode=%s）", source, ref,
                    decision.outcome, decision.reason_code, decision.override_action, decision.transfer_target,
                    executed, self._mode)

    def _execute(self, ref: str, decision: ArbitrationDecision, slot: PlanSlot, now: datetime) -> tuple[bool, str]:
        """on 模式的落地：只有 mark_abandon 會真的寫（abandon mark）；其餘一律不執行，也不碰 Presence。"""
        if decision.outcome != "ACCEPTED_TRANSFER":
            return False, EXEC_NOT_ACCEPTED
        if decision.override_action != "mark_abandon":
            return False, EXEC_TRANSFER_NOT_IMPLEMENTED  # begin／supersede／end／純移轉
        mark = arbiter.abandon_mark_for(slot, ref, now)
        if mark is None:
            return False, EXEC_NO_MARKABLE_SLOT
        try:
            mark_id = self._marks.begin_mark(kind="abandon", item_ref=mark.item_ref,
                                             planned_start_at=mark.planned_start_at, cause_request_id=ref)
        except Exception:
            logger.exception("仲裁 abandon mark 寫入失敗：%s", ref)
            self._count(f"{_METRIC}.mark_write_failed")
            return False, EXEC_MARK_WRITE_FAILED
        self._events.slot_marked(mark_id, "abandon", mark.item_ref, ref)
        if self._poke is not None:
            try:
                self._poke()
            except Exception:
                logger.debug("poke Planner 失敗", exc_info=True)
        # 移轉到主機的那一半（Presence 位置移轉）尚未實作；人已在主機時 transfer_target 為 None
        return True, EXEC_MARK_WRITTEN_NO_TRANSFER if decision.transfer_target else EXEC_MARK_WRITTEN

    # ---- 佇列 ----

    def _submit(self, kind: str, *args: Any, **kwargs: Any) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._process(kind, args, kwargs)  # 背景執行緒未啟動（測試）：同步處理
            return
        try:
            self._queue.put_nowait((kind, args, kwargs))
        except queue.Full:
            self._count(f"{_METRIC}.queue_full")
            logger.warning("仲裁佇列已滿，丟棄一筆 %s", kind)

    def _process(self, kind: str, args: tuple, kwargs: dict) -> None:
        handler = {
            "directive": self._handle_directive, "turn_skipped": self._handle_turn_skipped,
            "presence_request": self._handle_presence_request, "abandon": self._handle_abandon,
        }[kind]
        try:
            handler(*args, **kwargs)
        except Exception:
            logger.exception("仲裁處理 %s 失敗", kind)
            self._count(f"{_METRIC}.error")

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run, name="presence-arbiter", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_flag.is_set():
            try:
                kind, args, kwargs = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._process(kind, args, kwargs)
            finally:
                self._queue.task_done()

    def drain(self, timeout: float = 5.0) -> bool:
        """等佇列處理完（測試與關閉用）。"""
        import time as _time

        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0 and self._queue.empty():
                return True
            _time.sleep(0.01)
        return False

    # ---- 對外入口（只排佇列）----

    def on_directive_event(self, event: Any) -> None:
        self._submit("directive", event)

    def on_turn_skipped(self, event: Any) -> None:
        self._submit("turn_skipped", event)

    def on_abandon_signal(self, signal: Any) -> None:
        """決策層注入的 sink：只排佇列，由背景執行緒交給放棄消費端執行器。"""
        self._submit("abandon", signal)

    def on_presence_request(self, proposal: Any, *, request_id: str, actor_ref: str, actor_authenticated: bool,
                            is_owner: bool, source_platform: str) -> None:
        """決策層注入的 sink（缺席端輸入的在場請求）：只排佇列。"""
        self._submit("presence_request", proposal, request_id=request_id, actor_ref=actor_ref,
                     actor_authenticated=actor_authenticated, is_owner=is_owner, source_platform=source_platform)

    # ---- 處理 ----

    def _handle_directive(self, event: Any) -> None:
        directive_id = (getattr(event, "payload", None) or {}).get("directive_id")
        now = self._clock()
        slot = self._reader.resolve(now)
        reading = self._presence_fn()
        actual = arbiter.actual_location(reading)
        if actual is None:  # Presence 不明：無法重算 directive
            self._count(f"{_METRIC}.directive.stale")
            return
        directive = reconcile_directive(slot, actual, now)
        if directive is None or directive.directive_id != directive_id:
            self._count(f"{_METRIC}.directive.stale")
            return
        override = self._override_fn() if self._override_fn is not None else None
        decision = arbiter.arbitrate_directive(
            directive, slot=slot, presence=reading, override=override, probe=self._probe_fn(), now=now)
        self._record("directive", directive.directive_id, decision, slot, now)

    def _judge_request(self, request: ArbiterRequest) -> None:
        now = self._clock()
        slot = self._reader.resolve(now)
        override = self._override_fn() if self._override_fn is not None else None
        decision = arbiter.arbitrate_request(
            request, slot=slot, presence=self._presence_fn(), override=override, probe=self._probe_fn(), now=now)
        self._record("request", request.request_id, decision, slot, now)

    def _handle_turn_skipped(self, event: Any) -> None:
        payload = getattr(event, "payload", None) or {}
        if payload.get("presence_request") is not True:
            return
        self._count(f"{_METRIC}.presence_request_observed")
        target = payload.get("target_location")
        authenticated = payload.get("actor_authenticated") is True
        is_owner = payload.get("is_owner") == "true"
        ref = getattr(event, "event_id", "unknown")
        if target not in PLAN_LOCATIONS or not authenticated:
            self._count(f"{_METRIC}.request.no_target" if target not in PLAN_LOCATIONS
                        else f"{_METRIC}.request.unauthenticated")
            logger.info("仲裁：觀察到在場請求但無法判決（target=%s authenticated=%s）", target, authenticated)
            return
        if not is_owner:
            # member 需要 platform_links 的「已登記」事實，目前拿不到：非 owner 一律不建請求，只計數
            self._count(f"{_METRIC}.request.member_unverified")
            return
        # owner 只在 payload 明確 owner 且已認證時才建。AR-3 前要改成由 Core 依 platform_links 計算，不信 payload 字串
        self._seq += 1
        self._judge_request(ArbiterRequest(
            request_id=f"observed:{ref}", authority="owner", requester_ref=str(payload.get("actor_id") or "unknown"),
            actor_authenticated=True, target=target, received_seq=self._seq))

    def _handle_presence_request(self, proposal: Any, *, request_id: str, actor_ref: str, actor_authenticated: bool,
                                 is_owner: bool, source_platform: str) -> None:
        intent = getattr(proposal, "intent", None)
        intent = getattr(intent, "value", intent)
        target = getattr(proposal, "target_location", None)
        self._count(f"{_METRIC}.presence_request_observed")
        if intent != "presence_request" or target is None:
            self._count(f"{_METRIC}.request.not_a_move")  # 只有 intent==presence_request 且有 target 才仲裁
            return
        target = getattr(target, "value", target)
        if target not in PLAN_LOCATIONS:
            self._count(f"{_METRIC}.request.no_target")
            return
        if actor_authenticated is not True:
            self._count(f"{_METRIC}.request.unauthenticated")
            return
        if is_owner is not True:  # member 先不建（拿不到 platform_links 的已登記事實）
            self._count(f"{_METRIC}.request.member_unverified")
            return
        self._seq += 1
        self._judge_request(ArbiterRequest(
            request_id=request_id, authority="owner", requester_ref=actor_ref, actor_authenticated=True,
            target=target, received_seq=self._seq))

    def _handle_abandon(self, signal: Any) -> None:
        if self._abandon is None or self._mode != "on":
            self._count(f"{_METRIC}.abandon_signal_observed")  # shadow：只計數，不寫 mark、不發事件
            return
        self._abandon.handle(signal)

    # ---- 生命週期 ----

    def add_cleanup(self, fn: Callable[[], None]) -> None:
        self._cleanups.append(fn)

    def stop(self) -> None:
        self._stop_flag.set()
        cleanups, self._cleanups = self._cleanups, []
        for fn in cleanups:
            try:
                fn()
            except Exception:
                logger.warning("仲裁清理失敗", exc_info=True)
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(5.0)
        self._thread = None
