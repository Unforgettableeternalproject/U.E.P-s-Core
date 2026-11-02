"""torchaudio 2.9+ 相容層與 identify_speaker 記憶體輸入測試。"""

import sys
import types

import numpy as np
import pytest

from echo_stt import _torchaudio_compat as compat


def _fake_torchaudio(**attrs):
    mod = types.ModuleType("torchaudio")
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


class TestEnsureTorchaudioCompat:
    def test_patches_missing_attributes(self, monkeypatch):
        fake = _fake_torchaudio()
        monkeypatch.setitem(sys.modules, "torchaudio", fake)

        assert compat.ensure_torchaudio_compat() is True
        meta = fake.AudioMetaData(16000, 100, 1, 16, "PCM_S")
        assert meta.sample_rate == 16000 and meta.num_channels == 1
        assert fake.list_audio_backends() == ["soundfile"]
        assert fake.set_audio_backend("soundfile") is None

    def test_noop_when_attributes_exist(self, monkeypatch):
        sentinel_meta = object()
        sentinel_list = lambda: ["sox_io"]  # noqa: E731
        sentinel_set = lambda *a, **k: "orig"  # noqa: E731
        fake = _fake_torchaudio(
            AudioMetaData=sentinel_meta,
            list_audio_backends=sentinel_list,
            set_audio_backend=sentinel_set,
        )
        monkeypatch.setitem(sys.modules, "torchaudio", fake)

        assert compat.ensure_torchaudio_compat() is False
        assert fake.AudioMetaData is sentinel_meta
        assert fake.list_audio_backends is sentinel_list
        assert fake.set_audio_backend is sentinel_set

    def test_idempotent(self, monkeypatch):
        fake = _fake_torchaudio()
        monkeypatch.setitem(sys.modules, "torchaudio", fake)
        assert compat.ensure_torchaudio_compat() is True
        first = fake.AudioMetaData
        assert compat.ensure_torchaudio_compat() is False
        assert fake.AudioMetaData is first

    def test_does_not_import_torchaudio(self, monkeypatch):
        monkeypatch.delitem(sys.modules, "torchaudio", raising=False)
        monkeypatch.setattr(sys, "meta_path", [])  # 若嘗試 import 會失敗
        assert compat.ensure_torchaudio_compat() is False
        assert "torchaudio" not in sys.modules


class TestIdentifySpeakerInMemory:
    """identify_speaker 不再寫暫存檔，改傳 waveform dict 給 pyannote pipeline。"""

    def test_pipeline_receives_waveform_dict(self):
        torch = pytest.importorskip("torch")
        from echo_stt.speaker.identification import SpeakerIdentification

        received = {}

        class _Track:
            start, end = 0.0, 1.0

        class _Diarization:
            def itertracks(self, yield_label=True):
                return iter([(_Track(), None, "SPEAKER_00")])

        def _pipeline(arg):
            received["arg"] = arg
            return _Diarization()

        speaker = SpeakerIdentification(context_manager=None)
        speaker.pipeline = _pipeline
        audio = np.random.RandomState(0).randint(-8000, 8000, 16000, dtype=np.int16)

        info = speaker.identify_speaker(audio)

        arg = received["arg"]
        assert isinstance(arg, dict)
        assert arg["sample_rate"] == speaker.sample_rate
        waveform = arg["waveform"]
        assert waveform.shape == (1, 16000)
        assert waveform.dtype == torch.float32
        assert np.allclose(waveform[0].numpy(), audio.astype(np.float32) / 32768.0)
        assert info is not None

    def test_pyannote_accepts_waveform_dict_without_torchaudio_io(self):
        torch = pytest.importorskip("torch")
        pytest.importorskip("torchaudio")
        from echo_stt.speaker import identification

        if not identification.PYANNOTE_AVAILABLE:
            pytest.skip("pyannote 不可用")
        from pyannote.audio.core.io import Audio

        audio = Audio(sample_rate=16000, mono="downmix")
        file = {"waveform": torch.zeros(1, 16000), "sample_rate": 16000}
        waveform, sr = audio(file)
        assert sr == 16000 and waveform.shape[-1] == 16000
