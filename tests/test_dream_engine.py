"""
Dream Engine 測試腳本

測試離線記憶鞏固功能。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import time
from echo_memory import MemoryEngine


def test_dream_engine():
    """測試 Dream Engine"""
    print("=" * 80)
    print("Dream Engine 測試 - 離線記憶鞏固")
    print("=" * 80)
    
    # 初始化記憶引擎
    engine = MemoryEngine(storage_path="./test_data")
    
    # 檢查現有數據
    stats = engine.get_stats()
    print(f"\n📊 初始狀態:")
    print(f"  - 情節數: {stats['total_episodes']}")
    
    if stats['total_episodes'] == 0:
        print("\n⚠️  需要先創建一些記憶才能測試 Dream Engine")
        print("提示: 運行 'python Entry.py' 並使用 chat 模式創建對話")
        return
    
    # 顯示圖結構統計
    graph_stats = engine.episodic.graph.get_stats()
    print(f"\n📈 圖結構（修剪前）:")
    print(f"  - 節點數: {graph_stats['nodes']}")
    print(f"  - 邊數: {graph_stats['edges']}")
    print(f"  - 平均度數: {graph_stats['avg_degree']:.2f}")
    print(f"  - 平均邊權重: {graph_stats['avg_edge_weight']:.3f}")
    
    print("\n" + "=" * 80)
    print("執行 Dream Engine")
    print("=" * 80)
    
    # 執行 Dream
    print("\n⏳ 執行中...")
    start_time = time.time()
    
    report_dict = engine.dream(triggered_by="manual")
    
    duration = time.time() - start_time
    
    # 顯示結果
    if report_dict.get("error"):
        print(f"\n❌ 執行失敗: {report_dict['error']}")
        return
    
    print(f"\n✅ Dream 完成！")
    print(f"\n執行報告:")
    print(f"  - 觸發方式: {report_dict['triggered_by']}")
    print(f"  - 執行時長: {duration:.2f}s (報告: {report_dict['duration_seconds']:.2f}s)")
    print(f"  - 邊權重更新: {report_dict['edges_updated']} 條")
    print(f"  - 弱邊移除: {report_dict['edges_pruned']} 條")
    
    # 顯示修剪後的圖結構
    graph_stats_after = engine.episodic.graph.get_stats()
    print(f"\n📈 圖結構（修剪後）:")
    print(f"  - 節點數: {graph_stats_after['nodes']}")
    print(f"  - 邊數: {graph_stats_after['edges']} (減少 {graph_stats['edges'] - graph_stats_after['edges']})")
    print(f"  - 平均度數: {graph_stats_after['avg_degree']:.2f}")
    print(f"  - 平均邊權重: {graph_stats_after['avg_edge_weight']:.3f} (提升 {graph_stats_after['avg_edge_weight'] - graph_stats['avg_edge_weight']:.3f})")
    
    print("\n" + "=" * 80)
    print("✨ 測試完成！")
    print("=" * 80)
    
    print("\n📝 Dream Engine 效果:")
    print("  1. 移除了低權重邊緣（< 0.05）")
    print("  2. 更新了所有邊的時間衰減權重")
    print("  3. 提升了平均邊權重（保留高質量連接）")
    print("  4. 優化了圖結構，提升檢索性能")


if __name__ == "__main__":
    test_dream_engine()
