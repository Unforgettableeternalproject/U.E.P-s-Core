"""SessionStart 健康告警：記憶層停止工作時主動說一聲。

**存在的理由是一次實際發生的事故。** 2026-08-22 起夜間管線因為 doctor 誤報
連停三天、注入連掛四天，而 `--doctor` 全綠、排程照跑、hook 也沒報錯——
表面上一切正常，是 8/24 手動翻 log 才發現的。

這類系統的失敗模式不是壞掉，是**安靜地什麼都不做**。而「什麼都不做」與
「這段時間剛好沒有可注入的記憶」在任何單一指標上長得一模一樣，
所以只能靠「多久沒有動靜」來分辨。

刻意的設計：

- **沒問題就完全不輸出。** 每個 session 都講一次話的告警等於沒有告警，
  三天之後就沒人在讀了。
- **不注入任何記憶。** 這支只做健康檢查。SessionStart 的記憶注入
  （`hook_session_start.py`）仍刻意未掛載，不能藉著告警把它一起偷渡上去。
- **任何例外都吞掉並 exit 0。** 告警機制本身絕不能擋住 session——
  監控壞掉的代價是看不到警訊，不該是不能工作。

掛載（全域 `~/.claude/settings.json` 的 `SessionStart`）：
    python <這支的絕對路徑>

手動檢查（會印出結論，正常時也印）：
    python hook_health_alert.py --check
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

WORK_DIR = Path.home() / ".claude" / "agent-memory-spike"
STATE_PATH = WORK_DIR / "pipeline_state.json"
LOG_DIR = WORK_DIR / "logs"
INJECTION_LOG = WORK_DIR / "injections.jsonl"
EPISODE_DIR = WORK_DIR / "episodes"

# 各項的容忍天數。定得比實際週期寬一格，寧可晚一天發現，
# 也不要因為「昨天剛好沒編輯任何檔案」這種正常情況每天喊狼來了。
STALE_PIPELINE_DAYS = 2    # 排程每日 03:30，兩天沒 log 就是真的沒跑
STALE_EPISODE_DAYS = 3     # 收料停掉代表 Stop hook 掛了，最根本的故障
SILENT_INJECT_DAYS = 7     # 注入本來就稀疏（約 18%/編輯輪），要放寬


def _age_days(path: Path) -> float | None:
    """檔案／目錄有多久沒被動過。取不到就回 None（當作無法判斷，不報）。"""
    try:
        return (time.time() - path.stat().st_mtime) / 86400
    except OSError:
        return None


def _newest_pipeline_log() -> tuple[Path | None, float | None]:
    try:
        logs = sorted(LOG_DIR.glob("pipeline-*.log"))
    except OSError:
        return None, None
    if not logs:
        return None, None
    newest = logs[-1]
    return newest, _age_days(newest)


def collect_alerts() -> list[str]:
    """回傳要告警的句子。沒問題就是空 list。"""
    alerts: list[str] = []

    # 1. 管線上次執行卡在哪個階段。health 是硬閘門，卡住就代表蒸餾與校準全部沒跑
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        for result in (state.get("last_run") or {}).get("results") or []:
            if not result.get("ok"):
                summary = str(result.get("summary") or "").strip()
                alerts.append(
                    f"夜間管線上次卡在 **{result.get('stage')}** 階段：{summary[:120]}"
                    "（後續階段全部沒跑）")
                break
    except (OSError, json.JSONDecodeError, AttributeError):
        pass

    # 2. 排程本身有沒有在跑。管線失敗至少還留得下 log，排程掛了連 log 都不會有——
    #    後者更難察覺，因為所有既有紀錄看起來都還是好的
    newest, age = _newest_pipeline_log()
    if newest is None:
        alerts.append("找不到任何管線 log，排程可能沒掛載")
    elif age is not None and age > STALE_PIPELINE_DAYS:
        alerts.append(f"管線已 {age:.0f} 天沒有執行紀錄（最新：{newest.name}），排程可能停了")

    # 3. 收料有沒有停。Stop hook 掛掉的話語料就此凍結，
    #    而下游的一切（蒸餾、校準、注入）都還會用舊資料照常運作
    age = _age_days(EPISODE_DIR)
    if age is not None and age > STALE_EPISODE_DAYS:
        alerts.append(f"語料已 {age:.0f} 天沒有新增，Stop hook 可能沒在跑")

    # 4. 注入有沒有掛零。這項最鬆：注入本來就稀疏，連續一週掛零才值得看一眼
    age = _age_days(INJECTION_LOG)
    if age is not None and age > SILENT_INJECT_DAYS:
        alerts.append(f"已 {age:.0f} 天沒有任何記憶被注入，可能是 scope 對不上（例如 repo 改名）")

    return alerts


def format_alert(alerts: list[str]) -> str:
    lines = ["<agent-memory-health>",
             "⚠️ coding agent 記憶層有異常，這是自動健檢的結果："]
    lines += [f"- {a}" for a in alerts]
    lines.append("查法：`python agent_memory_spike/hook_stop.py --doctor`、"
                 "`~/.claude/agent-memory-spike/logs/`")
    lines.append("</agent-memory-health>")
    return "\n".join(lines)


def run() -> dict[str, Any] | None:
    alerts = collect_alerts()
    if not alerts:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": format_alert(alerts),
        }
    }


def _configure_streams() -> None:
    """把 stdout/stderr 釘成 UTF-8。

    🚨 **不做這件事的話，告警只在「有話要說」的時候炸。** Windows 的
    console 預設是 cp950，編不出 `⚠️`（實測 UnicodeEncodeError），
    而正常路徑不輸出任何東西 —— 於是平常一切正常，真的出事那次才會壞掉，
    壞法還是 hook 拋例外。監控自己的失敗模式跟它要監控的東西同型。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def main() -> int:
    _configure_streams()
    parser = argparse.ArgumentParser(description="記憶層健康告警（SessionStart）")
    parser.add_argument("--check", action="store_true",
                        help="手動檢查，正常時也印出結論")
    args = parser.parse_args()

    # payload 這支用不到（檢查的是檔案系統狀態，與是哪個 session 無關），
    # 但當成 hook 跑時仍要讀掉，否則寫入端可能因為管線沒人收而阻塞。
    # **`--check` 一定要跳過**：手動執行時 stdin 沒有人會關，read() 會直接卡死
    if not args.check:
        try:
            sys.stdin.read()
        except (OSError, ValueError):
            pass

    try:
        alerts = collect_alerts()
    except Exception as exc:  # noqa: BLE001 — 監控壞掉不該擋住 session
        print(f"[health] 檢查失敗（不影響 session）: {exc}", file=sys.stderr)
        return 0

    if args.check:
        if alerts:
            print(format_alert(alerts), file=sys.stderr)
        else:
            print("[health] 一切正常：管線有在跑、語料有在長、注入沒有掛零", file=sys.stderr)
        return 0

    if alerts:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": format_alert(alerts),
            }
        }, ensure_ascii=False))
        print(f"[health] {len(alerts)} 項異常已告警", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
