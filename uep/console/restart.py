"""Console 的旗標覆寫與重啟：``/restart`` 寫覆寫檔後以約定退出碼結束，``scripts\\console.bat`` 讀檔、重設環境並重新啟動。

覆寫檔只在同一次 bat 執行內累加（bat 啟動時刪除）；每輪重啟由 bat 從原始環境重建「基線＋覆寫」，
所以從覆寫檔移除某個 KEY ＝ 還原成 bat 基線（基線沒有的旗標就是未設定）。
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

RESTART_EXIT_CODE = 75
"""``/restart`` 結束行程的退出碼；bat 看到這個值才會重啟。"""
OVERRIDE_FILE_ENV = "UEP_CONSOLE_OVERRIDE_FILE"
"""bat 指定覆寫檔路徑的環境變數；未設＝``DEFAULT_OVERRIDE_FILE``。"""
RELAUNCH_ENV = "UEP_CONSOLE_RELAUNCH"
"""由 bat 設為 1，表示外層有迴圈會自動重啟。"""
DEFAULT_OVERRIDE_FILE = Path("outputs") / "e2e" / "console_overrides.env"

_KEY_RE = re.compile(r"^UEP_[A-Z0-9_]+$")
_UNSAFE_VALUE_CHARS = set('"%^&|<>!\r\n')
"""bat 的 ``set "KEY=VALUE"`` 與 for /f 讀檔會誤解的字元；含這些字元的值一律拒絕。"""

# 測試相關旗標（/flags 列出）：(環境變數名, 說明)。實際值從 settings 欄位（以 validation_alias 對應）讀取。
TEST_FLAGS: tuple[tuple[str, str], ...] = (
    ("UEP_TOOL_PATH", "工具路徑 legacy｜agent"),
    ("UEP_AGENT_LOOP_ENTRY", "route=agent_loop 入口"),
    ("UEP_TOOL_TESTKIT", "測試工具組（僅 agent 路徑）"),
    ("UEP_CONSOLE_INGRESS_ENDPOINT", "Console 視為已認證 owner"),
    ("UEP_BEHAVIOR_DRIVER", "Behavior driver shadow｜on"),
    ("UEP_SCHEDULE_PLANNER", "排程 planner off｜shadow｜on"),
    ("UEP_SCHEDULE_REST_WINDOW", "休息時段 fixed｜plan"),
    ("UEP_PRESENCE_ARBITER", "Presence arbiter off｜on"),
    ("UEP_DREAM_WINDOW_START", "夢境窗口起（C9）"),
    ("UEP_DREAM_WINDOW_END", "夢境窗口迄（C9）"),
    ("UEP_INFER_VOX_EMOTION_ENABLED", "VOX 情緒語音（C11）"),
    ("UEP_GATE_REQUIRE_INTERACTION", "gate 要求 interaction"),
    ("UEP_TOOL_MODULE_EVENTS", "module.* 就緒事件訂閱"),
    ("UEP_MODULE_COORDINATOR", "module coordinator（producer）"),
    ("UEP_MODULE_UNLOAD_RELEASE_RATIO", "模組卸載釋放比"),
    ("UEP_DECISION_PROPOSER", "決策 proposer"),
)


class RestartError(ValueError):
    """``/restart`` 參數不合法。"""


def parse_overrides(arg: str) -> dict[str, str]:
    """解析 ``KEY=VALUE ...``；KEY 必須是 ``UEP_`` 開頭的大寫識別字，值可為空（＝還原成基線）。"""
    result: dict[str, str] = {}
    for token in arg.split():
        key, sep, value = token.partition("=")
        if not sep:
            raise RestartError(f"「{token}」不是 KEY=VALUE 形式。")
        if not _KEY_RE.match(key):
            raise RestartError(f"只接受 UEP_ 開頭的旗標，拒絕「{key}」。")
        bad = _UNSAFE_VALUE_CHARS.intersection(value)
        if bad:
            raise RestartError(f"{key} 的值含 bat 無法安全處理的字元：{''.join(sorted(bad))!r}。")
        result[key] = value
    return result


def override_path() -> Path:
    raw = os.environ.get(OVERRIDE_FILE_ENV)
    return Path(raw) if raw else DEFAULT_OVERRIDE_FILE


def read_overrides(path: Path | None = None) -> dict[str, str]:
    """讀目前覆寫檔；檔案不存在或壞行一律略過。"""
    path = path or override_path()
    if not path.is_file():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        key, sep, value = line.partition("=")
        if sep and _KEY_RE.match(key):
            result[key] = value
    return result


def merge_overrides(current: Mapping[str, str], new: Mapping[str, str]) -> dict[str, str]:
    """累加：新值覆蓋舊值；值為空（``KEY=``）＝從覆寫檔移除，讓下一輪回到 bat 基線。"""
    merged = dict(current)
    for key, value in new.items():
        if value == "":
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def write_overrides(overrides: Mapping[str, str], path: Path | None = None) -> Path:
    """寫覆寫檔（每行 ``KEY=VALUE``）。"""
    path = path or override_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{k}={v}\n" for k, v in sorted(overrides.items())), encoding="utf-8")
    return path


def effective_flags(settings) -> list[tuple[str, str, str]]:
    """回傳 (環境變數名, 生效值, 說明)；值從 settings 實例讀（以欄位的 validation_alias 對應），不看環境變數字串。"""
    by_alias = {
        field.validation_alias: name
        for name, field in type(settings).model_fields.items()
        if isinstance(field.validation_alias, str)
    }
    rows = []
    for key, desc in TEST_FLAGS:
        attr = by_alias.get(key)
        value = "（無此欄位）" if attr is None else _show(getattr(settings, attr))
        rows.append((key, value, desc))
    return rows


def _show(value: object) -> str:
    if value is None:
        return "（未設）"
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)
