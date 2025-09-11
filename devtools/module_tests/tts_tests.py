# -*- coding: utf-8 -*-
"""
TTS 模組測試函數
⚠️ 未重構模組 - 使用傳統模組呼叫方式
"""

from utils.debug_helper import debug_log, info_log, error_log
import asyncio

# ⚠️ 未重構模組標註
# 以下測試函數適用於尚未重構的 TTS 模組

def tts_test(modules, text, mood="neutral", save=False):
    tts = modules.get("tts")
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