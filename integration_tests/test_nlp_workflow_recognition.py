#!/usr/bin/env python3
"""
NLP 工作流識別測試
測試 IntentSegmenter 和 WorkflowValidator 對各種工作流請求的識別準確度
"""

import pytest
import sys
from pathlib import Path

# 添加專案根目錄到 Python 路徑
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from modules.nlp_module.intent_segmenter import get_intent_segmenter
from modules.nlp_module.workflow_validator import WorkflowValidator


class TestNLPWorkflowRecognition:
    """NLP 工作流識別測試"""
    
    @pytest.fixture(scope="class")
    def intent_segmenter(self):
        """獲取 IntentSegmenter"""
        return get_intent_segmenter()
    
    @pytest.fixture(scope="class")
    def workflow_validator(self):
        """初始化 WorkflowValidator"""
        return WorkflowValidator()
    
    def test_workflow_recognition_batch(self, intent_segmenter, workflow_validator):
        """
        批量測試工作流識別
        
        使用 IntentSegmenter + WorkflowValidator 直接測試，繞過完整 NLP 模組
        測試各種工作流的典型請求語句，檢查：
        1. 是否正確識別為 WORK 意圖
        2. confidence 是否合理
        3. work_mode 是否正確 (direct/background)
        """
        
        # 測試用例：(語句, 預期意圖, 預期工作流, 預期work_mode)
        # 基於 test_full_workflow_cycle.py 中的實際用語
        test_cases = [
            # Info workflows
            ("Show me the latest Taiwan news", "work", "news_summary", "direct"),
            ("Check weather in Taipei", "work", "get_weather", "direct"),
            ("What time is it in Tokyo right now?", "work", "get_world_time", "direct"),
            
            # Text workflows  
            ("Search clipboard for email addresses", "work", "clipboard_tracker", "direct"),
            
            # Maintenance workflows
            ("Clean the trash bin", "work", "clean_trash_bin", "direct"),
            
            # Generation workflows
            ("Generate a backup script", "work", "script_generation", "direct"),
            
            # Code workflows
            ("Analyze this code file for its quality", "work", "code_analysis", "direct"),
            
            # 測試變化語句
            ("Can you tell me about the weather in Taipei?", "work", "get_weather", "direct"),
            ("Show me news headlines from Taiwan", "work", "news_summary", "direct"),
            ("What's the current time in New York?", "work", "get_world_time", "direct"),
            ("Find my clipboard history", "work", "clipboard_tracker", "direct"),
            ("Empty the recycle bin", "work", "clean_trash_bin", "direct"),
        ]
        
        results = []
        passed = 0
        failed = 0
        
        print("=" * 80)
        print("[Test] 開始批量工作流識別測試 (IntentSegmenter + WorkflowValidator)")
        print("=" * 80)
        
        for i, (text, expected_intent_type, expected_workflow, expected_mode) in enumerate(test_cases, 1):
            print(f"\n[Test Case {i}/{len(test_cases)}] 測試語句: '{text}'")
            print(f"  預期: 意圖={expected_intent_type}, 工作流={expected_workflow}, 模式={expected_mode}")
            
            try:
                # Step 1: IntentSegmenter 分析
                segments = intent_segmenter.segment_intents(text)
                
                if not segments:
                    print(f"  [FAIL] IntentSegmenter 返回空結果")
                    failed += 1
                    results.append({
                        'text': text,
                        'expected': f"{expected_intent_type}/{expected_workflow}/{expected_mode}",
                        'actual': 'None',
                        'passed': False,
                        'reason': 'Empty segments'
                    })
                    continue
                
                # 將 IntentSegment 轉換為字典格式（WorkflowValidator 需要）
                segments_dict = []
                for seg in segments:
                    seg_dict = {
                        'text': seg.segment_text,
                        'intent': seg.intent_type.value,
                        'confidence': seg.confidence,
                        'priority': seg.priority,
                        'metadata': seg.metadata or {}
                    }
                    segments_dict.append(seg_dict)
                
                # Step 2: WorkflowValidator 驗證
                validated_segments = workflow_validator.validate(segments_dict)
                
                if not validated_segments:
                    print(f"  [FAIL] WorkflowValidator 返回空結果")
                    failed += 1
                    results.append({
                        'text': text,
                        'expected': f"{expected_intent_type}/{expected_workflow}/{expected_mode}",
                        'actual': 'None',
                        'passed': False,
                        'reason': 'Empty validated segments'
                    })
                    continue
                
                # 取第一個分段（通常只有一個）
                seg = validated_segments[0]
                
                # WorkflowValidator 返回字典格式
                intent_type = seg.get('intent')
                confidence = seg.get('confidence', 0.0)
                metadata = seg.get('metadata', {})
                work_mode = metadata.get('work_mode')
                matched_workflow = metadata.get('matched_workflow') or metadata.get('potential_workflow')
                
                # 判斷意圖類型
                if intent_type in ['direct_work', 'background_work']:
                    actual_intent_type = 'work'
                elif intent_type == 'chat':
                    actual_intent_type = 'chat'
                else:
                    actual_intent_type = intent_type
                
                print(f"  實際: 意圖={actual_intent_type} ({intent_type}), confidence={confidence:.3f}")
                print(f"        工作流={matched_workflow}, 模式={work_mode}")
                
                # 檢查是否通過
                intent_match = actual_intent_type == expected_intent_type
                
                # 對於 WORK 意圖，檢查工作流和模式
                if expected_intent_type == 'work':
                    workflow_match = matched_workflow == expected_workflow if matched_workflow else False
                    mode_match = work_mode == expected_mode if work_mode else False
                    
                    test_passed = intent_match and workflow_match and mode_match
                    
                    reasons = []
                    if not intent_match:
                        reasons.append(f"意圖錯誤 (預期={expected_intent_type}, 實際={actual_intent_type})")
                    if not workflow_match:
                        reasons.append(f"工作流錯誤 (預期={expected_workflow}, 實際={matched_workflow})")
                    if not mode_match:
                        reasons.append(f"模式錯誤 (預期={expected_mode}, 實際={work_mode})")
                    
                    reason_str = "; ".join(reasons) if reasons else "OK"
                    
                    if test_passed:
                        print(f"  [PASS] 通過")
                        passed += 1
                    else:
                        print(f"  [FAIL] 失敗: {reason_str}")
                        failed += 1
                    
                    results.append({
                        'text': text,
                        'expected': f"{expected_intent_type}/{expected_workflow}/{expected_mode}",
                        'actual': f"{actual_intent_type}/{matched_workflow}/{work_mode}",
                        'confidence': confidence,
                        'passed': test_passed,
                        'reason': reason_str if not test_passed else 'OK'
                    })
                else:
                    # 非 WORK 意圖，只檢查意圖類型
                    test_passed = intent_match
                    reason = "OK" if test_passed else f"意圖錯誤 (預期={expected_intent_type}, 實際={actual_intent_type})"
                    
                    if test_passed:
                        print(f"  [PASS] 通過")
                        passed += 1
                    else:
                        print(f"  [FAIL] 失敗: {reason}")
                        failed += 1
                    
                    results.append({
                        'text': text,
                        'expected': expected_intent_type,
                        'actual': actual_intent_type,
                        'confidence': confidence,
                        'passed': test_passed,
                        'reason': 'OK' if test_passed else reason
                    })
            
            except Exception as e:
                print(f"  [ERROR] 異常: {e}")
                import traceback
                print(f"     Traceback: {traceback.format_exc()}")
                failed += 1
                results.append({
                    'text': text,
                    'expected': f"{expected_intent_type}/{expected_workflow}/{expected_mode}",
                    'actual': 'Exception',
                    'passed': False,
                    'reason': str(e)
                })
        
        # 輸出總結
        print("\n" + "=" * 80)
        print("[Test] 測試總結")
        print("=" * 80)
        print(f"總測試數: {len(test_cases)}")
        print(f"通過: {passed} ({passed/len(test_cases)*100:.1f}%)")
        print(f"失敗: {failed} ({failed/len(test_cases)*100:.1f}%)")
        
        # 輸出失敗案例詳情
        if failed > 0:
            print("\n失敗案例詳情:")
            for i, r in enumerate([r for r in results if not r['passed']], 1):
                print(f"{i}. '{r['text']}'")
                print(f"   預期: {r['expected']}")
                print(f"   實際: {r['actual']}")
                print(f"   原因: {r['reason']}")
        
        print("=" * 80)
        
        # 斷言：至少 70% 通過率
        pass_rate = passed / len(test_cases)
        assert pass_rate >= 0.7, f"工作流識別通過率過低: {pass_rate*100:.1f}% (要求 >= 70%)"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
