"""Spike 1 量測腳本：PydanticAI 能否當模型層（Agent 架構提案 §7.4，S1-1～5）。

會呼叫真實 API（OpenAI chat／Responses、本機 Ollama），所以只在明確指示下手動執行：

    $PY scripts/spike1_model_layer.py --only s5,s1,s2,s4,s3 --n 10 --out <結果.json>

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


def stats(values: list[float]) -> dict[str, float]:
    return {"n": len(values), "median_ms": round(statistics.median(values), 1), "p90_ms": round(p90(values), 1),
            "min_ms": round(min(values), 1), "max_ms": round(max(values), 1)}


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
    best = 0
    for i in range(len(a)):
        for k in range(best + 1, min(cap, len(a) - i) + 1):
            if a[i:i + k] in b:
                best = k
            else:
                break
    return best


async def s2(cfg: Cfg) -> dict[str, Any]:
    prompt = "123 乘以 47 等於多少？先想清楚再回答，答案一句話。"
    out: dict[str, Any] = {}
    runs = [
        ("openai_chat", "openai", cfg.port("openai_chat"), "medium"),
        ("openai_responses", "openai", cfg.port("openai_responses", reasoning_summary="auto"), "medium"),
        ("ollama", "ollama", cfg.port("ollama", thinking=True), None),
    ]
    for dialect, provider, port, effort in runs:
        events, _ = await run_port(port, user_req(f"s2-{dialect}", prompt, max_tokens=1500, effort=effort, tools=False), provider)
        thought = "".join(e.text for e in events if e.kind == "thought_delta")
        text = "".join(e.text for e in events if e.kind == "text_delta")
        kinds = [e.kind for e in events]
        first_text = kinds.index("text_delta") if "text_delta" in kinds else None
        last_thought = max((i for i, k in enumerate(kinds) if k == "thought_delta"), default=None)
        overlap = longest_common_substring_len(thought, text) if thought and text else 0
        out[dialect] = {
            "thought_chars": len(thought), "text_chars": len(text), "thought_events": kinds.count("thought_delta"),
            "text_events": kinds.count("text_delta"), "finish": events[-1].finish_reason,
            "thought_before_text": (last_thought is None or first_text is None or last_thought < first_text),
            "max_common_substring": overlap, "thought_head": thought[:80], "text_head": text[:80],
            "emits_thinking": bool(thought),
            # 混入判定：thought 的開頭不得出現在 text 串流；part 型別決定分流，事件序上 thought 全部先於 text。
            # max_common_substring 僅供參考：模型在思考中草擬/引用最終答案屬內容重複，不是串流混入。
            "thought_leaked_into_text": bool(thought) and thought[:24] in text,
            "pass": not (bool(thought) and thought[:24] in text)
            and (last_thought is None or first_text is None or last_thought < first_text),
            "note": "" if thought else "此 provider／設定下不產生 thinking（thought_delta 為空）",
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


async def s5(cfg: Cfg) -> dict[str, Any]:
    port = cfg.port("openai_chat")
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
        "pass_median_le_50ms": (statistics.median(new) - statistics.median(base)) <= 50.0,
        "note": "Ollama 無現行 OpenAI 相容 backend 可比；對照組為直打 /v1 chat（無 SDK）",
    }
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="s5,s1,s2,s4,s3")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--n-cancel", type=int, default=3)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    cfg = Cfg()
    result: dict[str, Any] = {"model": cfg.model, "ollama_model": OLLAMA_MODEL}
    for name in a.only.split(","):
        name = name.strip()
        t = time.perf_counter()
        if name == "s1":
            result[name] = await s1(cfg)
        elif name == "s2":
            result[name] = await s2(cfg)
        elif name == "s3":
            result[name] = await s3(cfg, a.n)
        elif name == "s4":
            result[name] = await s4(cfg, a.n_cancel)
        elif name == "s5":
            result[name] = await s5(cfg)
        print(f"[{name}] done in {time.perf_counter() - t:.1f}s; api calls so far {CALLS}", flush=True)
    result["api_calls"] = dict(CALLS)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    asyncio.run(main())
