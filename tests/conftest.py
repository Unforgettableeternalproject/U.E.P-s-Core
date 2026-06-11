"""
tests/conftest.py - 全域測試配置與 fixtures

由 pytest 自動載入，提供所有測試模組共用的 fixtures。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


# ─── 品質基準資料集 Fixtures ──────────────────────────────────────────────────

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_DATASET_PATH = _FIXTURES_DIR / "benchmark_dataset.json"


@pytest.fixture(scope="session")
def benchmark_dataset() -> dict[str, Any]:
    """
    載入 benchmark_dataset.json。

    Scope: session（整個測試執行期間只載入一次）。
    若資料集不存在，測試將以明確錯誤訊息失敗。
    """
    if not _DATASET_PATH.exists():
        pytest.fail(
            f"找不到品質基準資料集：{_DATASET_PATH}\n"
            "請確認 tests/fixtures/benchmark_dataset.json 已存在。"
        )
    with open(_DATASET_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="session")
def episodes_by_id(benchmark_dataset: dict[str, Any]) -> dict[str, dict]:
    """
    建立 episode_id -> episode dict 的快速查詢表。

    Scope: session（與 benchmark_dataset 相同生命週期）。
    """
    return {ep["id"]: ep for ep in benchmark_dataset.get("episodes", [])}
