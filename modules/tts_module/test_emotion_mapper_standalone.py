# -*- coding: utf-8 -*-
"""
Emotion Mapper 獨立測試 (不依賴 debug_helper)
"""

import sys
from pathlib import Path

# 添加項目根目錄到路徑
project_root = Path(__file__).resolve().parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


# 暫時替換 debug_log
def mock_debug_log(level, msg):
    pass


# Monkey patch
import modules.tts_module.emotion_mapper as em_module
em_module.debug_log = mock_debug_log

from modules.tts_module.emotion_mapper import EmotionMapper, quick_map


def test_basic_mapping():
    """測試基本映射功能"""
    print("=" * 60)
    print("測試 1: 基本情感映射")
    print("=" * 60)
    
    mapper = EmotionMapper(max_strength=0.3)
    
    # 測試純 joy
    status = {
        "joy": 1.0,
        "anger": 0.0,
        "sadness": 0.0,
        "fear": 0.0,
        "trust": 0.0,
        "disgust": 0.0,
        "surprise": 0.0,
        "anticipation": 0.0
    }
    
    vector = mapper.map_from_status(status)
    print(f"\n純 joy (1.0) → {vector}")
    print(f"  happy={vector[0]:.3f} (應該最高)")
    print(f"  總強度={sum(vector):.3f} (應該 ≤ 0.3)")
    
    assert vector[0] > 0, "happy 應該有值"
    assert sum(vector) <= 0.31, f"總強度超過限制: {sum(vector)}"
    
    print("✓ 測試通過")


def test_negative_emotions():
    """測試負面情感"""
    print("\n" + "=" * 60)
    print("測試 2: 負面情感映射")
    print("=" * 60)
    
    mapper = EmotionMapper(max_strength=0.3)
    
    # 測試 anger
    status = {
        "joy": 0.0,
        "anger": 1.0,
        "sadness": 0.0,
        "fear": 0.0,
        "trust": 0.0,
        "disgust": 0.0,
        "surprise": 0.0,
        "anticipation": 0.0
    }
    
    vector = mapper.map_from_status(status)
    print(f"\n純 anger (1.0) → {vector}")
    print(f"  angry={vector[1]:.3f} (應該最高)")
    print(f"  總強度={sum(vector):.3f}")
    
    assert vector[1] > 0, "angry 應該有值"
    
    print("✓ 測試通過")


def test_mixed_emotions():
    """測試混合情感"""
    print("\n" + "=" * 60)
    print("測試 3: 混合情感")
    print("=" * 60)
    
    mapper = EmotionMapper(max_strength=0.3)
    
    # 測試 joy + sadness
    status = {
        "joy": 0.5,
        "anger": 0.0,
        "sadness": 0.5,
        "fear": 0.0,
        "trust": 0.0,
        "disgust": 0.0,
        "surprise": 0.0,
        "anticipation": 0.0
    }
    
    vector = mapper.map_from_status(status)
    print(f"\njoy (0.5) + sadness (0.5) → {vector}")
    print(f"  happy={vector[0]:.3f}")
    print(f"  sad={vector[2]:.3f}")
    print(f"  總強度={sum(vector):.3f}")
    
    assert vector[0] > 0 and vector[2] > 0, "happy 和 sad 都應該有值"
    
    print("✓ 測試通過")


def test_arousal_valence():
    """測試 arousal 和 valence 影響"""
    print("\n" + "=" * 60)
    print("測試 4: Arousal & Valence 調整")
    print("=" * 60)
    
    mapper = EmotionMapper(max_strength=0.3)
    
    base_status = {
        "joy": 0.5,
        "anger": 0.0,
        "sadness": 0.0,
        "fear": 0.0,
        "trust": 0.3,
        "disgust": 0.0,
        "surprise": 0.0,
        "anticipation": 0.0
    }
    
    # 高 arousal
    vector_high = mapper.map_from_status(base_status, arousal=0.9, valence=0.7)
    print(f"\n高 arousal (0.9), 正 valence (0.7):")
    print(f"  向量: {[f'{v:.3f}' for v in vector_high]}")
    print(f"  happy={vector_high[0]:.3f}, surprised={vector_high[6]:.3f}")
    
    # 低 arousal
    vector_low = mapper.map_from_status(base_status, arousal=0.1, valence=0.7)
    print(f"\n低 arousal (0.1), 正 valence (0.7):")
    print(f"  向量: {[f'{v:.3f}' for v in vector_low]}")
    print(f"  calm={vector_low[7]:.3f}")
    
    print("✓ 測試通過")


def test_normalization():
    """測試歸一化功能"""
    print("\n" + "=" * 60)
    print("測試 5: 歸一化")
    print("=" * 60)
    
    mapper = EmotionMapper(max_strength=0.3)
    
    # 測試超過限制的向量
    over_limit = [0.5, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    normalized = mapper.normalize_vector(over_limit)
    
    print(f"\n原始向量: {over_limit}")
    print(f"  總和: {sum(over_limit):.3f}")
    print(f"\n歸一化後: {[f'{v:.3f}' for v in normalized]}")
    print(f"  總和: {sum(normalized):.3f}")
    print(f"  原始聲音保留: {(1 - sum(normalized)) * 100:.1f}%")
    
    assert sum(normalized) <= 0.31, f"歸一化失敗: {sum(normalized)}"
    
    print("✓ 測試通過")


def test_presets():
    """測試預設情感"""
    print("\n" + "=" * 60)
    print("測試 6: 預設情感")
    print("=" * 60)
    
    mapper = EmotionMapper(max_strength=0.3)
    
    presets = ["happy", "angry", "sad", "calm", "excited", "neutral"]
    
    for preset_name in presets:
        vector = mapper.get_preset_emotion(preset_name, strength=0.3)
        print(f"\n{preset_name}: {[f'{v:.3f}' for v in vector]}")
        print(f"  總強度: {sum(vector):.3f}")
        assert sum(vector) <= 0.31, f"{preset_name} 超過限制"
    
    print("\n✓ 測試通過")


def test_blending():
    """測試情感混合"""
    print("\n" + "=" * 60)
    print("測試 7: 情感混合")
    print("=" * 60)
    
    mapper = EmotionMapper(max_strength=0.3)
    
    # 混合 happy (60%) + excited (40%)
    blended = mapper.blend_emotions([
        ("happy", 0.6),
        ("excited", 0.4)
    ])
    
    print(f"\nhappy (60%) + excited (40%):")
    print(f"  向量: {[f'{v:.3f}' for v in blended]}")
    print(f"  總強度: {sum(blended):.3f}")
    
    assert sum(blended) <= 0.31, f"混合超過限制: {sum(blended)}"
    
    print("✓ 測試通過")


def test_quick_map():
    """測試快速映射函數"""
    print("\n" + "=" * 60)
    print("測試 8: 快速映射")
    print("=" * 60)
    
    vector = quick_map(joy=0.7, anger=0.3)
    print(f"\nquick_map(joy=0.7, anger=0.3):")
    print(f"  向量: {[f'{v:.3f}' for v in vector]}")
    print(f"  總強度: {sum(vector):.3f}")
    
    assert sum(vector) <= 0.31, f"快速映射超過限制: {sum(vector)}"
    
    print("✓ 測試通過")


def test_edge_cases():
    """測試邊界情況"""
    print("\n" + "=" * 60)
    print("測試 9: 邊界情況")
    print("=" * 60)
    
    mapper = EmotionMapper(max_strength=0.3)
    
    # 全零輸入
    empty_status = {e: 0.0 for e in EmotionMapper.PLUTCHIK_EMOTIONS}
    vector_zero = mapper.map_from_status(empty_status)
    print(f"\n全零輸入: {[f'{v:.3f}' for v in vector_zero]}")
    print(f"  總強度: {sum(vector_zero):.3f}")
    
    # 全滿輸入
    full_status = {e: 1.0 for e in EmotionMapper.PLUTCHIK_EMOTIONS}
    vector_full = mapper.map_from_status(full_status)
    print(f"\n全滿輸入: {[f'{v:.3f}' for v in vector_full]}")
    print(f"  總強度: {sum(vector_full):.3f} (應該 ≤ 0.3)")
    
    assert sum(vector_full) <= 0.31, f"全滿輸入超過限制: {sum(vector_full)}"
    
    print("✓ 測試通過")


def run_all_tests():
    """運行所有測試"""
    print("\n")
    print("╔" + "=" * 58 + "╗")
    print("║" + " " * 12 + "Emotion Mapper 測試套件" + " " * 21 + "║")
    print("╚" + "=" * 58 + "╝")
    
    try:
        test_basic_mapping()
        test_negative_emotions()
        test_mixed_emotions()
        test_arousal_valence()
        test_normalization()
        test_presets()
        test_blending()
        test_quick_map()
        test_edge_cases()
        
        print("\n" + "=" * 60)
        print("🎉 所有測試通過! Emotion Mapper 工作正常!")
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
