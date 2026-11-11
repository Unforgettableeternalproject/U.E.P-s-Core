"""``--mode real`` 的真實元件組裝：STT 輸入 stage、記憶 Host（bge-m3）、warmup 計時與 GPU 守門。

console 屬 uep 內部，只經 ``uep.stream.adapters.*``、``uep.echo.memory_wiring``、``uep.memory`` 取得子系統能力。
所有函式的「建構」都不載模型；重載入（VOX、ASR、bge-m3）發生在 Host／MemoryHost 啟動時，並把各自耗時記進 ``warmups``。
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import Any

from uep.memory import MemoryHost, NamespaceSpec
from uep.memory.identity import IdentityDirectory

logger = logging.getLogger(__name__)

MEMORY_NAMESPACE = "console_owner"
MEMORY_OWNER = "owner"
DEFAULT_MEMORY_ROOT = Path.home() / ".uep" / "data" / "console-memory-bgem3"
GPU_ENV = "UEP_E2E_ALLOW_GPU"
START_MARGIN_S = 30.0
"""Host 啟動逾時在 speak／think／input 三個 prepare 逾時加總之外的餘裕（ASR 暖機、記憶 warmup 前的雜項）。"""
STT_WARMUP_SECONDS = 1.5
GPU_HINT = f"real 模式需要 GPU：請先確認 GPU 空閒（bot 未執行），再設 {GPU_ENV}=1 後重試"


def gpu_allowed(env: MutableMapping[str, str] | None = None) -> bool:
    return (env if env is not None else os.environ).get(GPU_ENV) == "1"


def tts_engine(settings: Any) -> str:
    """Speak 引擎：明確設了 ``UEP_INFER_TTS_ENGINE`` 就用它，沒設則預設 VOX（console 的預設）。"""
    if "infer_tts_engine" in getattr(settings, "model_fields_set", ()):
        return settings.infer_tts_engine
    return "vox"


def resolve_memory_root(settings: Any) -> tuple[Path, bool]:
    """記憶資料根與是否為 console 預設目錄。``UEP_MEMORY_DATA_ROOT`` 優先；未設用全新的開發目錄（不碰任何舊資料）。"""
    configured = getattr(settings, "memory_data_root", None)
    if configured is not None:
        return Path(configured), False
    return DEFAULT_MEMORY_ROOT, True


def memory_device(settings: Any) -> str:
    """bge-m3 的裝置：``UEP_MEMORY_EMBEDDING_DEVICE`` → ``UEP_MEMORY_DEVICE`` → 預設 cpu（艾斯維爾裁決）。"""
    return getattr(settings, "memory_embedding_device", None) or settings.memory_device or "cpu"


def host_start_timeout_s(settings: Any, engine: str) -> float:
    """Host 啟動逾時：``UEP_INFER_HOST_START_TIMEOUT_S`` 優先，否則 speak／think／input 三個 prepare 逾時加總加餘裕。"""
    explicit = getattr(settings, "infer_host_start_timeout_s", None)
    if explicit is not None:
        return float(explicit)
    from uep.echo.inference_wiring import SPEAK_PREPARE_TIMEOUT_DEFAULTS

    speak = settings.infer_speak_prepare_timeout_s
    if speak is None:
        speak = SPEAK_PREPARE_TIMEOUT_DEFAULTS[engine]
    return float(speak + settings.infer_think_prepare_timeout_s + settings.infer_input_prepare_timeout_s + START_MARGIN_S)


# ---- warmup 計時 ----

def timed_prepare(stage: Any, name: str, warmups: MutableMapping[str, float], *, after: Callable[[Any], None] | None = None) -> None:
    """把 ``stage.prepare`` 換成會記錄耗時的版本（實例屬性，Host 照常呼叫）；``after`` 在 prepare 後同步執行並併入耗時。"""
    original = stage.prepare

    async def prepare() -> Any:
        t0 = time.perf_counter()
        try:
            return await original()
        finally:
            if after is not None:
                try:
                    import asyncio

                    await asyncio.get_running_loop().run_in_executor(None, after, stage)
                except Exception:  # noqa: BLE001 - 暖機失敗只記 log，首輪會較慢
                    logger.exception("%s 暖機失敗", name)
            warmups[name] = time.perf_counter() - t0

    stage.prepare = prepare


def stt_warmup(stage: Any) -> None:
    """用一段低振幅雜訊跑一次辨識，把 CUDA kernel／首次推論成本移到啟動期（結果丟棄）。"""
    import numpy as np

    backend = getattr(stage, "_backend", None)
    if backend is None:
        return
    rng = np.random.default_rng(0)
    samples = int(16000 * STT_WARMUP_SECONDS)
    pcm = (rng.standard_normal(samples) * 600).astype(np.int16).tobytes()
    backend.transcribe(pcm, 16000)


def build_stt_stage(settings: Any, warmups: MutableMapping[str, float]) -> Any:
    """真實 STT 輸入 stage（echo_stt／faster-whisper，設定檔 ``stt_config.yaml``）；不載模型，prepare 時載入並暖機。

    斷句與 VAD 門檻取自 ``UEP_STT_*``（silence／min_utterance／vad threshold、hysteresis、hangover），與正式組裝共用同一個轉換。
    """
    from uep.echo.inference_wiring import stt_turn_kwargs

    if settings.infer_stt_mode == "single":
        from uep.stream.adapters.input_stt import SttInputStage as stage_cls
    else:
        from uep.stream.adapters.input_multichannel import MultiChannelSttInputStage as stage_cls
    stage = stage_cls(
        language=settings.infer_stt_language,
        **stt_turn_kwargs(
            settings.infer_stt_mode, silence_threshold_s=settings.stt_silence_threshold_s,
            min_utterance_s=settings.stt_min_utterance_s, vad_threshold=settings.stt_vad_threshold,
            vad_hysteresis=settings.stt_vad_hysteresis, vad_hangover_s=settings.stt_vad_hangover_s,
        ),
    )
    timed_prepare(stage, "stt", warmups, after=stt_warmup)
    return stage


# ---- 記憶 ----

class WarmEngineFactory:
    """包住 ``EchoEngineFactory``：建 engine 前先預載行程共用的 embedding 模型（``echo_memory`` 的模組級 ``warmup``）。

    bge-m3 冷載入 9–18 秒遠大於 Access Layer 的 3 秒檢索逾時；共用模型讓之後每個 engine 都不再冷啟動。
    """

    def __init__(
        self, inner: Callable[[str, Path], Any], warmups: MutableMapping[str, float], *, model: str | None,
        device: str, backend: str | None,
    ) -> None:
        self._inner = inner
        self._warmups = warmups
        self._model = model
        self._device = device
        self._backend = backend
        self.engines: list[Any] = []

    def __call__(self, namespace_id: str, data_root: Path) -> Any:
        if self._model and (self._backend or "sentence-transformers") == "sentence-transformers":
            from uep.echo.memory_wiring import warmup_embedding

            t0 = time.perf_counter()
            try:
                warmup_embedding(self._model, self._device)
            except Exception:  # noqa: BLE001 - 暖機失敗不阻擋啟動，首次檢索會降級並記 log
                logger.exception("記憶 embedding 暖機失敗（檢索可能降級）")
            self._warmups["memory_embedding"] = self._warmups.get("memory_embedding", 0.0) + (time.perf_counter() - t0)
        engine = self._inner(namespace_id, data_root)
        self.engines.append(engine)
        return engine


def build_memory_host(
    settings: Any, root: Path, identity: IdentityDirectory, *, publisher: Any, session_factory: Any,
    warmups: MutableMapping[str, float],
) -> MemoryHost:
    """真實記憶 Host（EM engine、bge-m3、assess 關閉以免額外 LLM 呼叫）；資料根由呼叫端決定。"""
    from uep.echo.memory_wiring import build_dream_loop_config, build_memory_wiring

    dream_loop, dream_rest_policy = build_dream_loop_config(settings)
    wiring = build_memory_wiring(
        device=memory_device(settings), embedding_backend=settings.memory_embedding_backend,
        embedding_model=settings.memory_embedding_model, min_similarity=settings.memory_min_similarity,
        assess_mode="off", dream_loop=dream_loop, dream_rest_policy=dream_rest_policy,
    )
    kwargs = wiring.host_kwargs()
    kwargs["engine_factory"] = WarmEngineFactory(
        kwargs["engine_factory"], warmups, model=settings.memory_embedding_model, device=memory_device(settings),
        backend=settings.memory_embedding_backend,
    )
    return MemoryHost(
        data_root=root, namespaces=(NamespaceSpec(MEMORY_NAMESPACE, owner_id=MEMORY_OWNER),), identity=identity,
        publisher=publisher, session_factory=session_factory, **kwargs,
    )


def memory_status_lines(host: MemoryHost, root: Path) -> list[str]:
    """``/mem`` 顯示用：namespace、資料根、episode 數、最近寫入（摘要前 40 字）。"""
    engine = host.engine(MEMORY_NAMESPACE)
    with host.engine_lock(MEMORY_NAMESPACE):
        episodic = engine.episodic
        total = episodic.count()
        recent = episodic.list_episodes(limit=5)
    lines = [f"記憶：namespace={MEMORY_NAMESPACE}　owner={MEMORY_OWNER}　資料根={root}", f"episode 數：{total}"]
    for ep in recent:
        summary = (getattr(ep, "content_summary", "") or "").replace("\n", " ")
        lines.append(f"  - {ep.timestamp:%H:%M:%S}　session={str(ep.session_id)[-8:]}　{summary[:40]}")
    if not recent:
        lines.append("  （尚無寫入）")
    status = host.dream_status()
    if not status.get("enabled"):
        lines.append("做夢迴圈：未啟用")
    else:
        last = status.get("last_decision") or {}
        would_run = sum(v for k, v in host.metrics.snapshot().items() if k.startswith("memory.dream.shadow_would_run."))
        lines.append(
            f"做夢迴圈：running={status.get('running')}　shadow={status.get('shadow')}　paused={status.get('paused')}"
            f"　最近決策={last.get('action')}/{last.get('trigger')}/{last.get('reason')}　shadow would_run={would_run}"
        )
    return lines
