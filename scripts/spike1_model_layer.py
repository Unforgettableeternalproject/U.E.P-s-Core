"""Spike 1 量測腳本：PydanticAI 能否當模型層（Agent 架構提案 §7.4，S1-1～5）。

會呼叫真實 API（OpenAI chat／Responses、本機 Ollama），所以只在明確指示下手動執行：

    $PY scripts/spike1_model_layer.py --only s1,s2,s4,s3 --n 30 --out <結果.json>

離線項目（s3a SDK 開銷、s5 D60）不讀 .env、不打 API：``--only s3a,s5 --n-overhead 100``。

- 憑證讀 repo 根目錄的 ``.env``（``UEP_LLM_*``）；不印出任何金鑰。量測完請刪除 ``.env``。
- Ollama 位址／模型取自 ``OllamaBackend`` 預設值（``http://localhost:11434``、``gemma4:e4b-it-q4_K_M``），可用
  ``UEP_SPIKE1_OLLAMA_URL`` ／ ``UEP_SPIKE1_OLLAMA_MODEL`` 覆寫。
- API 呼叫節制：prompt 很短、max_tokens 很小；腳本會累計並在結尾印出呼叫總數（OpenAI／Ollama 分開）。
- S1-3 基準為現行 ``OpenAIBackend.stream_query``（echo_thought_core），A/B 交錯執行以抵銷網路漂移。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values

from uep.models import CapabilityMissingError, ChatMessage, ModelEvent, ModelRequest
from uep.models.pydantic_ai_port import PydanticAIModelPort
from uep.tools.render import render_tools

CAPABILITY_SET = {"tools": [
    {"tool": "deep_recall", "version": 1, "description": "查詢長期記憶", "variant": None,
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}},
                      "required": ["query"], "additionalProperties": False}},
    {"tool": "member_recall", "version": 1, "description": "查詢成員資料", "variant": None,
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}},
                      "required": ["name"], "additionalProperties": False}},
]}
OLLAMA_URL = os.environ.get("UEP_SPIKE1_OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("UEP_SPIKE1_OLLAMA_MODEL", "gemma4:e4b-it-q4_K_M")

CALLS = {"openai": 0, "ollama": 0}


def _env() -> dict[str, str]:
    values = {k: v for k, v in dotenv_values(ROOT / ".env").items() if v}
    if not values.get("UEP_LLM_API_KEY"):
        sys.exit("缺少 .env 的 UEP_LLM_API_KEY")
    return values


def p90(values: list[float]) -> float:
    """nearest-rank p90。"""
    s = sorted(values)
    return s[max(0, math.ceil(0.9 * len(s)) - 1)]


def pct(values: list[float], q: float) -> float:
    """nearest-rank 百分位。"""
    s = sorted(values)
    return s[max(0, math.ceil(q * len(s)) - 1)]


def histogram(values: list[float], width: float = 100.0) -> dict[str, int]:
    """固定寬度直方圖（預設 100 ms 一格）。"""
    bins: dict[int, int] = {}
    for v in values:
        bins[int(v // width)] = bins.get(int(v // width), 0) + 1
    return {f"{int(k * width)}-{int((k + 1) * width)}ms": bins[k] for k in sorted(bins)}


def stats(values: list[float], precision: int = 1) -> dict[str, Any]:
    return {"n": len(values), "median_ms": round(statistics.median(values), precision),
            "p90_ms": round(p90(values), precision), "p99_ms": round(pct(values, 0.99), precision),
            "min_ms": round(min(values), precision), "max_ms": round(max(values), precision)}


class Cfg:
    def __init__(self) -> None:
        e = _env()
        self.key = e["UEP_LLM_API_KEY"]
        self.project = e.get("UEP_LLM_PROJECT")
        self.org = e.get("UEP_LLM_ORGANIZATION")
        self.model = e.get("UEP_LLM_MODEL", "gpt-6-luna")
        self.base_url = e.get("UEP_LLM_BASE_URL")

    def port(self, dialect: str, **kw: Any) -> PydanticAIModelPort:
        if dialect == "ollama":
            return PydanticAIModelPort("ollama", OLLAMA_MODEL, base_url=OLLAMA_URL.rstrip("/") + "/v1", **kw)
        return PydanticAIModelPort(
            dialect, self.model, api_key=self.key, project=self.project,  # type: ignore[arg-type]
            organization=self.org, base_url=self.base_url, **kw,
        )

    def backend(self, api: str, effort: str, max_tokens: int):
        from echo_thought_core.backends.openai import OpenAIBackend
        return OpenAIBackend(
            model=self.model, api_key=self.key, project=self.project, organization=self.org, base_url=self.base_url,
            api=api, reasoning_effort=effort, max_tokens=max_tokens,
        )


def count(provider: str) -> None:
    CALLS[provider] += 1


async def run_port(port: PydanticAIModelPort, req: ModelRequest, provider: str,
                   on_event=None) -> tuple[list[ModelEvent], list[float]]:
    count(provider)
    events: list[ModelEvent] = []
    stamps: list[float] = []
    t0 = time.perf_counter()
    async for e in port.stream(req):
        events.append(e)
        stamps.append((time.perf_counter() - t0) * 1000)
        if on_event:
            on_event(e)
    return events, stamps


def user_req(attempt: str, text: str, *, max_tokens: int, effort: str | None, tools: bool = True) -> ModelRequest:
    cap = CAPABILITY_SET if tools else {"tools": []}
    return ModelRequest(attempt, [ChatMessage("user", text)], cap, max_output_tokens=max_tokens, reasoning_effort=effort)


# ── S1-1：SDK 執行工具次數 ＝ 0 ───────────────────────────────────────────


async def s1(cfg: Cfg) -> dict[str, Any]:
    out: dict[str, Any] = {}
    prompt = "請呼叫 deep_recall 工具，query 設為「貓」。不要只用文字回答。"
    for dialect, provider in (("openai_chat", "openai"), ("openai_responses", "openai"), ("ollama", "ollama")):
        port = cfg.port(dialect)
        core_exec: list[str] = []  # mock executor：由 Core 端（這支腳本）執行並計數

        def mock_core_executor(tool: str, args: dict) -> str:
            core_exec.append(tool)
            return json.dumps({"items": [{"ref": "m1", "text": "貓是家人"}]}, ensure_ascii=False)

        events, _ = await run_port(port, user_req(f"s1-{dialect}", prompt, max_tokens=300, effort="none"), provider)
        intents = [e for e in events if e.kind == "tool_intent"]
        ids = [(e.call_id, e.tool) for e in intents]
        result_text = ""
        for e in intents:
            if e.args_complete:
                result_text = mock_core_executor(e.tool, dict(e.args or {}))
        # 第二輪：Core 持有 history，帶工具結果回餵（新 run）
        second_text, second_intents = "", 0
        if intents and result_text:
            e0 = intents[0]
            msgs = [
                ChatMessage("user", prompt),
                ChatMessage("assistant", "", tool_calls=({"id": e0.call_id, "name": e0.tool, "args": dict(e0.args or {})},)),
                ChatMessage("tool", result_text, tool_call_id=e0.call_id),
            ]
            ev2, _ = await run_port(
                port, ModelRequest(f"s1b-{dialect}", msgs, CAPABILITY_SET, max_output_tokens=300, reasoning_effort="none"),
                provider,
            )
            second_text = "".join(x.text for x in ev2 if x.kind == "text_delta")
            second_intents = sum(1 for x in ev2 if x.kind == "tool_intent")
            second_finish = ev2[-1].finish_reason
        else:
            second_finish = None
        out[dialect] = {
            "intents": ids, "args": [dict(e.args or {}) for e in intents], "finish": events[-1].finish_reason,
            "core_executions": len(core_exec), "sdk_tool_executions": port.sdk_tool_executions[0],
            "second_round_text": second_text[:60], "second_round_intents": second_intents,
            "second_round_finish": second_finish,
            "pass": bool(intents) and port.sdk_tool_executions[0] == 0,
        }
    return out


# ── S1-2：想／說零混入 ────────────────────────────────────────────────────


def longest_common_substring_len(a: str, b: str, cap: int = 40) -> int:
    """僅供參考輸出：模型打草稿時 thought 可與 text 內容相同，不能拿來判通過／失敗。"""
    best = 0
    for i in range(len(a)):
        for k in range(best + 1, min(cap, len(a) - i) + 1):
            if a[i:i + k] in b:
                best = k
            else:
                break
    return best


_ALLOWED_ROUTES = {("thinking", "thought_delta"), ("text", "text_delta"), ("tool_call", "tool_call_delta")}


async def s2(cfg: Cfg) -> dict[str, Any]:
    """結構判定：每個 delta 的 SDK part 歸屬（port.audit）必須對到固定的事件種類，且沒有被丟棄的 delta。
    字串比對只留作參考輸出。"""
    prompt = "123 乘以 47 等於多少？先想清楚再回答，答案一句話。"
    out: dict[str, Any] = {}
    runs = [
        ("openai_chat", "openai", cfg.port("openai_chat"), "medium"),
        ("openai_responses", "openai", cfg.port("openai_responses", reasoning_summary="auto"), "medium"),
        ("ollama", "ollama", cfg.port("ollama", thinking=True), None),
    ]
    for dialect, provider, port, effort in runs:
        port.audit.clear()
        port.dropped_deltas.clear()
        events, _ = await run_port(port, user_req(f"s2-{dialect}", prompt, max_tokens=1500, effort=effort, tools=False), provider)
        thought = "".join(e.text for e in events if e.kind == "thought_delta")
        text = "".join(e.text for e in events if e.kind == "text_delta")
        kinds = [e.kind for e in events]
        bad_routes = [pair for pair in port.audit if pair not in _ALLOWED_ROUTES]
        n_thinking_src = sum(1 for src, _ in port.audit if src == "thinking")
        n_thought_out = sum(1 for _, kind in port.audit if kind == "thought_delta")
        text_from_thinking = sum(1 for src, kind in port.audit if src == "thinking" and kind == "text_delta")
        out[dialect] = {
            "structural": {
                "delta_routes": {f"{src}->{kind}": sum(1 for pair in port.audit if pair == (src, kind))
                                 for src, kind in sorted(set(port.audit))},
                "thinking_deltas_mapped_to_text": text_from_thinking, "bad_routes": len(bad_routes),
                "dropped_deltas": list(port.dropped_deltas),
                "thinking_src_equals_thought_out": n_thinking_src == n_thought_out,
            },
            "finish": events[-1].finish_reason, "emits_thinking": bool(thought),
            "pass": text_from_thinking == 0 and not bad_routes and not port.dropped_deltas
            and n_thinking_src == n_thought_out,
            "note": "" if thought else "此 provider／設定下不產生 thinking（thought_delta 為空）",
            "reference_only": {  # 字串比對：僅參考
                "thought_chars": len(thought), "text_chars": len(text), "thought_events": kinds.count("thought_delta"),
                "text_events": kinds.count("text_delta"),
                "max_common_substring": longest_common_substring_len(thought, text),
                "thought_head": thought[:80], "text_head": text[:80],
            },
        }
    return out


# ── S1-4：取消 ────────────────────────────────────────────────────────────


async def s4(cfg: Cfg, n: int) -> dict[str, Any]:
    prompt = "從 1 數到 200，每個數字獨立一行，不要加任何其他文字。"
    out: dict[str, Any] = {}
    for dialect, provider in (("openai_chat", "openai"), ("openai_responses", "openai"), ("ollama", "ollama")):
        port = cfg.port(dialect)
        rows = []
        for i in range(n):
            attempt = f"s4-{dialect}-{i}"
            count(provider)
            t0 = time.perf_counter()
            t_cancel = None
            n_text = 0
            late_after_cancel = 0
            last_content = None
            t_end = None
            async for e in port.stream(user_req(attempt, prompt, max_tokens=600, effort="none", tools=False)):
                now = (time.perf_counter() - t0) * 1000
                if e.kind in ("text_delta", "thought_delta"):
                    n_text += 1
                    if t_cancel is not None:
                        late_after_cancel += 1
                    else:
                        last_content = now
                    if t_cancel is None and n_text == 5:
                        port.cancel(attempt)
                        t_cancel = (time.perf_counter() - t0) * 1000
                if e.kind == "finish":
                    t_end = now
                    reason = e.finish_reason
            await asyncio.sleep(1.0)  # 取消後再等 1 秒：沒有任何程式碼在背景補送事件（generator 已結束，這裡確認任務清乾淨）
            rows.append({
                "cancel_to_end_ms": round(t_end - t_cancel, 1) if t_cancel is not None and t_end is not None else None,
                "late_content_events": late_after_cancel, "finish": reason, "n_text_before_cancel": n_text,
                "completed_naturally": t_cancel is None,
            })
        vals = [r["cancel_to_end_ms"] for r in rows if r["cancel_to_end_ms"] is not None]
        out[dialect] = {
            "runs": rows, "cancel_to_end": stats(vals) if vals else None,
            "pass": bool(vals) and max(vals) < 500 and all(r["late_content_events"] == 0 for r in rows),
        }
    return out


# ── S1-5：D60 啟動斷言（離線）─────────────────────────────────────────────


async def s5() -> dict[str, Any]:
    from pydantic_ai.models.function import FunctionModel

    async def _never(messages, info):
        raise AssertionError("D60 未擋下就送出請求")
        yield ""  # pragma: no cover

    port = PydanticAIModelPort("openai_chat", model=FunctionModel(stream_function=_never))
    cases = {}
    for name, req in {
        "builtin_tools": ModelRequest("s5a", [ChatMessage("user", "x")], CAPABILITY_SET, builtin_tools=("web_search",)),
        "malformed_capability_set": ModelRequest("s5b", [ChatMessage("user", "x")], {"tools": [{"tool": "a"}]}),
    }.items():
        try:
            async for _ in port.stream(req):
                pass
            cases[name] = "NOT_BLOCKED"
        except CapabilityMissingError as exc:
            cases[name] = f"blocked:{exc.event.error['code']}"
        except ValueError as exc:
            cases[name] = f"blocked:ValueError({exc.__class__.__name__})"
    original = port._tool_defs
    for name, mut in {"missing_tool": lambda d: d[:1], "extra_tool": lambda d: [*d, d[0].__class__(name="sneak", description="x")]}.items():
        port._tool_defs = lambda r, _o=original, _m=mut: (lambda dd: (_m(dd[0]), dd[1]))(_o(r))  # type: ignore[method-assign]
        try:
            async for _ in port.stream(ModelRequest("s5c", [ChatMessage("user", "x")], CAPABILITY_SET)):
                pass
            cases[name] = "NOT_BLOCKED"
        except CapabilityMissingError as exc:
            cases[name] = f"blocked:{exc.event.error['code']}"
    return {"cases": cases, "api_calls": 0, "pass": all(v.startswith("blocked") for v in cases.values())}


# ── S1-3：首 token 額外延遲（與現行 openai.py 交錯比較）──────────────────


async def first_token_base(backend, text: str) -> tuple[float, float]:
    from echo_thought_core.schemas.message import Message
    t0 = time.perf_counter()
    first_any = None
    async for piece in backend.stream_query([Message(role="user", content=text)], tools=render_tools(CAPABILITY_SET)):
        if first_any is None:
            first_any = (time.perf_counter() - t0) * 1000
        if isinstance(piece, str) and piece:
            return first_any, (time.perf_counter() - t0) * 1000
    return first_any or -1.0, -1.0


async def first_token_port(port: PydanticAIModelPort, text: str, effort: str, max_tokens: int) -> tuple[float, float]:
    t0 = time.perf_counter()
    first_any = None
    gen = port.stream(user_req("s3", text, max_tokens=max_tokens, effort=effort))
    try:
        async for e in gen:
            if first_any is None and e.kind in ("text_delta", "thought_delta", "tool_call_delta"):
                first_any = (time.perf_counter() - t0) * 1000
            if e.kind == "text_delta":
                return first_any or -1.0, (time.perf_counter() - t0) * 1000
    finally:
        await gen.aclose()
    return first_any or -1.0, -1.0


async def drain(agen) -> None:
    await agen.aclose()


async def s3(cfg: Cfg, n: int) -> dict[str, Any]:
    text = "用一句話向我問好。"
    out: dict[str, Any] = {}
    for api, dialect in (("chat", "openai_chat"), ("responses", "openai_responses")):
        effort, max_tokens = "none", 64
        backend = cfg.backend(api, effort, max_tokens)
        port = cfg.port(dialect)
        # warm-up（連線、TLS、DNS）：各一次，不計入
        count("openai")
        await first_token_base(backend, text)
        count("openai")
        await first_token_port(port, text, effort, max_tokens)
        base, new, diffs = [], [], []
        for i in range(n):
            order = ("base", "new") if i % 2 == 0 else ("new", "base")
            got: dict[str, float] = {}
            for who in order:
                count("openai")
                if who == "base":
                    got[who] = (await first_token_base(backend, text))[1]
                else:
                    got[who] = (await first_token_port(port, text, effort, max_tokens))[1]
            base.append(got["base"])
            new.append(got["new"])
            diffs.append(got["new"] - got["base"])
        out[dialect] = {
            "baseline_openai_py": stats(base), "pydantic_ai": stats(new),
            "median_diff_ms": round(statistics.median(new) - statistics.median(base), 1),
            "paired_diff": {"median_ms": round(statistics.median(diffs), 1), "p90_ms": round(p90(diffs), 1)},
            "raw": {"base": [round(x, 1) for x in base], "new": [round(x, 1) for x in new]},
            "distribution": {"base": histogram(base), "new": histogram(new), "paired_diff": histogram(diffs, 50.0)},
            "pass_median_le_50ms": (statistics.median(new) - statistics.median(base)) <= 50.0,
        }
    # Ollama：對照組是直打 OpenAI 相容端點（同一個 /v1 路徑，無 SDK），量 SDK 層本身的額外延遲
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key="ollama", base_url=OLLAMA_URL.rstrip("/") + "/v1")

    async def raw_first_text() -> float:
        t0 = time.perf_counter()
        stream = await client.chat.completions.create(
            model=OLLAMA_MODEL, stream=True, max_completion_tokens=64,
            messages=[{"role": "user", "content": text}], tools=render_tools(CAPABILITY_SET),
        )
        try:
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    return (time.perf_counter() - t0) * 1000
        finally:
            await stream.close()
        return -1.0

    oport = cfg.port("ollama")
    count("ollama")
    await raw_first_text()
    count("ollama")
    await first_token_port(oport, text, "none", 64)
    base, new, diffs = [], [], []
    for i in range(n):
        order = ("base", "new") if i % 2 == 0 else ("new", "base")
        got = {}
        for who in order:
            count("ollama")
            got[who] = await raw_first_text() if who == "base" else (await first_token_port(oport, text, "none", 64))[1]
        base.append(got["base"])
        new.append(got["new"])
        diffs.append(got["new"] - got["base"])
    out["ollama"] = {
        "baseline_raw_openai_compat": stats(base), "pydantic_ai": stats(new),
        "median_diff_ms": round(statistics.median(new) - statistics.median(base), 1),
        "paired_diff": {"median_ms": round(statistics.median(diffs), 1), "p90_ms": round(p90(diffs), 1)},
        "raw": {"base": [round(x, 1) for x in base], "new": [round(x, 1) for x in new]},
        "distribution": {"base": histogram(base), "new": histogram(new), "paired_diff": histogram(diffs, 50.0)},
        "pass_median_le_50ms": (statistics.median(new) - statistics.median(base)) <= 50.0,
        "note": "Ollama 無現行 OpenAI 相容 backend 可比；對照組為直打 /v1 chat（無 SDK）",
    }
    return out


async def s3a(n: int) -> dict[str, Any]:
    """離線 SDK 開銷：FunctionModel（無網路）從 stream() 呼叫到第一個 text_delta 的時間，與整段 stream 結束的時間。"""
    from pydantic_ai.models.function import FunctionModel

    async def fn(messages, info):
        yield "你"
        yield "好"

    port = PydanticAIModelPort("openai_chat", model=FunctionModel(stream_function=fn))
    req = user_req("s3a", "x", max_tokens=16, effort=None)
    first, total = [], []
    for i in range(n + 10):  # 前 10 次暖機不計
        t0 = time.perf_counter()
        t_first = None
        async for e in port.stream(req):
            if t_first is None and e.kind == "text_delta":
                t_first = (time.perf_counter() - t0) * 1000
        t_all = (time.perf_counter() - t0) * 1000
        if i >= 10:
            first.append(t_first or -1.0)
            total.append(t_all)
    return {"first_text_delta": stats(first, 2), "full_stream": stats(total, 2), "api_calls": 0,
            "note": "FunctionModel 無網路；是 SDK 層（含 Agent 啟動與事件佇列）的絕對開銷"}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="s5,s1,s2,s4,s3")
    ap.add_argument("--n", type=int, default=30, help="s3 線上 A/B 次數")
    ap.add_argument("--n-cancel", type=int, default=3)
    ap.add_argument("--n-overhead", type=int, default=100, help="s3a 離線開銷次數（>=100）")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    names = [x.strip() for x in a.only.split(",") if x.strip()]
    cfg = Cfg() if set(names) - {"s3a", "s5"} else None  # 只跑離線項目時不讀 .env
    result: dict[str, Any] = {"model": cfg.model if cfg else None, "ollama_model": OLLAMA_MODEL}
    for name in names:
        t = time.perf_counter()
        if name == "s1":
            result[name] = await s1(cfg)
        elif name == "s2":
            result[name] = await s2(cfg)
        elif name == "s3":
            result[name] = await s3(cfg, a.n)
        elif name == "s4":
            result[name] = await s4(cfg, a.n_cancel)
        elif name == "s3a":
            result[name] = await s3a(a.n_overhead)
        elif name == "s5":
            result[name] = await s5()
        print(f"[{name}] done in {time.perf_counter() - t:.1f}s; api calls so far {CALLS}", flush=True)
    result["api_calls"] = dict(CALLS)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    asyncio.run(main())
