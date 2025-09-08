# -*- coding: utf-8 -*-
"""
STT 模組測試函數
已重構模組 - 完整功能測試
"""

import asyncio
from utils.debug_helper import debug_log, info_log, error_log

def on_stt_result(result, continuous_mode=False):
    """
    STT 結果回調函數 - 統一版本，可處理單次和持續辨識模式
    
    Args:
        result: 語音識別結果，可以是字典或對象
        continuous_mode: 是否為持續辨識模式 (影響輸出格式)
    """
    # 首先檢查結果是否為 None 或非字典（處理錯誤情況）
    if result is None:
        print("⚠️  STT 結果為空")
        return
        
    if isinstance(result, dict):
        # 標準格式處理
        text = result.get("text", "")
        confidence = result.get("confidence", 0.0)
        speaker_id = result.get("speaker_id", "unknown")
        speaker_confidence = result.get("speaker_confidence", 0.0)
        
        # 選擇輸出格式
        if continuous_mode:
            # 持續模式：簡潔輸出
            print(f"🎤 [{speaker_id}]: {text}")
            if confidence < 0.7:
                print(f"   ⚠️  低置信度: {confidence:.2f}")
        else:
            # 單次模式：詳細輸出
            print(f"📝 識別文本: {text}")
            print(f"🎯 識別置信度: {confidence:.2f}")
            print(f"👤 說話人ID: {speaker_id}")
            print(f"🔍 說話人置信度: {speaker_confidence:.2f}")
    else:
        # 備用格式處理（若結果不是字典）
        print(f"🎤 STT 結果: {str(result)}")

def stt_test_single(modules, enable_speaker_id=True, language="en-US"):
    """單次 STT 測試 - 手動模式"""
    stt = modules.get("stt")
    if stt is None:
        print("❌ STT 模組未載入")
        return None

    print(f"🎤 STT 手動測試")
    print("   請說話，系統將錄製並識別您的語音...")
    
    # 使用手動模式進行錄音
    result = stt.handle({
        "mode": "manual",
        "language": language,
        "enable_speaker_id": enable_speaker_id,
        "duration": 5
    })  
    
    # 使用 on_stt_result 處理結果，指定為單次模式 (continuous_mode=False，這是預設值)
    on_stt_result(result.get("data"))
    return result

def stt_test_continuous_listening(modules, duration=30):
    """持續背景監聽測試 - 直接在控制台輸出識別結果"""
    stt = modules.get("stt")
    if stt is None:
        print("❌ STT 模組未載入")
        return None

    print(f"🎧 持續背景監聽測試 ({duration}秒)")
    print("   系統將持續監聽並直接輸出識別結果")
    print("   按 Ctrl+C 可隨時中斷監聽")
    
    # 創建一個連接到主要處理函數的回調
    def continuous_result_callback(result):
        on_stt_result(result, continuous_mode=True)
    
    try:
        result = stt.handle({
            "mode": "continuous",
            "duration": duration,
            "callback": continuous_result_callback
        })
        
        print(f"✅ 持續監聽測試完成，總計 {duration} 秒")
        return result
        
    except KeyboardInterrupt:
        print("\n🛑 用戶中斷了持續監聽測試")
        return {"status": "interrupted", "message": "用戶中斷"}
        
    except Exception as e:
        print(f"❌ 持續監聽測試失敗: {str(e)}")
        return {"status": "error", "error": str(e)}

def stt_get_stats(modules):
    """獲取 STT 統計信息"""
    stt = modules.get("stt")
    if stt is None:
        print("❌ STT 模組未載入")
        return None

    # 嘗試從說話人模組獲取統計信息
    if hasattr(stt, 'speaker_module'):
        stats = stt.speaker_module.get_stats()
        print("\n📊 STT 統計信息:")
        print(f"   總說話人數: {stats.get('total_speakers', 0)}")
        print(f"   總樣本數: {stats.get('total_samples', 0)}")
        print(f"   平均置信度: {stats.get('avg_confidence', 0.0):.2f}")
        return stats
    else:
        print("⚠️  STT 模組未包含說話人統計功能")
        return None

# STT 說話人管理功能

def stt_speaker_list(modules):
    """列出所有已識別的說話人"""
    stt = modules.get("stt")
    if stt is None:
        print("❌ STT 模組未載入")
        return None
        
    if hasattr(stt, 'speaker_module'):
        speakers = stt.speaker_module.list_speakers()
        print(f"\n👥 已識別說話人列表 (共 {len(speakers)} 人):")
        for speaker in speakers:
            print(f"   {speaker['id']} - 樣本數: {speaker['sample_count']}, 最後更新: {speaker['last_updated']}")
        return speakers
    else:
        print("⚠️  STT 模組未包含說話人管理功能")
        return None

def stt_speaker_rename(modules, old_id: str, new_id: str):
    """重新命名說話人"""
    stt = modules.get("stt")
    if stt is None:
        print("❌ STT 模組未載入")
        return False
        
    if hasattr(stt, 'speaker_module'):
        result = stt.speaker_module.rename_speaker(old_id, new_id)
        if result:
            print(f"✅ 說話人 '{old_id}' 已重新命名為 '{new_id}'")
        else:
            print(f"❌ 重新命名失敗")
        return result
    else:
        print("⚠️  STT 模組未包含說話人管理功能")
        return False

def stt_speaker_delete(modules, speaker_id: str):
    """刪除指定說話人"""
    stt = modules.get("stt")
    if stt is None:
        print("❌ STT 模組未載入")
        return False
        
    if hasattr(stt, 'speaker_module'):
        result = stt.speaker_module.delete_speaker(speaker_id)
        if result:
            print(f"✅ 說話人 '{speaker_id}' 已刪除")
        else:
            print(f"❌ 刪除說話人 '{speaker_id}' 失敗")
        return result
    else:
        print("⚠️  STT 模組未包含說話人管理功能")
        return False

def stt_speaker_clear_all(modules):
    """清空所有說話人數據"""
    stt = modules.get("stt")
    if stt is None:
        print("❌ STT 模組未載入")
        return False
        
    if hasattr(stt, 'speaker_module'):
        result = stt.speaker_module.clear_all_speakers()
        if result:
            print("✅ 所有說話人數據已清空")
        else:
            print("❌ 清空說話人數據失敗")
        return result
    else:
        print("⚠️  STT 模組未包含說話人管理功能")
        return False

def stt_speaker_backup(modules):
    """備份說話人數據"""
    stt = modules.get("stt")
    if stt is None:
        print("❌ STT 模組未載入")
        return None
        
    if hasattr(stt, 'speaker_module'):
        backup_path = stt.speaker_module.backup_speakers()
        if backup_path:
            print(f"✅ 說話人數據已備份至: {backup_path}")
        else:
            print("❌ 說話人數據備份失敗")
        return backup_path
    else:
        print("⚠️  STT 模組未包含說話人管理功能")
        return None

def stt_speaker_restore(modules, backup_path: str = None):
    """恢復說話人數據"""
    stt = modules.get("stt")
    if stt is None:
        print("❌ STT 模組未載入")
        return False
        
    if hasattr(stt, 'speaker_module'):
        result = stt.speaker_module.restore_speakers(backup_path)
        if result:
            print(f"✅ 說話人數據已恢復")
        else:
            print("❌ 說話人數據恢復失敗")
        return result
    else:
        print("⚠️  STT 模組未包含說話人管理功能")
        return False

def stt_speaker_info(modules):
    """顯示說話人資料庫詳細信息"""
    stt = modules.get("stt")
    if stt is None:
        print("❌ STT 模組未載入")
        return None
        
    if hasattr(stt, 'speaker_module'):
        info = stt.speaker_module.get_database_info()
        print("\n🗄️  說話人資料庫詳細信息:")
        print(f"   資料庫路徑: {info.get('database_path', 'N/A')}")
        print(f"   總記錄數: {info.get('total_records', 0)}")
        print(f"   資料庫大小: {info.get('database_size', 'N/A')}")
        print(f"   最後更新: {info.get('last_updated', 'N/A')}")
        return info
    else:
        print("⚠️  STT 模組未包含說話人管理功能")
        return None

def stt_speaker_adjust_threshold(modules, threshold: float = None):
    """調整說話人相似度閾值"""
    stt = modules.get("stt")
    if stt is None:
        print("❌ STT 模組未載入")
        return None
        
    # 使用統一的說話人識別系統
    if hasattr(stt, 'speaker_module'):
        if threshold is None:
            current_threshold = stt.speaker_module.get_threshold()
            print(f"🎯 當前說話人相似度閾值: {current_threshold}")
            return current_threshold
        else:
            result = stt.speaker_module.set_threshold(threshold)
            if result:
                print(f"✅ 說話人相似度閾值已設為: {threshold}")
            else:
                print(f"❌ 設置閾值失敗")
            return result
    else:
        print("⚠️  STT 模組未包含說話人管理功能")
        return None
