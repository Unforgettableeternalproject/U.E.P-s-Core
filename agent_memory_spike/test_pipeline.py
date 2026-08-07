"""Phase 3 C 管線機制的測試。

裁決本身沒辦法測（那是一次真的 LLM 呼叫），但**圍繞裁決的那圈可以**，
而出事的通常是那圈：鎖沒清、JSON 抽錯、失敗了還往下跑。

執行：``python -m pytest agent_memory_spike/test_pipeline.py -q``
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import pipeline  # noqa: E402
from pipeline import (  # noqa: E402
    LockBusy,
    acquire_lock,
    adjudicate_to_file,
    extract_json,
    release_lock,
    run_pipeline,
)


# --- 鎖 ---------------------------------------------------------------------

def test_lock_blocks_a_second_run(tmp_path):
    """兩條蒸餾疊在一起會重複寫入：watermark 是跑完才寫的，
    疊跑期間兩邊都看到「還沒做過」。"""
    lock = tmp_path / "pipeline.lock"
    acquire_lock(lock)
    with pytest.raises(LockBusy):
        acquire_lock(lock)
    release_lock(lock)
    acquire_lock(lock)  # 放掉之後可以再拿


def test_a_stale_lock_gets_taken_over(tmp_path, monkeypatch):
    """程序被 kill 時鎖不會自己消失，沒有接管機制管線會永遠停擺。"""
    lock = tmp_path / "pipeline.lock"
    acquire_lock(lock)
    old = time.time() - pipeline.LOCK_STALE_SECONDS - 60
    import os

    os.utime(lock, (old, old))
    acquire_lock(lock)  # 不該拋
    assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == os.getpid()


def test_lock_is_released_even_when_a_stage_explodes(tmp_path, monkeypatch):
    """階段炸掉還握著鎖的話，下一次排程會被自己擋在門外。"""
    lock = tmp_path / "pipeline.lock"
    monkeypatch.setattr(pipeline, "LOCK_PATH", lock)
    monkeypatch.setattr(pipeline, "STATE_PATH", tmp_path / "state.json")

    def boom(ctx):
        raise RuntimeError("炸了")

    monkeypatch.setattr(pipeline, "STAGES", [("boom", "測試用", boom)])
    acquire_lock(lock)
    release_lock(lock)
    assert run_pipeline(dry_run=False, max_groups=1, only=None) == 1


# --- JSON 抽取 --------------------------------------------------------------

def test_extract_json_from_a_fenced_block():
    reply = '我判完了，結果如下：\n\n```json\n{"verdicts": [{"id": "c-1"}]}\n```\n\n共 1 筆。'
    assert extract_json(reply) == {"verdicts": [{"id": "c-1"}]}


def test_extract_json_without_a_fence():
    """裁決者忘記加柵欄是常態，退路要救得回來。"""
    assert extract_json('結果：[{"id": "c-1"}] 以上。') == [{"id": "c-1"}]


def test_extract_json_returns_none_rather_than_garbage():
    """抽不出來要說抽不出來。**不能回一個空結構**——
    下游的 --ingest 收到零筆會看起來像正常跑完，那是靜默失敗。"""
    assert extract_json("我沒辦法完成這個任務。") is None


def test_adjudication_failure_writes_nothing(tmp_path, monkeypatch):
    """裁決回不出 JSON 時不該留下空檔案，理由同上。"""
    monkeypatch.setattr(pipeline, "adjudicate", lambda prompt, timeout=0: (True, "抱歉，我做不到"))
    target = tmp_path / "out" / "batch-00.json"
    ok, reason = adjudicate_to_file("...", target)
    assert not ok
    assert not target.exists()
    assert "沒有可解析的 JSON" in reason


def test_adjudication_lands_the_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "adjudicate",
                        lambda prompt, timeout=0: (True, '```json\n[{"id": "c-1"}]\n```'))
    target = tmp_path / "out" / "batch-00.json"
    ok, summary = adjudicate_to_file("...", target)
    assert ok and "1 筆" in summary
    assert json.loads(target.read_text(encoding="utf-8")) == [{"id": "c-1"}]


# --- 階段編排 ---------------------------------------------------------------

def test_a_failed_stage_stops_the_rest(tmp_path, monkeypatch):
    """語料壞掉時蒸餾只會蒸出錯的記憶，收斂沒跑完就校準則是白測。"""
    monkeypatch.setattr(pipeline, "LOCK_PATH", tmp_path / "lock")
    monkeypatch.setattr(pipeline, "STATE_PATH", tmp_path / "state.json")
    ran: list[str] = []

    def ok_stage(ctx):
        ran.append("ok")
        return True, ""

    def bad_stage(ctx):
        ran.append("bad")
        return False, "壞了"

    def never(ctx):
        ran.append("never")
        return True, ""

    monkeypatch.setattr(pipeline, "STAGES", [
        ("a", "", ok_stage), ("b", "", bad_stage), ("c", "", never),
    ])
    assert run_pipeline(dry_run=False, max_groups=1, only=None) == 1
    assert ran == ["ok", "bad"]


def test_dry_run_leaves_no_state(tmp_path, monkeypatch):
    """dry-run 要能安全地在正式排程之前跑，不能污染上次執行的紀錄。"""
    state = tmp_path / "state.json"
    monkeypatch.setattr(pipeline, "LOCK_PATH", tmp_path / "lock")
    monkeypatch.setattr(pipeline, "STATE_PATH", state)
    monkeypatch.setattr(pipeline, "STAGES", [("a", "", lambda ctx: (True, ""))])
    run_pipeline(dry_run=True, max_groups=1, only=None)
    assert not state.exists()


def test_only_runs_the_requested_stage(tmp_path, monkeypatch):
    """某一階段的裁決失敗時，前面完成的部分不該重來——蒸餾特別貴。"""
    monkeypatch.setattr(pipeline, "LOCK_PATH", tmp_path / "lock")
    monkeypatch.setattr(pipeline, "STATE_PATH", tmp_path / "state.json")
    ran: list[str] = []
    monkeypatch.setattr(pipeline, "STAGES", [
        ("a", "", lambda ctx: (ran.append("a"), (True, ""))[1]),
        ("b", "", lambda ctx: (ran.append("b"), (True, ""))[1]),
    ])
    run_pipeline(dry_run=False, max_groups=1, only="b")
    assert ran == ["b"]
