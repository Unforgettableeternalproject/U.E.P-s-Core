"""LatencyTracer 的測試。

沒有量測就沒有優化——上次只有「30-40 秒」這個總數，不知道該修哪裡。
"""

from __future__ import annotations

import json

from uep.stream.contracts.types import TurnPhase
from uep.stream.tracer import (
    BUDGET_MS,
    MARK_INPUT_FINAL,
    MARK_SPEAK_FIRST_CHUNK,
    MARK_SPEECH_END,
    MARK_THINK_FIRST_SENTENCE,
    LatencyTracer,
)


def test_ttfa_從使用者說完起算():
    tracer = LatencyTracer()
    tracer.start("t1")
    tracer.mark("t1", MARK_SPEECH_END, at=100.0)
    tracer.mark("t1", MARK_SPEAK_FIRST_CHUNK, at=102.5)
    assert tracer.get("t1").ttfa_ms == 2500.0


def test_同名打點只記第一次():
    """think_first_token 這類「首次」語意的打點，重複呼叫不能覆蓋。"""
    tracer = LatencyTracer()
    tracer.start("t1")
    tracer.mark("t1", MARK_SPEAK_FIRST_CHUNK, at=100.0)
    tracer.mark("t1", MARK_SPEAK_FIRST_CHUNK, at=200.0)
    assert tracer.get("t1").marks[MARK_SPEAK_FIRST_CHUNK] == 100.0


def test_分段拆解():
    tracer = LatencyTracer()
    tracer.start("t1")
    tracer.mark("t1", MARK_SPEECH_END, at=0.0)
    tracer.mark("t1", MARK_INPUT_FINAL, at=0.4)
    tracer.mark("t1", MARK_THINK_FIRST_SENTENCE, at=1.2)
    tracer.mark("t1", MARK_SPEAK_FIRST_CHUNK, at=2.2)

    segs = tracer.get("t1").segments_ms()
    assert segs["stt"] == 400.0
    assert round(segs["llm_first_sentence"], 1) == 800.0
    assert round(segs["tts_first_chunk"], 1) == 1000.0
    assert round(segs["ttfa"], 1) == 2200.0


def test_缺打點時回_None_而不是爆掉():
    tracer = LatencyTracer()
    tracer.start("t1")
    segs = tracer.get("t1").segments_ms()
    assert segs["stt"] is None
    assert segs["ttfa"] is None


def test_切點成因記帳():
    tracer = LatencyTracer()
    tracer.start("t1")
    tracer.count_sentence("t1", "terminal")
    tracer.count_sentence("t1", "terminal")
    tracer.count_sentence("t1", "max_length")
    trace = tracer.get("t1")
    assert trace.sentence_count == 3
    assert trace.split_reasons == {"terminal": 2, "max_length": 1}


def test_音訊時長累加():
    tracer = LatencyTracer()
    tracer.start("t1")
    tracer.count_chunk("t1", 1.5)
    tracer.count_chunk("t1", 2.0)
    trace = tracer.get("t1")
    assert trace.chunk_count == 2
    assert trace.spoken_duration_s == 3.5


def test_寫出_jsonl(tmp_path):
    path = tmp_path / "trace.jsonl"
    tracer = LatencyTracer(output_path=path)
    tracer.start("t1")
    tracer.mark("t1", MARK_SPEECH_END, at=0.0)
    tracer.mark("t1", MARK_SPEAK_FIRST_CHUNK, at=1.0)
    tracer.finish("t1", TurnPhase.DONE)

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["turn_id"] == "t1"
    assert record["phase"] == "done"
    assert record["segments_ms"]["ttfa"] == 1000.0


def test_報告含預算對照():
    """只看數字不知道 800ms 算好算壞，看到超支倍數才知道要修哪裡。"""
    tracer = LatencyTracer()
    tracer.start("t1")
    tracer.mark("t1", MARK_SPEECH_END, at=0.0)
    tracer.mark("t1", MARK_INPUT_FINAL, at=2.0)  # 遠超 400ms 預算
    tracer.mark("t1", MARK_SPEAK_FIRST_CHUNK, at=5.0)
    tracer.finish("t1", TurnPhase.DONE)

    report = tracer.report("t1")
    assert "TTFA" in report
    assert "超支" in report
    assert str(int(BUDGET_MS["ttfa"])) in report


def test_取消的_turn_不列入_summary():
    tracer = LatencyTracer()
    tracer.start("t1")
    tracer.mark("t1", MARK_SPEECH_END, at=0.0)
    tracer.mark("t1", MARK_SPEAK_FIRST_CHUNK, at=1.0)
    tracer.finish("t1", TurnPhase.CANCELLED, cancel_reason="barge_in")
    assert "沒有完成的 turn" in tracer.summary()


def test_多_turn_summary():
    tracer = LatencyTracer()
    for i, ttfa in enumerate([1.0, 2.0, 3.0]):
        tid = f"t{i}"
        tracer.start(tid)
        tracer.mark(tid, MARK_SPEECH_END, at=0.0)
        tracer.mark(tid, MARK_SPEAK_FIRST_CHUNK, at=ttfa)
        tracer.finish(tid, TurnPhase.DONE)

    summary = tracer.summary()
    assert "完成 3 turn" in summary
    assert "2000ms" in summary  # p50


def test_沒有記錄時的報告不爆掉():
    assert "沒有任何 turn" in LatencyTracer().report()
