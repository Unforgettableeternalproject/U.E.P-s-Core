"""
run_latency.py - EchoMemory 延遲基準腳本

測量 EpisodicMemory.search() 的冷熱啟動延遲（p50/p95）。
不使用 sentence-transformers，直接生成隨機 4 維向量以避免模型依賴。

執行方式：
    python tests/benchmark/run_latency.py
    python tests/benchmark/run_latency.py --n-episodes 500 --n-queries 200
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

# 確保可以 import echo_memory
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from echo_memory.schemas.episode import Episode
from echo_memory.storage.episodic import EpisodicMemory

# ─── 輔助函式 ────────────────────────────────────────────────────────────────

def _random_4d_vector() -> list[float]:
    """生成隨機 4 維向量並 L2 正規化"""
    vec = np.array([random.random() for _ in range(4)], dtype=np.float32)
    norm = float(np.linalg.norm(vec))
    if norm == 0.0:
        return [0.25, 0.25, 0.25, 0.25]
    return (vec / norm).tolist()


def _build_test_episode(index: int) -> Episode:
    """生成測試用 Episode（不需要 sentence-transformers）"""
    return Episode(
        id=f"ep_latency_{index:06d}",
        user_input=f"測試輸入 {index}",
        agent_response=f"測試回應 {index}",
        session_id=f"session_latency_{index % 10}",
        content_summary=f"延遲測試情節 {index}",
        embedding=_random_4d_vector(),
        salience_score=random.random(),
    )


def _percentile(data: list[float], p: float) -> float:
    """計算百分位數"""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    idx = (len(sorted_data) - 1) * p / 100.0
    lower = int(idx)
    upper = min(lower + 1, len(sorted_data) - 1)
    frac = idx - lower
    return sorted_data[lower] * (1 - frac) + sorted_data[upper] * frac


# ─── 主要測試流程 ─────────────────────────────────────────────────────────────

def run_latency_benchmark(
    n_episodes: int = 100,
    n_queries: int = 50,
    top_k: int = 10,
    min_similarity: float = 0.0,
    warm_runs: int = 3,
) -> dict[str, float]:
    """
    執行延遲基準測試。

    Args:
        n_episodes: 注入的 episode 數量
        n_queries: 執行的查詢次數
        top_k: 每次查詢返回的結果數
        min_similarity: 相似度門檻（設為 0.0 確保有結果）
        warm_runs: 暖機查詢次數（不計入統計）

    Returns:
        包含 p50、p95、min、max 的字典（單位：毫秒）
    """
    with tempfile.TemporaryDirectory(prefix="echo_latency_") as tmpdir:
        storage_path = Path(tmpdir) / "episodic"

        print("[延遲基準] 初始化 EpisodicMemory...")
        mem = EpisodicMemory(
            storage_path=storage_path,
            embedding_model="all-MiniLM-L6-v2",
        )

        # 注入 Episodes
        print(f"[延遲基準] 注入 {n_episodes} 個 episodes...")
        t_inject_start = time.perf_counter()
        for i in range(n_episodes):
            ep = _build_test_episode(i)
            mem.write(ep)
        t_inject_elapsed = (time.perf_counter() - t_inject_start) * 1000
        print(f"[延遲基準] 注入完成，耗時 {t_inject_elapsed:.1f}ms（平均 {t_inject_elapsed/n_episodes:.2f}ms/episode）")

        # 生成查詢向量
        query_vectors = [_random_4d_vector() for _ in range(n_queries + warm_runs)]

        # 暖機（不計入統計）
        print(f"[延遲基準] 暖機 {warm_runs} 次...")
        for i in range(warm_runs):
            mem.search(query_vectors[i], top_k=top_k, min_similarity=min_similarity)

        # 正式測量
        print(f"[延遲基準] 開始測量 {n_queries} 次查詢延遲...")
        latencies_ms: list[float] = []

        for i in range(n_queries):
            qvec = query_vectors[warm_runs + i]
            t_start = time.perf_counter()
            mem.search(qvec, top_k=top_k, min_similarity=min_similarity)
            t_end = time.perf_counter()
            latencies_ms.append((t_end - t_start) * 1000)

        # 統計
        p50 = _percentile(latencies_ms, 50)
        p95 = _percentile(latencies_ms, 95)
        p99 = _percentile(latencies_ms, 99)
        lat_min = min(latencies_ms)
        lat_max = max(latencies_ms)
        lat_mean = statistics.mean(latencies_ms)

        return {
            "n_episodes": n_episodes,
            "n_queries": n_queries,
            "p50_ms": p50,
            "p95_ms": p95,
            "p99_ms": p99,
            "min_ms": lat_min,
            "max_ms": lat_max,
            "mean_ms": lat_mean,
        }


# ─── 主程式 ──────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="EchoMemory 延遲基準測試",
    )
    parser.add_argument("--n-episodes", type=int, default=100, help="注入 episode 數量（預設 100）")
    parser.add_argument("--n-queries", type=int, default=50, help="查詢次數（預設 50）")
    parser.add_argument("--top-k", type=int, default=10, help="每次查詢返回數量（預設 10）")
    parser.add_argument("--warm-runs", type=int, default=3, help="暖機次數（預設 3）")
    args = parser.parse_args()

    print("=" * 60)
    print("EchoMemory 延遲基準測試")
    print("=" * 60)

    result = run_latency_benchmark(
        n_episodes=args.n_episodes,
        n_queries=args.n_queries,
        top_k=args.top_k,
        warm_runs=args.warm_runs,
    )

    print("\n[結果]")
    print(f"  Episodes 數量 : {result['n_episodes']}")
    print(f"  查詢次數      : {result['n_queries']}")
    print(f"  P50 延遲      : {result['p50_ms']:.3f} ms")
    print(f"  P95 延遲      : {result['p95_ms']:.3f} ms")
    print(f"  P99 延遲      : {result['p99_ms']:.3f} ms")
    print(f"  最小延遲      : {result['min_ms']:.3f} ms")
    print(f"  最大延遲      : {result['max_ms']:.3f} ms")
    print(f"  平均延遲      : {result['mean_ms']:.3f} ms")

    # MVP 門檻驗證
    print("\n[MVP 門檻驗證]")
    p50_target = 100.0  # < 100ms（單元測試層級）
    p95_target = 1000.0  # < 1s（整合測試層級）

    p50_ok = result["p50_ms"] < p50_target
    p95_ok = result["p95_ms"] < p95_target

    print(f"  P50 < {p50_target:.0f}ms: {'PASS' if p50_ok else 'FAIL'} ({result['p50_ms']:.2f}ms)")
    print(f"  P95 < {p95_target:.0f}ms: {'PASS' if p95_ok else 'FAIL'} ({result['p95_ms']:.2f}ms)")

    if p50_ok and p95_ok:
        print("\n總結：PASS - 延遲符合 MVP 門檻")
        sys.exit(0)
    else:
        print("\n總結：FAIL - 延遲超出 MVP 門檻，需要優化")
        sys.exit(1)


if __name__ == "__main__":
    main()
