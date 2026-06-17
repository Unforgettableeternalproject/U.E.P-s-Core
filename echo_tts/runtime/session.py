from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from echo_tts.benchmarks.metrics import ProfileReport
from echo_tts.runtime.audio import AudioPlayer, NullAudioPlayer, StreamingAudioPlayer


class PersistentTTSBackend(Protocol):
    def load(self) -> None:
        ...

    def generate(self, text: str, output_path: Path) -> Path:
        ...

    def unload(self) -> None:
        ...


@dataclass(frozen=True)
class SayResult:
    text: str
    output_path: Path


@dataclass(frozen=True)
class ProfileResult:
    text: str
    output_path: Path
    report: ProfileReport


class TTSInteractiveSession:
    """Keeps a TTS backend loaded across multiple CLI commands."""

    def __init__(self, backend: PersistentTTSBackend, output_dir: str | Path, player: AudioPlayer | None = None):
        self.backend = backend
        self.output_dir = Path(output_dir)
        self.player = player or NullAudioPlayer()
        self._loaded = False
        self._say_count = 0
        self._save_count = 0
        self._play_count = 0
        self._last_output_path: Path | None = None

    def set_emotion(self, emotion_vector: list[float]) -> None:
        """Dynamically change the emotion vector for subsequent say/save/profile calls."""
        if len(emotion_vector) != 8:
            raise ValueError("emotion_vector must have exactly 8 dimensions")
        self.backend.emotion_vector = list(emotion_vector)

    def get_emotion(self) -> list[float]:
        """Return the current emotion vector."""
        return list(self.backend.emotion_vector or [0.0] * 8)

    def set_emotion_overrides(self, overrides: dict | None) -> None:
        """Set preset-level dynamics overrides (speed_variation, etc.)."""
        self._emotion_overrides = overrides

    def get_emotion_overrides(self) -> dict | None:
        """Return current preset-level dynamics overrides."""
        return getattr(self, "_emotion_overrides", None)

    def set_language(self, language: str) -> None:
        """Set synthesis language ('en' or 'zh')."""
        self._language = language

    def get_language(self) -> str:
        """Return current synthesis language."""
        return getattr(self, "_language", "en")

    # ── LoRA 動態混合 ──

    def load_lora(self, checkpoint_path: str | None = None, scale: float = 1.0) -> None:
        """載入 LoRA checkpoint（自動確保 engine 已載入）。

        Args:
            checkpoint_path: 已合併的 checkpoint 路徑，None 則使用 backend 的預設路徑。
            scale: 混合比例 (0.0 ~ 1.0)
        """
        self.load()
        self.backend.load_lora(checkpoint_path, scale=scale)

    def set_lora_scale(self, scale: float) -> None:
        """調整 LoRA 混合比例。"""
        self.backend.set_lora_scale(scale)

    def unload_lora(self) -> None:
        """移除 LoRA，還原原始權重。"""
        self.backend.unload_lora()

    def get_lora_status(self) -> dict:
        """回傳 LoRA 狀態。"""
        return self.backend.get_lora_status()

    # ── Speed (語速) 控制 ──

    def set_speed(self, value: float) -> float:
        """設定語速偏移（絕對值）。[-1.0, +1.0]，正=快，負=慢，0=預設。"""
        self.load()
        return self.backend.set_speed(value)

    def adjust_speed(self, delta: float) -> float:
        """基於目前值微調語速。"""
        self.load()
        return self.backend.adjust_speed(delta)

    def get_speed(self) -> float:
        """取得目前語速偏移值。"""
        return self.backend.get_speed()

    def get_speed_info(self) -> dict:
        """取得語速完整資訊。"""
        return self.backend.get_speed_info()

    # ── Poise 呼吸法控制 ──

    def get_poise_config(self) -> dict:
        """回傳 Poise 設定。"""
        return self.backend.get_poise_config()

    def set_poise_enabled(self, enabled: bool) -> None:
        """啟用/停用 Poise 系統。"""
        self.backend.set_poise_enabled(enabled)

    def set_poise_potency(self, potency: float) -> None:
        """設定 Poise 強度 (0.0–1.0)。"""
        self.backend.set_poise_potency(potency)

    def set_poise_count(self, count: float) -> None:
        """設定 Poise 觸發頻率。"""
        self.backend.set_poise_count(count)

    def load(self) -> None:
        if self._loaded:
            return
        self.backend.load()
        self._loaded = True

    def say(self, text: str) -> SayResult:
        """Generate and play audio.  Streams per-segment playback when possible.

        If the player is a ``StreamingAudioPlayer``, segments are enqueued
        non-blockingly so the engine can keep generating while the previous
        segment is still playing (true Producer/Consumer parallelism).
        Otherwise falls back to synchronous blocking playback per segment.
        """
        if not text.strip():
            raise ValueError("text must not be empty")

        self.load()
        output_path = self._resolve_auto_output_path(prefix="say")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        is_streaming = isinstance(self.player, StreamingAudioPlayer)
        segments_played = 0

        lang = self.get_language()

        if is_streaming:
            # --- Producer/Consumer path ---
            self.player.start_stream()

            # Pre-generate a short silence tensor for inter-segment gaps.
            # The saved WAV file has interval_silence baked into the
            # concatenation step, but streaming plays segments independently
            # so we need to insert silence explicitly.
            import torch as _torch
            _interval_ms = 200  # matches default synthesis.interval_silence_ms
            _interval_samples = int(_interval_ms * 22050 / 1000)
            _interval_silence = _torch.zeros(1, _interval_samples)

            def _on_segment(audio_tensor, seg_idx: int, total: int):
                self.player.enqueue_tensor(audio_tensor)
                # Insert inter-segment silence (except after the last segment)
                if seg_idx < total - 1:
                    self.player.enqueue_tensor(_interval_silence)

            generated = self.backend.generate(
                text, output_path, on_segment_audio=_on_segment, language=lang,
                emotion_overrides=self.get_emotion_overrides(),
            )

            segments_played = self.player.wait_stream()

        else:
            # --- Synchronous fallback path ---
            play_tensor = getattr(self.player, "play_tensor", None)

            def _on_segment(audio_tensor, seg_idx: int, total: int):
                nonlocal segments_played
                if play_tensor is not None:
                    play_tensor(audio_tensor)
                    segments_played += 1

            _emo_overrides = self.get_emotion_overrides()
            if play_tensor is not None:
                generated = self.backend.generate(
                    text, output_path, on_segment_audio=_on_segment, language=lang,
                    emotion_overrides=_emo_overrides,
                )
            else:
                generated = self.backend.generate(
                    text, output_path, language=lang,
                    emotion_overrides=_emo_overrides,
                )

        # Fallback: if no segments were streamed, play the final file
        if segments_played == 0:
            self.player.play(generated)

        self._say_count += 1
        self._play_count += 1
        self._last_output_path = generated
        self._cleanup_temporary_output(generated)
        return SayResult(text=text, output_path=generated)

    def save(self, text: str, output_path: str | Path) -> SayResult:
        generated = self._generate(text, self._resolve_output_path(output_path))
        self._save_count += 1
        self._last_output_path = generated
        return SayResult(text=text, output_path=generated)

    def save_auto(self, text: str) -> SayResult:
        """Generate and save with auto-generated filename (not cleaned up)."""
        if not text.strip():
            raise ValueError("text must not be empty")
        self.load()
        output_path = self._resolve_auto_output_path(prefix="save")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        generated = self.backend.generate(
            text, output_path, language=self.get_language(),
            emotion_overrides=self.get_emotion_overrides(),
        )
        self._save_count += 1
        self._last_output_path = generated
        return SayResult(text=text, output_path=generated)

    def profile(self, text: str) -> ProfileResult:
        """Generate speech with full stage profiling.  Temporary file is cleaned up."""
        if not text.strip():
            raise ValueError("text must not be empty")

        self.load()
        output_path = self._resolve_auto_output_path(prefix="profile")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if hasattr(self.backend, "profile_generate"):
            generated, report = self.backend.profile_generate(
                text, output_path, emotion_overrides=self.get_emotion_overrides(),
            )
        else:
            # Fallback: generate without profiling data
            generated = self.backend.generate(text, output_path)
            report = ProfileReport()

        self._last_output_path = generated
        self._cleanup_temporary_output(generated)
        return ProfileResult(text=text, output_path=generated, report=report)

    def unload(self) -> None:
        if not self._loaded:
            return
        self.backend.unload()
        self._loaded = False

    def status(self) -> dict[str, object]:
        return {
            "loaded": self._loaded,
            "say_count": self._say_count,
            "save_count": self._save_count,
            "play_count": self._play_count,
            "last_output_path": str(self._last_output_path) if self._last_output_path else None,
            "output_dir": str(self.output_dir),
        }

    def _generate(self, text: str, output_path: Path) -> Path:
        if not text.strip():
            raise ValueError("text must not be empty")

        self.load()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        return self.backend.generate(
            text, output_path, language=self.get_language(),
            emotion_overrides=self.get_emotion_overrides(),
        )

    def _resolve_output_path(self, output_path: str | Path) -> Path:
        path = Path(output_path)
        if not path.is_absolute():
            path = self.output_dir / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return self._avoid_overwrite(path)

    @staticmethod
    def _avoid_overwrite(path: Path) -> Path:
        """If *path* already exists, append a numeric suffix to avoid overwriting.

        Example: ``hello.wav`` → ``hello_2.wav`` → ``hello_3.wav`` → ...
        """
        if not path.exists():
            return path
        stem = path.stem
        suffix = path.suffix
        parent = path.parent
        counter = 2
        while True:
            candidate = parent / f"{stem}_{counter}{suffix}"
            if not candidate.exists():
                return candidate
            counter += 1

    def _resolve_auto_output_path(self, prefix: str) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        index = self._say_count + self._save_count + 1
        return self.output_dir / f"{prefix}-{timestamp}-{index:03d}.wav"

    @staticmethod
    def _cleanup_temporary_output(output_path: Path) -> None:
        if output_path.exists():
            output_path.unlink()
