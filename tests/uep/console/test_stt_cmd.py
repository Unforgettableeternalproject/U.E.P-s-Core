"""Console `/stt`：以 fake backend 驗證指令層（不載模型、不需 PostgreSQL）。"""
from __future__ import annotations

import wave
from types import SimpleNamespace

import pytest

from uep.console import stt_cmd


def _write_wav(path, *, rate=16000, channels=1, seconds=0.5, width=2):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b"\x01\x00" * int(rate * seconds) * channels if width == 2 else b"\x01" * 100)


class FakeBackend:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.pcm_len = None
        self.closed = False

    def transcribe(self, pcm, sample_rate):
        self.pcm_len = len(pcm)
        assert sample_rate == 16000
        return SimpleNamespace(text="  你好世界 ", language="zh", confidence=0.9)

    def close(self):
        self.closed = True


def _run(arg, env=None, factory=None):
    lines: list[str] = []
    made: list[FakeBackend] = []

    def default_factory(**kw):
        made.append(FakeBackend(**kw))
        return made[-1]

    stt_cmd.run_stt(arg, lines.append, backend_factory=factory or default_factory, env=env or {})
    return "\n".join(lines), made


def test_parse_args_拆路徑與選項():
    assert stt_cmd.parse_args('"C:/a b/x.wav" --model small --lang zh') == ("C:/a b/x.wav", {"model": "small", "lang": "zh"})
    assert stt_cmd.parse_args("x.wav") == ("x.wav", {})
    assert stt_cmd.parse_args("") == ("", {})


def test_無參數印用法():
    text, made = _run("")
    assert "用法" in text and not made


def test_預設強制CPU與小模型(tmp_path):
    wav = tmp_path / "a.wav"
    _write_wav(wav, rate=22050, seconds=1.0)
    text, made = _run(str(wav))
    assert made[0].kwargs == {"language": None, "device": "cpu", "model_size": "base"}
    assert "強制 CPU" in text and "辨識結果：你好世界" in text and "語言：zh" in text and "RTF" in text
    assert made[0].closed
    # 22050 -> 16000 重採樣：1 秒 -> 16000 樣本 x 2 bytes
    assert made[0].pcm_len == 16000 * 2


def test_允許GPU時依設定檔且可指定模型(tmp_path):
    wav = tmp_path / "a.wav"
    _write_wav(wav)
    text, made = _run(f"{wav} --model small --lang en", env={"UEP_E2E_ALLOW_GPU": "1"})
    assert made[0].kwargs == {"language": "en", "device": None, "model_size": "small"}
    assert "強制 CPU" not in text


def test_立體聲下混為單聲道(tmp_path):
    wav = tmp_path / "s.wav"
    _write_wav(wav, channels=2, seconds=0.25)
    pcm, seconds = stt_cmd.load_wav_16k_mono(wav)
    assert len(pcm) == 16000 // 4 * 2 and seconds == pytest.approx(0.25)


def test_壞檔與非16bit回錯誤訊息不拋例外(tmp_path):
    text, made = _run(str(tmp_path / "missing.wav"))
    assert "無法讀取 wav" in text and not made
    bad = tmp_path / "w8.wav"
    _write_wav(bad, width=1)
    text, made = _run(str(bad))
    assert "16-bit" in text and not made


def test_後端載入失敗與空結果不中止(tmp_path):
    wav = tmp_path / "a.wav"
    _write_wav(wav)

    def boom(**_kw):
        raise RuntimeError("模型缺失")

    text, _ = _run(str(wav), factory=boom)
    assert "後端載入失敗" in text and "模型缺失" in text

    class Empty(FakeBackend):
        def transcribe(self, pcm, sample_rate):
            return None

    text, _ = _run(str(wav), factory=lambda **kw: Empty(**kw))
    assert "（空" in text
