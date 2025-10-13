# -*- coding: utf-8 -*-
"""
TTS 模組測試函數 (互動式手動測試)

提供終端中的互動式測試功能,用於手動驗證 TTS 模組的功能:
1. tts_interactive_synthesis - TTS 即時合成測試 (連續輸入文本和情緒)
2. tts_emotion_variation_test - 情感變化測試 (同一文本,不同情緒)
3. tts_streaming_test - 串流測試 (長文本分段)

✅ 已重構 - 使用新的 IndexTTS Lite 架構
"""

from utils.debug_helper import debug_log, info_log, error_log
from core.status_manager import status_manager
import time


# ============================================================================
# 預設情緒庫 (方便快速測試)
# ============================================================================

PRESET_EMOTIONS = {
    "neutral": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.3],      # 中性平靜
    "happy": [0.3, 0.0, 0.0, 0.0, 0.0, 0.0, 0.2, 0.1],        # 開心
    "excited": [0.4, 0.0, 0.0, 0.0, 0.0, 0.0, 0.4, 0.0],      # 興奮驚喜
    "sad": [0.0, 0.0, 0.3, 0.0, 0.0, 0.2, 0.0, 0.1],          # 悲傷憂鬱
    "angry": [0.0, 0.4, 0.0, 0.0, 0.1, 0.0, 0.0, 0.0],        # 憤怒
    "afraid": [0.0, 0.0, 0.1, 0.3, 0.0, 0.1, 0.2, 0.0],       # 害怕驚訝
    "calm": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.5],         # 極度平靜
    "cheerful": [0.25, 0.0, 0.0, 0.0, 0.0, 0.0, 0.15, 0.2],   # 愉快
}

EMOTION_LABELS = ["happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm"]


# ============================================================================
# 輔助函數
# ============================================================================

def parse_emotion_input(user_input: str):
    """
    解析使用者輸入的情緒參數
    
    支援格式:
    1. 預設名稱: "happy", "sad", "neutral" 等
    2. 8個數值 (空格分隔): "0.1 0.2 0.0 0.0 0.3 0.1 0.0 0.0"
    3. "status" - 從 Status Manager 獲取
    4. 空白 - 使用 neutral
    
    Returns:
        List[float] 或 None (錯誤時)
    """
    user_input = user_input.strip().lower()
    
    # 空白 = neutral
    if not user_input:
        return PRESET_EMOTIONS["neutral"]
    
    # 從 Status Manager 獲取
    if user_input == "status":
        info_log("[TTS Test] 從 Status Manager 獲取情緒向量")
        status = status_manager.get_status()
        mood = status.get("mood", 0.0)
        pride = status.get("pride", 0.5)
        helpfulness = status.get("helpfulness", 0.5)
        boredom = status.get("boredom", 0.0)
        
        # 這裡需要 emotion_mapper (假設可以從 modules 獲取)
        # 暫時返回 neutral
        info_log(f"   Mood: {mood:.2f}, Pride: {pride:.2f}, Helpfulness: {helpfulness:.2f}, Boredom: {boredom:.2f}")
        return PRESET_EMOTIONS["neutral"]
    
    # 預設情緒名稱
    if user_input in PRESET_EMOTIONS:
        return PRESET_EMOTIONS[user_input]
    
    # 數值輸入
    try:
        values = [float(v.strip()) for v in user_input.split()]
        if len(values) != 8:
            error_log(f"[TTS Test] 情緒向量需要 8 個數值,但收到 {len(values)} 個")
            return None
        
        # 檢查範圍
        if any(v < 0 or v > 1 for v in values):
            error_log("[TTS Test] 情緒向量數值必須在 0.0-1.0 之間")
            return None
        
        return values
    except ValueError:
        error_log(f"[TTS Test] 無法解析情緒參數: {user_input}")
        return None


def display_emotion_vector(emotion_vector):
    """顯示情緒向量 (帶標籤)"""
    print("\n📊 情緒向量:")
    for i, (label, value) in enumerate(zip(EMOTION_LABELS, emotion_vector)):
        bar = "█" * int(value * 20)
        print(f"   {label:12s}: {bar:20s} {value:.3f}")


# ============================================================================
# 測試函數 1: TTS 即時合成測試 (互動式)
# ============================================================================

def tts_interactive_synthesis(modules):
    """
    TTS 即時合成測試 - 連續輸入文本和情緒
    
    使用者可以:
    - 輸入文本
    - 選擇情緒 (預設名稱/數值/status)
    - 選擇是否儲存
    - 連續測試多次
    """
    tts = modules.get("tts")
    if tts is None:
        error_log("[TTS Test] ❌ 無法載入 TTS 模組")
        return
    
    info_log("=" * 70)
    info_log("TTS 即時合成測試 (互動式)")
    info_log("=" * 70)
    
    print("\n📝 預設情緒選項:")
    for name in PRESET_EMOTIONS.keys():
        print(f"   - {name}")
    print("   - status (從 Status Manager 獲取)")
    print("   - 或直接輸入 8 個數值 (空格分隔): 0.1 0.2 0.0 0.0 0.3 0.1 0.0 0.0")
    
    test_count = 0
    
    while True:
        print("\n" + "=" * 70)
        print(f"🧪 測試 #{test_count + 1}")
        print("=" * 70)
        
        # 1. 輸入文本
        text = input("\n📝 請輸入文本 (或 'exit' 結束):\n> ").strip()
        
        if text.lower() in ["exit", "quit", "q", "e"]:
            info_log("[TTS Test] 結束測試")
            break
        
        if not text:
            error_log("[TTS Test] 文本不能為空")
            continue
        
        # 2. 輸入情緒
        emotion_input = input("\n🎭 請輸入情緒 (預設: neutral):\n> ").strip()
        emotion_vector = parse_emotion_input(emotion_input or "neutral")
        
        if emotion_vector is None:
            continue
        
        display_emotion_vector(emotion_vector)
        
        # 3. 是否儲存
        save_input = input("\n💾 是否儲存音檔? (y/n, 預設: n):\n> ").strip().lower()
        save = save_input in ["y", "yes"]
        
        # 4. 執行合成
        print("\n🎙️  開始合成...")
        start_time = time.perf_counter()
        
        try:
            result = tts.handle({
                "text": text,
                "emotion_vector": emotion_vector,
                "save": save
            })
            
            end_time = time.perf_counter()
            duration = end_time - start_time
            
            # 5. 顯示結果
            if result["status"] == "success":
                print(f"\n✅ 合成成功! (耗時: {duration:.2f}s)")
                print(f"   文本長度: {len(text)} 字符")
                print(f"   分段: {'是' if result['is_chunked'] else '否'}")
                print(f"   段落數: {result['chunk_count']}")
                
                if save:
                    print(f"   儲存路徑: {result['output_path']}")
                else:
                    print(f"   已自動播放")
                
                test_count += 1
            else:
                error_log(f"[TTS Test] ❌ 合成失敗: {result.get('message', '未知錯誤')}")
        
        except Exception as e:
            error_log(f"[TTS Test] ❌ 執行錯誤: {e}")
            import traceback
            debug_log(1, traceback.format_exc())
    
    print(f"\n📊 測試完成,共進行 {test_count} 次測試")


# ============================================================================
# 測試函數 2: 情感變化測試
# ============================================================================

def tts_emotion_variation_test(modules):
    """
    情感變化測試 - 同一文本,不同情緒
    
    使用固定文本,讓使用者依次嘗試不同情緒,比較效果
    """
    tts = modules.get("tts")
    if tts is None:
        error_log("[TTS Test] ❌ 無法載入 TTS 模組")
        return
    
    info_log("=" * 70)
    info_log("TTS 情感變化測試")
    info_log("=" * 70)
    
    # 1. 選擇測試文本
    default_texts = [
        "Hello! How are you doing today?",
        "I'm sorry to hear that you're not feeling well.",
        "That's amazing! I'm so happy for you!",
        "Please be careful, this could be dangerous.",
    ]
    
    print("\n📝 選擇測試文本:")
    for i, text in enumerate(default_texts, 1):
        print(f"   {i}. {text}")
    print(f"   5. 自訂文本")
    
    choice = input("\n請選擇 (1-5): ").strip()
    
    if choice == "5":
        text = input("\n請輸入自訂文本:\n> ").strip()
        if not text:
            error_log("[TTS Test] 文本不能為空")
            return
    elif choice in ["1", "2", "3", "4"]:
        text = default_texts[int(choice) - 1]
    else:
        error_log("[TTS Test] 無效選擇")
        return
    
    info_log(f"\n✅ 測試文本: {text}")
    
    # 2. 是否儲存
    save_input = input("\n💾 是否儲存所有音檔? (y/n, 預設: n):\n> ").strip().lower()
    save = save_input in ["y", "yes"]
    
    # 3. 依次測試每個預設情緒
    print("\n" + "=" * 70)
    print("開始情感變化測試")
    print("=" * 70)
    
    results = []
    
    for emotion_name, emotion_vector in PRESET_EMOTIONS.items():
        print(f"\n🎭 測試情緒: {emotion_name}")
        display_emotion_vector(emotion_vector)
        
        input("\n按 Enter 繼續...")
        
        start_time = time.perf_counter()
        
        try:
            result = tts.handle({
                "text": text,
                "emotion_vector": emotion_vector,
                "save": save
            })
            
            end_time = time.perf_counter()
            duration = end_time - start_time
            
            if result["status"] == "success":
                print(f"✅ 合成成功! (耗時: {duration:.2f}s)")
                results.append({
                    "emotion": emotion_name,
                    "duration": duration,
                    "chunks": result["chunk_count"]
                })
            else:
                error_log(f"❌ 合成失敗: {result.get('message', '未知錯誤')}")
        
        except Exception as e:
            error_log(f"❌ 執行錯誤: {e}")
    
    # 4. 總結
    print("\n" + "=" * 70)
    print("📊 測試總結")
    print("=" * 70)
    
    print(f"\n文本: {text}")
    print(f"文本長度: {len(text)} 字符")
    print("\n結果:")
    
    for r in results:
        print(f"   {r['emotion']:12s}: {r['duration']:.2f}s ({r['chunks']} 段)")


# ============================================================================
# 測試函數 3: 串流測試
# ============================================================================

def tts_streaming_test(modules):
    """
    串流測試 - 長文本分段合成
    
    使用者可以設定 chunking threshold,系統用預設長文本測試
    """
    tts = modules.get("tts")
    if tts is None:
        error_log("[TTS Test] ❌ 無法載入 TTS 模組")
        return
    
    info_log("=" * 70)
    info_log("TTS 串流測試 (長文本分段)")
    info_log("=" * 70)
    
    # 預設長文本
    long_text = (
        "Area 3 is a totalitarian state called Crambell, divided into four quadrants "
        "named after the Four Horsemen of the Apocalypse. Each quadrant serves a specific "
        "purpose: Famein for the elderly, weak, women, and children; Pestilens as an arms "
        "control Zone and rest for the army; Wyar as the industrial and important district "
        "with connections to Area 4; and finally, Delth where most citizens live and work, "
        "having mining operations and dorms for miners. The city is ruled by a mysterious "
        "figure known as the Governor, who maintains strict control over all aspects of life."
    )
    
    print(f"\n📝 測試文本 (長度: {len(long_text)} 字符):")
    print(f"   {long_text[:100]}...")
    
    # 1. 設定 threshold
    current_threshold = tts.chunking_threshold
    print(f"\n⚙️  當前 chunking threshold: {current_threshold} 字符")
    
    threshold_input = input(f"\n請輸入新的 threshold (或直接 Enter 使用當前值):\n> ").strip()
    
    if threshold_input:
        try:
            new_threshold = int(threshold_input)
            if new_threshold < 50 or new_threshold > 500:
                error_log("[TTS Test] Threshold 應在 50-500 之間")
                return
            
            # 臨時修改 threshold
            original_threshold = tts.chunking_threshold
            tts.chunking_threshold = new_threshold
            info_log(f"[TTS Test] 臨時設定 threshold: {new_threshold}")
        except ValueError:
            error_log("[TTS Test] 無效的數值")
            return
    else:
        original_threshold = None
    
    # 2. 選擇情緒
    emotion_input = input("\n🎭 請輸入情緒 (預設: neutral):\n> ").strip()
    emotion_vector = parse_emotion_input(emotion_input or "neutral")
    
    if emotion_vector is None:
        return
    
    display_emotion_vector(emotion_vector)
    
    # 3. 是否儲存
    save_input = input("\n💾 是否儲存音檔? (y/n, 預設: n):\n> ").strip().lower()
    save = save_input in ["y", "yes"]
    
    # 4. 執行合成
    print("\n🎙️  開始串流合成...")
    start_time = time.perf_counter()
    
    try:
        result = tts.handle({
            "text": long_text,
            "emotion_vector": emotion_vector,
            "save": save,
            "force_chunking": True  # 強制分段
        })
        
        end_time = time.perf_counter()
        duration = end_time - start_time
        
        # 5. 顯示結果
        if result["status"] == "success":
            print(f"\n✅ 串流合成成功!")
            print(f"   總耗時: {duration:.2f}s")
            print(f"   文本長度: {len(long_text)} 字符")
            print(f"   段落數: {result['chunk_count']}")
            print(f"   平均每段: {duration / result['chunk_count']:.2f}s")
            
            if save:
                print(f"   儲存路徑: {result['output_path']}")
        else:
            error_log(f"[TTS Test] ❌ 合成失敗: {result.get('message', '未知錯誤')}")
    
    except Exception as e:
        error_log(f"[TTS Test] ❌ 執行錯誤: {e}")
        import traceback
        debug_log(1, traceback.format_exc())
    
    finally:
        # 恢復原始 threshold
        if original_threshold is not None:
            tts.chunking_threshold = original_threshold
            info_log(f"[TTS Test] 恢復 threshold: {original_threshold}")