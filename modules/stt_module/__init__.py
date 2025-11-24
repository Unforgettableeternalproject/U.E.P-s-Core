# modules/stt_module/__init__.py
"""
STT模組 - 語音識別模組

功能:
- 語音轉文字
- 語者識別
- 語音活動檢測
- 實時轉錄
"""

from .stt_module import STTModule
from configs.config_loader import load_module_config


def register():
    """註冊STT模組"""
    try:
        config = load_module_config("stt_module")
        
        # 創建 STT-NLP 直接連接的回調函數
        def stt_to_nlp_callback(stt_result):
            """STT 結果處理：將說話人資料存儲到Working Context，文本傳遞給NLP"""
            try:
                from core.framework import core_framework
                from core.working_context import working_context_manager
                from utils.debug_helper import debug_log, info_log
                from datetime import datetime
                
                # 檢查是否有有效的語音內容
                if hasattr(stt_result, 'text'):
                    text = stt_result.text.strip() if stt_result.text else ''
                elif isinstance(stt_result, dict):
                    text = stt_result.get('text', '').strip()
                else:
                    debug_log(1, f"[STT-NLP] 未知的STT結果格式: {type(stt_result)}")
                    return
                
                if not text:
                    debug_log(3, "[STT-NLP] 空白識別結果，不處理")
                    return
                
                info_log(f"[STT-NLP] 檢測到語音，處理說話人資料和文本: '{text[:50]}...'")
                
                # 停止當前STT監聽
                stt_module = core_framework.get_module('stt')
                if stt_module:
                    stt_module.stop_listening()
                    debug_log(2, "[STT-NLP] 已中斷STT持續監聽")
                
                # 處理說話人資料 - 存儲到Working Context
                speaker_data = None
                if hasattr(stt_result, 'speaker_info') and stt_result.speaker_info:
                    # STTOutput 對象格式 - 兼容物件和字典格式
                    speaker_info = stt_result.speaker_info
                    if hasattr(speaker_info, 'speaker_id'):
                        # 物件格式
                        speaker_data = {
                            'speaker_id': speaker_info.speaker_id,
                            'confidence': speaker_info.confidence,
                            'status': speaker_info.status,
                            'context_id': getattr(stt_result.metadata, 'context_id', None) if hasattr(stt_result, 'metadata') else None,
                            'timestamp': datetime.now().isoformat()
                        }
                    else:
                        # 字典格式
                        speaker_data = {
                            'speaker_id': speaker_info.get('speaker_id'),
                            'confidence': speaker_info.get('confidence', 0.0),
                            'status': speaker_info.get('status', 'unknown'),
                            'context_id': getattr(stt_result.metadata, 'context_id', None) if hasattr(stt_result, 'metadata') else None,
                            'timestamp': datetime.now().isoformat()
                        }
                elif isinstance(stt_result, dict) and 'speaker_info' in stt_result:
                    # 字典格式
                    speaker_info = stt_result['speaker_info']
                    if speaker_info:
                        speaker_data = {
                            'speaker_id': speaker_info.get('speaker_id'),
                            'confidence': speaker_info.get('confidence', 0.0),
                            'status': speaker_info.get('status', 'unknown'),
                            'context_id': stt_result.get('metadata', {}).get('context_id'),
                            'timestamp': datetime.now().isoformat()
                        }
                
                if speaker_data and speaker_data['speaker_id']:
                    # 存儲到Working Context而非直接傳給NLP
                    if working_context_manager and speaker_data['context_id']:
                        working_context_manager.update_context_data(
                            speaker_data['context_id'], 
                            {'current_speaker': speaker_data}
                        )
                        info_log(f"[STT-WC] 說話人資料已存儲到Working Context: {speaker_data['speaker_id']}")
                    else:
                        debug_log(2, "[STT-WC] Working Context不可用或無context_id，說話人資料未存儲")
                else:
                    debug_log(2, "[STT-WC] 無有效說話人資料")
                
                # 準備文本資料給NLP，不包含說話人資訊
                from datetime import datetime
                timestamp = datetime.now().isoformat()
                
                # 檢查 metadata 以判斷是否為文字輸入模式
                metadata = {}
                if hasattr(stt_result, 'metadata'):
                    metadata = stt_result.metadata if isinstance(stt_result.metadata, dict) else {}
                elif isinstance(stt_result, dict):
                    metadata = stt_result.get('metadata', {})
                
                nlp_input = {
                    'text': text,
                    'timestamp': timestamp,
                    'source': 'stt_direct',
                    'metadata': metadata,  # 傳遞 metadata 給 NLP
                    # 不直接包含speaker_id，讓NLP從Working Context獲取
                }
                
                # 將文本傳遞給NLP模組，NLP將從Working Context獲取說話人資料
                nlp_module = core_framework.get_module('nlp')
                if nlp_module:
                    if metadata.get('input_mode') == 'text':
                        debug_log(2, f"[STT-NLP] 文字輸入模式：傳遞文本給NLP")
                    else:
                        debug_log(2, f"[STT-NLP] 傳遞文本給NLP，說話人資料已存儲到WC")
                    return nlp_module.handle(nlp_input)
                else:
                    from utils.debug_helper import error_log
                    error_log("[STT-NLP] 無法獲取NLP模組")
            except Exception as e:
                from utils.debug_helper import error_log
                error_log(f"[STT-NLP] 回調函數錯誤: {e}")
        
        # 使用回調函數初始化 STT 模組
        instance = STTModule(config=config, result_callback=stt_to_nlp_callback)
        instance.initialize()
        return instance
            
    except Exception as e:
        from utils.debug_helper import error_log
        error_log(f"[STT] 模組註冊失敗：{e}")
        return None


# 匯出主要類別
__all__ = [
    "STTModule",
    "register"
]
