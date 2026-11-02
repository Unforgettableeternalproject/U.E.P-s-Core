"""Console `/tts <文字> [--engine vox|indextts2]`：(a) 子系統隔離——以 Core 的 Speak adapter 直接合成並寫 wav。

不經 Core、不經 Think，只量三段：模型載入（prepare）、首音延遲（stream 起算到第一個 chunk）與總耗時。
console 屬 uep 內部，不直接 import 子系統：stage 一律經 ``uep.stream.adapters.speak_*`` 取得（echo_tts 由 adapter 載入）。

GPU 守門（沿用 N2 的 ``UEP_E2E_ALLOW_GPU``）：TTS 需要 GPU，未設為 ``1`` 時明確提示並結束該指令，不載入任何模型。
權重位置見 ``echo_tts.config.model_paths``：VOX 讀 ``UEP_TTS_MODELS_DIR``（預設 ``~/.uep/models/vox``），
IndexTTS2 讀 ``UEP_TTS_INDEXTTS_DIR``。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

ENGINES = ("vox", "indextts2")
USAGE = "用法：/tts <文字> [--engine vox|indextts2]（合成並寫入 wav，印首音延遲與總耗時；需要 GPU 與 UEP_E2E_ALLOW_GPU=1）"
DEFAULT_OUT_DIR = Path("outputs") / "e2e"

_OPT_RE = re.compile(r"\s--engine\s+(\S+)")


def gpu_allowed(env: dict[str, str] | None = None) -> bool:
    return (env if env is not None else os.environ).get("UEP_E2E_ALLOW_GPU") == "1"


def parse_args(arg: str) -> tuple[str, str | None]:
    """拆出合成文字與 ``--engine``；文字可含空白，前後成對引號會去除。"""
    text = " " + arg.strip()
    m = _OPT_RE.search(text)
    engine = m.group(1).lower() if m else None
    body = _OPT_RE.sub("", text).strip()
    if len(body) >= 2 and body[0] == body[-1] and body[0] in ('"', "'"):
        body = body[1:-1]
    return body, engine


def default_engine() -> str:
    """未指定 ``--engine`` 時取 Core 設定的引擎（``UEP_INFER_TTS_ENGINE``）。"""
    from uep.config import get_settings

    return get_settings().infer_tts_engine


def build_speak_stage(engine: str) -> Any:
    """依引擎建真實 Speak stage（不載模型；``prepare`` 才載）。設定來自 ``UepSettings``（``UEP_INFER_TTS_*``）。"""
    from uep.config import get_settings

    settings = get_settings()
    if engine == "vox":
        from uep.stream.adapters.speak_vox import VoxSpeakStage

        kwargs: dict[str, Any] = {}
        raw = settings.infer_vox_config
        if raw and raw.strip():
            kwargs["backend_config"] = dict(json.loads(raw))
        if settings.infer_tts_language is not None:
            kwargs["language"] = settings.infer_tts_language
        if settings.infer_tts_speed is not None:
            kwargs["speed"] = settings.infer_tts_speed
        return VoxSpeakStage(**kwargs)
    if engine == "indextts2":
        from uep.stream.adapters.speak_indextts import IndexTTS2SpeakStage

        return IndexTTS2SpeakStage(
            device=settings.infer_tts_device, use_fp16=settings.infer_tts_fp16, use_accel=settings.infer_tts_use_accel,
            warmup=settings.infer_tts_warmup, lora_scale=settings.infer_tts_lora_scale,
            speed=settings.infer_tts_speed, language=settings.infer_tts_language,
        )
    raise ValueError(f"未知的 TTS 引擎：{engine}（可用：{', '.join(ENGINES)}）")


def prepare_timeout_s(engine: str) -> float:
    """real 模式 Host 啟動逾時需涵蓋 speak 的 prepare（VOX 240 秒，見 D101）。"""
    from uep.echo.inference_wiring import SPEAK_PREPARE_TIMEOUT_DEFAULTS

    return SPEAK_PREPARE_TIMEOUT_DEFAULTS[engine]


def vram_peak_mib() -> float | None:
    """行程內 torch 的 CUDA 配置峰值（MiB）；沒有 torch 或沒有 CUDA 時回 None。"""
    try:
        import torch

        if not torch.cuda.is_available():
            return None
        return torch.cuda.max_memory_allocated() / (1024 * 1024)
    except Exception:  # noqa: BLE001 - 量測失敗不影響合成結果
        return None


def write_wav(path: Path, pcm: bytes, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)


def run_tts(
    arg: str,
    out: Callable[[str], None],
    *,
    out_dir: Path = DEFAULT_OUT_DIR,
    stage_factory: Callable[[str], Any] = build_speak_stage,
    engine_default: Callable[[], str] = default_engine,
    env: dict[str, str] | None = None,
    vram_probe: Callable[[], float | None] = vram_peak_mib,
) -> None:
    text, engine = parse_args(arg)
    if not text:
        out(USAGE)
        return
    engine = engine or engine_default()
    if engine not in ENGINES:
        out(f"未知的 TTS 引擎：{engine}（可用：{', '.join(ENGINES)}）")
        return
    if not gpu_allowed(env):
        out("TTS 需要 GPU：未設 UEP_E2E_ALLOW_GPU=1，已略過（不載入任何模型）。確認 GPU 空閒後設該環境變數再執行。")
        return

    from uep.console.llm_cmd import run_coro
    from uep.stream.contracts.cancellation import CancellationToken
    from uep.stream.contracts.types import Sentence

    try:
        stage = stage_factory(engine)
    except Exception as exc:  # noqa: BLE001 - 隔離指令顯示失敗原因，不中止 console
        out(f"TTS 後端建立失敗：{type(exc).__name__}：{exc}")
        return

    chunks: list[Any] = []
    marks: dict[str, float] = {}

    async def _go() -> None:
        t0 = time.perf_counter()
        await stage.prepare()
        marks["prepare"] = time.perf_counter() - t0
        sentence = Sentence(text=text, turn_id="tts-cmd", index=0, is_first=True, is_last=True)

        async def _sentences():
            yield sentence

        t1 = time.perf_counter()
        async for chunk in stage.stream(_sentences(), CancellationToken()):
            if "first" not in marks:
                marks["first"] = time.perf_counter() - t1
            chunks.append(chunk)
        marks["total"] = time.perf_counter() - t1

    async def _close() -> None:
        aclose = getattr(stage, "aclose", None)
        if callable(aclose):
            result = aclose()
            if asyncio.iscoroutine(result):
                await result

    out(f"TTS：engine={engine}　文字：{text}")
    try:
        run_coro(_go())
    except Exception as exc:  # noqa: BLE001
        out(f"TTS 合成失敗：{type(exc).__name__}：{exc}")
        return
    finally:
        try:
            run_coro(_close())
        except Exception:  # noqa: BLE001 - 收尾失敗不影響結果顯示
            pass

    if not chunks:
        out(f"（無音訊輸出）載入 {marks.get('prepare', 0.0):.1f} 秒")
        return
    rate = chunks[0].sample_rate
    pcm = b"".join(c.pcm for c in chunks)
    path = Path(out_dir) / f"tts-{engine}-{int(time.time() * 1000)}.wav"
    write_wav(path, pcm, rate)
    audio_s = len(pcm) / 2 / rate if rate else 0.0
    out(f"已寫入：{path}（{audio_s:.2f} 秒，{rate} Hz，{len(chunks)} 個 chunk）")
    rtf = f"（RTF {marks['total'] / audio_s:.2f}）" if audio_s else ""
    out(f"載入 {marks['prepare']:.1f} 秒；首音延遲 {marks['first']:.2f} 秒；合成總耗時 {marks['total']:.2f} 秒{rtf}")
    peak = vram_probe()
    out(f"VRAM 峰值（torch 配置）：{peak:.0f} MiB" if peak is not None else "VRAM 峰值：無法量測（無 CUDA 或 torch）")
