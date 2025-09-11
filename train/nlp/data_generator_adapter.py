#!/usr/bin/env python3
"""
適配器：將新的資料生成器輸出轉換為標註工具格式
"""

import json
import sys
from pathlib import Path
from typing import List, Dict, Any

# 添加項目根目錄到路徑
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from train.nlp.annotation_tool import AnnotationTool, TrainingExample, SegmentAnnotation

class DataGeneratorAdapter:
    """資料生成器格式適配器"""
    
    def __init__(self):
        self.annotation_tool = AnnotationTool()
    
    def convert_generated_to_annotation_format(self, generated_data: List[Dict[str, Any]]) -> List[TrainingExample]:
        """將生成的資料轉換為標註工具格式"""
        converted_examples = []
        
        for data in generated_data:
            # 轉換分段格式
            segments = []
            for seg in data['segments']:
                segment_annotation = SegmentAnnotation(
                    text=seg['text'],
                    label=seg['label'],
                    start=seg['start'],
                    end=seg['end'],
                    confidence=seg.get('confidence', 1.0),
                    annotator_notes=seg.get('annotator_notes', '')
                )
                segments.append(segment_annotation)
            
            # 創建訓練範例
            example = TrainingExample(
                id=data['id'],
                text=data['text'],
                segments=segments,
                metadata=data['metadata']
            )
            
            converted_examples.append(example)
            
        return converted_examples
    
    def import_generated_dataset(self, jsonl_file: str) -> int:
        """導入生成的JSONL數據集到標註工具"""
        imported_count = 0
        
        try:
            with open(jsonl_file, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, 1):
                    if line.strip():
                        try:
                            data = json.loads(line)
                            
                            # 添加到標註工具
                            example = TrainingExample(
                                id=data['id'],
                                text=data['text'],
                                segments=[
                                    SegmentAnnotation(
                                        text=seg['text'],
                                        label=seg['label'],
                                        start=seg['start'],
                                        end=seg['end'],
                                        confidence=seg.get('confidence', 1.0),
                                        annotator_notes=seg.get('annotator_notes', '')
                                    ) for seg in data['segments']
                                ],
                                metadata=data['metadata']
                            )
                            
                            # 添加到工具
                            self.annotation_tool.examples.append(example)
                            imported_count += 1
                            
                        except json.JSONDecodeError as e:
                            print(f"⚠️  第 {line_num} 行JSON解析錯誤: {e}")
                        except Exception as e:
                            print(f"⚠️  第 {line_num} 行處理錯誤: {e}")
            
            print(f"✅ 成功導入 {imported_count} 個訓練範例")
            
            # 導出為標準格式
            if imported_count > 0:
                self.annotation_tool.export_training_data(format_type="both")
                print("📤 已導出為標準訓練格式")
            
            return imported_count
            
        except FileNotFoundError:
            print(f"❌ 檔案不存在: {jsonl_file}")
            return 0
        except Exception as e:
            print(f"❌ 導入失敗: {e}")
            return 0
    
    def validate_generated_data(self, jsonl_file: str) -> Dict[str, Any]:
        """驗證生成的數據質量"""
        stats = {
            'total_examples': 0,
            'valid_examples': 0,
            'invalid_examples': 0,
            'label_distribution': {},
            'complexity_distribution': {'single': 0, 'double': 0, 'triple': 0},
            'errors': []
        }
        
        try:
            with open(jsonl_file, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, 1):
                    if line.strip():
                        stats['total_examples'] += 1
                        
                        try:
                            data = json.loads(line)
                            
                            # 驗證必要欄位
                            required_fields = ['id', 'text', 'tokens', 'bio_labels', 'segments']
                            missing_fields = [field for field in required_fields if field not in data]
                            
                            if missing_fields:
                                stats['invalid_examples'] += 1
                                stats['errors'].append(f"第 {line_num} 行缺少欄位: {missing_fields}")
                                continue
                            
                            # 驗證BIO標籤一致性
                            if len(data['tokens']) != len(data['bio_labels']):
                                stats['invalid_examples'] += 1
                                stats['errors'].append(f"第 {line_num} 行: tokens和bio_labels長度不一致")
                                continue
                            
                            # 驗證分段
                            text = data['text']
                            segments = data['segments']
                            segment_valid = True
                            
                            for seg in segments:
                                start, end = seg['start'], seg['end']
                                if start >= end or end > len(text):
                                    stats['invalid_examples'] += 1
                                    stats['errors'].append(f"第 {line_num} 行: 無效的分段位置 [{start}, {end}]")
                                    segment_valid = False
                                    break
                                
                                actual_text = text[start:end]
                                if actual_text != seg['text']:
                                    stats['invalid_examples'] += 1
                                    stats['errors'].append(f"第 {line_num} 行: 分段文本不匹配")
                                    segment_valid = False
                                    break
                            
                            if not segment_valid:
                                continue
                            
                            # 統計
                            stats['valid_examples'] += 1
                            
                            # 標籤分佈
                            for seg in segments:
                                label = seg['label']
                                stats['label_distribution'][label] = stats['label_distribution'].get(label, 0) + 1
                            
                            # 複雜度分佈
                            segment_count = len(segments)
                            if segment_count == 1:
                                stats['complexity_distribution']['single'] += 1
                            elif segment_count == 2:
                                stats['complexity_distribution']['double'] += 1
                            else:
                                stats['complexity_distribution']['triple'] += 1
                                
                        except json.JSONDecodeError as e:
                            stats['invalid_examples'] += 1
                            stats['errors'].append(f"第 {line_num} 行JSON解析錯誤: {e}")
                        except Exception as e:
                            stats['invalid_examples'] += 1
                            stats['errors'].append(f"第 {line_num} 行處理錯誤: {e}")
        
        except Exception as e:
            stats['errors'].append(f"檔案讀取錯誤: {e}")
        
        return stats

def test_adapter():
    """測試適配器功能"""
    adapter = DataGeneratorAdapter()
    
    # 檢查是否有生成的數據檔案
    jsonl_file = "nlp_training_data.jsonl"
    
    if not Path(jsonl_file).exists():
        print("🔍 未找到生成的數據檔案，先運行資料生成器...")
        
        # 運行資料生成器
        try:
            exec(open('training_data_generator.py').read())
            print("✅ 資料生成器運行完成")
        except Exception as e:
            print(f"❌ 資料生成器運行失敗: {e}")
            return
    
    # 驗證數據
    print("🔍 驗證生成的數據...")
    stats = adapter.validate_generated_data(jsonl_file)
    
    print(f"\n📊 數據驗證結果:")
    print(f"   總範例數: {stats['total_examples']}")
    print(f"   有效範例: {stats['valid_examples']}")
    print(f"   無效範例: {stats['invalid_examples']}")
    print(f"   標籤分佈: {stats['label_distribution']}")
    print(f"   複雜度分佈: {stats['complexity_distribution']}")
    
    if stats['errors']:
        print(f"\n⚠️  發現 {len(stats['errors'])} 個錯誤:")
        for i, error in enumerate(stats['errors'][:5], 1):  # 只顯示前5個
            print(f"   {i}. {error}")
        if len(stats['errors']) > 5:
            print(f"   ... 還有 {len(stats['errors']) - 5} 個錯誤")
    
    # 導入數據
    if stats['valid_examples'] > 0:
        print(f"\n📥 導入有效數據到標註工具...")
        imported_count = adapter.import_generated_dataset(jsonl_file)
        
        if imported_count > 0:
            print(f"🎉 成功處理 {imported_count} 個訓練範例")
            print("📁 數據已導出為標準格式到 train/nlp/data/annotated/")
        else:
            print("❌ 導入失敗")
    else:
        print("❌ 沒有有效數據可導入")

if __name__ == "__main__":
    test_adapter()
