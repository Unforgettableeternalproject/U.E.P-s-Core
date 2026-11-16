"""Console 對話紀錄（transcript.jsonl）：每輪一行 JSON，事後對照「這一輪實際發生了什麼」。

只存在於 Console（測試模式）：本模組位於 ``uep.console``，只有 Console 的組裝（``rig.build_fake_runtime``）與指令層
（``commands.Console._report_turn``）會用到；正式平台的組裝（``assemble_core``、各 Host）不 import ``uep.console``，
所以不會寫檔，也不會被掛上工具觀測。

資料來源（不在核心流程加呼叫）：
- 決策摘要：``submit_utterance``／``tick`` 回傳的 ``IntakeResult``（ticket、proposal）。
- 各段耗時：``LatencyTracer`` 既有的 ``segments_ms``（同 latency.jsonl，不重算）。
- 工具呼叫：DB 的 ``action_intents``／``receipts`` 提供名稱、狀態與錯誤碼；參數、結果文字與耗時只存在記憶體
  （D33：結果原文不進 receipt／DB，``result_ref`` 只是 ``blob_<action_id>`` 的標籤，不對應 ``payload_blobs`` 資料列），
  由 ``ToolCallTap`` 包住 ``TaskManager`` 的 executor 在 ``execute`` 前後記錄。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import OrderedDict
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select

from uep.tasks.models import ActionIntentRow, ReceiptRow, RunRow, TaskRow

logger = logging.getLogger(__name__)

TRANSCRIPT_LOG_ENV = "UEP_CONSOLE_TRANSCRIPT_LOG"
"""對話紀錄 JSONL 的路徑覆寫；未設＝``<session_dir>/transcript.jsonl``，``off``／``0``／``false``／``none``＝關閉。"""
TRANSCRIPT_LOG_OFF = ("off", "0", "false", "none")

TOOL_TAP_MAX = 500  # 記憶體只留最近這麼多筆工具呼叫
PARAM_STR_MAX = 300
PARAM_LIST_MAX = 20
RESULT_SUMMARY_MAX = 600
SEARCH_HITS_MAX = 5

_SEARCH_FAILURE = re.compile(r"（([^（）]+)）")


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"…（共 {len(text)} 字）"


def clip_value(value: Any, depth: int = 0) -> Any:
    """把參數壓成可寫進 JSON 的短值：長字串截斷、長清單截斷、過深或不認得的型別轉字串。"""
    if isinstance(value, str):
        return clip(value, PARAM_STR_MAX)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= 4:
        return clip(str(value), PARAM_STR_MAX)
    if isinstance(value, Mapping):
        return {str(k): clip_value(v, depth + 1) for k, v in list(value.items())[:PARAM_LIST_MAX]}
    if isinstance(value, (list, tuple)):
        items = [clip_value(v, depth + 1) for v in list(value)[:PARAM_LIST_MAX]]
        if len(value) > PARAM_LIST_MAX:
            items.append(f"…（共 {len(value)} 項）")
        return items
    return clip(str(value), PARAM_STR_MAX)


class ToolCallTap:
    """包住 ``TaskManager`` 的 executor，記錄每次 ``execute`` 的參數、結果文字與耗時（只在 Console 組裝時安裝）。

    不改變 executor 的行為：``execute`` 前後各取一次時間，其餘屬性（``lookup_receipt``、``cancel``…）一律轉給原物件；
    記錄失敗只記 debug，不影響呼叫。"""

    def __init__(self) -> None:
        self.records: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def install(self, tasks: Any) -> int:
        """把 ``tasks`` 的每個 executor 換成包裝；回傳包了幾個。找不到 executor 表時不動（回 0）。"""
        table = getattr(tasks, "_executors", None)
        if not isinstance(table, dict):
            return 0
        for name, executor in list(table.items()):
            if not isinstance(executor, _TappedExecutor):
                table[name] = _TappedExecutor(executor, self)
        return len(table)

    def record(self, executor: Any, call: Any, receipt: Any, elapsed_ms: float, error: str | None) -> None:
        try:
            text = None
            cache = getattr(executor, "cache", None)
            if cache is not None and hasattr(cache, "text"):
                text = cache.text(call.action_id)
            entry = {
                "tool": call.tool, "params": clip_value(dict(call.params) if isinstance(call.params, Mapping) else {}),
                "elapsed_ms": round(elapsed_ms, 1), "result_text": text, "raised": error,
            }
            self.records[call.action_id] = entry
            while len(self.records) > TOOL_TAP_MAX:
                self.records.popitem(last=False)
        except Exception as exc:  # noqa: BLE001 - 觀測失敗不能影響工具呼叫
            logger.debug("工具呼叫記錄失敗：%s", type(exc).__name__)


class _TappedExecutor:
    def __init__(self, inner: Any, tap: ToolCallTap) -> None:
        self._inner = inner
        self._tap = tap

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def execute(self, call: Any) -> Any:
        started = time.perf_counter()
        try:
            receipt = self._inner.execute(call)
        except BaseException as exc:
            self._tap.record(self._inner, call, None, (time.perf_counter() - started) * 1000, type(exc).__name__)
            raise
        self._tap.record(self._inner, call, receipt, (time.perf_counter() - started) * 1000, None)
        return receipt


def _search_detail(params: Mapping[str, Any], text: str | None) -> dict[str, Any]:
    """web.search 的查詢、採用的 provider、各 provider 的嘗試結果與命中筆數（取自結果文字，不另外查）。"""
    detail: dict[str, Any] = {"query": params.get("query"), "provider": None, "attempts": [], "hit_count": 0}
    if not text:
        return detail
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict) and "provider" in data:
        results = data.get("results") if isinstance(data.get("results"), list) else []
        detail.update(
            provider=data.get("provider"), fallback_used=data.get("fallback_used"),
            attempts=data.get("attempts") or [], hit_count=len(results),
            hits=[
                {"title": clip(str(r.get("title", "")), 80), "url": clip(str(r.get("url", "")), 200)}
                for r in results[:SEARCH_HITS_MAX] if isinstance(r, dict)
            ],
        )
        return detail
    found = _SEARCH_FAILURE.search(text)  # 失敗訊息：「…失敗或沒有結果（a:blocked、b:empty）。」
    if found:
        for part in found.group(1).split("、"):
            provider, _, outcome = part.partition(":")
            if provider and outcome:
                detail["attempts"].append({"provider": provider, "outcome": outcome})
    return detail


def _result_summary(tool: str, text: str | None, search: dict[str, Any] | None) -> str | None:
    if search is not None:
        attempts = ",".join(f"{a.get('provider')}:{a.get('outcome')}" for a in search["attempts"] if isinstance(a, dict))
        return f"provider={search['provider']} hits={search['hit_count']} attempts=[{attempts}]"
    return None if text is None else clip(text, RESULT_SUMMARY_MAX)


def collect_tool_calls(session_factory: Any, turn_id: str, tap: ToolCallTap | None) -> list[dict[str, Any]]:
    """該 turn 的工具呼叫（依建立順序）：DB 給名稱／狀態／錯誤碼，``tap`` 補參數、結果摘要與耗時。"""
    with session_factory() as s:
        tasks = s.scalars(select(TaskRow).where(or_(
            TaskRow.origin_ref["ref"].astext == turn_id, TaskRow.origin_ref["turn_id"].astext == turn_id,
        )))
        rows: list[tuple[ActionIntentRow, ReceiptRow | None]] = []
        for task in tasks:
            for run in s.scalars(select(RunRow).where(RunRow.task_id == task.task_id).order_by(RunRow.attempt)):
                for action in s.scalars(
                    select(ActionIntentRow).where(ActionIntentRow.run_id == run.run_id).order_by(ActionIntentRow.created_at)
                ):
                    rows.append((action, s.scalar(select(ReceiptRow).where(ReceiptRow.action_id == action.action_id))))
        snapshot = [
            (a.action_id, a.tool, a.status, a.side_effect_class,
             None if r is None else (r.succeeded, r.error_code, r.result_ref, (r.meta or {}).get("status")))
            for a, r in rows
        ]
    calls: list[dict[str, Any]] = []
    for action_id, tool, status, side_effect, receipt in snapshot:
        seen = tap.records.get(action_id) if tap is not None else None
        params = seen["params"] if seen else None
        text = seen["result_text"] if seen else None
        search = _search_detail(params or {}, text) if tool == "web.search" and seen else None
        entry: dict[str, Any] = {
            "action_id": action_id, "tool": tool, "status": status, "side_effect": side_effect,
            "ok": None if receipt is None else receipt[0],
            "error_code": None if receipt is None else receipt[1],
            "receipt_status": None if receipt is None else receipt[3],
            "result_ref": None if receipt is None else receipt[2],
            "params": params, "result_summary": _result_summary(tool, text, search),
            "elapsed_ms": seen["elapsed_ms"] if seen else None,
        }
        if seen and seen["raised"]:
            entry["raised"] = seen["raised"]
        if search is not None:
            entry["search"] = search
        calls.append(entry)
    return calls


def build_record(
    *, turn_id: str | None, terminal: str | None, source: str, text: str, fact: Any, result: Any,
    reply: str | None, system_messages: list[str], tools: list[dict[str, Any]], trace: Any, events: list[str] | None = None,
) -> dict[str, Any]:
    """組出一輪的紀錄（純函式；欄位說明見 Core 驗收 Console 使用指南）。"""
    ticket = getattr(result, "ticket", None)
    proposal = getattr(result, "proposal", None)
    stt = None
    if source != "say":
        stt = {k: getattr(fact, k, None) for k in ("language", "confidence", "duration_ms")}
    reply_mode = getattr(ticket, "reply_mode", None) or getattr(getattr(proposal, "reply_mode", None), "value", None)
    return {
        "ts": datetime.now(UTC).isoformat(),
        "turn_id": turn_id,
        "terminal": terminal,
        "input": {"text": text, "source": source, "stt": stt},
        "decision": {
            "opened": ticket is not None,
            "reason_code": getattr(result, "reason_code", None),
            "reply_mode": reply_mode,
            "proposer": getattr(proposal, "proposer_id", None),
            "proposer_version": getattr(proposal, "proposer_version", None),
            "proposal_reason_code": getattr(proposal, "reason_code", None),
            "addressed": getattr(proposal, "addressed", None),
            "confidence": getattr(proposal, "confidence", None),
            "degraded": getattr(ticket, "degraded", None),
        },
        "route": getattr(proposal, "route", None),
        "tools": tools,
        "reply": reply,
        "system_messages": system_messages,
        "timings": None if trace is None else {
            "phase": trace.phase.value, "cancel_reason": trace.cancel_reason, "error": trace.error,
            "segments_ms": {k: (round(v, 1) if v is not None else None) for k, v in trace.segments_ms().items()},
        },
        "events": events,
    }


def transcript_path(session_dir: Path | str) -> Path | None:
    """依環境變數決定寫入路徑；關閉時回 None。"""
    raw = os.environ.get(TRANSCRIPT_LOG_ENV, "").strip()
    if raw.lower() in TRANSCRIPT_LOG_OFF:
        return None
    return Path(raw) if raw else Path(session_dir) / "transcript.jsonl"


def append_record(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
