from logging import config
from core.registry import get_module
from configs.config_loader import load_config
from utils.debug_helper import debug_log, info_log, error_log
from utils.debug_file_dropper import open_demo_window, open_folder_dialog
from module_tests.integration_tests import test_stt_nlp  # 新版整合測試 (精簡版)
# from module_tests.integration_tests_v2 import *  # 保留舊版整合測試，暫時停用
from module_tests.extra_tests import *
import time
import asyncio

config = load_config()
enabled = config.get("modules_enabled", {})

def safe_get_module(name):
    if not enabled.get(name, False):
        # print(f"[Controller] [X] 模組 '{name}' 未啟用，請檢查配置") # Ignored
        return None

    info_log(f"[Controller] 嘗試載入模組 '{name}'")

    try:
        mod = get_module(name)
        if mod is None:
            raise ImportError(f"{name} register() 回傳為 None")
        info_log(f"[Controller] [OK] 載入模組成功：{name}")
        return mod
    except NotImplementedError:
        error_log(f"[Controller] [X] 模組 '{name}' 尚未被實作")
        return None
    except Exception as e:
        error_log(f"[Controller] [X] 無法載入模組 '{name}': {e}")
        return None

modules = {
    "stt": safe_get_module("stt_module"),
    "nlp": safe_get_module("nlp_module"),
    "mem": safe_get_module("mem_module"),
    "llm": safe_get_module("llm_module"), 
    "tts": safe_get_module("tts_module"),
    "sysmod": safe_get_module("sys_module")
}

# 測試 STT 模組 - Phase 2 版本

def on_stt_result(result, continuous_mode=False):
    """
    STT 結果回調函數 - 統一版本，可處理單次和持續辨識模式
    
    Args:
        result: 語音識別結果，可以是字典或對象
        continuous_mode: 是否為持續辨識模式 (影響輸出格式)
    """
    # 首先檢查結果是否為 None 或非字典（處理錯誤情況）
    if result is None:
        print("❌ 語音識別失敗：沒有識別結果")
        return
        
    if isinstance(result, dict):
        # 提取基本信息
        text = result.get("text", "")
        confidence = result.get("confidence", 0)
        speaker_info = result.get("speaker_info")
        error = result.get("error")
        
        # 處理錯誤情況
        if error:
            print(f"❌ 語音識別錯誤：{error}")
            return
            
        # 沒有識別出文字的情況
        if not text or not text.strip():
            print("🔇 未識別到有效語音內容")
            return
        
        # 顯示語音辨識結果 (根據模式調整格式)
        if continuous_mode:
            print(f"\n🎤 語音識別: 「{text}」")
        else:
            print(f"\n📢 語音識別: 「{text}」 (信心度: {confidence:.2f})")
        
        # 顯示說話人信息
        if speaker_info:
            speaker_id = speaker_info.get("speaker_id", "未定")
            speaker_confidence = speaker_info.get("confidence", 0)
            is_new = "(新說話人)" if speaker_info.get("is_new_speaker", False) else ""
            
            if continuous_mode:
                print(f"👤 說話人: {speaker_id} {is_new} (信心度: {speaker_confidence:.2f})")
            else:
                print(f"👤 說話人：{speaker_id} {is_new} (信心度: {speaker_confidence:.2f})")
        else:
            print("👤 說話人：未定")

    else:
        # 直接顯示結果
        print(f"✨ 識別結果：{result}")

def stt_test_single(enable_speaker_id=True, language="en-US"):
    """單次 STT 測試 - 手動模式"""
    stt = modules["stt"]

    if stt is None:
        error_log("[Controller] ❌ 無法載入 STT 模組")
        return

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

def stt_test_continuous_listening(duration=30):
    """持續背景監聽測試 - 直接在控制台輸出識別結果"""
    stt = modules["stt"]

    if stt is None:
        error_log("[Controller] ❌ 無法載入 STT 模組")
        return

    print(f"🎧 持續背景監聽測試 ({duration}秒)")
    print("   系統將持續監聽並直接輸出識別結果")
    print("   按 Ctrl+C 可隨時中斷監聽")
    
    # 創建一個連接到主要處理函數的回調
    def continuous_result_callback(result):
        if result is None:
            return
            
        # 將 result 轉換為標準字典格式，以便重用 on_stt_result 函數
        if not isinstance(result, dict):
            # 提取文字
            text = result.text if hasattr(result, "text") else ""
            
            # 提取說話人信息
            speaker_info = None
            if hasattr(result, "speaker_info") and result.speaker_info:
                if isinstance(result.speaker_info, dict):
                    speaker_info = result.speaker_info
                else:
                    # 轉換為字典
                    speaker_info = {
                        "speaker_id": getattr(result.speaker_info, "speaker_id", "未定"),
                        "confidence": getattr(result.speaker_info, "confidence", 0),
                        "is_new_speaker": getattr(result.speaker_info, "is_new_speaker", False)
                    }
                    
            # 創建標準格式
            formatted_result = {
                "text": text,
                "confidence": getattr(result, "confidence", 0),
                "speaker_info": speaker_info
            }
            
            # 使用標準結果處理函數，並傳遞 continuous_mode=True
            on_stt_result(formatted_result, continuous_mode=True)
        else:
            # 已經是字典格式
            on_stt_result(result, continuous_mode=True)
    
    try:
        # 臨時設置回調函數
        original_callback = None
        if hasattr(stt, "result_callback"):
            original_callback = stt.result_callback
            stt.result_callback = continuous_result_callback
        
        print("\n開始持續監聽，識別結果將直接顯示...\n")
        
        # 使用持續監聽模式
        result = stt.handle({
            "mode": "continuous",
            "language": "en-US",
            "enable_speaker_id": True,
            "duration": duration,
            "context": "controller_test"
        })
        
        # 恢復原來的回調函數
        if hasattr(stt, "result_callback") and original_callback is not None:
            stt.result_callback = original_callback
            
        print("\n持續監聽完成")
        return result
        
    except KeyboardInterrupt:
        # 恢復原來的回調函數
        if hasattr(stt, "result_callback") and original_callback is not None:
            stt.result_callback = original_callback
            
        print("\n⏹️ 用戶中斷監聽")
        return None
        
    except Exception as e:
        # 恢復原來的回調函數
        if hasattr(stt, "result_callback") and original_callback is not None:
            stt.result_callback = original_callback
            
        error_log(f"[Controller] 持續監聽失敗: {e}")
        return None

def stt_get_stats():
    """獲取 STT 統計信息"""
    stt = modules["stt"]

    if stt is None:
        error_log("[Controller] ❌ 無法載入 STT 模組")
        return

    # 嘗試從說話人模組獲取統計信息
    if hasattr(stt, 'speaker_module'):
        speaker_info = stt.speaker_module.get_database_info()
        speakers = stt.speaker_module.list_speakers()
        
        print("📊 STT 統計信息:")
        print("說話人統計:")
        if speakers:
            for speaker_id, metadata in speakers.items():
                sample_count = metadata.get('sample_count', 0)
                print(f"  {speaker_id}: {sample_count} 個語音樣本")
        else:
            print("  無說話人數據")
        
        print("\n資料庫統計:")
        print(f"  總說話人數: {speaker_info.get('total_speakers', 0)}")
        print(f"  總語音樣本: {speaker_info.get('total_samples', 0)}")
        print(f"  檔案大小: {speaker_info.get('file_size_mb', 0):.2f} MB")
        print(f"  相似度閾值: {speaker_info.get('similarity_threshold', 0):.2f}")
        
        return {
            "speaker_stats": speakers,
            "database_info": speaker_info
        }
    else:
        print("⚠️ 當前版本不支援詳細統計功能")
        return {"error": "統計功能不可用"}

# STT 說話人管理功能

def stt_speaker_list():
    """列出所有已識別的說話人"""
    stt = modules["stt"]

    if stt is None:
        error_log("[Controller] ❌ 無法載入 STT 模組")
        return
        
    if hasattr(stt, 'speaker_module'):
        speakers = stt.speaker_module.list_speakers()
        if speakers:
            print("👥 已識別說話人:")
            for speaker_id, metadata in speakers.items():
                # metadata['embeddings'] 已經是數量，不需要再用 len()
                embeddings_count = metadata.get('embeddings', 0)
                print(f"  {speaker_id}: {embeddings_count} 個語音樣本")
        else:
            print("📝 尚未識別任何說話人")
        return speakers
    else:
        print("⚠️ 說話人識別模組不可用")

def stt_speaker_rename(old_id: str, new_id: str):
    """重新命名說話人"""
    stt = modules["stt"]

    if stt is None:
        error_log("[Controller] ❌ 無法載入 STT 模組")
        return
        
    if hasattr(stt, 'speaker_module'):
        success = stt.speaker_module.rename_speaker(old_id, new_id)
        if success:
            print(f"✅ 說話人 '{old_id}' 已重新命名為 '{new_id}'")
        else:
            print(f"❌ 重新命名失敗：說話人 '{old_id}' 不存在")
        return success
    else:
        print("⚠️ 說話人識別模組不可用")

def stt_speaker_delete(speaker_id: str):
    """刪除指定說話人"""
    stt = modules["stt"]

    if stt is None:
        error_log("[Controller] ❌ 無法載入 STT 模組")
        return
        
    if hasattr(stt, 'speaker_module'):
        success = stt.speaker_module.delete_speaker(speaker_id)
        if success:
            print(f"✅ 說話人 '{speaker_id}' 已刪除")
        else:
            print(f"❌ 刪除失敗：說話人 '{speaker_id}' 不存在")
        return success
    else:
        print("⚠️ 說話人識別模組不可用")

def stt_speaker_clear_all():
    """清空所有說話人數據"""
    stt = modules["stt"]

    if stt is None:
        error_log("[Controller] ❌ 無法載入 STT 模組")
        return
        
    if hasattr(stt, 'speaker_module'):
        confirmation = input("⚠️ 確定要清空所有說話人數據嗎？(y/N): ")
        if confirmation.lower() == 'y':
            success = stt.speaker_module.clear_all_speakers()
            if success:
                print("✅ 所有說話人數據已清空")
            else:
                print("❌ 清空失敗")
            return success
        else:
            print("❌ 操作已取消")
            return False
    else:
        print("⚠️ 說話人識別模組不可用")

def stt_speaker_backup():
    """備份說話人數據"""
    stt = modules["stt"]

    if stt is None:
        error_log("[Controller] ❌ 無法載入 STT 模組")
        return
        
    if hasattr(stt, 'speaker_module'):
        import datetime
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"memory/speaker_models_backup_{timestamp}.pkl"
        
        success = stt.speaker_module.backup_speakers(backup_path)
        if success:
            print(f"✅ 說話人數據已備份至: {backup_path}")
        else:
            print("❌ 備份失敗")
        return success
    else:
        print("⚠️ 說話人識別模組不可用")

def stt_speaker_restore(backup_path: str = None):
    """恢復說話人數據"""
    stt = modules["stt"]

    if stt is None:
        error_log("[Controller] ❌ 無法載入 STT 模組")
        return
        
    if hasattr(stt, 'speaker_module'):
        if backup_path is None:
            backup_path = input("請輸入備份檔案路徑: ")
        
        success = stt.speaker_module.restore_speakers(backup_path)
        if success:
            print(f"✅ 說話人數據已從備份恢復: {backup_path}")
        else:
            print("❌ 恢復失敗")
        return success
    else:
        print("⚠️ 說話人識別模組不可用")

def stt_speaker_info():
    """顯示說話人資料庫詳細信息"""
    stt = modules["stt"]

    if stt is None:
        error_log("[Controller] ❌ 無法載入 STT 模組")
        return
        
    if hasattr(stt, 'speaker_module'):
        info = stt.speaker_module.get_database_info()
        if info:
            print("📊 說話人資料庫信息:")
            print(f"  總說話人數: {info.get('total_speakers', 0)}")
            print(f"  總語音樣本: {info.get('total_samples', 0)}")
            print(f"  檔案大小: {info.get('file_size_mb', 0):.2f} MB")
            print(f"  相似度閾值: {info.get('similarity_threshold', 0):.2f}")
            print(f"  儲存位置: {info.get('database_path', 'N/A')}")
        else:
            print("❌ 無法獲取資料庫信息")
        return info
    else:
        print("⚠️ 說話人識別模組不可用")

def stt_speaker_adjust_threshold(threshold: float = None):
    """調整說話人相似度閾值"""
    stt = modules["stt"]

    if stt is None:
        error_log("[Controller] ❌ 無法載入 STT 模組")
        return
        
    # 使用統一的說話人識別系統
    if hasattr(stt, 'speaker_module'):
        if threshold is None:
            current = stt.speaker_module.similarity_threshold
            print(f"當前相似度閾值: {current:.2f}")
            try:
                threshold = float(input("請輸入新的閾值 (0.0-1.0): "))
            except ValueError:
                print("❌ 無效的閾值")
                return False
        
        if 0.0 <= threshold <= 1.0:
            stt.speaker_module.update_similarity_threshold(threshold)
            print(f"✅ 相似度閾值已更新為: {threshold:.2f}")
            return True
        else:
            print("❌ 閾值必須在 0.0 到 1.0 之間")
            return False
    else:
        print("⚠️ 說話人識別模組不可用")
        return False

# 測試 NLP 模組

def nlp_test(text: str = "", enable_identity: bool = True, enable_segmentation: bool = True):
    """測試增強版NLP模組 - 包含語者身份和意圖分析"""
    nlp = modules["nlp"]

    if nlp is None:
        error_log("[Controller] ❌ 無法載入 NLP 模組")
        return

    test_text = text if text else "Hello UEP, please save my work and then play some music"
    
    print(f"\n🧠 測試增強版NLP - 文本: '{test_text}'")
    print("=" * 60)
    
    # 準備測試輸入
    nlp_input = {
        "text": test_text,
        "speaker_id": "test_speaker_001",
        "speaker_confidence": 0.85,
        "speaker_status": "known",
        "enable_identity_processing": enable_identity,
        "enable_segmentation": enable_segmentation,
        "current_system_state": "idle",
        "conversation_history": []
    }
    
    try:
        result = nlp.handle(nlp_input)
        
        print(f"📝 原始文本: {result.get('original_text', 'N/A')}")
        print(f"🎯 主要意圖: {result.get('primary_intent', 'N/A')}")
        print(f"📊 整體信心度: {result.get('overall_confidence', 0):.3f}")
        
        # 語者身份信息
        identity = result.get('identity')
        if identity:
            print(f"👤 語者身份: {identity.get('identity_id', 'N/A')}")
            print(f"🔄 身份動作: {result.get('identity_action', 'N/A')}")
        else:
            print("👤 語者身份: 未識別")
        
        # 意圖分段
        segments = result.get('intent_segments', [])
        print(f"\n📋 意圖分段 ({len(segments)}個):")
        for i, segment in enumerate(segments, 1):
            if hasattr(segment, 'text'):
                print(f"  {i}. '{segment.text}' -> {segment.intent} (信心度: {segment.confidence:.3f})")
            else:
                print(f"  {i}. '{segment.get('text', 'N/A')}' -> {segment.get('intent', 'N/A')}")
        
        # 上下文信息
        context_ids = result.get('context_ids', [])
        if context_ids:
            print(f"\n🔗 創建的上下文: {len(context_ids)}個")
            for ctx_id in context_ids:
                print(f"  - {ctx_id}")
        
        # 執行計劃
        execution_plan = result.get('execution_plan', [])
        if execution_plan:
            print(f"\n📋 執行計劃:")
            for plan_item in execution_plan:
                print(f"  步驟{plan_item.get('step', 'N/A')}: {plan_item.get('description', 'N/A')} (優先級: {plan_item.get('priority', 'N/A')})")
        
        # 狀態轉換
        state_transition = result.get('state_transition')
        if state_transition:
            print(f"\n🔄 狀態轉換: {state_transition}")
        
        # 下一步模組
        next_modules = result.get('next_modules', [])
        if next_modules:
            print(f"➡️ 下一步模組: {', '.join(next_modules)}")
        
        # 處理註記
        processing_notes = result.get('processing_notes', [])
        if processing_notes:
            print(f"\n📝 處理註記:")
            for note in processing_notes:
                print(f"  - {note}")
        
        return result
        
    except Exception as e:
        error_log(f"[NLP] 增強版測試失敗: {e}")
        return None

def nlp_test_state_queue_integration(text: str = ""):
    """測試NLP與狀態佇列的整合"""
    nlp = modules["nlp"]
    if nlp is None:
        error_log("[Controller] ❌ 無法載入 NLP 模組")
        return

    from core.state_queue import get_state_queue_manager
    state_queue = get_state_queue_manager()

    test_text = text if text else "Hi UEP, how are you? Please save my work and then remind me about the meeting."
    
    print(f"\n🔄 測試NLP與狀態佇列整合")
    print(f"📝 測試文本: '{test_text}'")
    print("=" * 80)
    
    # 清空佇列開始測試
    state_queue.clear_queue()
    print(f"🧹 清空狀態佇列")
    
    # 顯示初始狀態
    initial_status = state_queue.get_queue_status()
    print(f"🏁 初始狀態: {initial_status['current_state']}")
    print(f"📋 初始佇列長度: {initial_status['queue_length']}")
    
    # 執行NLP分析
    result = nlp_test(test_text, enable_segmentation=True)
    
    # 顯示分析後的狀態佇列
    print(f"\n📊 NLP分析後的狀態佇列:")
    final_status = state_queue.get_queue_status()
    print(f"🎯 當前狀態: {final_status['current_state']}")
    print(f"📋 佇列長度: {final_status['queue_length']}")
    
    if final_status['queue_items']:
        print(f"📝 佇列內容:")
        for i, item in enumerate(final_status['queue_items'], 1):
            print(f"  {i}. {item['state']} (優先級: {item['priority']})")
            print(f"     觸發: {item['trigger_content']}")
            print(f"     上下文: {item['context_content']}")
            print()
    
    return result

def nlp_test_multi_intent(text: str = ""):
    """測試多意圖上下文管理"""
    nlp = modules["nlp"]

    if nlp is None:
        error_log("[Controller] ❌ 無法載入 NLP 模組")
        return

    test_text = text if text else "Hey system, please save my document and then remind me about the meeting tomorrow"
    
    print(f"\n🔄 測試多意圖上下文管理")
    print(f"📝 測試文本: '{test_text}'")
    print("=" * 70)
    
    result = nlp_test(test_text, enable_segmentation=True)
    
    if result and hasattr(nlp, 'intent_analyzer'):
        analyzer = nlp.intent_analyzer
        
        # 獲取上下文摘要
        context_summary = analyzer.get_context_summary()
        print(f"\n📊 上下文管理摘要:")
        print(f"  活躍上下文: {context_summary.get('active_contexts', 0)}")
        print(f"  待執行上下文: {context_summary.get('pending_contexts', 0)}")
        print(f"  已完成上下文: {context_summary.get('completed_contexts', 0)}")
        
        # 獲取下一個可執行的上下文
        next_context = analyzer.get_next_context()
        if next_context:
            state, context = next_context
            print(f"\n➡️ 下一個可執行上下文:")
            print(f"  上下文ID: {context.context_id}")
            print(f"  類型: {context.context_type.value}")
            print(f"  任務描述: {context.task_description or context.conversation_topic}")
            print(f"  優先級: {context.priority}")
        else:
            print(f"\n➡️ 無待執行的上下文")

def nlp_test_identity_management(speaker_id: str = "test_user"):
    """測試語者身份管理"""
    nlp = modules["nlp"]

    if nlp is None:
        error_log("[Controller] ❌ 無法載入 NLP 模組")
        return

    print(f"\n👤 測試語者身份管理 - 語者ID: {speaker_id}")
    print("=" * 50)
    
    # 多次交互測試身份累積和識別
    test_interactions = [
        "Hello, I'm testing the system",
        "Can you help me organize my files?", 
        "I want to schedule a meeting for tomorrow",
        "Play my favorite music please"
    ]
    
    for i, text in enumerate(test_interactions, 1):
        print(f"\n--- 交互 {i} ---")
        
        nlp_input = {
            "text": text,
            "speaker_id": speaker_id,
            "speaker_confidence": 0.8 + (i * 0.05),  # 逐漸提高信心度
            "speaker_status": "known" if i > 2 else "accumulating",
            "enable_identity_processing": True,
            "enable_segmentation": True
        }
        
        result = nlp.handle(nlp_input)
        
        print(f"文本: '{text}'")
        print(f"身份動作: {result.get('identity_action', 'N/A')}")
        
        identity = result.get('identity')
        if identity:
            print(f"身份ID: {identity.get('identity_id', 'N/A')}")
            print(f"互動次數: {identity.get('interaction_stats', {}).get('total_interactions', 0)}")

def nlp_analyze_context_queue():
    """分析NLP模組的上下文佇列狀態"""
    nlp = modules["nlp"]

    if nlp is None:
        error_log("[Controller] ❌ 無法載入 NLP 模組")
        return

    if not hasattr(nlp, 'context_manager'):
        print("❌ NLP模組沒有上下文管理器")
        return

    context_manager = nlp.context_manager
    
    print(f"\n📊 多意圖上下文佇列分析")
    print("=" * 40)
    
    # 獲取佇列狀態
    summary = context_manager.get_context_summary()
    
    print(f"總上下文數: {len(context_manager.contexts)}")
    print(f"活躍上下文: {len(context_manager.active_contexts)}")
    print(f"已完成上下文: {len(context_manager.completed_contexts)}")
    print(f"佇列長度: {len(context_manager.state_queue)}")
    
    # 顯示活躍上下文詳情
    if context_manager.active_contexts:
        print(f"\n🔄 活躍上下文:")
        for ctx_id in context_manager.active_contexts:
            if ctx_id in context_manager.contexts:
                ctx = context_manager.contexts[ctx_id]
                print(f"  {ctx_id}: {ctx.context_type.value} - {ctx.task_description or ctx.conversation_topic}")
    
    # 顯示佇列中的條目
    if context_manager.state_queue:
        print(f"\n📋 佇列條目:")
        for i, entry in enumerate(context_manager.state_queue[:5]):  # 只顯示前5個
            ctx = entry.context
            print(f"  {i+1}. {ctx.context_id}: {ctx.context_type.value} (優先級: {ctx.priority})")
        
        if len(context_manager.state_queue) > 5:
            print(f"  ... 還有 {len(context_manager.state_queue) - 5} 個條目")

def nlp_clear_contexts():
    """清空NLP模組的上下文"""
    nlp = modules["nlp"]

    if nlp is None:
        error_log("[Controller] ❌ 無法載入 NLP 模組")
        return

    if not hasattr(nlp, 'context_manager'):
        print("❌ NLP模組沒有上下文管理器")
        return

    context_manager = nlp.context_manager
    
    # 清空上下文
    context_manager.contexts.clear()
    context_manager.active_contexts.clear()
    context_manager.completed_contexts.clear()
    context_manager.state_queue.clear()
    context_manager.dependency_graph.clear()
    
    print("✅ 已清空所有NLP上下文")

# 測試 MEM 模組

def mem_fetch_test(text : str = ""):
    mem = modules["mem"]
    if mem is None:
        error_log("[Controller] ❌ 無法載入 MEM 模組")
        return

    result = mem.handle(
        {"mode": "fetch", "text": ("Test chat" if text == "" else text)})

    if result["status"] == "empty":
        print("\n🧠 MEM 回傳：查無相關記憶")
        return

    print(f"\n🧠 MEM 輸出結果：\n\n使用者: {result['results'][0]['user']} \n回應: {result['results'][0]['response']}")

def mem_store_test(user_text : str = "Test chat", response_text : str = "Test response"):
    mem = modules["mem"]
    if mem is None:
        error_log("[Controller] ❌ 無法載入 MEM 模組")
        return

    result = mem.handle(
        {"mode": "store", "entry": {"user": user_text, "response": response_text}})
    print("\n🧠 MEM 回傳：", "儲存" + ("成功" if result["status"] == "stored" else "失敗"))

def mem_clear_test(text : str = "ALL", top_k : int = 1):
    mem = modules["mem"]
    if mem is None:
        error_log("[Controller] ❌ 無法載入 MEM 模組")
        return

    result = mem.handle(
        {"mode": "clear_all" if text == "ALL" else "clear_by_text", "text": text, "top_k": top_k})
    print("\n🧠 MEM 回傳：", "清除" +
          ("成功" if result["status"] == "cleared" else "失敗"))


def mem_list_all_test(page : int = 1):
    mem = modules["mem"]
    if mem is None:
        error_log("[Controller] ❌ 無法載入 MEM 模組")
        return

    result = mem.handle({"mode": "list_all", "page": page})

    if result["status"] == "empty":
        print("\n🧠 MEM 回傳：查無相關記憶")
        return

    if result["status"] == "failed":
        print("\n🧠 MEM 回傳：記憶查詢有誤 (也許是頁碼問題)")
        return
    
    for i, record in enumerate(result["records"], start=1):
        print(f"記錄 {i}: 使用者: {record['user']}，回應: {record['response']}")

# 測試 LLM 模組

def llm_test_chat(text):
    llm = modules.get("llm")
    if llm is None:
        error_log("[Controller] ❌ 無法載入 LLM 模組")
        return

    memory = "No relevant memory found."  

    result = llm.handle({
        "text": text,
        "intent": "chat",
        "memory": memory
    })

    print("🧠 Gemini 回應：", result.get("text", "[無回應]"))
    print("🧭 心情標記（mood）：", result.get("mood", "neutral"))
    # print("⚙️ 系統指令：", result.get("sys_action")) 因為是聊天測試所以這個應該不需要

def llm_test_command(text):
    llm = modules.get("llm")
    if llm is None:
        error_log("[Controller] ❌ 無法載入 LLM 模組")
        return

    memory = "No relevant memory found."  

    result = llm.handle({
        "text": text,
        "intent": "command",
        "memory": memory
    })

    print("🧠 Gemini 指令分析：", result.get("text", "[無回應]"))
    print("🧭 心情標記（mood）：", result.get("mood", "neutral"))
    print("⚙️ 系統指令：", result.get("sys_action"))
    print("📋 指令類型：", result.get("sys_action", {}).get("action", "無") if isinstance(result.get("sys_action"), dict) else "無")
    
# 測試 TTS 模組

def tts_test(text, mood="neutral", save=False):
    tts = modules["tts"]
    if tts is None:
        error_log("[Controller] ❌ 無法載入 TTS 模組")
        return
    if not text:
        error_log("[Controller] ❌ TTS 測試文本為空")
        return

    result = asyncio.run(tts.handle({
        "text": text,
        "mood": mood,
        "save": save
    }))
    
    if result["status"] == "error":
        print("\n❌ TTS 錯誤：", result["message"])
    elif result["status"] == "processing":
        print("\n⏳ TTS 處理中，分為", result.get("chunk_count", "未知"), "個區塊...")
    else:
        if save:
            print("\n✅ TTS 成功，音檔已經儲存到", result["output_path"])
        else: 
            print("\n✅ TTS 成功，音檔已經被撥放\n")

# 測試 SYS 模組

def sys_list_functions():
    sysmod = modules["sysmod"]

    if sysmod is None:
        error_log("[Controller] ❌ 無法載入 SYS 模組")
        return

    resp = sysmod.handle({"mode": "list_functions", "params": {}})

    print("=== SYS 功能清單 ===")
    import json
    print(json.dumps(resp.get("data", {}), ensure_ascii=False, indent=2))

# 測試多步驟工作流程
def test_command_workflow(command_text: str = "幫我整理和摘要桌面上的文件"):
    """測試多步驟指令工作流程"""
    sysmod = modules["sysmod"]
    llm = modules["llm"]

    if sysmod is None or llm is None:
        error_log("[Controller] ❌ 無法載入 SYS 或 LLM 模組")
        return

    info_log(f"[Controller] 測試指令工作流程：'{command_text}'")
    
    # 第一步：LLM 分析指令
    llm_resp = llm.handle({
        "text": command_text,
        "intent": "command",
        "memory": ""
    })
    
    print("\n🧠 LLM 分析指令：", llm_resp.get("text", "[無回應]"))
    
    # 第二步：啟動工作流程（假設為檔案處理類型）
    workflow_resp = sysmod.handle({
        "mode": "start_workflow",
        "params": {
            "workflow_type": "file_processing",
            "command": command_text
        }
    })
    
    session_id = workflow_resp.get("session_id")
    if not session_id:
        error_log("[Controller] ❌ 工作流程啟動失敗")
        return
        
    print(f"\n🔄 工作流程已啟動，ID: {session_id}")
    print(f"🔹 系統提示：{workflow_resp.get('prompt')}")
    
    # 模擬用戶交互
    while workflow_resp.get("requires_input", False):
        # 請求用戶輸入
        user_input = input("\n✍️ 請輸入回應: ")
        
        if user_input.lower() in ("exit", "quit", "取消"):
            # 取消工作流程
            cancel_resp = sysmod.handle({
                "mode": "cancel_workflow",
                "params": {
                    "session_id": session_id,
                    "reason": "用戶取消"
                }
            })
            print(f"\n❌ 工作流程已取消：{cancel_resp.get('message')}")
            break
            
        # 繼續工作流程
        workflow_resp = sysmod.handle({
            "mode": "continue_workflow",
            "params": {
                "session_id": session_id,
                "user_input": user_input
            }
        })
        
        print(f"\n🔄 工作流程步驟 {workflow_resp.get('data', {}).get('step', '?')} 完成")
        print(f"🔹 系統訊息：{workflow_resp.get('message')}")
        
        if workflow_resp.get("requires_input", False):
            print(f"🔹 下一步提示：{workflow_resp.get('prompt')}")
        else:
            # 工作流程完成或異常終止
            status = workflow_resp.get("status")
            if status == "completed":
                print("\n✅ 工作流程成功完成！")
                result_data = workflow_resp.get("data", {})
                if result_data:
                    print("\n📊 工作流程結果:")
                    for key, value in result_data.items():
                        if isinstance(value, str) and len(value) > 100:
                            print(f"  {key}: {value[:100]}...")
                        else:
                            print(f"  {key}: {value}")
            else:
                print(f"\n⚠️ 工作流程異常結束，狀態: {status}")
    
    print("\n==== 工作流程測試結束 ====")

def sys_test_functions(mode : int = 1, sub : int = 1): 
    sysmod = modules["sysmod"]
    if sysmod is None:
        error_log("[Controller] ❌ 無法載入 SYS 模組")
        return

    match mode:
        case 1: # 檔案互動功能 (僅工作流程模式)
            info_log("[Controller] 開啟檔案互動功能 (工作流程模式)")
            match sub:
                case 1: # 測試檔案工作流程 - Drop and Read
                    print("=== 測試檔案讀取工作流程 ===")
                    test_file_workflow("drop_and_read")
                case 2: # 測試檔案工作流程 - Intelligent Archive
                    print("=== 測試智慧歸檔工作流程 ===")
                    test_file_workflow("intelligent_archive")
                case 3: # 測試檔案工作流程 - Summarize Tag
                    print("=== 測試摘要標籤工作流程 ===")
                    test_file_workflow("summarize_tag")
                case 4: # 測試一般多步驟工作流程
                    command = input("請輸入指令（如：幫我整理文件）：")
                    if command:
                        test_command_workflow(command)
                    else:
                        print("未輸入指令，取消測試")
                case _:
                    print("未知的子功能選項")
        case _:
            print("未知的功能選項")

def sys_test_workflows(workflow_type: int = 1):
    """測試各種測試工作流程
    
    Args:
        workflow_type: 工作流程類型
            1: echo - 簡單回顯
            2: countdown - 倒數計時
            3: data_collector - 資料收集
            4: random_fail - 隨機失敗
            5: tts_test - TTS文字轉語音測試
    """
    sysmod = modules["sysmod"]
    if sysmod is None:
        error_log("[Controller] ❌ 無法載入 SYS 模組")
        return
        
    workflow_map = {
        1: "echo",
        2: "countdown", 
        3: "data_collector",
        4: "random_fail",
        5: "tts_test"
    }
    
    workflow_display_name = {
        1: "簡單回顯",
        2: "倒數計時",
        3: "資料收集",
        4: "隨機失敗",
        5: "TTS文字轉語音"
    }
    
    if workflow_type not in workflow_map:
        error_log(f"[Controller] ❌ 無效的工作流程類型: {workflow_type}")
        return
        
    workflow_name = workflow_display_name[workflow_type]
    workflow_type_name = workflow_map[workflow_type]
    
    print(f"\n=== 開始測試 {workflow_name} 工作流程 ===")
    
    # 啟動工作流程（使用統一的 start_workflow 模式）
    resp = sysmod.handle({
        "mode": "start_workflow", 
        "params": {
            "workflow_type": workflow_type_name,
            "command": f"測試 {workflow_name} 工作流程"
        }
    })
    
    print("\n工作流程已啟動!")
    print(f"回應狀態: {resp.get('status', '未知')}")
    print(f"回應訊息: {resp.get('message', '無訊息')}")
    
    # 處理工作流程後續互動
    session_id = resp.get("session_id")
    if not session_id:
        print("無法獲取會話 ID，工作流程可能無法繼續")
        return
    
    # 進入互動循環
    while resp.get("requires_input", False) or resp.get("status") == "waiting":
        requires_input = resp.get("requires_input", False)
        prompt = resp.get("prompt", "請輸入")
        
        if requires_input:
            print(f"\n{prompt}")
            user_input = input("> ")
            
            # 如果用戶輸入 exit 或 quit，取消工作流程
            if user_input.lower() in ["exit", "quit", "取消"]:
                cancel_resp = sysmod.handle({
                    "mode": "cancel_workflow",
                    "params": {
                        "session_id": session_id,
                        "reason": "用戶取消"
                    }
                })
                print(f"\n❌ 工作流程已取消：{cancel_resp.get('message', '已取消')}")
                break
            
            # 繼續工作流程（使用統一的 continue_workflow 模式）
            resp = sysmod.handle({
                "mode": "continue_workflow", 
                "params": {
                    "session_id": session_id,
                    "user_input": user_input
                }
            })
            
            print(f"\n回應狀態: {resp.get('status', '未知')}")
            print(f"回應訊息: {resp.get('message', '無訊息')}")
            
            # 如果狀態是 waiting，繼續自動推進
            while resp.get("status") == "waiting" and not resp.get("requires_input", False):
                import time
                time.sleep(0.5)  # 短暫延遲
                resp = sysmod.handle({
                    "mode": "continue_workflow", 
                    "params": {
                        "session_id": session_id,
                        "user_input": ""  # 自動推進不需要輸入
                    }
                })
                print(f"回應狀態: {resp.get('status', '未知')}")
                print(f"回應訊息: {resp.get('message', '無訊息')}")
        else:
            # 工作流程已完成或失敗
            break
    
    print(f"\n=== {workflow_name} 工作流程結束 ===")
    print(f"最終狀態: {resp.get('status', '未知')}")
    print(f"最終訊息: {resp.get('message', '無訊息')}")
    
    # 顯示工作流程結果（如果有）
    if "data" in resp:
        print("\n工作流程結果:")
        data = resp["data"]
        print(data)
        
        # 特殊處理資料收集工作流程的結果
        if workflow_type == 3 and data and "enhanced_summary" in data:
            print("\n========== LLM 增強摘要 ==========")
            print(data["enhanced_summary"])
            print("========== 摘要結束 ==========")

# 整合測試 - 新版

def integration_test_SN():
    """STT + NLP 整合測試"""
    # 直接傳入模組字典
    test_stt_nlp(modules)

# 暫時停用其他整合測試，只保留 STT+NLP (因為其他模組尚未完成重構)
# 其他整合測試將在相應模組重構完成後添加

# 注意：目前只有 STT 和 NLP 模組完成重構，其他整合測試將在模組重構後添加
#
# 以下是可用的整合測試：
# - STT + NLP: integration_test_SN()
#
# 為保持程式碼整潔，其餘整合測試函數已移除

def integration_test_SN(production_mode=False):
    """STT + NLP 整合測試"""
    info_log(f"[Controller] 執行 STT+NLP 整合測試 (新版) ({'生產模式' if production_mode else '除錯模式'})")
    # 目前生產模式參數未被使用，因為新版整合測試不區分生產和除錯模式
    return test_stt_nlp(modules)

# 額外測試

def test_summrize():
    test_chunk_and_summarize()

def test_chat():
    test_uep_chatting(modules)

def sys_list_test_workflows():
    """列出所有可用的測試工作流程"""
    print("\n=== 可用的測試工作流程 ===")
    print("1. echo - 簡單回顯工作流程")
    print("   - 單步驟工作流程")
    print("   - 測試工作流程機制的基本功能")
    print("   - 接受一個訊息並回顯它")
    print()
    print("2. countdown - 倒數計時工作流程")
    print("   - 多步驟工作流程")
    print("   - 測試工作流程中的狀態保持")
    print("   - 從指定數字開始倒數計時直到零")
    print()
    print("3. data_collector - 資料收集工作流程")
    print("   - 多步驟工作流程")
    print("   - 測試工作流程中的用戶輸入處理")
    print("   - 收集各種用戶資訊並在最後匯總")
    print()
    print("4. random_fail - 隨機失敗工作流程")
    print("   - 多步驟工作流程")
    print("   - 測試工作流程的錯誤處理")
    print("   - 在隨機步驟可能失敗，以測試錯誤恢復機制")
    print()
    print("5. tts_test - TTS文字轉語音測試工作流程")
    print("   - 多步驟工作流程")
    print("   - 測試與TTS模組的整合")
    print("   - 讓用戶輸入文字、情緒，並將其轉換成語音")
    print()
    print("=== 可用的文件工作流程 ===")
    print("drop_and_read - 檔案讀取工作流程")
    print("   - 多步驟工作流程")
    print("   - 等待檔案路徑輸入，確認後讀取檔案內容")
    print()
    print("intelligent_archive - 智慧歸檔工作流程")
    print("   - 多步驟工作流程")
    print("   - 根據檔案類型和歷史記錄智慧歸檔檔案")
    print()
    print("summarize_tag - 摘要標籤工作流程")
    print("   - 多步驟工作流程")
    print("   - 使用LLM為檔案生成摘要和標籤")

def test_file_workflow(workflow_type: str):
    """測試檔案工作流程
    
    Args:
        workflow_type: 工作流程類型 ('drop_and_read', 'intelligent_archive', 'summarize_tag')
    """
    sysmod = modules["sysmod"]
    if sysmod is None:
        error_log("[Controller] ❌ 無法載入 SYS 模組")
        return
        
    workflow_display_names = {
        "drop_and_read": "檔案讀取",
        "intelligent_archive": "智慧歸檔", 
        "summarize_tag": "摘要標籤"
    }
    
    workflow_name = workflow_display_names.get(workflow_type, workflow_type)
    
    print(f"\n=== 開始測試 {workflow_name} 工作流程 ===")
    
    # 啟動工作流程
    resp = sysmod.handle({
        "mode": "start_workflow",
        "params": {
            "workflow_type": workflow_type,
            "command": f"測試 {workflow_name} 工作流程"
        }
    })
    
    print("\n工作流程已啟動!")
    print(f"回應狀態: {resp.get('status', '未知')}")
    print(f"回應訊息: {resp.get('message', '無訊息')}")
    
    # 處理工作流程後續互動
    session_id = resp.get("session_id")
    if not session_id:
        print("無法獲取會話 ID，工作流程可能無法繼續")
        return
    
    # 進入互動循環
    while resp.get("requires_input", False) or resp.get("status") == "waiting":
        requires_input = resp.get("requires_input", False)
        prompt = resp.get("prompt", "請輸入")
        
        if requires_input:
            print(f"\n{prompt}")
            
            # 檢查是否需要檔案選擇（更精確的判斷）
            # 只有當提示明確要求選擇檔案，且不是確認步驟時，才開啟檔案選擇視窗
            needs_file_selection = (
                any(keyword in prompt.lower() for keyword in [
                    "請輸入要讀取的檔案路徑", 
                    "請選擇要歸檔的檔案路徑",
                    "請輸入要生成摘要的檔案路徑",
                    "請選擇檔案", 
                    "請輸入檔案路徑", 
                    "file path"
                ]) and
                "確認" not in prompt.lower() and
                "是否" not in prompt.lower() and
                "y/n" not in prompt.lower()
            )
            
            if needs_file_selection:
                print("🔍 正在開啟檔案選擇視窗...")
                try:
                    file_path = open_demo_window()
                    if file_path:
                        print(f"✅ 已選擇檔案: {file_path}")
                        user_input = file_path
                    else:
                        print("❌ 未選擇檔案，取消測試")
                        break
                except Exception as e:
                    error_log(f"[Controller] 檔案選擇出現錯誤: {e}")
                    print("❌ 檔案選擇失敗，取消測試")
                    break
            else:
                # 一般文字輸入或確認步驟
                user_input = input("> ")
                
                # 如果用戶輸入 exit 或 quit，取消工作流程
                if user_input.lower() in ["exit", "quit", "取消"]:
                    cancel_resp = sysmod.handle({
                        "mode": "cancel_workflow",
                        "params": {
                            "session_id": session_id,
                            "reason": "用戶取消"
                        }
                    })
                    print(f"\n❌ 工作流程已取消：{cancel_resp.get('message', '已取消')}")
                    break
            
            # 繼續工作流程
            resp = sysmod.handle({
                "mode": "continue_workflow",
                "params": {
                    "session_id": session_id,
                    "user_input": user_input
                }
            })
            
            print(f"\n回應狀態: {resp.get('status', '未知')}")
            print(f"回應訊息: {resp.get('message', '無訊息')}")
            
            # 如果狀態是 waiting，繼續自動推進
            while resp.get("status") == "waiting" and not resp.get("requires_input", False):
                import time
                time.sleep(0.5)  # 短暫延遲
                resp = sysmod.handle({
                    "mode": "continue_workflow", 
                    "params": {
                        "session_id": session_id,
                        "user_input": ""  # 自動推進不需要輸入
                    }
                })
                print(f"自動推進 - 回應狀態: {resp.get('status', '未知')}")
                print(f"自動推進 - 回應訊息: {resp.get('message', '無訊息')}")
        else:
            # 工作流程已完成或失敗
            break
    
    print(f"\n=== {workflow_name} 工作流程結束 ===")
    print(f"最終狀態: {resp.get('status', '未知')}")
    print(f"最終訊息: {resp.get('message', '無訊息')}")
    
    # 顯示工作流程結果（如果有）
    if "data" in resp:
        print("\n🎯 工作流程結果:")
        data = resp["data"]
        
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, str) and len(value) > 200:
                    print(f"  {key}: {value[:200]}...")
                elif isinstance(value, list) and len(value) > 5:
                    print(f"  {key}: {value[:5]}... (總共 {len(value)} 項)")
                else:
                    print(f"  {key}: {value}")
        else:
            print(f"  結果: {data}")
            
        # 特殊處理不同類型的檔案工作流程結果
        if workflow_type == "drop_and_read" and isinstance(data, dict):
            if "content" in data:
                print(f"\n📄 檔案內容預覽:")
                content = data["content"]
                if len(content) > 500:
                    print(f"{content[:500]}...")
                else:
                    print(content)
                    
        elif workflow_type == "intelligent_archive" and isinstance(data, dict):
            if "archive_path" in data:
                print(f"\n📁 檔案已歸檔至: {data['archive_path']}")
            if "category" in data:
                print(f"📂 分類: {data['category']}")
                
        elif workflow_type == "summarize_tag" and isinstance(data, dict):
            if "summary" in data:
                print(f"\n📝 摘要: {data['summary']}")
            if "tags" in data:
                print(f"🏷️ 標籤: {', '.join(data['tags'])}")

# === 工作上下文管理功能 ===

def setup_working_context():
    """初始化工作上下文管理器"""
    from core.working_context import working_context_manager, ContextType
    
    # 註冊決策處理器
    try:
        # 註冊語者識別決策處理器
        if modules.get("stt"):
            from modules.stt_module.speaker_context_handler import SpeakerContextHandler
            speaker_handler = SpeakerContextHandler(modules["stt"])
            working_context_manager.register_decision_handler(ContextType.SPEAKER_ACCUMULATION, speaker_handler)
            info_log("[Controller] 語者識別決策處理器已註冊")
    except Exception as e:
        error_log(f"[Controller] 註冊決策處理器失敗: {e}")
    
    info_log("[Controller] 工作上下文管理器已初始化")

def cleanup_session_contexts(min_samples: int = 15):
    """
    清理會話結束時未完成的上下文
    
    Args:
        min_samples: 最小樣本數，低於此數值的語者上下文將被清理
    """
    from core.working_context import working_context_manager, ContextType
    
    info_log(f"[Controller] 開始清理會話上下文 (最小樣本數: {min_samples})")
    
    # 清理語者識別相關的未完成上下文
    cleaned_count = working_context_manager.cleanup_incomplete_contexts(
        context_type=ContextType.SPEAKER_ACCUMULATION,
        min_threshold=min_samples
    )
    
    if cleaned_count > 0:
        info_log(f"[Controller] 清理了 {cleaned_count} 個樣本不足的語者上下文")
    else:
        info_log("[Controller] 沒有需要清理的語者上下文")
    
    # 注意：不在這裡調用 cleanup_expired_contexts，因為已完成的上下文可能還有用
    
    return cleaned_count

def get_working_context_status():
    """獲取工作上下文狀態"""
    from core.working_context import working_context_manager
    
    contexts = working_context_manager.get_all_contexts_info()
    
    print("🔄 工作上下文狀態:")
    if not contexts:
        print("   無活躍的工作上下文")
        return
    
    for ctx in contexts:
        context_id = ctx['context_id']
        context_type = ctx['type']
        status = ctx['status']
        sample_count = ctx['sample_count']
        threshold = ctx['threshold']
        is_ready = ctx['is_ready']
        
        print(f"   {context_id}:")
        print(f"     類型: {context_type}")
        print(f"     狀態: {status}")
        print(f"     樣本: {sample_count}/{threshold}")
        print(f"     就緒: {'是' if is_ready else '否'}")
    
    return contexts

def test_speaker_context_workflow():
    """測試語者上下文工作流程"""
    print("🎤 語者上下文工作流程測試")
    print("   這個測試會累積多個語音樣本，並觀察工作上下文的行為")
    
    # 初始化工作上下文
    setup_working_context()
    
    # 執行多次 STT 測試以累積樣本
    for i in range(5):
        print(f"\n--- 第 {i+1} 次語音識別 ---")
        result = stt_test_single(mode="manual", enable_speaker_id=True)
        
        # 顯示工作上下文狀態
        get_working_context_status()
        
        if i < 4:  # 最後一次不需要暫停
            print("   按 Enter 繼續下一次測試...")
            input()
    
    print("\n✅ 語者上下文工作流程測試完成")

# 在模組載入時自動初始化工作上下文
setup_working_context()