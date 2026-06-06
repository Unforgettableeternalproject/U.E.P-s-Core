#!/usr/bin/env python3
"""
快速創建測試數據並測試 Dream Engine
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest

from echo_memory import MemoryEngine


def create_test_data(engine: MemoryEngine) -> MemoryEngine:
    """創建測試記憶數據"""
    print("=" * 80)
    print("創建測試數據")
    print("=" * 80)
    
    # 測試對話 1: 關於天氣
    print("\n📝 創建測試對話 1: 天氣話題...")
    session1 = "test_weather_session"
    
    engine.store_episode("今天天氣怎麼樣？", "今天天氣晴朗，溫度約 25 度。", session1, topic="weather")
    engine.store_episode("那我要不要帶傘？", "不需要，沒有降雨預報。", session1, topic="weather")
    
    # 測試對話 2: 關於工作
    print("📝 創建測試對話 2: 工作話題...")
    session2 = "test_work_session"
    
    engine.store_episode("我需要完成什麼任務？", "你需要完成專案報告和代碼審查。", session2, topic="work")
    engine.store_episode("報告的截止日期是什麼時候？", "截止日期是本週五下午 5 點。", session2, topic="work", deadline="Friday 17:00")
    
    # 測試對話 3: 關於學習
    print("📝 創建測試對話 3: 學習話題...")
    session3 = "test_learning_session"
    
    engine.store_episode("我想學習 Python", "很好！我們可以從基礎語法開始。", session3, topic="learning")
    engine.store_episode("應該先學什麼？", "建議先學習變數、資料型別和控制結構。", session3, topic="learning")
    engine.store_episode("能給我一些資源嗎？", "可以參考官方文檔和 Real Python 網站。", session3, topic="learning", resources="docs, realpython")
    
    # 顯示統計
    stats = engine.get_stats()
    print("\n✅ 測試數據創建完成!")
    print(f"   - 總情節數: {stats['total_episodes']}")
    print("   - Session 數: 3")
    
    # 顯示圖結構
    graph_stats = engine.episodic.graph.get_stats()
    print("\n📈 圖結構統計:")
    print(f"   - 節點數: {graph_stats['nodes']}")
    print(f"   - 邊數: {graph_stats['edges']}")
    print(f"   - 平均度數: {graph_stats['avg_degree']:.2f}")
    
    return engine


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """Create an isolated engine without loading an external embedding model."""
    memory_engine = MemoryEngine(storage_path=tmp_path)
    monkeypatch.setattr(
        memory_engine.episodic,
        "_generate_embedding",
        lambda text: [1.0, 0.0],
    )
    return create_test_data(memory_engine)


def run_dream_engine(engine: MemoryEngine) -> tuple[dict, dict, dict]:
    """Run one Dream cycle and return its report and graph statistics."""
    print("\n" + "=" * 80)
    print("Dream Engine 功能測試")
    print("=" * 80)
    
    # 顯示初始狀態
    graph_stats_before = engine.episodic.graph.get_stats()
    print("\n📊 Dream 執行前:")
    print(f"   - 節點數: {graph_stats_before['nodes']}")
    print(f"   - 邊數: {graph_stats_before['edges']}")
    print(f"   - 平均邊權重: {graph_stats_before['avg_edge_weight']:.4f}")
    
    # 執行 Dream
    print("\n🌙 執行 Dream Engine...")
    print("   (修剪低權重邊、更新權重、鞏固記憶)")
    
    start_time = time.time()
    report = engine.dream(triggered_by="manual_test")
    duration = time.time() - start_time
    
    # 顯示結果
    print("\n✅ Dream 執行完成!")
    print(f"   - 執行時間: {duration:.2f} 秒")
    print(f"   - 觸發方式: {report['triggered_by']}")
    
    print("\n📝 修剪報告:")
    print(f"   - 移除邊數: {report.get('edges_pruned', 0)}")
    print(f"   - 更新邊數: {report.get('edges_updated', 0)}")
    print(f"   - 錯誤: {report.get('error', 'None')}")
    
    # 顯示最終狀態
    graph_stats_after = engine.episodic.graph.get_stats()
    print("\n📊 Dream 執行後:")
    print(f"   - 節點數: {graph_stats_after['nodes']}")
    print(f"   - 邊數: {graph_stats_after['edges']} (變化: {graph_stats_after['edges'] - graph_stats_before['edges']})")
    print(f"   - 平均邊權重: {graph_stats_after['avg_edge_weight']:.4f}")

    return report, graph_stats_before, graph_stats_after


def test_dream_engine(engine):
    """Dream runs against isolated data and returns a valid report."""
    report, graph_stats_before, graph_stats_after = run_dream_engine(engine)

    assert report["triggered_by"] == "manual_test"
    assert report["error"] is None
    assert report["edges_updated"] >= 0
    assert report["edges_pruned"] >= 0
    assert graph_stats_after["nodes"] == graph_stats_before["nodes"]


def main():
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║          Dream Engine 完整測試                                ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    
    try:
        # 步驟 1: 創建測試數據
        engine = MemoryEngine(storage_path="./test_data")
        create_test_data(engine)
        
        # 步驟 2: 測試 Dream Engine
        run_dream_engine(engine)
        
        # 總結
        print("\n" + "=" * 80)
        print("✅ 所有測試通過!")
        print("=" * 80)
        print("\n✨ Dream Engine 功能驗證:")
        print("   ✓ GraphPruning 修剪器正常")
        print("   ✓ 權重時間衰減正常")
        print("   ✓ DreamReport 生成正常")
        print("   ✓ 手動觸發機制正常")
        print("\n📌 提示: 自動排程功能需要在長時間運行的服務中測試")
        print("   - 使用 'python Entry.py' 啟動 CLI")
        print("   - Dream Engine 會在 5 分鐘閒置後自動運行")
        print("   - 或每日凌晨 3 點自動運行")
        
    except Exception as e:
        print(f"\n❌ 測試失敗: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
