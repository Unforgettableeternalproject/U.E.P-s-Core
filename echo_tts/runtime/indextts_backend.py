from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from echo_tts.benchmarks.metrics import InitProfile, SynthesisProfile, ProfileReport
from echo_tts.runtime.indextts_generate import GenerationPaths, validate_generation_paths

EngineFactory = Callable[..., Any]


class IndexTTS2Backend:
    """Persistent backend wrapper around the IndexTTS2 Lite engine."""

    # Default LoRA adapter path (relative to project root)
    # 支援 adapter 目錄（PEFT 格式）或舊版 .pth checkpoint（向後相容）
    DEFAULT_LORA_CHECKPOINT = "experiments/index-tts-lora/finetune_models/checkpoints/gpt_best"
    DEFAULT_LORA_SCALE = 0.85

    def __init__(
        self,
        paths: GenerationPaths,
        engine_factory: EngineFactory | None = None,
        device: str | None = "cuda",
        use_fp16: bool = True,
        use_cuda_kernel: bool = True,
        use_accel: bool = False,
        emotion_vector: Sequence[float] | None = None,
        quick_streaming_tokens: int = 50,
        lora_checkpoint: str | None = None,
    ):
        self.paths = paths
        self.engine_factory = engine_factory
        self.device = device
        self.use_fp16 = use_fp16
        self.use_cuda_kernel = use_cuda_kernel
        self.use_accel = use_accel
        self.emotion_vector = list(emotion_vector) if emotion_vector else None
        self.quick_streaming_tokens = quick_streaming_tokens
        self.engine: Any | None = None
        self.loaded = False
        self._lora_checkpoint = lora_checkpoint

    def load(self) -> None:
        if self.loaded:
            return

        validate_generation_paths(self.paths)
        engine_root = str(self.paths.engine_root)
        if engine_root not in sys.path:
            sys.path.insert(0, engine_root)

        factory = self.engine_factory or self._default_engine_factory()
        self.engine = factory(
            cfg_path=str(self.paths.cfg_path),
            model_dir=str(self.paths.model_dir),
            use_fp16=self.use_fp16,
            device=self.device,
            use_cuda_kernel=self.use_cuda_kernel,
            use_accel=self.use_accel,
        )
        self.engine.load_character(self.paths.character_path)
        self.loaded = True

    def generate(self, text: str, output_path: str | Path, on_segment_audio=None, language: str = "en") -> Path:
        if not text.strip():
            raise ValueError("text must not be empty")

        self.load()
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        ok = self.engine.synthesize(
            text=text,
            output_path=str(output),
            emotion_vector=self.emotion_vector,
            on_segment_audio=on_segment_audio,
            quick_streaming_tokens=self.quick_streaming_tokens,
            language=language,
        )
        if not ok or not output.exists():
            raise RuntimeError("IndexTTS2 generation did not produce an output file")
        return output

    def profile_generate(self, text: str, output_path: str | Path) -> tuple[Path, ProfileReport]:
        """Generate speech and return a full profiling report."""
        if not text.strip():
            raise ValueError("text must not be empty")

        self.load()
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        if not hasattr(self.engine, "synthesize_with_profile"):
            raise NotImplementedError(
                "Engine does not support synthesize_with_profile. "
                "Use the instrumented IndexTTSLite engine."
            )

        ok, timings = self.engine.synthesize_with_profile(
            text=text,
            output_path=str(output),
            emotion_vector=self.emotion_vector,
        )
        if not ok or not output.exists():
            raise RuntimeError("IndexTTS2 generation did not produce an output file")

        # Build init profile from engine's stored init timings
        init_timings = getattr(self.engine, "_init_timings", {})
        init_profile = InitProfile.from_dict(init_timings)
        synth_profile = SynthesisProfile.from_dict(timings)

        return output, ProfileReport(init=init_profile, synthesis=synth_profile, success=ok)

    # ── LoRA 動態混合 ──

    def load_lora(self, checkpoint_path: str | None = None, scale: float | None = None) -> None:
        """載入 LoRA checkpoint 到引擎（需要先 load engine）。

        Args:
            checkpoint_path: 已合併的 checkpoint 路徑，None 則使用預設路徑。
            scale: 混合比例，None 則使用預設值 (0.85)。
        """
        self.load()
        path = checkpoint_path or self._lora_checkpoint or self._resolve_default_lora()
        s = scale if scale is not None else self.DEFAULT_LORA_SCALE
        self.engine.load_lora(path, scale=s)

    def _resolve_default_lora(self) -> str:
        """Resolve the default LoRA adapter/checkpoint path relative to project root."""
        import os
        project_root = str(Path(self.paths.engine_root).parent)
        candidate = os.path.join(project_root, self.DEFAULT_LORA_CHECKPOINT)
        # 優先找 adapter 目錄（PEFT 格式）
        if os.path.isdir(candidate):
            return candidate
        # 向後相容：找 .pth 檔案
        pth_candidate = candidate + ".pth"
        if os.path.isfile(pth_candidate):
            return pth_candidate
        # 再找舊版 merged checkpoint
        old_merged = os.path.join(project_root, "experiments/index-tts-lora/finetune_models/checkpoints/gpt_best_v2_merged.pth")
        if os.path.isfile(old_merged):
            return old_merged
        raise FileNotFoundError(
            f"找不到 LoRA adapter 或 checkpoint: {candidate}\n"
            f"請用 lora load <path> 指定路徑，或確認 adapter 存在。"
        )

    def set_lora_scale(self, scale: float) -> None:
        """調整 LoRA 混合比例。"""
        if not self.loaded or self.engine is None:
            raise RuntimeError("Engine not loaded")
        self.engine.set_lora_scale(scale)

    def unload_lora(self) -> None:
        """移除 LoRA，還原原始權重。"""
        if self.engine is not None:
            self.engine.unload_lora()

    def get_lora_status(self) -> dict:
        """回傳 LoRA 狀態。"""
        if self.engine is None:
            return {"active": False, "scale": 0.0, "path": None, "delta_keys": 0}
        return self.engine.get_lora_status()

    # ── Poise 呼吸法控制 ──

    def get_poise_config(self) -> dict:
        """回傳 Poise 設定（dict 格式）。"""
        if self.engine is None:
            return {"enabled": False, "potency": 0.0, "count": 0.0}
        cfg = self.engine.poise_config
        import dataclasses
        return dataclasses.asdict(cfg)

    def set_poise_enabled(self, enabled: bool) -> None:
        """啟用/停用 Poise 系統。"""
        self.load()
        self.engine.poise_config.enabled = enabled

    def set_poise_potency(self, potency: float) -> None:
        """設定 Poise 強度 (0.0–1.0)。"""
        self.load()
        self.engine.poise_config.potency = max(0.0, min(1.0, potency))

    def set_poise_count(self, count: float) -> None:
        """設定 Poise 觸發頻率（每 N 段觸發一次）。"""
        self.load()
        self.engine.poise_config.count = max(2.0, count)

    # ── Speed (語速) 控制 ──

    def set_speed(self, value: float) -> float:
        """設定語速（絕對值）。"""
        self.load()
        return self.engine.set_speed(value)

    def adjust_speed(self, delta: float) -> float:
        """基於目前值調整語速。"""
        self.load()
        return self.engine.adjust_speed(delta)

    def get_speed(self) -> float:
        """取得目前語速偏移值。"""
        if self.engine is None:
            return 0.0
        return self.engine.get_speed()

    def get_speed_info(self) -> dict:
        """取得語速完整資訊。"""
        if self.engine is None:
            return {"speed": 0.0, "stretch_ratio": 1.7, "config_stretch_ratio": 1.7}
        return self.engine.get_speed_info()

    def unload(self) -> None:
        self.engine = None
        self.loaded = False

    @staticmethod
    def _default_engine_factory() -> EngineFactory:
        from modules.tts_module.lite_engine import IndexTTSLite

        return IndexTTSLite
