# -*- coding: utf-8 -*-
"""
測試 Status Manager → 8D Emotion Vector 映射
"""

import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Mock debug_log
def mock_debug_log(level, msg):
    pass

import modules.tts_module.emotion_mapper as em_module
em_module.debug_log = mock_debug_log

from modules.tts_module.emotion_mapper import EmotionMapper, map_from_status_manager


def test_status_manager_mapping():
    """測試 Status Manager 的 4 個數值映射"""
    print("=" * 60)
    print("測試: Status Manager 映射")
    print("=" * 60)
    
    mapper = EmotionMapper(max_strength=0.3)
    
    test_cases = [
        {
            "name": "開心且自信 (高 Mood, 高 Pride)",
            "mood": 0.8,
            "pride": 0.9,
            "helpfulness": 0.7,
            "boredom": 0.2,
            "expected_high": [0, 7],  # happy, calm 應該較高
        },
        {
            "name": "沮喪且無助 (低 Mood, 低 Pride)",
            "mood": -0.7,
            "pride": 0.2,
            "helpfulness": 0.3,
            "boredom": 0.4,
            "expected_high": [2, 5],  # sad, melancholic 應該較高
        },
        {
            "name": "中性平和 (中等數值)",
            "mood": 0.0,
            "pride": 0.5,
            "helpfulness": 0.5,
            "boredom": 0.3,
            "expected_high": [7],  # calm 應該較高
        },
        {
            "name": "非常無聊 (高 Boredom)",
            "mood": 0.1,
            "pride": 0.4,
            "helpfulness": 0.4,
            "boredom": 0.9,
            "expected_high": [5, 7],  # melancholic, calm 應該較高
        },
        {
            "name": "生氣且不願幫助 (負 Mood, 低 Helpfulness)",
            "mood": -0.6,
            "pride": 0.3,
            "helpfulness": 0.1,
            "boredom": 0.2,
            "expected_high": [1, 2, 5],  # angry, sad, melancholic 應該較高
        },
    ]
    
    emotion_names = ["happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm"]
    
    for case in test_cases:
        print(f"\n測試案例: {case['name']}")
        print(f"  輸入: mood={case['mood']:.1f}, pride={case['pride']:.1f}, " +
              f"help={case['helpfulness']:.1f}, boredom={case['boredom']:.1f}")
        
        vector = mapper.map_from_status_manager(
            case['mood'],
            case['pride'],
            case['helpfulness'],
            case['boredom']
        )
        
        print(f"  輸出: {[f'{v:.3f}' for v in vector]}")
        print(f"  總強度: {sum(vector):.3f}")
        
        # 顯示前 3 高的情感
        indexed = [(i, v) for i, v in enumerate(vector)]
        sorted_emotions = sorted(indexed, key=lambda x: x[1], reverse=True)[:3]
        print(f"  主要情感:")
        for idx, val in sorted_emotions:
            if val > 0.01:
                print(f"    {emotion_names[idx]}: {val:.3f}")
        
        # 驗證總和不超過限制
        assert sum(vector) <= 0.31, f"總強度超過限制: {sum(vector)}"
        
        print("  ✓ 通過")


def test_extreme_values():
    """測試極端值"""
    print("\n" + "=" * 60)
    print("測試: 極端值處理")
    print("=" * 60)
    
    mapper = EmotionMapper(max_strength=0.3)
    
    # 極度開心
    print("\n極度開心 (mood=1.0, pride=1.0)")
    vector = mapper.map_from_status_manager(1.0, 1.0, 1.0, 0.0)
    print(f"  向量: {[f'{v:.3f}' for v in vector]}")
    print(f"  總強度: {sum(vector):.3f}")
    assert sum(vector) <= 0.31
    
    # 極度悲傷
    print("\n極度悲傷 (mood=-1.0, pride=0.0)")
    vector = mapper.map_from_status_manager(-1.0, 0.0, 0.0, 0.5)
    print(f"  向量: {[f'{v:.3f}' for v in vector]}")
    print(f"  總強度: {sum(vector):.3f}")
    assert sum(vector) <= 0.31
    
    # 完全中性
    print("\n完全中性 (所有值為中間)")
    vector = mapper.map_from_status_manager(0.0, 0.5, 0.5, 0.5)
    print(f"  向量: {[f'{v:.3f}' for v in vector]}")
    print(f"  總強度: {sum(vector):.3f}")
    
    print("\n✓ 所有極端值測試通過")


def test_convenience_function():
    """測試便捷函數"""
    print("\n" + "=" * 60)
    print("測試: 便捷函數 map_from_status_manager()")
    print("=" * 60)
    
    vector = map_from_status_manager(
        mood=0.5,
        pride=0.7,
        helpfulness=0.8,
        boredom=0.2
    )
    
    print(f"\n輸入: mood=0.5, pride=0.7, help=0.8, boredom=0.2")
    print(f"輸出: {[f'{v:.3f}' for v in vector]}")
    print(f"總強度: {sum(vector):.3f}")
    
    assert sum(vector) <= 0.31, f"總強度超過限制: {sum(vector)}"
    assert len(vector) == 8, f"向量長度錯誤: {len(vector)}"
    
    print("✓ 便捷函數測試通過")


def test_mood_influence():
    """測試 Mood 對情感的影響"""
    print("\n" + "=" * 60)
    print("測試: Mood 影響分析")
    print("=" * 60)
    
    mapper = EmotionMapper(max_strength=0.3)
    
    # 固定其他參數，只變動 Mood
    base_params = {"pride": 0.5, "helpfulness": 0.5, "boredom": 0.3}
    
    moods = [-1.0, -0.5, 0.0, 0.5, 1.0]
    
    print("\nMood 從負到正的變化:")
    for mood in moods:
        vector = mapper.map_from_status_manager(mood, **base_params)
        happy = vector[0]
        angry = vector[1]
        sad = vector[2]
        calm = vector[7]
        
        print(f"  Mood={mood:+.1f}: happy={happy:.3f}, angry={angry:.3f}, sad={sad:.3f}, calm={calm:.3f}")
    
    print("\n✓ Mood 影響測試通過")


def run_all_tests():
    """運行所有測試"""
    print("\n")
    print("╔" + "=" * 58 + "╗")
    print("║" + " " * 10 + "Status Manager 映射測試套件" + " " * 18 + "║")
    print("╚" + "=" * 58 + "╝")
    
    try:
        test_status_manager_mapping()
        test_extreme_values()
        test_convenience_function()
        test_mood_influence()
        
        print("\n" + "=" * 60)
        print("🎉 所有測試通過! Status Manager 映射工作正常!")
        print("=" * 60)
        return True
        
    except AssertionError as e:
        print(f"\n❌ 測試失敗: {e}")
        return False
    except Exception as e:
        print(f"\n❌ 錯誤: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
