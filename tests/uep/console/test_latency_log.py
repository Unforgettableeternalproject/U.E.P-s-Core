"""Console 延遲報告落地：每輪結束附加寫 latency.jsonl（不需 DB，以最小 stub 驅動 ``_write_latency``）。"""
from __future__ import annotations

import json
from types import SimpleNamespace

from uep.console.commands import LATENCY_LOG_ENV, Console
from uep.stream.tracer import MARK_SPEAK_FIRST_CHUNK, LatencyTracer


def _console(session_dir, tracer) -> Console:
    console = object.__new__(Console)  # 只需要 rt.tracer／rt.session_dir，略過整個 Core 組裝
    console.rt = SimpleNamespace(tracer=tracer, session_dir=session_dir)
    return console


def _tracer_with_turn(turn_id: str = "t1") -> LatencyTracer:
    tracer = LatencyTracer()
    tracer.start(turn_id)
    tracer.mark(turn_id, MARK_SPEAK_FIRST_CHUNK)
    tracer.finish(turn_id)
    return tracer


def test_每輪附加一行json到session_dir(monkeypatch, tmp_path):
    monkeypatch.delenv(LATENCY_LOG_ENV, raising=False)
    tracer = _tracer_with_turn("t1")
    tracer.start("t2")
    tracer.finish("t2")
    console = _console(tmp_path / "e2e", tracer)
    console._write_latency("t1", "turn.completed")
    console._write_latency("t2", "turn.cancelled")
    lines = (tmp_path / "e2e" / "latency.jsonl").read_text(encoding="utf-8").splitlines()
    first, second = (json.loads(line) for line in lines)
    assert first["turn_id"] == "t1" and first["terminal"] == "turn.completed" and first["ts"]
    assert first["segments_ms"]["ttfa"] is not None
    assert second["turn_id"] == "t2" and second["terminal"] == "turn.cancelled"
    assert second["segments_ms"]["ttfa"] is None


def test_環境變數覆寫路徑(monkeypatch, tmp_path):
    target = tmp_path / "x" / "my.jsonl"
    monkeypatch.setenv(LATENCY_LOG_ENV, str(target))
    _console(tmp_path / "e2e", _tracer_with_turn())._write_latency("t1", "turn.completed")
    assert target.exists() and not (tmp_path / "e2e" / "latency.jsonl").exists()


def test_off關閉不寫檔(monkeypatch, tmp_path):
    monkeypatch.setenv(LATENCY_LOG_ENV, "OFF")
    _console(tmp_path, _tracer_with_turn())._write_latency("t1", "turn.completed")
    assert not (tmp_path / "latency.jsonl").exists()


def test_寫檔失敗只記警告不拋例外(monkeypatch, tmp_path, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")  # 父路徑是檔案 -> mkdir／open 必失敗
    monkeypatch.setenv(LATENCY_LOG_ENV, str(blocker / "latency.jsonl"))
    with caplog.at_level("WARNING"):
        _console(tmp_path, _tracer_with_turn())._write_latency("t1", "turn.completed")
    assert "延遲報告寫檔失敗" in caplog.text


def test_無trace的turn不寫檔(monkeypatch, tmp_path):
    monkeypatch.delenv(LATENCY_LOG_ENV, raising=False)
    _console(tmp_path, LatencyTracer())._write_latency("missing", "turn.completed")
    assert not (tmp_path / "latency.jsonl").exists()
