"""音訊讀寫（soundfile 取代 torchaudio.load/save）round-trip 測試。"""

import numpy as np
import pytest
import soundfile as sf
import torch

from echo_tts.modules.tts_module.tts_utils.audio_io import (
    load_audio_tensor,
    save_audio_tensor,
)
from echo_tts.modules.tts_module.tts_utils import common as tts_common
from echo_tts.modules.tts_module.tts_utils import utils as tts_utils_mod
from echo_tts.modules.tts_module.poise.sfx_loader import SfxLoader, TARGET_SR


def _tone(frames: int, channels: int = 1, sr: int = 22050) -> torch.Tensor:
    t = torch.arange(frames, dtype=torch.float32) / sr
    wave = 0.5 * torch.sin(2 * np.pi * 440 * t)
    return wave.unsqueeze(0).repeat(channels, 1)


def test_save_load_round_trip_mono(tmp_path):
    audio = _tone(2205)
    path = tmp_path / "mono.wav"
    save_audio_tensor(path, audio, 22050)
    loaded, sr = load_audio_tensor(path)
    assert sr == 22050
    assert loaded.shape == (1, 2205)
    assert loaded.dtype == torch.float32
    # 16-bit 量化誤差
    assert torch.allclose(loaded, audio, atol=1e-3)


def test_save_accepts_1d_tensor(tmp_path):
    path = tmp_path / "flat.wav"
    save_audio_tensor(path, _tone(100).squeeze(0), 16000)
    loaded, sr = load_audio_tensor(path)
    assert sr == 16000
    assert loaded.shape == (1, 100)


def test_save_is_pcm16_and_channels_first(tmp_path):
    path = tmp_path / "stereo.wav"
    save_audio_tensor(path, _tone(300, channels=2), 22050)
    info = sf.info(str(path))
    assert info.subtype == "PCM_16"
    assert info.channels == 2
    assert info.frames == 300
    loaded, _ = load_audio_tensor(path)
    assert loaded.shape == (2, 300)


def test_save_clips_out_of_range(tmp_path):
    path = tmp_path / "loud.wav"
    save_audio_tensor(path, torch.tensor([[2.0, -2.0, 0.0]]), 8000)
    loaded, _ = load_audio_tensor(path)
    assert loaded.max() <= 1.0 and loaded.min() >= -1.0
    assert loaded[0, 0] > 0.99 and loaded[0, 1] < -0.99


@pytest.mark.parametrize("module", [tts_common, tts_utils_mod])
def test_load_audio_mono_mix_and_resample(module, tmp_path):
    path = tmp_path / "stereo.wav"
    save_audio_tensor(path, _tone(8000, channels=2, sr=16000), 16000)
    audio = module.load_audio(str(path), 22050)
    assert audio.shape[0] == 1
    assert audio.shape[1] == pytest.approx(8000 * 22050 / 16000, abs=2)
    assert audio.abs().max() <= 1.0


def test_load_audio_same_rate_keeps_length(tmp_path):
    path = tmp_path / "same.wav"
    save_audio_tensor(path, _tone(500, sr=24000), 24000)
    audio = tts_common.load_audio(str(path), 24000)
    assert audio.shape == (1, 500)


def test_sfx_loader_loads_mono_resampled(tmp_path):
    save_audio_tensor(tmp_path / "breath_inhale_01.wav", _tone(8000, channels=2, sr=16000), 16000)
    loader = SfxLoader(tmp_path)
    wave = loader.load("breath_inhale_01")
    assert wave is not None
    assert wave.shape[0] == 1
    assert wave.shape[1] == pytest.approx(8000 * TARGET_SR / 16000, abs=2)
    # 第二次取自快取
    assert loader.load("breath_inhale_01") is wave


def test_sfx_loader_missing_returns_none(tmp_path):
    assert SfxLoader(tmp_path).load("nope") is None
