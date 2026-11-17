"""uep.memtrace：階段標記、重複載入偵測與逐行落盤。"""
from __future__ import annotations

import time

from uep import memtrace


def test_phase_noop_when_not_started():
    with memtrace.phase("x"):
        pass  # 未啟動時不得拋錯


def test_phase_lines_and_duplicate_warning(tmp_path):
    tracer = memtrace.start(tmp_path, 0.05)
    assert tracer is not None
    try:
        for _ in range(2):  # 同一個模型識別載入兩次 -> 應出現 WARN
            with memtrace.phase("模型#abc", "model:test"):
                time.sleep(0.1)
        text = next(tmp_path.glob("mem-*.log")).read_text(encoding="utf-8")  # 不必先 stop：每行已 flush
    finally:
        memtrace.stop()
    assert "BEGIN" in text and "END" in text and "Δcommit" in text
    assert "重複載入：model:test 第 2 次" in text
