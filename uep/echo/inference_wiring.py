"""推理 Host 真 stage 組裝入口（P3 S6；D70、D90）：STT／TTS／Think 三個 ES stage 的建構與降級守衛。

與 ``memory_wiring.py`` 同層：這裡是 uep 內接觸 ES adapter 與 TTS／STT／Session 子系統的地方，所有 import 皆延遲，
沒有安裝推理依賴的環境仍可 import 本模組（測試以注入的 fake backend 組裝）。

原則：
- **建構不載模型**。重載入全在 Host 啟動的 ``_startup``（``prepare``）：TTS backend 在這裡預建**一次**
  （``IndexTTS2Backend`` 的建構子只存路徑與旗標，``load()`` 才載權重），注入 ``IndexTTS2SpeakStage``，
  所有 turn 共用同一個 backend；``warmup`` 預設開，在 ``prepare`` 內做（Host 啟動時，不在首輪）。
- **子系統路徑由設定明確給**（``UEP_INFER_{TTS,STT,SESSION}_REPO``），不猜同層目錄：ES 的 ``subsystem_path``
  同層猜測以 ES 自己的位置為基準，非 editable 安裝後會落到 site-packages 之上而找不到。
- **降級（設計文件提案 §16.2）**：``prepare`` 失敗或逾時時，不讓整個 Host 啟動失敗。守衛只包住 stage 實例的
  ``prepare``（以及降級後的 ``stream``／``open``），不改 ``EchoPipelineHost``：
  - speak：標記語音 not ready；之後的輪 ``stream`` 消耗句子、不產音訊（ES 判 ``no_audio``，文字照常 commit），
    不再重試載入（避免每輪都拖慢）。
  - think：標記 not ready；之後的輪 ``stream`` 立即以 ``ThinkNotReady`` 失敗（輪 FAILED，Host 與記憶、Presence 照常）。
  - input：標記 not ready；``open`` 立即失敗，由 Host 既有的輸入重啟／health ``unavailable`` 處理（``started`` 為 False，
    直接 ``submit_utterance`` 的文字路徑仍可用）。
  每個結果都有 log 與指標（``echo.<stage>_prepare_ok|failed|timeout``、``echo.<stage>_not_ready_turns``）。
  ``degrade_on_failure=False`` 時沿用原行為（例外照常上拋，Host 啟動失敗）。
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from uep.echo.metrics import Metrics

if TYPE_CHECKING:
    from uep.config import UepSettings

logger = logging.getLogger("uep.echo.inference_wiring")

STT_MODES = ("multichannel", "single")


class InferenceWiringError(RuntimeError):
    """推理組裝設定不完整（缺子系統路徑、未知模式）：fail-closed，不猜預設。"""


class StageNotReady(RuntimeError):
    """stage 的 prepare 失敗或逾時，之後的使用一律明確失敗。"""


@dataclass(frozen=True)
class InferenceConfig:
    tts_repo: Path | None = None
    stt_repo: Path | None = None
    session_repo: Path | None = None
    tts_device: str = "cuda"
    tts_fp16: bool = True
    tts_use_accel: bool = False
    tts_warmup: bool = True
    tts_lora_scale: float | None = None
    tts_speed: float | None = None
    tts_language: str | None = None
    stt_mode: str = "multichannel"
    stt_language: str | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    system_prompt: str = ""
    speak_prepare_timeout_s: float = 40.0
    think_prepare_timeout_s: float = 8.0
    input_prepare_timeout_s: float = 10.0
    degrade_on_failure: bool = True

    def __post_init__(self) -> None:
        if self.stt_mode not in STT_MODES:
            raise InferenceWiringError(f"stt_mode 必須是 {STT_MODES} 之一：{self.stt_mode!r}")
        for name in ("speak_prepare_timeout_s", "think_prepare_timeout_s", "input_prepare_timeout_s"):
            if getattr(self, name) <= 0:
                raise InferenceWiringError(f"{name} 必須為正數")

    @classmethod
    def from_settings(cls, settings: UepSettings | None = None) -> InferenceConfig:
        if settings is None:
            from uep.config import get_settings

            settings = get_settings()
        return cls(
            tts_repo=settings.infer_tts_repo, stt_repo=settings.infer_stt_repo,
            session_repo=settings.infer_session_repo, tts_device=settings.infer_tts_device,
            tts_fp16=settings.infer_tts_fp16, tts_use_accel=settings.infer_tts_use_accel,
            tts_warmup=settings.infer_tts_warmup, tts_lora_scale=settings.infer_tts_lora_scale,
            tts_speed=settings.infer_tts_speed, tts_language=settings.infer_tts_language,
            stt_mode=settings.infer_stt_mode, stt_language=settings.infer_stt_language,
            llm_provider=settings.infer_llm_provider, llm_model=settings.infer_llm_model,
            system_prompt=settings.infer_system_prompt,
            speak_prepare_timeout_s=settings.infer_speak_prepare_timeout_s,
            think_prepare_timeout_s=settings.infer_think_prepare_timeout_s,
            input_prepare_timeout_s=settings.infer_input_prepare_timeout_s,
            degrade_on_failure=settings.infer_degrade_on_failure,
        )


@dataclass
class StageReadiness:
    """單一 stage 的 prepare 結果（None＝尚未 prepare）。"""

    name: str
    ready: bool | None = None
    error: str | None = None
    timed_out: bool = False
    elapsed_s: float | None = None

    def snapshot(self) -> dict[str, Any]:
        return {"ready": self.ready, "error": self.error, "timed_out": self.timed_out, "elapsed_s": self.elapsed_s}


class _Guard:
    """把降級邏輯裝在 stage 實例上（實例屬性優先於類別方法）：Host 與 coordinator 照原樣呼叫即可。"""

    def __init__(
        self, stage: Any, readiness: StageReadiness, *, timeout_s: float, metrics: Callable[[], Metrics],
        degrade: bool,
    ) -> None:
        self._stage = stage
        self.readiness = readiness
        self._timeout_s = timeout_s
        self._metrics = metrics
        self._degrade = degrade
        self._inner_prepare = stage.prepare
        stage.prepare = self._prepare

    def _inc(self, suffix: str) -> None:
        self._metrics().inc(f"echo.{self.readiness.name}_{suffix}")

    async def _prepare(self) -> None:
        r = self.readiness
        if r.ready is not None:
            return  # 成功只做一次；失敗後不重試（避免每輪或每次 open 都重載而拖慢）
        t0 = time.perf_counter()
        try:
            await asyncio.wait_for(self._inner_prepare(), self._timeout_s)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            r.elapsed_s = time.perf_counter() - t0
            r.ready = False
            r.timed_out = isinstance(exc, TimeoutError)
            r.error = f"{type(exc).__name__}: {exc}"[:300] if str(exc) else type(exc).__name__
            self._inc("prepare_timeout" if r.timed_out else "prepare_failed")
            logger.error(
                "%s prepare %s（%.1fs）：%s%s", r.name, "逾時" if r.timed_out else "失敗", r.elapsed_s, r.error,
                "；降級繼續啟動" if self._degrade else "",
                exc_info=not r.timed_out,
            )
            if not self._degrade:
                raise
            self._install_degraded()
            return
        r.elapsed_s = time.perf_counter() - t0
        r.ready = True
        self._inc("prepare_ok")
        logger.info("%s prepare 完成（%.1fs）", r.name, r.elapsed_s)

    def _install_degraded(self) -> None:
        raise NotImplementedError


class _SpeakGuard(_Guard):
    def _install_degraded(self) -> None:
        self._stage.stream = self._silent_stream

    async def _silent_stream(self, sentences: AsyncIterator[Any], token: Any) -> AsyncIterator[Any]:
        """語音 not ready：把句子消耗掉（讓 think 不卡在背壓）、不產音訊；ES 判 ``no_audio``，文字照常 commit。"""
        self._inc("not_ready_turns")
        async for _ in sentences:
            token.raise_if_cancelled()
        return
        yield  # pragma: no cover - 讓這個函式是 async generator


class _ThinkGuard(_Guard):
    def _install_degraded(self) -> None:
        self._stage.stream = self._failing_stream
        self._stage.prewarm = self._no_prewarm

    async def _failing_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        self._inc("not_ready_turns")
        raise StageNotReady(f"think 未就緒：{self.readiness.error}")
        yield  # pragma: no cover

    async def _no_prewarm(self, *args: Any, **kwargs: Any) -> bool:
        return False


class _InputGuard(_Guard):
    def _install_degraded(self) -> None:
        self._stage.open = self._failing_open

    async def _failing_open(self, token: Any) -> AsyncIterator[Any]:
        self._inc("not_ready_turns")
        raise StageNotReady(f"輸入 stage 未就緒：{self.readiness.error}")
        yield  # pragma: no cover


def _require_repo(path: Path | None, env: str, what: str) -> Path:
    if path is None:
        raise InferenceWiringError(f"缺少 {what} 子系統路徑：請設定 {env}（不猜同層目錄）")
    if not Path(path).exists():
        raise InferenceWiringError(f"{env} 指向不存在的路徑：{path}")
    return Path(path)


def _ensure_importable(path: Path) -> None:
    entry = str(path)
    if entry not in sys.path:
        sys.path.insert(0, entry)


def default_tts_backend(cfg: InferenceConfig) -> Any:
    """預建 ``IndexTTS2Backend``（只存路徑與旗標，不載權重；``load()`` 在 SpeakStage.prepare）。"""
    repo = _require_repo(cfg.tts_repo, "UEP_INFER_TTS_REPO", "TTS")
    _ensure_importable(repo)
    from echo_tts.runtime.indextts_backend import IndexTTS2Backend
    from echo_tts.runtime.indextts_generate import default_generation_paths

    return IndexTTS2Backend(
        paths=default_generation_paths(repo), device=cfg.tts_device, use_fp16=cfg.tts_fp16,
        use_accel=cfg.tts_use_accel,
    )


def default_think_backend(cfg: InferenceConfig) -> Any:
    """從 SessionControl 的 registry 建 LLM backend（API client，無本地模型）。"""
    repo = _require_repo(cfg.session_repo, "UEP_INFER_SESSION_REPO", "Session")
    from echo_stream.adapters.think_llm import build_backend

    overrides: dict[str, Any] = {}
    if cfg.llm_model:
        overrides["model"] = cfg.llm_model
    return build_backend(cfg.llm_provider, session_repo=str(repo), **overrides)


@dataclass
class InferenceWiring:
    """三個已守衛的 stage 與其就緒狀態。``host_kwargs()`` 併入 ``EchoPipelineHost(...)``。"""

    think: Any
    speak: Any
    input_stage: Any
    config: InferenceConfig
    metrics: Metrics = field(default_factory=Metrics)
    readiness: dict[str, StageReadiness] = field(default_factory=dict)

    def host_kwargs(self) -> dict[str, Any]:
        return {"think": self.think, "speak": self.speak, "input_stage": self.input_stage}

    def attach_metrics(self, metrics: Metrics) -> None:
        """之後的指標寫進 Host 的 ``metrics``（prepare 在 Host 啟動時才發生，建構後 attach 即可）。"""
        self.metrics = metrics

    @property
    def voice_ready(self) -> bool:
        return self.readiness["speak"].ready is True

    def status(self) -> dict[str, dict[str, Any]]:
        return {name: r.snapshot() for name, r in self.readiness.items()}


def build_inference_wiring(
    config: InferenceConfig | None = None,
    *,
    metrics: Metrics | None = None,
    tts_backend: Any | None = None,
    tts_backend_factory: Callable[[InferenceConfig], Any] | None = None,
    think_backend: Any | None = None,
    think_backend_factory: Callable[[InferenceConfig], Any] | None = None,
    stt_backend: Any | None = None,
    stage_kwargs: Mapping[str, Mapping[str, Any]] | None = None,
) -> InferenceWiring:
    """建三個 stage（不載模型）。``*_backend``／``*_factory`` 供測試與替換注入；沒給時用真實預設。

    ``stage_kwargs`` 以 ``speak``／``think``／``input`` 為鍵，補傳 ES stage 建構子的額外參數
    （如 ``emotion_vector``、``split_policy``、``history_limit``），不覆蓋本模組已設定的參數名。
    """
    cfg = config or InferenceConfig.from_settings()
    extra = {k: dict(v) for k, v in (stage_kwargs or {}).items()}
    wiring = InferenceWiring(think=None, speak=None, input_stage=None, config=cfg, metrics=metrics or Metrics())

    def metrics_now() -> Metrics:
        return wiring.metrics

    # --- TTS：backend 預建一次、所有 turn 共用 ---
    from echo_stream.adapters.speak_indextts import IndexTTS2SpeakStage

    backend = tts_backend if tts_backend is not None else (tts_backend_factory or default_tts_backend)(cfg)
    speak_kwargs: dict[str, Any] = {
        "backend": backend, "device": cfg.tts_device, "use_fp16": cfg.tts_fp16, "use_accel": cfg.tts_use_accel,
        "warmup": cfg.tts_warmup, "lora_scale": cfg.tts_lora_scale, "speed": cfg.tts_speed,
        "language": cfg.tts_language,
    }
    speak_kwargs.update(extra.get("speak", {}))
    speak = IndexTTS2SpeakStage(**speak_kwargs)

    # --- STT：backend 在 prepare 才建（載 faster-whisper） ---
    if cfg.stt_mode == "multichannel":
        from echo_stream.adapters.input_multichannel import (
            MultiChannelSttInputStage as InputCls,
        )
    else:
        from echo_stream.adapters.input_stt import SttInputStage as InputCls
    stt_repo = None
    if stt_backend is None:
        stt_repo = str(_require_repo(cfg.stt_repo, "UEP_INFER_STT_REPO", "STT"))
    input_kwargs: dict[str, Any] = {"backend": stt_backend, "stt_repo": stt_repo, "language": cfg.stt_language}
    input_kwargs.update(extra.get("input", {}))
    input_stage = InputCls(**input_kwargs)

    # --- Think：LLM backend（API client） ---
    from echo_stream.adapters.think_llm import LLMThinkStage

    llm = think_backend if think_backend is not None else (think_backend_factory or default_think_backend)(cfg)
    think_kwargs: dict[str, Any] = {"system_prompt": cfg.system_prompt}
    think_kwargs.update(extra.get("think", {}))
    think = LLMThinkStage(llm, **think_kwargs)

    wiring.speak, wiring.think, wiring.input_stage = speak, think, input_stage
    for name, stage, guard_cls, timeout in (
        ("speak", speak, _SpeakGuard, cfg.speak_prepare_timeout_s),
        ("think", think, _ThinkGuard, cfg.think_prepare_timeout_s),
        ("input", input_stage, _InputGuard, cfg.input_prepare_timeout_s),
    ):
        readiness = StageReadiness(name)
        wiring.readiness[name] = readiness
        guard_cls(stage, readiness, timeout_s=timeout, metrics=metrics_now, degrade=cfg.degrade_on_failure)
    return wiring
