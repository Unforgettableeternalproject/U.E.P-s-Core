"""Console `/tts`：以 fake Speak stage 驗證指令層（不載模型、不需 GPU 與 PostgreSQL）。"""
from __future__ import annotations

import wave

import pytest

from uep.console import rig, tts_cmd
from uep.stream.contracts.types import AudioChunk


class FakeStage:
    def __init__(self) -> None:
        self.prepared = 0
        self.closed = 0
        self.sentences: list[str] = []

    async def prepare(self) -> None:
        self.prepared += 1

    async def stream(self, sentences, token):
        async for s in sentences:
            self.sentences.append(s.text)
            for i in range(2):
                yield AudioChunk(pcm=b"\x01\x00" * 480, sample_rate=48000, turn_id=s.turn_id, sentence_index=s.index, chunk_index=i)

    async def aclose(self) -> None:
        self.closed += 1


def _run(arg, tmp_path, *, env=None, stage=None, engine="vox", peak=1234.0):
    lines: list[str] = []
    made: list[tuple[str, FakeStage]] = []

    def factory(name):
        made.append((name, stage or FakeStage()))
        return made[-1][1]

    tts_cmd.run_tts(
        arg, lines.append, out_dir=tmp_path, stage_factory=factory, engine_default=lambda: engine,
        env=env if env is not None else {"UEP_E2E_ALLOW_GPU": "1"}, vram_probe=lambda: peak,
    )
    return "\n".join(lines), made


def test_parse_args_拆文字與引擎():
    assert tts_cmd.parse_args("你好 世界 --engine VOX") == ("你好 世界", "vox")
    assert tts_cmd.parse_args('"你好"') == ("你好", None)
    assert tts_cmd.parse_args("") == ("", None)


def test_無參數印用法(tmp_path):
    text, made = _run("", tmp_path)
    assert "用法" in text and not made


def test_未設GPU環境變數明確提示且不建立後端(tmp_path):
    text, made = _run("你好", tmp_path, env={})
    assert "UEP_E2E_ALLOW_GPU=1" in text and "需要 GPU" in text
    assert not made and not list(tmp_path.glob("*.wav"))


def test_未知引擎(tmp_path):
    text, made = _run("你好 --engine bogus", tmp_path)
    assert "未知的 TTS 引擎" in text and not made


def test_合成寫出wav並印延遲與VRAM(tmp_path):
    text, made = _run("你好，這是一句測試語音。", tmp_path)
    name, stage = made[0]
    assert name == "vox" and stage.prepared == 1 and stage.closed == 1
    assert stage.sentences == ["你好，這是一句測試語音。"]
    wavs = list(tmp_path.glob("tts-vox-*.wav"))
    assert len(wavs) == 1
    with wave.open(str(wavs[0]), "rb") as w:
        assert w.getframerate() == 48000 and w.getnframes() == 960
    assert "首音延遲" in text and "合成總耗時" in text and "1234 MiB" in text


def test_引擎旗標傳給工廠(tmp_path):
    _, made = _run("hello --engine indextts2", tmp_path)
    assert made[0][0] == "indextts2"


def test_prepare失敗只印原因(tmp_path):
    class Boom(FakeStage):
        async def prepare(self) -> None:
            raise RuntimeError("找不到權重")

    text, made = _run("你好", tmp_path, stage=Boom())
    assert "TTS 合成失敗" in text and "找不到權重" in text
    assert made[0][1].closed == 1


def test_real_speak_未設GPU環境變數拒絕(monkeypatch):
    monkeypatch.delenv("UEP_E2E_ALLOW_GPU", raising=False)
    with pytest.raises(rig.ConsoleError, match="UEP_E2E_ALLOW_GPU"):
        rig.real_speak_kwargs()


def test_real_speak_有GPU環境變數時建stage與啟動逾時(monkeypatch):
    stage = object()
    monkeypatch.setenv("UEP_E2E_ALLOW_GPU", "1")
    monkeypatch.setattr(tts_cmd, "default_engine", lambda: "vox")
    monkeypatch.setattr(tts_cmd, "build_speak_stage", lambda engine: stage)
    kwargs = rig.real_speak_kwargs()
    assert kwargs["speak_stage"] is stage and kwargs["start_timeout_s"] == 240.0 + 60.0
