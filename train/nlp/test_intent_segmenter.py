#!/usr/bin/env python3
"""
測試 IntentSegmenter 與 BIOS Tagger 整合
"""

import sys
from pathlib import Path

# 添加項目根目錄到路徑
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from modules.nlp_module.intent_segmenter import IntentSegmenter, segment_user_input
from modules.nlp_module.intent_types import IntentType


def test_intent_segmenter():
    """測試 IntentSegmenter"""
    print("=" * 70)
    print("IntentSegmenter 整合測試")
    print("=" * 70)
    print()
    
    # 測試案例
    test_cases = [
        "Hello UEP",
        "Open the file",
        "Play some music",
        "asdf jkl qwer",
        "Hello UEP. How are you today",
        "Hey there. Open my calendar",
        "I'm feeling great. Save this file",
        "Good morning, Play some music",
    ]
    
    for text in test_cases:
        print(f"輸入: '{text}'")
        segments = segment_user_input(text)
        
        print(f"  分段數: {len(segments)}")
        for i, seg in enumerate(segments, 1):
            print(f"  片段 {i}:")
            print(f"    文本: '{seg.segment_text}'")
            print(f"    意圖: {seg.intent_type.value}")
            print(f"    優先級: {seg.priority}")
            print(f"    置信度: {seg.confidence:.2f}")
        print()
    
    # 測試複合意圖判斷
    print("=" * 70)
    print("複合意圖判斷測試")
    print("=" * 70)
    
    compound_tests = [
        "Hello UEP. How are you today",
        "Open the file",
    ]
    
    for text in compound_tests:
        segments = segment_user_input(text)
        from modules.nlp_module.intent_types import IntentSegment
        
        is_compound = IntentSegment.is_compound_input(segments)
        highest = IntentSegment.get_highest_priority_segment(segments)
        
        print(f"輸入: '{text}'")
        print(f"  是否複合意圖: {is_compound}")
        print(f"  最高優先級片段: '{highest.segment_text}' ({highest.intent_type.value})")
        print()


if __name__ == "__main__":
    test_intent_segmenter()
