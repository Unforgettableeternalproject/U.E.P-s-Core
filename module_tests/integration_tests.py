# integration_tests.py
"""
整合測試 - 針對重構後的模組進行整合測試

本測試套件專注於測試已完成重構的模組之間的整合：
- STT + NLP 整合測試

其他模組將在重構完成後添加到測試套件中
"""

import time
from typing import Dict, Any, Optional
from utils.debug_helper import debug_log, info_log, error_log

class IntegrationTestRunner:
    """整合測試運行器 - 簡化版"""
    
    def __init__(self, modules=None):
        """
        初始化測試運行器
        
        Args:
            modules: 模組字典，直接使用已初始化的模組實例
        """
        self.modules = modules
        self.test_results = {}
        
    def run_stt_nlp_test(self):
        """運行 STT+NLP 整合測試"""
        test_name = "STT-NLP 整合"
        info_log(f"🧪 測試 {test_name}...")
        
        try:
            # 取得 STT 和 NLP 模組
            if not self.modules:
                raise Exception("未提供模組字典")
                
            stt_module = self.modules.get("stt")
            nlp_module = self.modules.get("nlp")
            
            if not stt_module:
                raise Exception("STT 模組不可用")
            if not nlp_module:
                raise Exception("NLP 模組不可用")
            
            # 使用 STT 模組進行實際錄音和辨識
            info_log("🎤 請說話，系統將錄製並識別您的語音...")
            stt_result = stt_module.handle({
                "mode": "manual",
                "language": "en-US",
                "enable_speaker_id": True,
                "duration": 5
            }).get("data")
            
            # 檢查 STT 結果
            if not stt_result or not isinstance(stt_result, dict) or not stt_result.get("text", "").strip():
                info_log("⚠️ 未識別到有效語音內容，使用預設文字測試")
                # 使用預設文字進行後續測試
                stt_text = "Hello UEP, please help me organize my files"
                speaker_id = "test_user_integration"
                speaker_confidence = 0.90
                speaker_status = "known"
                language = "en-US"
            else:
                # 使用實際識別結果
                stt_text = stt_result.get("text", "")
                speaker_info = stt_result.get("speaker_info", {})
                speaker_id = speaker_info.get("speaker_id", "unknown")
                speaker_confidence = speaker_info.get("confidence", 0.0)
                speaker_status = speaker_info.get("status", "unknown")
                language = stt_result.get("language", "en-US")
                
                info_log(f"🎤 STT 識別結果: '{stt_text}'")
            
            # 將 STT 結果轉換為 NLP 輸入格式
            nlp_input = {
                "text": stt_text,
                "speaker_id": speaker_id,
                "speaker_confidence": speaker_confidence,
                "speaker_status": speaker_status,
                "language": language,
                "enable_identity_processing": True,
                "enable_segmentation": True
            }
            
            # 執行 NLP 處理
            nlp_result = nlp_module.handle(nlp_input)
            
            # 驗證結果
            success = (
                nlp_result is not None and
                "primary_intent" in nlp_result and
                "intent_segments" in nlp_result
            )
            
            self.test_results[test_name] = {
                "status": "pass" if success else "fail",
                "details": {
                    "stt_input": stt_text,
                    "nlp_intent": nlp_result.get("primary_intent") if nlp_result else None,
                    "segments_count": len(nlp_result.get("intent_segments", [])) if nlp_result else 0
                }
            }
            
            if success:
                info_log(f"✅ {test_name} 測試通過")
                info_log(f"   主要意圖: {nlp_result.get('primary_intent')}")
                info_log(f"   意圖段落數: {len(nlp_result.get('intent_segments', []))}")
            else:
                error_log(f"❌ {test_name} 測試失敗")
            
            return success
                
        except Exception as e:
            error_log(f"❌ {test_name} 測試異常: {e}")
            self.test_results[test_name] = {
                "status": "error",
                "error": str(e)
            }
            return False

# 便利函數，供 debug_api.py 調用

def test_stt_nlp(modules):
    """
    測試 STT-NLP 整合
    
    Args:
        modules: 模組字典
    """
    runner = IntegrationTestRunner(modules=modules)
    return runner.run_stt_nlp_test()
