"""健康告警的測試。

重點是**沒問題時要完全安靜**——會每次講話的告警，三天後就沒人在讀了，
那等於沒有告警。其餘各項是「多久沒動靜」的門檻。

執行：``python -m pytest agent_memory_spike/test_health_alert.py -q``
"""

from __future__ import annotations

import io
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
    assert "distill" in out["systemMessage"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "distill" in out["hookSpecificOutput"]["additionalContext"]


def _run_main(monkeypatch, capsys) -> tuple[str, str]:
    """以 hook 模式跑 main()：stdin 餵 payload、無參數，回傳 (stdout, stderr)。"""
    monkeypatch.setattr(sys, "argv", ["hook_health_alert.py"])
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"hook_event_name": "SessionStart"}'))
    assert health.main() == 0
    captured = capsys.readouterr()
    return captured.out, captured.err


def test_main_is_silent_when_everything_is_fine(work, monkeypatch, capsys):
    """沒異常時 stdout 必須完全空白——連空 JSON 都不行。"""
    out, _ = _run_main(monkeypatch, capsys)
    assert out == ""


def test_main_alert_has_user_visible_system_message(work, monkeypatch, capsys):
    """additionalContext 只給模型看，被略過了 13 晚；必須另有 systemMessage 給人看。"""
    (work / "pipeline_state.json").write_text(json.dumps({"last_run": {"results": [
        {"stage": "health", "ok": False, "summary": "[doctor] 發現 888 個問題："},
    ]}}), encoding="utf-8")
    _aged(work / "episodes", health.STALE_EPISODE_DAYS + 1)
    out, _ = _run_main(monkeypatch, capsys)
    payload = json.loads(out)
    msg = payload["systemMessage"]
    assert msg.startswith("⚠️ coding agent 記憶層異常：")
    assert "health" in msg and "（共 2 項）" in msg
    assert "**" not in msg and "\n" not in msg
    # 模型那份細節仍要保留
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "888" in ctx and "Stop hook" in ctx


def test_user_summary_truncates_long_first_alert():
    msg = health.format_user_summary(["很長" * 100])
    assert "…" in msg and "共" not in msg


# ⚠️ `--check` 必須跳過 stdin：實測手動執行時沒有人會關 stdin，`read()` 直接卡死
# （踩過，卡滿兩分鐘）。這條沒有寫成測試——要測就得起 subprocess 並等它逾時，
# 為一行 if 付那個代價不划算。改動 main() 的 stdin 那段時記得手動跑一次 `--check`。
