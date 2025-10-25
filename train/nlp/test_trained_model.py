#!/usr/bin/env python3
"""
測試訓練完成的 BIOS Tagger 模型
"""

import sys
from pathlib import Path

# 添加項目根目錄到路徑
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from modules.nlp_module.bio_tagger import BIOTagger
from modules.nlp_module.intent_types import IntentType


def test_model():
    """測試模型"""
    print("=" * 70)
    print("BIOS Tagger 模型測試")
    print("=" * 70)
    print()
    
    # 載入模型
    model_path = project_root / "models" / "nlp" / "bio_tagger"
    print(f"載入模型: {model_path}")
    
    tagger = BIOTagger()
    if not tagger.load_model(str(model_path)):
        print("❌ 模型載入失敗")
        return
    
    print("✅ 模型載入成功")
    print(f"標籤集: {tagger.BIO_LABELS}")
    print()
    
    # 測試案例
    test_cases = [
        # CALL 測試
        ("Hello UEP", "CALL"),
        ("Hey assistant", "CALL"),
        ("System wake up", "CALL"),
        
        # CHAT 測試
        ("How are you today", "CHAT"),
        ("The weather is nice", "CHAT"),
        ("I had a great day", "CHAT"),
        
        # DIRECT_WORK 測試
        ("Open the file", "DIRECT_WORK"),
        ("Delete this document", "DIRECT_WORK"),
        ("Search for information", "DIRECT_WORK"),
        ("Show me the calendar", "DIRECT_WORK"),
        
        # BACKGROUND_WORK 測試
        ("Play some music", "BACKGROUND_WORK"),
        ("Sync my devices", "BACKGROUND_WORK"),
        ("Backup my data", "BACKGROUND_WORK"),
        ("Download updates", "BACKGROUND_WORK"),
        
        # UNKNOWN 測試
        ("asdf jkl qwer", "UNKNOWN"),
        ("me want do thing", "UNKNOWN"),
        ("open open the the", "UNKNOWN"),
        
        # 複合意圖測試（加入標點符號分隔）
        ("Hello UEP. How are you today", "CALL+CHAT"),
        ("Hello UEP, How are you today", "CALL+CHAT"),
        ("Hey there. Open my calendar", "CALL+DIRECT_WORK"),
        ("Hey there, Open my calendar", "CALL+DIRECT_WORK"),
        ("Good morning. Play some music", "CALL+BACKGROUND_WORK"),
        ("Good morning, Play some music", "CALL+BACKGROUND_WORK"),
        ("I'm feeling great. Save this file", "CHAT+DIRECT_WORK"),
        ("I'm feeling great, Save this file", "CHAT+DIRECT_WORK"),
        
        # 無標點符號複合意圖（保留原測試）
        ("Hello UEP How are you today", "CALL+CHAT"),
        ("Hey there Open my calendar", "CALL+DIRECT_WORK"),
    ]
    
    print("測試結果:")
    print("=" * 70)
    
    correct = 0
    total = 0
    
    for text, expected in test_cases:
        segments = tagger.predict(text)
        
        if segments:
            predicted_labels = [seg['intent'].upper() for seg in segments]
            predicted = '+'.join(predicted_labels)
        else:
            predicted = "NONE"
        
        # 檢查是否正確
        is_correct = False
        if '+' in expected:
            # 複合意圖：檢查是否包含預期的標籤
            expected_labels = set(expected.split('+'))
            predicted_labels_set = set(predicted.split('+'))
            is_correct = expected_labels == predicted_labels_set
        else:
            # 單一意圖
            is_correct = expected in predicted
        
        status = "✅" if is_correct else "❌"
        
        print(f"{status} 輸入: '{text}'")
        print(f"   預期: {expected}")
        print(f"   預測: {predicted}")
        
        if segments:
            for seg in segments:
                print(f"   片段: '{seg['text']}' -> {seg['intent']} (信心度: {seg['confidence']:.2f})")
        
        print()
        
        if is_correct:
            correct += 1
        total += 1
    
    # 統計結果
    accuracy = (correct / total) * 100 if total > 0 else 0
    print("=" * 70)
    print(f"準確率: {correct}/{total} ({accuracy:.1f}%)")
    print("=" * 70)


if __name__ == "__main__":
    test_model()
