"""
run_quality.py - EchoMemory MVP 品質指標計算腳本

計算 Recall@K、Precision@K、MRR、空結果精準率等指標。
可作為獨立腳本執行，也可從測試套件匯入函式使用。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# ─── 核心指標函式 ────────────────────────────────────────────────────────────

def compute_recall_at_k(retrieved_ids: list[str], relevant_ids: list[str], k: int) -> float:
    """
    計算 Recall@K。

    Args:
        retrieved_ids: 檢索結果 ID 列表（已排序）
        relevant_ids: 相關 ID 列表
        k: 計算前 K 個結果

    Returns:
        Recall@K 分數（0.0 ~ 1.0）。若 relevant_ids 為空則返回 1.0。
    """
    top_k = retrieved_ids[:k]
    if not relevant_ids:
        return 1.0
    return len(set(top_k) & set(relevant_ids)) / len(relevant_ids)


def compute_precision_at_k(retrieved_ids: list[str], relevant_ids: list[str], k: int) -> float:
    """
    計算 Precision@K。

    Args:
        retrieved_ids: 檢索結果 ID 列表（已排序）
        relevant_ids: 相關 ID 列表
        k: 計算前 K 個結果

    Returns:
        Precision@K 分數（0.0 ~ 1.0）。若 top_k 為空則返回 0.0。
    """
    top_k = retrieved_ids[:k]
    if not top_k:
        return 0.0
    return len(set(top_k) & set(relevant_ids)) / len(top_k)


def compute_mrr(retrieved_ids: list[str], relevant_ids: list[str]) -> float:
    """
    計算 MRR（Mean Reciprocal Rank）。

    Args:
        retrieved_ids: 檢索結果 ID 列表（已排序）
        relevant_ids: 相關 ID 列表

    Returns:
        MRR 分數（0.0 ~ 1.0）。若無相關結果則返回 0.0。
    """
    for i, rid in enumerate(retrieved_ids):
        if rid in relevant_ids:
            return 1.0 / (i + 1)
    return 0.0


def compute_empty_precision(results: list[dict]) -> float:
    """
    計算空結果精準率。

    評估「應該返回空結果的 scenario」中，實際返回空結果的比例。

    Args:
        results: 每個 scenario 的結果字典，需包含：
            - empty_expected (bool): 是否預期為空結果
            - is_empty (bool): 實際是否為空結果

    Returns:
        空結果精準率（0.0 ~ 1.0）。若無應空的 scenario 則返回 1.0。
    """
    empty_scenarios = [r for r in results if r["empty_expected"]]
    if not empty_scenarios:
        return 1.0
    correct = sum(1 for r in empty_scenarios if r["is_empty"])
    return correct / len(empty_scenarios)


# ─── 摘要報告 ────────────────────────────────────────────────────────────────

def generate_report(scenario_results: list[dict[str, Any]]) -> str:
    """
    根據 scenario 結果列表生成品質報告。

    Args:
        scenario_results: 每個 scenario 的結果，包含指標計算結果

    Returns:
        格式化的報告字串（Markdown）
    """
    lines: list[str] = ["## 品質基準報告\n"]

    total = len(scenario_results)
    passed = sum(1 for r in scenario_results if r.get("passed", False))
    lines.append("### 概要")
    lines.append(f"- 總 scenario 數：{total}")
    lines.append(f"- 通過：{passed}/{total} ({passed/total*100:.1f}%)\n" if total > 0 else "- 通過：0/0\n")

    empty_prec = compute_empty_precision(scenario_results)
    lines.append(f"- 空結果精準率：{empty_prec:.2%}\n")

    lines.append("### Scenario 明細")
    lines.append("| ID | 名稱 | Recall@3 | Precision@3 | MRR | 空結果 | 通過 |")
    lines.append("|----|----- |----------|-------------|-----|--------|------|")

    for r in scenario_results:
        sc_id = r.get("scenario_id", "?")
        name = r.get("name", "?")
        rec = r.get("recall_at_3", 0.0)
        prec = r.get("precision_at_3", 0.0)
        mrr = r.get("mrr", 0.0)
        is_empty = "是" if r.get("is_empty", False) else "否"
        status = "PASS" if r.get("passed", False) else "FAIL"
        lines.append(f"| {sc_id} | {name} | {rec:.2f} | {prec:.2f} | {mrr:.2f} | {is_empty} | {status} |")

    return "\n".join(lines)


# ─── 主程式 ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    """
    獨立執行模式：載入 benchmark_dataset.json 並印出指標摘要。

    注意：此模式不實際執行 MemoryEngine，僅示範指標計算函式的用法。
    實際的端對端基準測試請使用：
        pytest tests/test_benchmark_quality.py -v
    """
    fixtures_dir = Path(__file__).parent.parent / "fixtures"
    dataset_path = fixtures_dir / "benchmark_dataset.json"

    if not dataset_path.exists():
        print(f"[錯誤] 找不到資料集：{dataset_path}", file=sys.stderr)
        sys.exit(1)

    with open(dataset_path, encoding="utf-8") as f:
        dataset = json.load(f)

    scenarios = dataset.get("scenarios", [])
    print(f"載入資料集：{dataset.get('description', '未知')}")
    print(f"Episodes 總數：{len(dataset.get('episodes', []))}")
    print(f"Scenarios 總數：{len(scenarios)}\n")

    # 示範：對每個 scenario 印出基本資訊
    print("Scenario 列表：")
    for sc in scenarios:
        print(
            f"  [{sc['id']}] {sc['name']}"
            f" - 相關 IDs: {sc['expected']['relevant_ids']}"
            f" | 預期空結果: {sc['expected']['empty_result_expected']}"
        )

    print("\n品質指標函式已就緒。請使用 pytest 執行完整基準測試：")
    print("  pytest tests/test_benchmark_quality.py -v")
