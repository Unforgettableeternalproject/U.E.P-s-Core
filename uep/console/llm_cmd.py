"""Console `/llm <文字>`：(a) 子系統隔離——直接以 Core 的 Think adapter 呼叫真實 LLM，不經 Core、不經記憶。

印出每個切句與其距送出的耗時（首句＝首句延遲），結尾列出總耗時與句數。
設定由 ``UepSettings.think_config()`` 解析（``UEP_LLM_*``）；缺少金鑰時明確報錯，不印任何金鑰內容。
console 屬 uep 內部，LLM 後端一律經 ``uep.stream.adapters.think_llm`` 取得，不直接 import 子系統。
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from typing import Any

from uep.stream.adapters.think_config import ThinkConfig, ThinkConfigError
from uep.stream.contracts.cancellation import CancellationToken
from uep.stream.contracts.types import Utterance

USAGE = "用法：/llm <文字>（直接呼叫 Think 產生句子，印出切句與耗時；需 LLM 金鑰）"


def load_think_config() -> tuple[ThinkConfig, str]:
    """回傳 (ThinkConfig, system_prompt)；來源為 Core 設定（環境變數或 repo 根 ``.env``）。"""
    from uep.config import get_settings

    settings = get_settings()
    return settings.think_config(), settings.infer_system_prompt


def default_stage_factory(config: ThinkConfig, system_prompt: str, tracer: Any) -> Any:
    """建真實 Think stage；缺金鑰時於此拋 ``ThinkConfigError``（不等到第一句話）。"""
    from uep.config import get_settings
    from uep.stream.adapters.think_compress import compression_kwargs
    from uep.stream.adapters.think_llm import LLMThinkStage, build_backend

    backend = build_backend(config=config)
    # 與 Host 路徑共用同一份壓縮接線（開關、缺金鑰退路、視窗預設都一致）
    compress = compression_kwargs(get_settings().compress_config(), config, backend)
    return LLMThinkStage(backend, system_prompt=system_prompt, tracer=tracer, config=config, **compress)


def run_coro(coro: Any) -> Any:
    """在同步 console 內跑 coroutine；若目前執行緒已有 event loop 則改用工作執行緒。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - 轉交呼叫端
            box["error"] = exc

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


async def _close_stage(stage: Any) -> None:
    """在當前 loop 內關閉 stage（優先 ``aclose``，其次 ``close``）；收尾失敗不影響結果顯示。"""
    for name in ("aclose", "close"):
        closer = getattr(stage, name, None)
        if callable(closer):
            try:
                result = closer()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001
                pass
            return


def run_llm(
    arg: str,
    out: Callable[[str], None],
    *,
    config_loader: Callable[[], tuple[ThinkConfig, str]] = load_think_config,
    stage_factory: Callable[[ThinkConfig, str, Any], Any] = default_stage_factory,
) -> None:
    text = arg.strip()
    if not text:
        out(USAGE)
        return
    from uep.stream.tracer import LatencyTracer

    try:
        config, system_prompt = config_loader()
        tracer = LatencyTracer()
        stage = stage_factory(config, system_prompt, tracer)
    except ThinkConfigError as exc:
        out(f"LLM 設定錯誤：{exc}")
        return
    except Exception as exc:  # noqa: BLE001 - 隔離指令顯示失敗原因，不中止 console
        out(f"LLM 後端建立失敗：{type(exc).__name__}：{exc}")
        return

    out(f"LLM：provider={config.provider} model={config.model} api={config.api}")
    utterance = Utterance(text=text)
    t0 = time.perf_counter()
    stamps: list[tuple[float, str]] = []

    async def _go() -> None:
        # 串流與關閉必須在同一個 event loop 內：backend 的 async client（連線池）綁定建立它的 loop，
        # 留到 GC 才 aclose 會排程到已關閉的 loop（Event loop is closed、Task exception never retrieved）
        try:
            async for sentence in stage.stream(utterance, CancellationToken()):
                stamps.append((time.perf_counter() - t0, sentence.text))
        finally:
            await _close_stage(stage)

    try:
        run_coro(_go())
    except Exception as exc:  # noqa: BLE001
        out(f"LLM 呼叫失敗：{type(exc).__name__}：{exc}")
        return
    total = time.perf_counter() - t0
    if not stamps:
        out(f"（無輸出句子）總耗時 {total:.2f} 秒")
        return
    for i, (at, sentence_text) in enumerate(stamps):
        out(f"[{i}] +{at:.2f}s  {sentence_text}")
    out(f"首句 {stamps[0][0]:.2f} 秒；共 {len(stamps)} 句；總耗時 {total:.2f} 秒")
