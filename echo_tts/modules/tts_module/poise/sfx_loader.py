"""
SfxLoader — 音效素材載入器

載入呼吸聲和動作音效，resample 到 22050Hz，快取已載入的檔案。

第一版為 placeholder 實作：
- sfx_dir 未設定或目錄不存在時，回傳 None（呼叫端 fallback 到靜音）
- 真實音效素材由艾斯維爾之後準備

命名慣例：
  breath_inhale_01.wav, breath_exhale_01.wav
  action_water_sip.wav, action_put_down_cup.wav
"""

from pathlib import Path
from typing import Any, Dict, List, Optional


# Target sample rate for all TTS output
TARGET_SR = 22050


class SfxLoader:
    """
    Audio effect loader with caching and automatic resampling.

    Falls back gracefully when no assets are available.
    torch is imported lazily to avoid pulling heavy dependencies at module level.
    """

    def __init__(self, sfx_dir: Optional[Path] = None):
        self._sfx_dir = sfx_dir
        self._cache: Dict[str, Any] = {}  # torch.Tensor values, Any to avoid top-level import

    def load(self, name: str) -> Optional[Any]:
        """
        Load a sound effect by name.

        Args:
            name: Filename stem or full filename (e.g., "breath_inhale_01"
                  or "breath_inhale_01.wav").

        Returns:
            Audio tensor of shape [1, samples] at 22050Hz, or None
            if the asset doesn't exist or sfx_dir is not configured.
        """
        if self._sfx_dir is None or not self._sfx_dir.exists():
            return None

        # Check cache first
        if name in self._cache:
            return self._cache[name]

        # Resolve filename
        path = self._resolve_path(name)
        if path is None:
            return None

        try:
            import numpy as np
            import soundfile as sf
            import torch
            import torchaudio

            # torchaudio 2.11 的 load 需 torchcodec，改用 soundfile（輸出 (C, T) float32）
            data, sr = sf.read(str(path), dtype="float32", always_2d=True)
            waveform = torch.from_numpy(np.ascontiguousarray(data.T))

            # Convert to mono if stereo
            if waveform.shape[0] > 1:
                waveform = waveform.mean(dim=0, keepdim=True)

            # Resample if needed
            if sr != TARGET_SR:
                resampler = torchaudio.transforms.Resample(
                    orig_freq=sr, new_freq=TARGET_SR
                )
                waveform = resampler(waveform)

            self._cache[name] = waveform
            return waveform

        except Exception:
            return None

    def _resolve_path(self, name: str) -> Optional[Path]:
        """Resolve a name to a file path, trying common extensions."""
        if self._sfx_dir is None:
            return None

        # Try exact name first
        candidate = self._sfx_dir / name
        if candidate.exists():
            return candidate

        # Try with .wav extension
        candidate = self._sfx_dir / f"{name}.wav"
        if candidate.exists():
            return candidate

        # Try with .mp3 extension
        candidate = self._sfx_dir / f"{name}.mp3"
        if candidate.exists():
            return candidate

        return None

    def list_available(self) -> List[str]:
        """List all available sound effect names."""
        if self._sfx_dir is None or not self._sfx_dir.exists():
            return []

        return [
            p.stem
            for p in self._sfx_dir.iterdir()
            if p.suffix.lower() in ('.wav', '.mp3', '.flac', '.ogg')
        ]

    def get_breath(self) -> Optional[Any]:
        """
        Load a random breath sound effect.

        Looks for files matching breath_*.wav pattern.
        Returns None if no breath assets are available.
        """
        available = [n for n in self.list_available() if n.startswith('breath_')]
        if not available:
            return None

        import random
        name = random.choice(available)
        return self.load(name)

    def clear_cache(self) -> None:
        """Clear the loaded assets cache."""
        self._cache.clear()
