"""音訊檔案讀寫 — 以 soundfile 取代 torchaudio.load/save。

torchaudio 2.9 起 load/save 改依賴 torchcodec，2.11 已移除舊後端；
這裡統一用 soundfile，並維持與 torchaudio 相同的張量約定：
(channels, frames)、float32、範圍 [-1, 1]。
"""

from pathlib import Path
from typing import Tuple, Union

import numpy as np
import soundfile as sf
import torch


def load_audio_tensor(path: Union[str, Path]) -> Tuple[torch.Tensor, int]:
    """讀取音訊檔，回傳 (channels, frames) float32 張量與取樣率。"""
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return torch.from_numpy(np.ascontiguousarray(data.T)), int(sr)


def save_audio_tensor(path: Union[str, Path], audio: torch.Tensor, sample_rate: int) -> None:
    """將 (channels, frames) 或 (frames,) 張量存為 16-bit PCM WAV。"""
    arr = audio.detach().cpu().float().numpy()
    if arr.ndim == 1:
        arr = arr[None, :]
    sf.write(str(path), np.ascontiguousarray(arr.T), int(sample_rate), subtype="PCM_16")
