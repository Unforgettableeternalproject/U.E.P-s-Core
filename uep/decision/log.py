"""決策日誌（DL-3，提案 §3.6、§8 Q10）：把決策結果與 shadow 比對非同步落盤。葉節點。

熱路徑只做「組一列 dict＋``put_nowait``」：佇列滿、sink 慢或失敗都只計數（``decision_log.dropped``）與記 log，
絕不阻塞或影響 primary 結果。寫入由單一背景執行緒批次送給 sink（DB sink 在 ``uep.decision.db``）。
日誌列不存原文：只存 candidate_id、participant_count、addressed_hint、utterance_refs 這類摘要，原文以 ref 指回事件。
"""
from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from uep.decision.contracts import DecisionRequest, DecisionResult
from uep.decision.hub import DecisionHub
from uep.decision.registry import DecisionPointSpec

logger = logging.getLogger("uep.decision.log")

ROLE_PRIMARY = "primary"
ROLE_SHADOW = "shadow"

# 日誌列的欄位（與 decision_log 表一致）；每一列都含全部鍵，缺值為 None（批次寫入要求鍵集一致）
COLUMNS: tuple[str, ...] = (
    "decision_id", "request_id", "kind", "created_at", "role", "primary_decision_id",
    "engine", "proposer_id", "version", "outcome", "reason_code", "confidence", "elapsed_ms",
    "status", "agree", "fallback", "candidate_id", "participant_count", "addressed_hint", "utterance_refs",
    "sub_kind", "trigger", "namespace", "snapshot_version",
)

# 新增四欄只收 ID／列舉類的安全字元短字串，原文般的內容一律記 None（不存原文原則）
_SAFE_TOKEN = re.compile(r"^[a-z0-9_.:-]{1,64}$")


def _safe_token(value: Any) -> str | None:
    return value if isinstance(value, str) and _SAFE_TOKEN.fullmatch(value) else None


def _safe_version(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class DecisionLogSink(Protocol):
    def write(self, rows: Sequence[dict[str, Any]]) -> None: ...


def _blank_row() -> dict[str, Any]:
    return dict.fromkeys(COLUMNS)


def _primary_status(result: DecisionResult, spec: DecisionPointSpec) -> str:
    if not result.fallback:
        return "ok"
    reason = result.reason_code
    if reason and reason == spec.on_timeout_reason:
        return "timeout"
    if reason and reason == spec.on_busy_reason:
        return "busy"
    if reason and reason == spec.on_invalid_reason:
        return "invalid"
    return "error"


def primary_row(
    request: DecisionRequest, result: DecisionResult, spec: DecisionPointSpec, elapsed_ms: int,
    *, now: datetime | None = None,
) -> dict[str, Any]:
    """primary 決策 -> 日誌列。輸入摘要用 duck typing 從 ``request.native`` 取（不 import turn、不帶原文）。"""
    native = request.native
    refs = getattr(native, "utterance_refs", None)
    row = _blank_row()
    row.update(
        decision_id=result.decision_id, request_id=result.request_id, kind=result.kind,
        created_at=now or datetime.now(UTC), role=ROLE_PRIMARY,
        engine=result.engine.tier, proposer_id=result.engine.id, version=result.engine.version,
        outcome=result.outcome, reason_code=result.reason_code, confidence=result.confidence,
        elapsed_ms=elapsed_ms, status=_primary_status(result, spec), fallback=result.fallback,
        candidate_id=getattr(native, "candidate_id", None),
        participant_count=getattr(native, "participant_count", None),
        addressed_hint=getattr(native, "addressed_hint", None),
        utterance_refs=list(refs) if refs is not None else (list(request.text_refs) or None),
        sub_kind=_safe_token(request.facts.get("sub_kind")), trigger=_safe_token(request.trigger),
        namespace=_safe_token(request.subject.get("namespace")),
        snapshot_version=_safe_version(request.snapshot_versions.get("policy")),
    )
    return row


def shadow_row(record: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """``ShadowRecord`` -> 日誌列（duck typing，不 import turn）。"""
    row = _blank_row()
    row.update(
        decision_id=record.decision_id, request_id=record.request_id, kind=record.kind,
        created_at=now or datetime.now(UTC), role=ROLE_SHADOW, primary_decision_id=record.primary_decision_id,
        proposer_id=record.shadow_id, outcome=record.shadow_mode, reason_code=record.shadow_reason,
        elapsed_ms=record.elapsed_ms, status=record.status, agree=record.agree, candidate_id=record.candidate_id,
    )
    return row


def row_to_json(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    created = out.get("created_at")
    if isinstance(created, datetime):
        out["created_at"] = created.isoformat()
    return out


class JsonlSink:
    """附加寫入 JSONL（無 DB 時的離線留存；可直接餵重放工具）。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def write(self, rows: Sequence[dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write(json.dumps(row_to_json(row), ensure_ascii=False) + "\n")


class DecisionLogWriter:
    """非同步決策日誌寫入器：佇列＋單一背景執行緒；``sink=None`` 或 ``enabled=False`` 時完全停用（零成本）。"""

    def __init__(
        self,
        sink: DecisionLogSink | None,
        *,
        enabled: bool = True,
        max_queue: int = 1024,
        batch_size: int = 64,
        flush_interval: float = 0.2,
        metrics: Counter[str] | None = None,
    ) -> None:
        self._sink = sink
        self._enabled = enabled and sink is not None
        self._batch_size = max(1, batch_size)
        self._flush_interval = flush_interval
        self.metrics: Counter[str] = metrics if metrics is not None else Counter()
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=max_queue)
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._closed = False
        self._failing = False
        self._quiet: dict[str, Callable[[DecisionResult], bool]] = {}  # kind -> 靜默抑制判斷（True＝不寫日誌）

    @property
    def enabled(self) -> bool:
        return self._enabled and not self._closed

    # ---- 熱路徑（非阻塞）----

    def on_decision(
        self, request: DecisionRequest, result: DecisionResult, spec: DecisionPointSpec, elapsed_ms: int
    ) -> None:
        if not self.enabled:
            return
        try:
            quiet = self._quiet.get(result.kind)
            if quiet is not None and quiet(result):
                self.metrics[f"{result.kind}.quiet"] += 1  # 靜默的 tick 只計數，不寫列
                return
            self._enqueue(primary_row(request, result, spec, elapsed_ms))
        except Exception:
            self.metrics["decision_log.dropped"] += 1
            logger.exception("組決策日誌列失敗")

    def on_shadow(self, record: Any) -> None:
        if not self.enabled:
            return
        try:
            self._enqueue(shadow_row(record))
        except Exception:
            self.metrics["decision_log.dropped"] += 1
            logger.exception("組 shadow 日誌列失敗")

    def _enqueue(self, row: dict[str, Any]) -> None:
        self._ensure_thread()
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            self.metrics["decision_log.dropped"] += 1

    def attach(self, hub: DecisionHub, kind: str | None = None) -> None:
        """掛到中樞：登記結果觀察者；並串接 engine 的 ``on_shadow``（保留原回呼）。不需改 Host。

        ``kind`` 預設為所有有 shadow 回呼的內建決策點（turn.reply、turn.interject、memory.dream）；指定時只串接該 kind。
        """
        hub.add_observer(self.on_decision)
        for name in (kind,) if kind is not None else ("turn.reply", "turn.interject", "memory.dream"):
            engine = hub.engine(name)
            if engine is not None and callable(getattr(engine, "log_quiet", None)):
                self._quiet[name] = engine.log_quiet
            if engine is not None and hasattr(engine, "on_shadow"):
                self._chain_shadow(engine)

    def _chain_shadow(self, engine: Any) -> None:
        previous: Callable[[Any], None] | None = engine.on_shadow

        def chained(record: Any) -> None:
            if previous is not None:
                previous(record)
            self.on_shadow(record)

        engine.on_shadow = chained

    # ---- 背景寫入 ----

    def _ensure_thread(self) -> None:
        if self._thread is not None:
            return
        with self._start_lock:
            if self._thread is None:
                thread = threading.Thread(target=self._run, name="decision-log-writer", daemon=True)
                self._thread = thread
                thread.start()

    def _run(self) -> None:
        stop = False
        while not stop:
            taken = 1
            first = self._queue.get()
            batch: list[dict[str, Any]] = []
            if first is None:
                stop = True
            else:
                batch.append(first)
            deadline = time.monotonic() + self._flush_interval
            while not stop and len(batch) < self._batch_size:
                try:
                    remaining = deadline - time.monotonic()
                    item = self._queue.get(timeout=remaining) if remaining > 0 else self._queue.get_nowait()
                except queue.Empty:
                    break
                taken += 1
                if item is None:
                    stop = True
                else:
                    batch.append(item)
            if batch:
                self._write(batch)
            for _ in range(taken):
                self._queue.task_done()

    def _write(self, batch: list[dict[str, Any]]) -> None:
        assert self._sink is not None
        try:
            self._sink.write(batch)
        except Exception as exc:  # noqa: BLE001 - sink 失敗只降級
            self.metrics["decision_log.dropped"] += len(batch)
            (logger.debug if self._failing else logger.warning)("決策日誌寫入失敗，丟棄 %d 列：%s", len(batch), exc)
            self._failing = True
        else:
            self.metrics["decision_log.written"] += len(batch)
            self._failing = False

    def flush(self, timeout: float = 5.0) -> bool:
        """等佇列排空（測試與關閉前用）；回傳是否在期限內完成。"""
        end = time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if time.monotonic() >= end:
                return False
            time.sleep(0.005)
        return True

    def close(self, timeout: float = 5.0) -> None:
        """停止接收並盡量排空；sink 卡住時最多等 ``timeout``（背景執行緒是 daemon，不阻擋行程結束）。"""
        if self._closed:
            return
        self._closed = True
        thread = self._thread
        if thread is None:
            return
        try:
            self._queue.put(None, timeout=timeout)
        except queue.Full:
            return
        thread.join(timeout)
