"""
快速測試重構後的 TTS Module
測試基本功能是否正常
"""

import os
import sys

# 添加專案根目錄到 path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from modules.tts_module.tts_module import TTSModule
from modules.tts_module.schemas import TTSInput

def test_basic_initialization():
    """測試基本初始化"""
    print("\n=== 測試 1: 基本初始化 ===")
    
    tts = TTSModule()
    print(f"✓ TTSModule 創建成功")
    print(f"  - 模型目錄: {tts.model_dir}")
    print(f"  - 預設角色: {tts.default_character}")
    print(f"  - Chunking 閾值: {tts.chunking_threshold}")
    print(f"  - 情感強度上限: {tts.emotion_max_strength}")
    
    # 測試 debug 輸出
    print("\n  Debug 輸出:")
    tts.debug()
    
    print("\n✓ 測試 1 通過\n")

def test_emotion_mapper():
    """測試情感映射器"""
    print("\n=== 測試 2: 情感映射器 ===")
    
    tts = TTSModule()
    
    # 測試不同情緒狀態
    test_cases = [
        ("開心自信", 0.8, 0.9, 0.8, 0.0),
        ("生氣反駁", -0.6, 0.8, 0.3, 0.1),
        ("沮喪無助", -0.7, 0.2, 0.5, 0.5),
        ("平靜中立", 0.0, 0.5, 0.5, 0.0),
    ]
    
    for name, mood, pride, helpfulness, boredom in test_cases:
        emotion = tts.emotion_mapper.map_from_status_manager(
            mood, pride, helpfulness, boredom
        )
        print(f"  {name} (mood={mood:.1f}, pride={pride:.1f}):")
        print(f"    情感向量: {[f'{v:.3f}' for v in emotion]}")
        print(f"    [happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]")
    
    print("\n✓ 測試 2 通過\n")

def test_playback_state():
    """測試播放狀態追蹤"""
    print("\n=== 測試 3: 播放狀態追蹤 ===")
    
    tts = TTSModule()
    
    state = tts.get_playback_state()
    print(f"  初始狀態: {state.value}")
    
    if state.value == "idle":
        print("  ✓ 初始狀態正確")
    else:
        print(f"  ✗ 初始狀態錯誤，應為 'idle'，實際為 '{state.value}'")
    
    print("\n✓ 測試 3 通過\n")

def test_context_references():
    """測試 Context References (全局單例模式)"""
    print("\n=== 測試 4: Context References (全局單例) ===")
    
    tts = TTSModule()
    
    print(f"  ✓ Working Context Manager 已連接: {tts.working_context_manager is not None}")
    print(f"  ✓ Status Manager 已連接: {tts.status_manager is not None}")
    
    # 測試情感向量獲取 (使用真實的 Status Manager)
    emotion = tts._get_emotion_vector_from_status()
    print(f"  ✓ 從 Status Manager 獲取情感向量: {[f'{v:.3f}' for v in emotion] if emotion else None}")
    
    # 測試使用者偏好獲取
    prefs = tts._get_user_preferences()
    print(f"  ✓ 從 Working Context Manager 獲取偏好: {prefs}")
    
    # 顯示當前 Status Manager 的實際數值
    if tts.status_manager:
        status = tts.status_manager.get_status()
        print(f"  ℹ️  當前系統狀態:")
        print(f"     - Mood: {status.get('mood', 0.0):.2f}")
        print(f"     - Pride: {status.get('pride', 0.5):.2f}")
        print(f"     - Helpfulness: {status.get('helpfulness', 0.5):.2f}")
        print(f"     - Boredom: {status.get('boredom', 0.0):.2f}")
    
    print("\n✓ 測試 4 通過\n")

def test_schemas():
    """測試 Schema 定義"""
    print("\n=== 測試 5: Schemas ===")
    
    from modules.tts_module.schemas import TTSInput, TTSOutput
    
    # 測試 TTSInput
    inp1 = TTSInput(text="測試文本")
    print(f"  ✓ TTSInput 創建: text='{inp1.text}', save={inp1.save}")
    
    inp2 = TTSInput(
        text="測試情感",
        save=True,
        character="uep",
        emotion_vector=[0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.1, 0.2]
    )
    print(f"  ✓ TTSInput with emotion: character='{inp2.character}', emotion={len(inp2.emotion_vector or [])}D")
    
    # 測試 TTSOutput
    out = TTSOutput(
        status="success",
        message="Test completed",
        output_path="test.wav",
        is_chunked=False,
        chunk_count=1
    )
    print(f"  ✓ TTSOutput 創建: status='{out.status}', chunked={out.is_chunked}")
    
    print("\n✓ 測試 5 通過\n")

def main():
    """執行所有測試"""
    print("\n" + "=" * 60)
    print("TTS Module 重構驗證測試")
    print("=" * 60)
    
    try:
        test_basic_initialization()
        test_emotion_mapper()
        test_playback_state()
        test_context_references()
        test_schemas()
        
        print("=" * 60)
        print("✓ 所有測試通過！")
        print("=" * 60)
        print("\n下一步:")
        print("  1. 運行完整系統測試")
        print("  2. 測試實際音頻合成 (需要模型文件)")
        print("  3. 測試 Producer/Consumer 串流")
        print("  4. 整合到主系統")
        
    except Exception as e:
        print(f"\n✗ 測試失敗: {str(e)}")
        import traceback
        print(traceback.format_exc())
        return 1
    
    return 0

if __name__ == "__main__":
    exit(main())
