"""P3 S6 推理基準量測腳本（骨架；本輪只寫、不跑）。

會載入 TTS／STT 模型並佔用 GPU，所以預設拒絕執行：必須同時給 ``--run`` 與環境變數 ``UEP_BENCH_ALLOW_GPU=1``。
量測須在 STT／TTS／embedding 同卡負載下做（D90）；跑之前確認 GPU 沒有其他工作（bot、另一份 Host）。

量測項目（皆為毫秒）與判準：
- ``first_audio``：句子送入 SpeakStage 到第一個音訊 chunk（TTS 首音）。
- ``barge_in_to_silence``：取消 token 到 stream 結束（不再產生 chunk）。播放端靜音另在 sink 端量。
- ``dispatch_increment``（D73 (A)）：Core 路徑相對 ES Web 零點的增量，預算 ``EchoOutputConfig.dispatch_budget_ms``
  （暫定 +150）。Core 側以 ``host.dispatch_ms_max`` 與 ``echo.dispatch_over_budget`` 指標讀取。
- ``proposer``（D80）：決策插槽預算 ``DecisionConfig.proposer_budget_ms``（暫定 300）；讀 ``decision.*`` 逾時指標。

零點（``--mode baseline``）：只用 ES stage 直接量（不經 Core、不寫 DB），等同 ES Web 路徑的 stage 層耗時。
Core 路徑（``--mode core``）需要完整的 ``assemble_core(echo=...)``（PostgreSQL、Presence 等）：``run_core_mode`` 留待
下一輪依實機環境補齊，這裡只定義它要回報的欄位。
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import statistics
import sys
import time
from typing import Any

DEFAULT_TEXTS = ("你好，今天天氣真不錯。", "我們來聊聊接下來要做的事情吧。", "好的，那就這樣決定。")


def _summ(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    return {
        "n": len(values), "min": round(ordered[0], 1), "median": round(statistics.median(ordered), 1),
        "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 1), "max": round(ordered[-1], 1),
    }


async def _sentences(texts: tuple[str, ...], turn_id: str):
    from echo_stream.contracts.types import Sentence

    for i, text in enumerate(texts):
        yield Sentence(text=text, turn_id=turn_id, index=i, is_first=i == 0, is_last=i == len(texts) - 1)


async def measure_first_audio(speak: Any, texts: tuple[str, ...], runs: int) -> list[float]:
    from echo_stream.contracts.cancellation import CancellationToken

    out: list[float] = []
    for n in range(runs):
        token = CancellationToken()
        t0 = time.perf_counter()
        async for _chunk in speak.stream(_sentences(texts, f"bench_fa_{n}"), token):
            out.append((time.perf_counter() - t0) * 1000)
            token.cancel()
            break
        await asyncio.sleep(0.5)  # 讓取消的工作緒收尾，避免影響下一次
    return out


async def measure_barge_in(speak: Any, texts: tuple[str, ...], runs: int) -> list[float]:
    """第一個 chunk 到達後取消，量取消到 stream 結束。"""
    from echo_stream.contracts.cancellation import CancellationToken

    out: list[float] = []
    for n in range(runs):
        token = CancellationToken()
        stream = speak.stream(_sentences(texts, f"bench_bi_{n}"), token)
        cancelled_at: float | None = None
        with contextlib.suppress(Exception):  # 取消以例外結束 stream 亦視為靜音
            async for _chunk in stream:
                if cancelled_at is None:
                    cancelled_at = time.perf_counter()
                    token.cancel()
        if cancelled_at is not None:
            out.append((time.perf_counter() - cancelled_at) * 1000)
        await asyncio.sleep(0.5)
    return out


async def run_baseline(args: argparse.Namespace) -> dict[str, Any]:
    from uep.echo.inference_wiring import InferenceConfig, build_inference_wiring

    cfg = InferenceConfig.from_settings()
    wiring = build_inference_wiring(cfg)
    t0 = time.perf_counter()
    await wiring.speak.prepare()  # 載入 + warmup（Host 啟動時才做的那一段）
    prepare_s = time.perf_counter() - t0
    status = wiring.status()
    if not wiring.voice_ready:
        return {"mode": "baseline", "aborted": "speak not ready", "status": status}
    texts = tuple(args.text) or DEFAULT_TEXTS
    result = {
        "mode": "baseline", "speak_prepare_s": round(prepare_s, 1), "status": status,
        "first_audio_ms": _summ(await measure_first_audio(wiring.speak, texts, args.runs)),
        "barge_in_to_silence_ms": _summ(await measure_barge_in(wiring.speak, texts, args.runs)),
    }
    await wiring.speak.aclose()
    return result


async def run_core_mode(args: argparse.Namespace) -> dict[str, Any]:
    """TODO（下一輪）：以 assemble_core(echo=EchoPipelineHost(**wiring.host_kwargs(), ...)) 起完整 Core，
    送固定文字 utterance，回報：首音（turn 提交到 sink 第一個 chunk）、取消到靜音、
    ``host.dispatch_ms_max`` 與 ``host.metrics['echo.dispatch_over_budget']``（對 D73 +150ms）、
    ``decision.*`` 逾時指標（對 D80 300ms）。與 baseline 同環境同負載比較，差值即 Core 增量。"""
    raise NotImplementedError("Core 路徑量測下一輪補齊（需 PostgreSQL 與完整組裝）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("baseline", "core"), default="baseline")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--text", action="append", default=[], help="測試句（可重複）；預設內建三句")
    parser.add_argument("--out", help="輸出 JSON 路徑；預設印到 stdout")
    parser.add_argument("--run", action="store_true", help="確認要載入模型並佔用 GPU")
    args = parser.parse_args(argv)
    if not args.run or os.environ.get("UEP_BENCH_ALLOW_GPU") != "1":
        print("拒絕執行：會載入 GPU 模型。需 --run 且 UEP_BENCH_ALLOW_GPU=1。", file=sys.stderr)
        return 2
    runner = run_baseline if args.mode == "baseline" else run_core_mode
    result = asyncio.run(runner(args))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
