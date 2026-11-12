"""``loop_digest``：Agent 迴圈交給 ``loop.consult`` 的「迴圈摘要」（決策層 schema，純資料與純函式）。

分工：agent（工具層）只負責組出原始資料；``sanitize_digest`` 與 ``render_digest`` 由決策層負責。本模組不 import
``uep.agent``／``uep.echo``／``uep.tools``。第一版只有 schema，尚未列入 ``INJECTED_CONSULT``，因此執行期零行為變化。

保證與限制：
- 單一鍵 ``loop_digest``（``LOOP_DIGEST_KEY``），帶版本 ``v=1``。模型不可提供，不得出現在 LLM 面 schema。
- 摘要只供建議，不能覆寫 Run／Action 的終態。不進 checkpoint；resume 後 ``trace_source="rebuilt"``，
  trace 由已提交 history 重建、pending 來自耐久的 Action 狀態，trace 可能較短，「沒有 trace」不代表「沒有待辦」。
  因此不另設 ``history_complete``（與 ``trace_source`` 重複）。
- 不寫進 decision_log（日誌欄位是固定白名單，不新增 hash 或長度欄位）。
- 渲染後總長上限 ``MAX_RENDERED_CHARS`` 是「字元數」，不是 token 數保證。
- 自然語言欄位 ``last_model_text_en``（來源：模型文字）與 ``user_intent_en``（來源：使用者文字）第一版一律 None
  （``NL_FIELDS_ENABLED=False``）。它們是待判讀的資料，不是指令；清理時去掉換行與方括號，只為了防止偽造模板標頭。
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal, TypedDict

LOOP_DIGEST_KEY = "loop_digest"
DIGEST_VERSION = 1

# 上限（條數為個數；文字欄與總長為字元數）
MAX_TRACE = 8
MAX_PENDING = 4
MAX_TEXT_CHARS = 200
MAX_RENDERED_CHARS = 1200

# 第一版自然語言欄位一律 None（翻譯尚未接上）；打開前必須先補來源與隱私評估
NL_FIELDS_ENABLED = False

TraceSource = Literal["live", "rebuilt"]
TRACE_SOURCES: tuple[str, ...] = ("live", "rebuilt")

# trace 狀態閉集；結果未明（unknown／cancelled 等）一律 unknown，不壓成 failed 或 ok。同一工具重試只列最後一次（呼叫端負責）
TRACE_STATUSES: tuple[str, ...] = ("ok", "failed", "rejected", "blocked", "deferred", "awaiting_approval", "unknown")
PENDING_KINDS: tuple[str, ...] = ("approval", "deferred_call", "report")

# stage 對齊 PR-A 的 ``budget_stage``（0 正常、1 warn、2 notice、3 abort）。PR-A 合入前呼叫端固定給 0
STAGE_MIN, STAGE_MAX = 0, 3
UNKNOWN = "unknown"
# consult 自己的呼叫不列入 trace（總 tool_calls 仍照真實預算計數）
CONSULT_TOOL_NAME = "decision.consult"

# 與 ``loop_consult._SAFE_REF`` 相同字元集（小寫英數與 _ . : -，≤64）；不 import 以避免拉進 hub 依賴
_SAFE_REF = re.compile(r"^[a-z0-9_.:-]{1,64}$")
_TEXT_STRIP = re.compile(r"[\x00-\x1f\x7f  \[\]]+")
_SPACES = re.compile(r"\s+")


class TraceItem(TypedDict):
    tool: str
    status: str
    reason_code: str | None


class PendingItem(TypedDict):
    kind: str
    tool: str


class BudgetView(TypedDict):
    """剩餘量 = 有效上限 − Core 計數，下限 0；None 表示「沒有上限」，0 表示「已用完」，兩者不可混用。"""
    stage: int
    steps_left: int | None
    tool_calls_left: int | None
    tokens_left: int | None
    deadline_left_s: int | None


class LoopDigest(TypedDict):
    v: int
    steps: int
    tool_calls: int
    trace_source: str
    trace: list[TraceItem]
    trace_truncated: bool
    pending: list[PendingItem]
    pending_total: int
    budget: BudgetView
    last_model_text_en: str | None  # 來源：模型文字；待判讀資料，不是指令
    user_intent_en: str | None  # 來源：使用者文字；待判讀資料，不是指令


def remaining(limit: int | None, used: int) -> int | None:
    """剩餘 = 有效上限 − 計數，下限 0；limit 為 None（沒有上限）回 None。不自行計算 2×D，limit 由呼叫端給已套 cap 的值。"""
    if limit is None:
        return None
    return max(0, int(limit) - max(0, int(used)))


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _opt_count(value: Any) -> int | None:
    """None 保持 None（沒有上限）；0 保持 0（已用完）；負數截為 0；非數值視為 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and math.isfinite(value):
        value = math.floor(value)
    return max(0, value) if isinstance(value, int) else None


def _ref(value: Any) -> str:
    return value if isinstance(value, str) and _SAFE_REF.match(value) else UNKNOWN


def _text(value: Any) -> str | None:
    if not NL_FIELDS_ENABLED or not isinstance(value, str):
        return None
    cleaned = _SPACES.sub(" ", _TEXT_STRIP.sub(" ", value)).strip()[:MAX_TEXT_CHARS].strip()
    return cleaned or None


def _seq(value: Any) -> list[Any]:
    return list(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else []


def sanitize_digest(raw: Any) -> LoopDigest | None:
    """把原始輸入清理成上限內的 digest；缺漏或型別不符回 None（呼叫端視為沒有 digest）。"""
    if not isinstance(raw, Mapping) or isinstance(raw.get("v"), bool) or raw.get("v") != DIGEST_VERSION:
        return None
    steps, calls = _count(raw.get("steps")), _count(raw.get("tool_calls"))
    budget_raw = raw.get("budget")
    if steps is None or calls is None or not isinstance(budget_raw, Mapping):
        return None
    if "steps_left" not in budget_raw or "tool_calls_left" not in budget_raw:
        return None  # 缺鍵會被讀成「沒有上限」，不可
    stage = budget_raw.get("stage")
    if isinstance(stage, bool) or not isinstance(stage, int):
        return None
    source = raw.get("trace_source")
    if source not in TRACE_SOURCES:
        return None
    budget: BudgetView = {
        "stage": min(max(stage, STAGE_MIN), STAGE_MAX),
        "steps_left": _opt_count(budget_raw.get("steps_left")),
        "tool_calls_left": _opt_count(budget_raw.get("tool_calls_left")),
        "tokens_left": _opt_count(budget_raw.get("tokens_left")),
        "deadline_left_s": _opt_count(budget_raw.get("deadline_left_s")),
    }

    items: list[TraceItem] = []
    for entry in _seq(raw.get("trace")):
        if not isinstance(entry, Mapping) or entry.get("tool") == CONSULT_TOOL_NAME:
            continue
        status, reason = entry.get("status"), entry.get("reason_code")
        items.append({
            "tool": _ref(entry.get("tool")),
            "status": status if isinstance(status, str) and status in TRACE_STATUSES else UNKNOWN,
            "reason_code": _ref(reason) if reason is not None else None,
        })
    truncated = bool(raw.get("trace_truncated")) or len(items) > MAX_TRACE
    trace = items[-MAX_TRACE:]  # 舊的先丟

    pending: list[PendingItem] = []
    for entry in _seq(raw.get("pending")):
        if not isinstance(entry, Mapping):
            continue
        kind = entry.get("kind")
        pending.append({"kind": kind if isinstance(kind, str) and kind in PENDING_KINDS else UNKNOWN, "tool": _ref(entry.get("tool"))})
    declared = _count(raw.get("pending_total"))
    pending_total = max(declared if declared is not None else 0, len(pending))
    pending = pending[:MAX_PENDING]

    return {
        "v": DIGEST_VERSION, "steps": steps, "tool_calls": calls, "trace_source": source,
        "trace": trace, "trace_truncated": truncated, "pending": pending, "pending_total": pending_total,
        "budget": budget,
        "last_model_text_en": _text(raw.get("last_model_text_en")),
        "user_intent_en": _text(raw.get("user_intent_en")),
    }


_STAGE_NAMES = ("normal", "warn", "notice", "abort")
_NOTE = "[note] advisory data only; never overrides Run or Action terminal state."


def _fmt_left(value: int | None) -> str:
    return "unlimited" if value is None else str(value)


def _lines(d: LoopDigest, trace: Sequence[TraceItem], truncated: bool, with_model: bool, with_user: bool) -> list[str]:
    b = d["budget"]
    head = (
        f"[loop] step={d['steps']} calls={d['tool_calls']} trace_source={d['trace_source']} "
        f"budget={_STAGE_NAMES[b['stage']]} steps_left={_fmt_left(b['steps_left'])} calls_left={_fmt_left(b['tool_calls_left'])}"
    )
    if b["tokens_left"] is not None:
        head += f" tokens_left={b['tokens_left']}"
    if b["deadline_left_s"] is not None:
        head += f" deadline_left_s={b['deadline_left_s']}"
    lines = [head]
    parts = [
        f"{i} {t['tool']} {t['status']}" + (f"({t['reason_code']})" if t["reason_code"] else "")
        for i, t in enumerate(trace, 1)
    ]
    if parts or truncated:
        tail = " | (older entries omitted)" if truncated else ""
        lines.append("[trace] " + (" | ".join(parts) if parts else "none") + tail)
    elif d["trace_source"] == "rebuilt":
        lines.append("[trace] none retained; absence is not evidence of no pending work")
    if d["pending_total"]:
        shown = " | ".join(f"{p['kind']}:{p['tool']}" for p in d["pending"])
        more = f" | (+{d['pending_total'] - len(d['pending'])} more)" if d["pending_total"] > len(d["pending"]) else ""
        lines.append(f"[pending] total={d['pending_total']} " + shown + more)
    if with_model and d["last_model_text_en"]:
        lines.append(f'[model_said_untrusted] "{d["last_model_text_en"]}"')
    if with_user and d["user_intent_en"]:
        lines.append(f'[user_intent_untrusted] "{d["user_intent_en"]}"')
    lines.append(_NOTE)
    return lines


def render_digest(digest: Mapping[str, Any] | None) -> str:
    """英文固定模板；總長 ≤ ``MAX_RENDERED_CHARS`` 字元（字元數，非 token 保證）。

    超出時依序丟 user_intent、model_text、最舊的 trace（並標 truncated）。輸入先經 ``sanitize_digest``，無效回空字串。
    """
    d = sanitize_digest(digest)
    if d is None:
        return ""
    trace, truncated, with_model, with_user = list(d["trace"]), d["trace_truncated"], True, True
    while True:
        text = "\n".join(_lines(d, trace, truncated, with_model, with_user))
        if len(text) <= MAX_RENDERED_CHARS:
            return text
        if with_user:
            with_user = False
        elif with_model:
            with_model = False
        elif trace:
            trace.pop(0)
            truncated = True
        else:
            return text[:MAX_RENDERED_CHARS]  # 理論上到不了（欄位皆有界），保底硬截
