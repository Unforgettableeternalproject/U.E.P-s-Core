#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
測試新的 Whisper + pyannote STT 架構
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from modules.stt_module.stt_module import STTModule
from modules.stt_module.schemas import STTInput, ActivationMode
from utils.debug_helper import debug_log, info_log, error_log

def test_whisper_stt():
    """測試 Whisper STT 基本功能"""
    
    print("🎤 測試 Whisper + pyannote STT 模組")
    print("=" * 50)
    
    try:
        # 初始化 STT 模組
        stt = STTModule()
        
        if not stt.initialize():
            print("❌ STT 模組初始化失敗")
            return
        
        print("✅ STT 模組初始化成功")
        print("\n準備進行語音識別測試...")
        print("請在聽到提示後說話（5秒錄音時間）")
        
        # 測試手動識別
        test_input = STTInput(
            mode=ActivationMode.MANUAL,
            language="en",  # 使用英文
            duration=5.0,   # 5秒錄音
            enable_speaker_id=False  # 暫時關閉說話人識別
        )
        
        print("\n🔴 開始錄音...")
        result = stt.handle(test_input.dict())
        
        print("\n📝 識別結果:")
        print(f"文字: {result.get('text', 'N/A')}")
        print(f"信心度: {result.get('confidence', 0.0):.2f}")
        print(f"錯誤: {result.get('error', '無')}")
        print(f"處理時間: {result.get('processing_time', 0.0):.2f} 秒")
        
        if result.get('text'):
            print("✅ 語音識別成功！")
        else:
            print("⚠️ 未檢測到語音或識別失敗")
            
    except Exception as e:
        error_log(f"[TEST] 測試失敗: {str(e)}")
        print(f"❌ 測試失敗: {str(e)}")
    
    finally:
        try:
            stt.shutdown()
            print("\n🔚 STT 模組已關閉")
        except:
            pass

if __name__ == "__main__":
    test_whisper_stt()
