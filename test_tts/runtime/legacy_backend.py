from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from test_tts.benchmarks.metrics import InitProfile, SynthesisProfile, ProfileReport
from test_tts.runtime.legacy_generate import LegacyGenerationPaths, validate_generation_paths

EngineFactory = Callable[..., Any]


class LegacyIndexTTS2Backend:
    """Persistent backend wrapper around the copied IndexTTS2 Lite engine."""

    def __init__(
        self,
        paths: LegacyGenerationPaths,
        engine_factory: EngineFactory | None = None,
        device: str | None = "cuda",
        use_fp16: bool = True,
        use_cuda_kernel: bool = True,
        use_accel: bool = False,
        emotion_vector: Sequence[float] | None = None,
    ):
        self.paths = paths
        self.engine_factory = engine_factory
        self.device = device
        self.use_fp16 = use_fp16
        self.use_cuda_kernel = use_cuda_kernel
        self.use_accel = use_accel
        self.emotion_vector = list(emotion_vector) if emotion_vector else None
        self.engine: Any | None = None
        self.loaded = False

    def load(self) -> None:
        if self.loaded:
            return

        validate_generation_paths(self.paths)
        legacy_path = str(self.paths.legacy_root)
        if legacy_path not in sys.path:
            sys.path.insert(0, legacy_path)

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

    def generate(self, text: str, output_path: str | Path, on_segment_audio=None) -> Path:
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

    def unload(self) -> None:
        self.engine = None
        self.loaded = False

    @staticmethod
    def _default_engine_factory() -> EngineFactory:
        from modules.tts_module.lite_engine import IndexTTSLite

        return IndexTTSLite
