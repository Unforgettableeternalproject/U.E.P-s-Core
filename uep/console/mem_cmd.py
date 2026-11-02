"""Console `/mem embed <文字>`：量 query embedding 延遲（N5，§5.5 A5）。

走與記憶檢索同一條 embedding 路徑（``EpisodicMemory.generate_query_embedding``，經 ``uep.echo.memory_wiring.QueryEmbedder``），
但不碰任何記憶資料（暫存目錄）。量四段：載入模型（第一次呼叫含載入）、載入後首查、穩態 N 次的 p50／p95／max，
並對照 Ollama 路徑的 2.0 秒查詢上限（``EMBEDDING_QUERY_TIMEOUT``）給結論。

模型與後端取 ``UepSettings``（``UEP_MEMORY_EMBEDDING_*``，預設 sentence-transformers／BAAI/bge-m3）。預設 CPU；``--device cuda``
需 ``UEP_E2E_ALLOW_GPU=1``（先確認 bot 未執行：``botctl.py status``），未設時提示並結束，不載入模型。
"""
from __future__ import annotations

import os
import re
import tempfile
import time
from collections.abc import Callable
from typing import Any, Protocol

USAGE = "用法：/mem embed <文字> [--device cpu|cuda] [--n 次數]（量 query embedding 載入／首查／p50／p95；cuda 需 UEP_E2E_ALLOW_GPU=1）"
DEFAULT_RUNS = 30
LIMIT_S = 2.0
"""Ollama 路徑的 query embedding 逾時上限（EMBEDDING_QUERY_TIMEOUT 預設 2 秒）；用來對照。"""
DEVICES = ("cpu", "cuda")

_OPT = {"device": re.compile(r"\s--device\s+(\S+)"), "n": re.compile(r"\s--n\s+(\d+)")}


class Embedder(Protocol):
    def embed(self, text: str) -> Any: ...


def parse_args(arg: str) -> tuple[str, str, int]:
    """回傳 (文字, device, 次數)；文字前後成對引號會去除。"""
    text = " " + arg.strip()
    device, runs = "cpu", DEFAULT_RUNS
    m = _OPT["device"].search(text)
    if m:
        device = m.group(1).lower()
    text = _OPT["device"].sub("", text)
    m = _OPT["n"].search(text)
    if m:
        runs = int(m.group(1))
    body = _OPT["n"].sub("", text).strip()
    if len(body) >= 2 and body[0] == body[-1] and body[0] in ('"', "'"):
        body = body[1:-1]
    return body, device, runs


def percentile(values: list[float], pct: float) -> float:
    """最近秩法百分位（values 非空）。"""
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * pct // 100))  # ceil
    return ordered[int(rank) - 1]


def build_embedder(device: str) -> Embedder:
    """依 ``UepSettings`` 建真 embedder（暫存目錄，不碰記憶資料）；模型在第一次 ``embed`` 才載入。"""
    from uep.config import get_settings
    from uep.echo.memory_wiring import QueryEmbedder

    settings = get_settings()
    tmp = tempfile.mkdtemp(prefix="uep-mem-embed-")
    return QueryEmbedder(
        tmp, backend=settings.memory_embedding_backend or "sentence-transformers",
        model=settings.memory_embedding_model or "BAAI/bge-m3", device=device,
    )


def run_mem_embed(
    arg: str,
    out: Callable[[str], None],
    *,
    embedder_factory: Callable[[str], Embedder] = build_embedder,
    env: dict[str, str] | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, float] | None:
    """執行量測並印結果；回傳數字（測試用），無法量測時回 None。"""
    text, device, runs = parse_args(arg)
    if not text:
        out(USAGE)
        return None
    if device not in DEVICES:
        out(f"未知的 device：{device}（可用：{', '.join(DEVICES)}）")
        return None
    if runs < 1:
        out("--n 必須 >= 1")
        return None
    if device == "cuda" and (env if env is not None else os.environ).get("UEP_E2E_ALLOW_GPU") != "1":
        out("cuda 量測需要 GPU：未設 UEP_E2E_ALLOW_GPU=1，已略過（不載入模型）。先用 botctl.py status 確認 bot 未執行再設。")
        return None
    try:
        embedder = embedder_factory(device)
        t0 = clock()
        embedder.embed("warmup")
        load_s = clock() - t0
        t1 = clock()
        vec = embedder.embed(text)
        first_s = clock() - t1
        samples: list[float] = []
        for i in range(runs):
            t = clock()
            embedder.embed(f"{text} {i}")  # 加序號避免任何層級的快取讓穩態失真
            samples.append(clock() - t)
    except Exception as exc:  # noqa: BLE001 - 量測指令顯示失敗原因，不中止 console
        out(f"embedding 量測失敗：{type(exc).__name__}：{exc}")
        return None
    p50, p95, worst = percentile(samples, 50), percentile(samples, 95), max(samples)
    dim = len(vec)
    out(f"embedding：device={device}　維度={dim}　文字：{text}")
    out(f"載入（首次呼叫含載模型）{load_s:.2f} 秒；載入後首查 {first_s * 1000:.0f} ms")
    out(f"穩態 {runs} 次：p50 {p50 * 1000:.1f} ms／p95 {p95 * 1000:.1f} ms／max {worst * 1000:.1f} ms")
    verdict = "低於" if p95 < LIMIT_S else "超過"
    out(f"對照 Ollama 查詢上限 {LIMIT_S:.1f} 秒：穩態 p95 {verdict}上限（首查{'低於' if first_s < LIMIT_S else '超過'}上限）")
    return {"load_s": load_s, "first_s": first_s, "p50_s": p50, "p95_s": p95, "max_s": worst, "dim": float(dim)}
