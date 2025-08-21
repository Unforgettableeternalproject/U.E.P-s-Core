#!/usr/bin/env python3
"""
BIO標註模型全面測試腳本
測試各種場景和邊界情況
"""

import sys
import json
from pathlib import Path
from typing import List, Dict, Any, Tuple
import pandas as pd

# 添加項目根目錄到路徑
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from modules.nlp_module.bio_tagger import BIOTagger
from utils.debug_helper import debug_log, info_log, error_log

class BIOModelTester:
    """BIO模型測試器"""
    
    def __init__(self, model_path: str):
        self.model_path = model_path
        self.bio_tagger = BIOTagger()
        self.test_results = []
        
    def load_model(self) -> bool:
        """載入模型"""
        try:
            success = self.bio_tagger.load_model(self.model_path)
            if success:
                info_log(f"[Tester] 成功載入模型: {self.model_path}")
                return True
            else:
                error_log(f"[Tester] 模型載入失敗")
                return False
        except Exception as e:
            error_log(f"[Tester] 載入模型時發生錯誤: {e}")
            return False
    
    def test_single_intent_examples(self) -> None:
        """測試單一意圖範例"""
        info_log("[Tester] 開始測試單一意圖範例...")
        
        test_cases = [
            # CALL意圖
            ("Hello UEP", [("Hello UEP", "call")]),
            ("Hey assistant", [("Hey assistant", "call")]),
            ("Are you there?", [("Are you there?", "call")]),
            ("System wake up", [("System wake up", "call")]),
            ("Attention please", [("Attention please", "call")]),
            
            # CHAT意圖
            ("The weather is beautiful today", [("The weather is beautiful today", "chat")]),
            ("I had a great day", [("I had a great day", "chat")]),
            ("That's interesting", [("That's interesting", "chat")]),
            ("I'm feeling happy", [("I'm feeling happy", "chat")]),
            ("The movie was amazing", [("The movie was amazing", "chat")]),
            
            # COMMAND意圖
            ("Set a reminder for tomorrow", [("Set a reminder for tomorrow", "command")]),
            ("Open my calendar", [("Open my calendar", "command")]),
            ("Save this file", [("Save this file", "command")]),
            ("Turn on the lights", [("Turn on the lights", "command")]),
            ("Play some music", [("Play some music", "command")])
        ]
        
        for text, expected in test_cases:
            self._test_case(text, expected, "單一意圖")
    
    def test_multi_intent_examples(self) -> None:
        """測試多意圖範例"""
        info_log("[Tester] 開始測試多意圖範例...")
        
        test_cases = [
            # 雙意圖
            ("Hello UEP, set a reminder for 3pm", [("Hello UEP", "call"), ("set a reminder for 3pm", "command")]),
            ("I had a great day. Can you help me organize my photos?", [("I had a great day", "chat"), ("Can you help me organize my photos", "command")]),
            ("Hey there, the weather is nice today", [("Hey there", "call"), ("the weather is nice today", "chat")]),
            
            # 三意圖
            ("System wake up. I'm feeling excited today. Please open my calendar", 
             [("System wake up", "call"), ("I'm feeling excited today", "chat"), ("Please open my calendar", "command")]),
            ("Hey UEP, that movie was interesting. Can you recommend similar ones?",
             [("Hey UEP", "call"), ("that movie was interesting", "chat"), ("Can you recommend similar ones", "command")])
        ]
        
        for text, expected in test_cases:
            self._test_case(text, expected, "多意圖")
    
    def test_edge_cases(self) -> None:
        """測試邊界情況"""
        info_log("[Tester] 開始測試邊界情況...")
        
        test_cases = [
            # 空文本
            ("", []),
            
            # 極短文本
            ("Hi", [("Hi", "call")]),
            ("OK", [("OK", "chat")]),
            ("Go", [("Go", "command")]),
            
            # 極長文本
            ("Hello UEP, I hope you're doing well today because I have a really long story to tell you about my amazing adventure in the mountains where I met some incredible people and learned so much about life. Anyway, can you please help me organize all the photos I took during this trip?",
             [("Hello UEP", "call"), ("I hope you're doing well today because I have a really long story to tell you about my amazing adventure in the mountains where I met some incredible people and learned so much about life", "chat"), ("can you please help me organize all the photos I took during this trip", "command")]),
            
            # 特殊字符
            ("Hello! Can you help me? Thanks!", [("Hello", "call"), ("Can you help me", "command"), ("Thanks", "chat")]),
            ("@UEP #help $save", [("@UEP", "call"), ("#help $save", "command")]),
            
            # 數字和標點
            ("UEP, save file_123.txt", [("UEP", "call"), ("save file_123.txt", "command")]),
            ("Set timer for 30 minutes", [("Set timer for 30 minutes", "command")])
        ]
        
        for text, expected in test_cases:
            self._test_case(text, expected, "邊界情況")
    
    def test_ambiguous_cases(self) -> None:
        """測試模糊情況"""
        info_log("[Tester] 開始測試模糊情況...")
        
        test_cases = [
            # 可能有歧義的句子
            ("Can you hear me?", [("Can you hear me", "call")]),  # 可能是call或command
            ("I need help", [("I need help", "chat")]),  # 可能是chat或command
            ("That's good", [("That's good", "chat")]),  # 可能是chat或command回應
            ("Please", [("Please", "call")]),  # 單詞可能有多種解釋
            ("Thanks for helping", [("Thanks for helping", "chat")])  # 感謝可能是chat
        ]
        
        for text, expected in test_cases:
            # 對於模糊情況，我們主要檢查是否能成功識別，不強制要求特定標籤
            self._test_case(text, expected, "模糊情況", strict=False)
    
    def test_performance(self) -> None:
        """測試性能"""
        info_log("[Tester] 開始測試性能...")
        
        import time
        
        # 測試單次預測時間
        test_text = "Hello UEP, I had a great day today. Can you help me with my schedule?"
        
        times = []
        for i in range(10):
            start_time = time.time()
            segments = self.bio_tagger.predict(test_text)
            end_time = time.time()
            times.append(end_time - start_time)
        
        avg_time = sum(times) / len(times)
        max_time = max(times)
        min_time = min(times)
        
        info_log(f"[Tester] 性能測試結果:")
        info_log(f"   平均預測時間: {avg_time:.4f}秒")
        info_log(f"   最大預測時間: {max_time:.4f}秒")
        info_log(f"   最小預測時間: {min_time:.4f}秒")
        
        # 性能要求：平均預測時間應該小於1秒
        if avg_time < 1.0:
            info_log(f"✅ 性能測試通過 (平均 {avg_time:.4f}s < 1.0s)")
        else:
            error_log(f"❌ 性能測試失敗 (平均 {avg_time:.4f}s >= 1.0s)")
    
    def test_batch_prediction(self) -> None:
        """測試批量預測"""
        info_log("[Tester] 開始測試批量預測...")
        
        batch_texts = [
            "Hello UEP",
            "I'm having a good day",
            "Set a reminder",
            "Hey there, the weather is nice. Can you help me?",
            "System wake up. That's interesting. Please save this file."
        ]
        
        try:
            for i, text in enumerate(batch_texts):
                segments = self.bio_tagger.predict(text)
                info_log(f"   批量測試 {i+1}: '{text}' -> {len(segments)} 個分段")
            
            info_log("✅ 批量預測測試通過")
        except Exception as e:
            error_log(f"❌ 批量預測測試失敗: {e}")
    
    def _test_case(self, text: str, expected: List[Tuple[str, str]], category: str, strict: bool = True) -> None:
        """測試單個案例"""
        try:
            segments = self.bio_tagger.predict(text)
            
            success = True
            details = []
            
            if strict:
                # 嚴格模式：檢查分段數量和內容
                if len(segments) != len(expected):
                    success = False
                    details.append(f"分段數量不匹配: 期望{len(expected)}, 實際{len(segments)}")
                
                for i, (pred_seg, exp_seg) in enumerate(zip(segments, expected)):
                    exp_text, exp_intent = exp_seg
                    pred_text = pred_seg['text']
                    pred_intent = pred_seg['intent']
                    
                    if pred_intent != exp_intent:
                        success = False
                        details.append(f"分段{i+1}意圖不匹配: 期望'{exp_intent}', 實際'{pred_intent}'")
            else:
                # 寬鬆模式：只檢查是否能成功預測
                if len(segments) == 0 and len(expected) > 0:
                    success = False
                    details.append("無法識別任何分段")
            
            # 記錄結果
            result = {
                'category': category,
                'text': text,
                'expected': expected,
                'predicted': segments,
                'success': success,
                'details': details
            }
            self.test_results.append(result)
            
            # 顯示結果
            status = "✅" if success else "❌"
            info_log(f"  {status} [{category}] '{text[:50]}{'...' if len(text) > 50 else ''}'")
            if not success and details:
                for detail in details:
                    error_log(f"      {detail}")
            
        except Exception as e:
            error_log(f"❌ [{category}] 測試失敗: '{text}' - {e}")
            result = {
                'category': category,
                'text': text,
                'expected': expected,
                'predicted': [],
                'success': False,
                'details': [f"異常: {e}"]
            }
            self.test_results.append(result)
    
    def test_validation_data(self) -> None:
        """測試驗證數據集"""
        info_log("[Tester] 開始測試驗證數據集...")
        
        val_data_path = "./data/annotated/dev.jsonl"
        if not Path(val_data_path).exists():
            error_log(f"[Tester] 驗證數據集不存在: {val_data_path}")
            return
        
        try:
            correct_predictions = 0
            total_samples = 0
            
            with open(val_data_path, 'r', encoding='utf-8') as f:
                for i, line in enumerate(f):
                    if i >= 20:  # 只測試前20個樣本
                        break
                        
                    if line.strip():
                        data = json.loads(line)
                        text = ' '.join(data['tokens'])
                        expected_labels = data['bio_labels']
                        
                        # 預測
                        segments = self.bio_tagger.predict(text)
                        
                        # 簡單評估：檢查是否有正確的分段數量
                        expected_segments = self._count_segments_from_bio(expected_labels)
                        predicted_segments = len(segments)
                        
                        if abs(predicted_segments - expected_segments) <= 1:  # 允許1個分段的誤差
                            correct_predictions += 1
                        
                        total_samples += 1
            
            accuracy = correct_predictions / total_samples if total_samples > 0 else 0
            info_log(f"[Tester] 驗證數據集測試結果:")
            info_log(f"   測試樣本: {total_samples}")
            info_log(f"   正確預測: {correct_predictions}")
            info_log(f"   準確率: {accuracy:.2%}")
            
            if accuracy >= 0.8:
                info_log("✅ 驗證數據集測試通過 (準確率 >= 80%)")
            else:
                error_log("❌ 驗證數據集測試失敗 (準確率 < 80%)")
                
        except Exception as e:
            error_log(f"[Tester] 驗證數據集測試失敗: {e}")
    
    def _count_segments_from_bio(self, bio_labels: List[str]) -> int:
        """從BIO標籤計算分段數量"""
        count = 0
        for label in bio_labels:
            if label.startswith('B-'):
                count += 1
        return count
    
    def generate_test_report(self) -> None:
        """生成測試報告"""
        info_log("[Tester] 生成測試報告...")
        
        # 統計結果
        total_tests = len(self.test_results)
        passed_tests = sum(1 for r in self.test_results if r['success'])
        failed_tests = total_tests - passed_tests
        
        # 按類別統計
        category_stats = {}
        for result in self.test_results:
            category = result['category']
            if category not in category_stats:
                category_stats[category] = {'total': 0, 'passed': 0}
            category_stats[category]['total'] += 1
            if result['success']:
                category_stats[category]['passed'] += 1
        
        # 生成報告
        report = {
            'summary': {
                'total_tests': total_tests,
                'passed_tests': passed_tests,
                'failed_tests': failed_tests,
                'success_rate': passed_tests / total_tests if total_tests > 0 else 0
            },
            'category_stats': category_stats,
            'failed_cases': [r for r in self.test_results if not r['success']]
        }
        
        # 保存報告
        report_path = "./data/test_report.json"
        with open(report_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        
        # 顯示摘要
        info_log(f"\n📊 測試報告摘要:")
        info_log(f"   總測試數: {total_tests}")
        info_log(f"   通過測試: {passed_tests}")
        info_log(f"   失敗測試: {failed_tests}")
        info_log(f"   成功率: {report['summary']['success_rate']:.2%}")
        
        info_log(f"\n📋 分類統計:")
        for category, stats in category_stats.items():
            success_rate = stats['passed'] / stats['total'] if stats['total'] > 0 else 0
            info_log(f"   {category}: {stats['passed']}/{stats['total']} ({success_rate:.2%})")
        
        if failed_tests > 0:
            info_log(f"\n❌ 失敗案例 ({failed_tests}個):")
            for i, result in enumerate(report['failed_cases'][:5], 1):  # 只顯示前5個
                info_log(f"   {i}. [{result['category']}] {result['text'][:50]}...")
                for detail in result['details'][:2]:  # 只顯示前2個詳情
                    info_log(f"      - {detail}")
            if failed_tests > 5:
                info_log(f"   ... 還有 {failed_tests - 5} 個失敗案例")
        
        info_log(f"\n📁 完整報告已保存至: {report_path}")
    
    def run_all_tests(self) -> bool:
        """運行所有測試"""
        info_log("🚀 開始全面測試BIO標註模型...")
        
        if not self.load_model():
            return False
        
        # 運行各種測試
        self.test_single_intent_examples()
        self.test_multi_intent_examples()
        self.test_edge_cases()
        self.test_ambiguous_cases()
        self.test_performance()
        self.test_batch_prediction()
        self.test_validation_data()
        
        # 生成報告
        self.generate_test_report()
        
        # 判斷整體是否通過
        total_tests = len(self.test_results)
        passed_tests = sum(1 for r in self.test_results if r['success'])
        success_rate = passed_tests / total_tests if total_tests > 0 else 0
        
        if success_rate >= 0.8:
            info_log(f"🎉 所有測試完成！模型表現良好 (成功率: {success_rate:.2%})")
            return True
        else:
            error_log(f"⚠️  模型需要改進 (成功率: {success_rate:.2%} < 80%)")
            return False

def main():
    """主函數"""
    model_path = "../../models/nlp/bio_tagger"
    
    if not Path(model_path).exists():
        error_log(f"[Main] 模型不存在: {model_path}")
        error_log("[Main] 請先運行 train_bio_model.py 訓練模型")
        return
    
    # 創建測試器
    tester = BIOModelTester(model_path)
    
    # 運行測試
    success = tester.run_all_tests()
    
    if success:
        info_log("✅ 模型測試通過，可以投入使用！")
    else:
        error_log("❌ 模型測試未完全通過，建議進一步調優")

if __name__ == "__main__":
    main()
