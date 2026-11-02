"""Console `/stt <wav>`：(a) 子系統隔離——直接以 Core 的 STT adapter＋echo_stt ASR 辨識 wav。

不經 Core、不經 VAD／斷句（D103：VAD 與 turn 判定屬 ``uep/stream/audio``，echo_stt 只負責 ASR），
只量「載入模型」與「辨識」兩段耗時。console 屬 uep 內部，不直接 import 子系統：
backend 一律經 ``uep.stream.adapters.input_stt.EchoSttBackend`` 取得。

GPU 守門（沿用 N2 的 ``UEP_E2E_ALLOW_GPU``）：未設為 ``1`` 時強制 CPU 並改用小模型（預設 ``base``），
避免隔離指令意外佔用 GPU；設了則依 ``subsystems/echo_stt/configs/stt_config.yaml``（device=auto、large-v3-turbo）。
"""
from __future__ import annotations

import os
import re
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

STT_SAMPLE_RATE = 16000
CPU_DEFAULT_MODEL = "base"
USAGE = "用法：/stt <wav 路徑> [--model <faster-whisper 模型名>] [--lang <語言碼>]"

_OPT_RE = re.compile(r"\s--(model|lang)\s+(\S+)")


def gpu_allowed(env: dict[str, str] | None = None) -> bool:
    return (env if env is not None else os.environ).get("UEP_E2E_ALLOW_GPU") == "1"


def parse_args(arg: str) -> tuple[str, dict[str, str]]:
    """拆出 wav 路徑（可加引號、可含空白）與 ``--model``／``--lang`` 選項。"""
    text = " " + arg.strip()
    opts = {m.group(1): m.group(2) for m in _OPT_RE.finditer(text)}
    path = _OPT_RE.sub("", text).strip().strip('"')
    return path, opts


def load_wav_16k_mono(path: Path) -> tuple[bytes, float]:
    """讀 PCM16 wav，轉成 16 kHz 單聲道 int16 bytes；回傳 (pcm, 原始秒數)。"""
    import numpy as np

    with wave.open(str(path), "rb") as w:
        channels, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(frames)
    if width != 2:
        raise ValueError(f"只支援 16-bit PCM wav（此檔為 {width * 8}-bit）")
    seconds = frames / float(rate) if rate else 0.0
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    if channels > 1:
        data = data[: len(data) // channels * channels].reshape(-1, channels).mean(axis=1)
    if rate != STT_SAMPLE_RATE and len(data) > 1:
        # 線性內插重採樣；隔離指令用，品質不是重點（正式路徑的重採樣屬 Frontend Adapter）
        n_out = int(round(len(data) * STT_SAMPLE_RATE / rate))
        data = np.interp(np.linspace(0, len(data) - 1, n_out), np.arange(len(data)), data)
    return np.clip(data, -32768, 32767).astype(np.int16).tobytes(), seconds


def default_backend_factory(**kwargs: Any) -> Any:
    from uep.stream.adapters.input_stt import EchoSttBackend

    return EchoSttBackend(**kwargs)


def run_stt(
    arg: str,
    out: Callable[[str], None],
    *,
    backend_factory: Callable[..., Any] = default_backend_factory,
    env: dict[str, str] | None = None,
) -> None:
    path_text, opts = parse_args(arg)
    if not path_text:
        out(USAGE)
        return
    path = Path(path_text)
    try:
        pcm, seconds = load_wav_16k_mono(path)
    except (OSError, wave.Error, EOFError, ValueError) as exc:
        out(f"無法讀取 wav：{path}（{type(exc).__name__}：{exc}）")
        return
    except ImportError as exc:
        out(f"缺少 numpy（需 inference 環境）：{exc}")
        return

    if gpu_allowed(env):
        device, model = None, opts.get("model")
        out("STT 設定：依 stt_config.yaml（UEP_E2E_ALLOW_GPU=1，可能使用 GPU）。")
    else:
        device, model = "cpu", opts.get("model") or CPU_DEFAULT_MODEL
        out(
            f"STT 設定：強制 CPU、模型 {model}（未設 UEP_E2E_ALLOW_GPU=1；"
            "要用 GPU 與設定檔預設模型請先設該環境變數）。"
        )

    t0 = time.perf_counter()
    try:
        backend = backend_factory(language=opts.get("lang"), device=device, model_size=model)
    except Exception as exc:  # noqa: BLE001 - 隔離指令顯示失敗原因，不中止 console
        out(f"STT 後端載入失敗：{type(exc).__name__}：{exc}")
        return
    load_s = time.perf_counter() - t0
    try:
        t1 = time.perf_counter()
        result = backend.transcribe(pcm, STT_SAMPLE_RATE)
        run_s = time.perf_counter() - t1
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()
    if result is None or not result.text.strip():
        out("辨識結果：（空：無可用轉錄，可能被幻覺過濾或為純雜訊）")
    else:
        out(f"辨識結果：{result.text.strip()}")
        out(f"語言：{result.language}　信心：{result.confidence:.2f}")
    rtf = run_s / seconds if seconds else 0.0
    out(f"音檔 {seconds:.1f} 秒；載入 {load_s:.1f} 秒；辨識 {run_s:.2f} 秒（RTF {rtf:.2f}）")
