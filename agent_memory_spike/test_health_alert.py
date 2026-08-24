"""健康告警的測試。

重點是**沒問題時要完全安靜**——會每次講話的告警，三天後就沒人在讀了，
那等於沒有告警。其餘各項是「多久沒動靜」的門檻。

執行：``python -m pytest agent_memory_spike/test_health_alert.py -q``
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import hook_health_alert as health  # noqa: E402


def _aged(path: Path, days: float) -> Path:
    """把檔案的 mtime 往前推。門檻判的是「多久沒動靜」，只能這樣造。"""
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture()
def work(tmp_path, monkeypatch):
    """一套「一切正常」的檔案佈局，各測試再往裡面弄壞一項。"""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "pipeline-20260824.log").write_text("ok", encoding="utf-8")
    state = tmp_path / "pipeline_state.json"
    state.write_text(json.dumps({"last_run": {"results": [
        {"stage": "collect", "ok": True, "summary": "補上 0 輪"},
        {"stage": "health", "ok": True, "summary": "未發現不一致"},
    ]}}), encoding="utf-8")
    episodes = tmp_path / "episodes"
    episodes.mkdir()
    injections = tmp_path / "injections.jsonl"
    injections.write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(health, "LOG_DIR", logs)
    monkeypatch.setattr(health, "STATE_PATH", state)
    monkeypatch.setattr(health, "EPISODE_DIR", episodes)
    monkeypatch.setattr(health, "INJECTION_LOG", injections)
    return tmp_path


def test_silent_when_everything_is_fine(work):
    """一切正常就一個字都不能講。"""
    assert health.collect_alerts() == []
    assert health.run() is None


def test_pipeline_failure_is_reported(work):
    (work / "pipeline_state.json").write_text(json.dumps({"last_run": {"results": [
        {"stage": "collect", "ok": True, "summary": "補上 0 輪"},
        {"stage": "health", "ok": False, "summary": "[doctor] 發現 888 個問題："},
    ]}}), encoding="utf-8")
    alerts = health.collect_alerts()
    assert len(alerts) == 1
    assert "health" in alerts[0] and "888" in alerts[0]


def test_stale_schedule_is_reported(work):
    """管線失敗至少留得下 log；排程掛了連 log 都不會有，那更難察覺。"""
    _aged(work / "logs" / "pipeline-20260824.log", health.STALE_PIPELINE_DAYS + 1)
    assert any("排程可能停了" in a for a in health.collect_alerts())


def test_missing_log_dir_is_reported(work, monkeypatch):
    monkeypatch.setattr(health, "LOG_DIR", work / "nope")
    assert any("排程可能沒掛載" in a for a in health.collect_alerts())


def test_frozen_corpus_is_reported(work):
    """收料停掉是最根本的故障：下游全部還會用舊資料照常運作。"""
    _aged(work / "episodes", health.STALE_EPISODE_DAYS + 1)
    assert any("Stop hook" in a for a in health.collect_alerts())


def test_silent_injection_is_reported(work):
    _aged(work / "injections.jsonl", health.SILENT_INJECT_DAYS + 1)
    assert any("記憶被注入" in a for a in health.collect_alerts())


def test_injection_gap_within_threshold_is_silent(work):
    """注入本來就稀疏（約 18%/編輯輪），幾天沒有是正常的，不能喊。"""
    _aged(work / "injections.jsonl", health.SILENT_INJECT_DAYS - 1)
    assert health.collect_alerts() == []


def test_broken_state_file_does_not_raise(work):
    """監控自己壞掉的代價是看不到警訊，不該是不能工作。"""
    (work / "pipeline_state.json").write_text("{ 壞掉的 JSON", encoding="utf-8")
    assert health.collect_alerts() == []


def test_output_shape_is_session_start_context(work):
    (work / "pipeline_state.json").write_text(json.dumps({"last_run": {"results": [
        {"stage": "distill", "ok": False, "summary": "零筆進帳"},
    ]}}), encoding="utf-8")
    out = health.run()
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "distill" in out["hookSpecificOutput"]["additionalContext"]


# ⚠️ `--check` 必須跳過 stdin：實測手動執行時沒有人會關 stdin，`read()` 直接卡死
# （踩過，卡滿兩分鐘）。這條沒有寫成測試——要測就得起 subprocess 並等它逾時，
# 為一行 if 付那個代價不划算。改動 main() 的 stdin 那段時記得手動跑一次 `--check`。
