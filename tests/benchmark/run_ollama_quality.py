"""
run_ollama_quality.py — 真實 Ollama bge-m3 端對端檢索品質基準

獨立腳本，不經過 pytest（繞開 conftest.py 的 autouse monkeypatch）。
需要 Ollama 本地服務執行中且已有 bge-m3 模型。

執行方式：
    ..\\U.E.P-s-Core\\env\\Scripts\\python.exe tests/benchmark/run_ollama_quality.py

測量指標：
    - Recall@3 / Recall@5
    - MRR（Mean Reciprocal Rank）
    - 誤召回率（無關查詢應回傳空結果）
    - 中英混合檢索品質
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

# Windows cp950 無法顯示 emoji，強制 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# 確保專案根目錄在 sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from echo_memory import MemoryEngine
from tests.benchmark.run_quality import (
    compute_mrr,
    compute_precision_at_k,
    compute_recall_at_k,
)

# ── 真實中英混合對話資料集 ──────────────────────────────────────────

EPISODES = [
    # 食物偏好（中文）
    {
        "id": "food_01",
        "user": "我超喜歡吃牛肉麵，尤其是紅燒的",
        "agent": "紅燒牛肉麵確實是經典，台北有幾家很有名的店，像是永康街的牛肉麵。",
        "category": "food",
        "salience": 0.8,
    },
    {
        "id": "food_02",
        "user": "我對蝦子過敏，吃了會很嚴重",
        "agent": "了解，蝦子過敏是很常見的甲殼類過敏，我會記住避免推薦含蝦的食物。",
        "category": "food",
        "salience": 0.95,
    },
    {
        "id": "food_03",
        "user": "推薦一下台北好吃的火鍋店",
        "agent": "馬辣頂級麻辣鴛鴦火鍋和橘色涮涮屋都不錯，看你喜歡麻辣還是清淡的。",
        "category": "food",
        "salience": 0.6,
    },
    # 程式設計（中英混合）
    {
        "id": "prog_01",
        "user": "Python 的 GIL 是什麼？為什麼多線程效能不好？",
        "agent": "GIL（Global Interpreter Lock）是 CPython 的全局鎖，確保同一時間只有一個 thread 執行 Python bytecode。這就是為什麼 CPU-bound 任務用多線程沒有加速效果。",
        "category": "programming",
        "salience": 0.7,
    },
    {
        "id": "prog_02",
        "user": "asyncio 跟 threading 有什麼差別？",
        "agent": "asyncio 是協程式的單線程並行，適合 I/O-bound；threading 是搶占式多線程。asyncio 不受 GIL 影響因為它本來就是單線程。",
        "category": "programming",
        "salience": 0.7,
    },
    {
        "id": "prog_03",
        "user": "怎麼用 NetworkX 建立有向圖？",
        "agent": "用 nx.DiGraph() 建立有向圖，add_node() 加節點，add_edge(u, v, weight=1.0) 加邊。可以用 nx.shortest_path() 找最短路徑。",
        "category": "programming",
        "salience": 0.5,
    },
    # 旅遊（中文）
    {
        "id": "travel_01",
        "user": "京都有什麼必去的景點？",
        "agent": "清水寺、伏見稻荷大社、金閣寺是必去的。嵐山竹林和哲學之道也很值得一走。",
        "category": "travel",
        "salience": 0.6,
    },
    {
        "id": "travel_02",
        "user": "歐洲申根簽證怎麼辦？",
        "agent": "台灣護照免申根簽證，最長可停留 90 天。但要準備回程機票、保險證明和住宿訂單。",
        "category": "travel",
        "salience": 0.7,
    },
    # 記憶系統（英文技術）
    {
        "id": "mem_01",
        "user": "What is episodic memory in cognitive science?",
        "agent": "Episodic memory stores personally experienced events with temporal and spatial context. It's distinct from semantic memory which stores general knowledge without experiential detail.",
        "category": "memory_science",
        "salience": 0.8,
    },
    {
        "id": "mem_02",
        "user": "How does memory consolidation work during sleep?",
        "agent": "During sleep, the hippocampus replays recent experiences to the neocortex, strengthening important memories and weakening irrelevant ones. This is called memory consolidation or 'systems consolidation'.",
        "category": "memory_science",
        "salience": 0.75,
    },
]

# ── 查詢場景 ──────────────────────────────────────────────────────


@dataclass
class Scenario:
    id: str
    name: str
    query: str
    relevant_ids: list[str]
    empty_expected: bool = False
    min_recall_3: float = 0.5
    min_mrr: float = 0.3


SCENARIOS = [
    Scenario(
        id="OQ-01",
        name="食物偏好直接召回",
        query="我之前說過喜歡吃什麼？",
        relevant_ids=["food_01", "food_02", "food_03"],
        min_recall_3=0.33,
    ),
    Scenario(
        id="OQ-02",
        name="過敏記憶精準召回",
        query="我有什麼食物過敏嗎？",
        relevant_ids=["food_02"],
        min_mrr=0.5,
    ),
    Scenario(
        id="OQ-03",
        name="Python 並行相關",
        query="Python 多線程和 asyncio 的差異",
        relevant_ids=["prog_01", "prog_02"],
        min_recall_3=0.5,
    ),
    Scenario(
        id="OQ-04",
        name="圖資料庫相關",
        query="NetworkX 圖的操作方式",
        relevant_ids=["prog_03"],
        min_mrr=0.5,
    ),
    Scenario(
        id="OQ-05",
        name="日本旅遊",
        query="去日本旅遊有什麼建議？",
        relevant_ids=["travel_01"],
        min_recall_3=0.33,
    ),
    Scenario(
        id="OQ-06",
        name="英文記憶科學查詢",
        query="How does sleep affect memory?",
        relevant_ids=["mem_02", "mem_01"],
        min_recall_3=0.5,
    ),
    Scenario(
        id="OQ-07",
        name="中英混合查詢",
        query="episodic memory 跟 semantic memory 的差別",
        relevant_ids=["mem_01"],
        min_mrr=0.5,
    ),
    Scenario(
        id="OQ-08",
        name="無關查詢應為空（量子物理）",
        query="量子糾纏的 Bell 不等式是什麼？",
        relevant_ids=[],
        empty_expected=True,
    ),
    Scenario(
        id="OQ-09",
        name="無關查詢應為空（歷史）",
        query="法國大革命的起因是什麼？",
        relevant_ids=[],
        empty_expected=True,
    ),
    Scenario(
        id="OQ-10",
        name="高 salience 召回優先",
        query="有什麼重要的飲食注意事項？",
        relevant_ids=["food_02"],  # 蝦子過敏，salience=0.95
        min_mrr=0.5,
    ),
]


# ── 主程式 ────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 70)
    print("Echo Memory — 真實 Ollama bge-m3 檢索品質基準")
    print("=" * 70)

    # 1. 檢查 Ollama 可用性
    print("\n[1/4] 檢查 Ollama 環境...")
    try:
        import requests
        r = requests.get("http://localhost:11434/api/tags", timeout=5)
        models = [m["name"] for m in r.json().get("models", [])]
        if not any("bge-m3" in m for m in models):
            print(f"❌ Ollama 沒有 bge-m3 模型（可用模型：{models}）")
            return 1
        print("✅ Ollama 就緒，bge-m3 可用")
    except Exception as e:
        print(f"❌ 無法連接 Ollama: {e}")
        return 1

    # 2. 建立臨時引擎
    print("\n[2/4] 建立記憶引擎並注入 episodes...")
    tmp_dir = tempfile.mkdtemp(prefix="echo_benchmark_")
    try:
        engine = MemoryEngine(
            storage_path=tmp_dir,
            embedding_model="bge-m3",
            embedding_backend="ollama",
            ollama_base_url="http://localhost:11434",
            query_prefix="",
            namespace_id="benchmark",
        )

        # 注入 episodes
        ep_id_map: dict[str, str] = {}  # logical_id → actual_id
        for ep_data in EPISODES:
            actual_id = engine.store_episode(
                user_input=ep_data["user"],
                agent_response=ep_data["agent"],
                session_id=f"bench_{ep_data['category']}",
                salience=ep_data["salience"],
            )
            ep_id_map[ep_data["id"]] = actual_id

        print(f"   注入 {len(EPISODES)} 個 episodes")
        dim = engine.episodic._embeddings.shape[1] if engine.episodic._embeddings is not None else "?"
        print(f"   Embedding 維度: {dim}")

        # 3. 執行查詢場景
        print(f"\n[3/4] 執行 {len(SCENARIOS)} 個查詢場景...\n")
        results = []
        total_latency = 0.0

        for sc in SCENARIOS:
            t0 = time.time()
            ctx = engine.retrieve(
                query=sc.query,
                top_k=5,
                min_similarity=0.35,
            )
            latency = time.time() - t0
            total_latency += latency

            # 映射 actual IDs 回 logical IDs
            retrieved_actual = [ep.id for ep in ctx.episodes]
            retrieved_logical = []
            for actual in retrieved_actual:
                for logical, mapped in ep_id_map.items():
                    if mapped == actual:
                        retrieved_logical.append(logical)
                        break

            # 計算指標
            relevant = sc.relevant_ids
            recall_3 = compute_recall_at_k(retrieved_logical, relevant, 3)
            recall_5 = compute_recall_at_k(retrieved_logical, relevant, 5)
            prec_3 = compute_precision_at_k(retrieved_logical, relevant, 3)
            mrr = compute_mrr(retrieved_logical, relevant)
            is_empty = len(ctx.episodes) == 0

            passed = True
            if sc.empty_expected:
                passed = is_empty
            else:
                if recall_3 < sc.min_recall_3:
                    passed = False
                if mrr < sc.min_mrr:
                    passed = False

            status = "✅" if passed else "❌"
            print(
                f"  {status} {sc.id} {sc.name:<20s} "
                f"R@3={recall_3:.2f} R@5={recall_5:.2f} "
                f"P@3={prec_3:.2f} MRR={mrr:.2f} "
                f"{'空' if is_empty else f'{len(ctx.episodes)}筆'} "
                f"({latency*1000:.0f}ms)"
            )

            results.append({
                "scenario_id": sc.id,
                "name": sc.name,
                "recall_at_3": recall_3,
                "recall_at_5": recall_5,
                "precision_at_3": prec_3,
                "mrr": mrr,
                "is_empty": is_empty,
                "empty_expected": sc.empty_expected,
                "passed": passed,
                "latency_ms": latency * 1000,
                "retrieved": retrieved_logical,
            })

        # 4. 彙總報告
        print(f"\n[4/4] 彙總報告")
        print("-" * 70)

        non_empty = [r for r in results if not r["empty_expected"]]
        empty = [r for r in results if r["empty_expected"]]

        avg_recall_3 = sum(r["recall_at_3"] for r in non_empty) / len(non_empty) if non_empty else 0
        avg_recall_5 = sum(r["recall_at_5"] for r in non_empty) / len(non_empty) if non_empty else 0
        avg_mrr = sum(r["mrr"] for r in non_empty) / len(non_empty) if non_empty else 0
        avg_latency = total_latency / len(results) * 1000

        empty_correct = sum(1 for r in empty if r["is_empty"])
        empty_prec = empty_correct / len(empty) if empty else 1.0

        total_passed = sum(1 for r in results if r["passed"])

        print(f"  通過率:        {total_passed}/{len(results)} ({total_passed/len(results)*100:.0f}%)")
        print(f"  平均 Recall@3:  {avg_recall_3:.3f}")
        print(f"  平均 Recall@5:  {avg_recall_5:.3f}")
        print(f"  平均 MRR:       {avg_mrr:.3f}")
        print(f"  空結果精準率:   {empty_prec:.0%} ({empty_correct}/{len(empty)})")
        print(f"  平均延遲:       {avg_latency:.0f}ms")
        print("-" * 70)

        # 輸出 JSON 結果供後續分析
        report_path = Path(tmp_dir) / "benchmark_results.json"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "model": "bge-m3",
                    "backend": "ollama",
                    "episodes": len(EPISODES),
                    "scenarios": len(SCENARIOS),
                    "summary": {
                        "pass_rate": total_passed / len(results),
                        "avg_recall_3": avg_recall_3,
                        "avg_recall_5": avg_recall_5,
                        "avg_mrr": avg_mrr,
                        "empty_precision": empty_prec,
                        "avg_latency_ms": avg_latency,
                    },
                    "results": results,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        print(f"\n  結果 JSON: {report_path}")

        # MVP 門檻判定
        mvp_pass = (
            avg_recall_3 >= 0.4
            and avg_mrr >= 0.3
            and empty_prec >= 1.0
            and total_passed >= len(results) * 0.7
        )

        if mvp_pass:
            print("\n🎉 MVP 品質門檻通過！")
            return 0
        else:
            print("\n⚠️ 未達 MVP 品質門檻，需要調整檢索邏輯或資料集。")
            return 1

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
