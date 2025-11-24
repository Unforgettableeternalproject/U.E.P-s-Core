#!/usr/bin/env python3
"""
實用的數據收集和標註工具
替代自動生成器，支援手工標註和質量控制
"""

import json
import re
import pandas as pd
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional
from dataclasses import dataclass, asdict
from datetime import datetime
import hashlib

@dataclass
class SegmentAnnotation:
    """分段標註"""
    text: str
    label: str  # CALL, CHAT, COMMAND, COMPOUND
    start: int
    end: int
    confidence: float = 1.0
    annotator_notes: str = ""

@dataclass 
class TrainingExample:
    """訓練範例"""
    id: str
    text: str
    segments: List[SegmentAnnotation]
    metadata: Dict[str, Any]
    
    def to_dict(self) -> Dict[str, Any]:
        """轉換為字典"""
        return {
            'id': self.id,
            'text': self.text,
            'segments': [asdict(seg) for seg in self.segments],
            'metadata': self.metadata
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TrainingExample':
        """從字典創建"""
        segments = [SegmentAnnotation(**seg) for seg in data['segments']]
        return cls(
            id=data['id'],
            text=data['text'],
            segments=segments,
            metadata=data['metadata']
        )

class AnnotationTool:
    """標註工具"""
    
    def __init__(self, data_dir: str = "./train/nlp/data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        # 子目錄
        self.raw_dir = self.data_dir / "raw"
        self.annotated_dir = self.data_dir / "annotated"
        self.metadata_dir = self.data_dir / "metadata"
        self.statistics_dir = self.data_dir / "statistics"
        
        for dir_path in [self.raw_dir, self.annotated_dir, self.metadata_dir, self.statistics_dir]:
            dir_path.mkdir(exist_ok=True)
        
        self.examples: List[TrainingExample] = []
        self.load_existing_data()
    
    def load_existing_data(self):
        """載入已有的標註數據"""
        annotated_files = list(self.annotated_dir.glob("*.jsonl"))
        
        for file_path in annotated_files:
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        if line.strip():
                            data = json.loads(line)
                            example = TrainingExample.from_dict(data)
                            self.examples.append(example)
                            
                print(f"✅ 載入了 {len(self.examples)} 個已標註範例 from {file_path}")
            except Exception as e:
                print(f"⚠️  載入 {file_path} 失敗: {e}")
    
    def add_raw_text(self, text: str, source: str = "manual", 
                    scenario: str = "unknown") -> str:
        """添加原始文本準備標註"""
        # 生成唯一ID
        text_hash = hashlib.md5(text.encode()).hexdigest()[:8]
        example_id = f"{scenario}_{text_hash}"
        
        # 檢查是否已存在
        existing_ids = [ex.id for ex in self.examples]
        if example_id in existing_ids:
            print(f"⚠️  文本已存在: {example_id}")
            return example_id
        
        # 創建範例
        example = TrainingExample(
            id=example_id,
            text=text,
            segments=[],  # 待標註
            metadata={
                'source': source,
                'scenario': scenario,
                'created_date': datetime.now().isoformat(),
                'annotated': False,
                'quality_checked': False
            }
        )
        
        self.examples.append(example)
        print(f"✅ 添加了待標註文本: {example_id}")
        return example_id
    
    def annotate_example(self, example_id: str, segments: List[Dict[str, Any]], 
                        annotator: str = "unknown") -> bool:
        """標註範例"""
        # 找到範例
        example = None
        for ex in self.examples:
            if ex.id == example_id:
                example = ex
                break
        
        if not example:
            print(f"❌ 找不到範例: {example_id}")
            return False
        
        # 驗證分段
        if not self._validate_segments(example.text, segments):
            print(f"❌ 分段驗證失敗: {example_id}")
            return False
        
        # 創建分段標註
        segment_annotations = []
        for seg in segments:
            annotation = SegmentAnnotation(
                text=seg['text'],
                label=seg['label'],
                start=seg['start'],
                end=seg['end'],
                confidence=seg.get('confidence', 1.0),
                annotator_notes=seg.get('notes', '')
            )
            segment_annotations.append(annotation)
        
        # 更新範例
        example.segments = segment_annotations
        example.metadata.update({
            'annotated': True,
            'annotator': annotator,
            'annotation_date': datetime.now().isoformat()
        })
        
        print(f"✅ 完成標註: {example_id} ({len(segments)} 個分段)")
        return True
    
    def _validate_segments(self, text: str, segments: List[Dict[str, Any]]) -> bool:
        """驗證分段的合法性"""
        # 檢查基本欄位
        for seg in segments:
            required_fields = ['text', 'label', 'start', 'end']
            if not all(field in seg for field in required_fields):
                print(f"❌ 分段缺少必要欄位: {seg}")
                return False
            
            # 檢查位置
            start, end = seg['start'], seg['end']
            if start >= end or end > len(text):
                print(f"❌ 無效的分段位置: [{start}, {end}] for text length {len(text)}")
                return False
            
            # 檢查文本一致性
            segment_text = text[start:end]
            if segment_text != seg['text']:
                print(f"❌ 分段文本不匹配: '{segment_text}' != '{seg['text']}'")
                return False
            
            # 檢查標籤有效性
            valid_labels = ['CALL', 'CHAT', 'COMMAND', 'COMPOUND']
            if seg['label'] not in valid_labels:
                print(f"❌ 無效的標籤: {seg['label']}")
                return False
        
        # 檢查重疊
        segments_sorted = sorted(segments, key=lambda x: x['start'])
        for i in range(len(segments_sorted) - 1):
            if segments_sorted[i]['end'] > segments_sorted[i+1]['start']:
                print(f"❌ 分段重疊: {segments_sorted[i]} 和 {segments_sorted[i+1]}")
                return False
        
        return True
    
    def export_training_data(self, format_type: str = "both", 
                           train_ratio: float = 0.7, dev_ratio: float = 0.15):
        """導出訓練數據"""
        # 只導出已標註的數據
        annotated_examples = [ex for ex in self.examples if ex.metadata.get('annotated', False)]
        
        if not annotated_examples:
            print("❌ 沒有已標註的數據可導出")
            return
        
        print(f"📊 準備導出 {len(annotated_examples)} 個已標註範例")
        
        # 數據分割
        import random
        random.shuffle(annotated_examples)
        
        total = len(annotated_examples)
        train_size = int(total * train_ratio)
        dev_size = int(total * dev_ratio)
        
        train_data = annotated_examples[:train_size]
        dev_data = annotated_examples[train_size:train_size + dev_size]
        test_data = annotated_examples[train_size + dev_size:]
        
        print(f"📋 數據分割: Train={len(train_data)}, Dev={len(dev_data)}, Test={len(test_data)}")
        
        # 導出JSONL格式
        if format_type in ["jsonl", "both"]:
            self._export_jsonl(train_data, "train.jsonl")
            self._export_jsonl(dev_data, "dev.jsonl") 
            self._export_jsonl(test_data, "test.jsonl")
        
        # 導出CoNLL-U格式
        if format_type in ["conllu", "both"]:
            self._export_conllu(train_data, "train.conllu")
            self._export_conllu(dev_data, "dev.conllu")
            self._export_conllu(test_data, "test.conllu")
        
        # 生成統計報告
        self._generate_statistics(annotated_examples)
    
    def _export_jsonl(self, examples: List[TrainingExample], filename: str):
        """導出JSONL格式"""
        filepath = self.annotated_dir / filename
        
        with open(filepath, 'w', encoding='utf-8') as f:
            for example in examples:
                # 轉換為訓練格式
                training_data = {
                    'id': example.id,
                    'text': example.text,
                    'tokens': example.text.split(),  # 簡化分詞
                    'bio_labels': self._segments_to_bio(example),
                    'segments': [asdict(seg) for seg in example.segments],
                    'metadata': example.metadata
                }
                f.write(json.dumps(training_data, ensure_ascii=False) + '\n')
        
        print(f"✅ JSONL格式已導出: {filepath}")
    
    def _export_conllu(self, examples: List[TrainingExample], filename: str):
        """導出CoNLL-U格式"""
        filepath = self.annotated_dir / filename
        
        with open(filepath, 'w', encoding='utf-8') as f:
            for example in examples:
                # 寫入元數據
                f.write(f"# sent_id = {example.id}\n")
                f.write(f"# text = {example.text}\n")
                f.write(f"# intent_segments = {json.dumps([asdict(seg) for seg in example.segments])}\n")
                f.write(f"# metadata = {json.dumps(example.metadata)}\n")
                
                # 寫入token和BIO標籤
                tokens = example.text.split()  # 簡化分詞
                bio_labels = self._segments_to_bio(example)
                
                for i, (token, label) in enumerate(zip(tokens, bio_labels), 1):
                    f.write(f"{i}\t{token}\t_\t_\t_\t_\t_\t_\t{label}\t_\n")
                
                f.write("\n")  # 句子分隔
        
        print(f"✅ CoNLL-U格式已導出: {filepath}")
    
    def _segments_to_bio(self, example: TrainingExample) -> List[str]:
        """將分段轉換為BIO標籤"""
        tokens = example.text.split()
        bio_labels = ['O'] * len(tokens)
        
        for segment in example.segments:
            # 找到token範圍 (簡化版本)
            char_pos = 0
            token_start = None
            token_end = None
            
            for i, token in enumerate(tokens):
                token_char_start = char_pos
                token_char_end = char_pos + len(token)
                
                # 找到第一個相交的token
                if token_start is None and token_char_end > segment.start:
                    token_start = i
                
                # 找到最後一個相交的token
                if token_char_start < segment.end:
                    token_end = i
                
                char_pos = token_char_end + 1  # +1 for space
            
            # 標記BIO標籤
            if token_start is not None and token_end is not None:
                for i in range(token_start, token_end + 1):
                    if i == token_start:
                        bio_labels[i] = f'B-{segment.label}'
                    else:
                        bio_labels[i] = f'I-{segment.label}'
        
        return bio_labels
    
    def _generate_statistics(self, examples: List[TrainingExample]):
        """生成數據統計"""
        stats = {
            'total_examples': len(examples),
            'label_distribution': {},
            'length_distribution': {
                'min': float('inf'),
                'max': 0,
                'avg': 0,
                'tokens': []
            },
            'complexity_distribution': {
                'single_intent': 0,
                'multi_intent': 0
            },
            'quality_metrics': {
                'annotated_examples': len(examples),
                'avg_segments_per_example': 0
            }
        }
        
        total_segments = 0
        total_length = 0
        
        for example in examples:
            # 長度統計
            text_length = len(example.text)
            token_count = len(example.text.split())
            
            total_length += text_length
            stats['length_distribution']['min'] = min(stats['length_distribution']['min'], text_length)
            stats['length_distribution']['max'] = max(stats['length_distribution']['max'], text_length)
            stats['length_distribution']['tokens'].append(token_count)
            
            # 標籤分佈
            for segment in example.segments:
                label = segment.label
                stats['label_distribution'][label] = stats['label_distribution'].get(label, 0) + 1
                total_segments += 1
            
            # 複雜度分佈
            if len(example.segments) == 1:
                stats['complexity_distribution']['single_intent'] += 1
            else:
                stats['complexity_distribution']['multi_intent'] += 1
        
        # 計算平均值
        if examples:
            stats['length_distribution']['avg'] = total_length / len(examples)
            stats['quality_metrics']['avg_segments_per_example'] = total_segments / len(examples)
        
        # 保存統計
        stats_file = self.statistics_dir / "data_statistics.json"
        with open(stats_file, 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
        
        print(f"📊 數據統計已保存: {stats_file}")
        print(f"   總範例數: {stats['total_examples']}")
        print(f"   標籤分佈: {stats['label_distribution']}")
        print(f"   平均長度: {stats['length_distribution']['avg']:.1f} 字符")
        print(f"   複雜度分佈: {stats['complexity_distribution']}")
    
    def interactive_annotation(self):
        """互動式標註介面"""
        print("🏷️  進入互動式標註模式")
        print("輸入 'help' 查看命令列表，輸入 'quit' 退出")
        
        while True:
            try:
                command = input("\n📝 > ").strip()
                
                if command == 'quit':
                    break
                elif command == 'help':
                    self._show_help()
                elif command.startswith('add '):
                    text = command[4:].strip()
                    if text:
                        example_id = self.add_raw_text(text)
                        print(f"✅ 文本已添加，ID: {example_id}")
                elif command.startswith('list'):
                    self._list_examples()
                elif command.startswith('annotate '):
                    example_id = command[9:].strip()
                    self._interactive_annotate(example_id)
                elif command == 'export':
                    self.export_training_data()
                elif command == 'stats':
                    annotated = [ex for ex in self.examples if ex.metadata.get('annotated', False)]
                    if annotated:
                        self._generate_statistics(annotated)
                    else:
                        print("❌ 沒有已標註的數據")
                else:
                    print(f"❓ 未知命令: {command}")
                    
            except KeyboardInterrupt:
                print("\n👋 再見！")
                break
            except Exception as e:
                print(f"❌ 錯誤: {e}")
    
    def _show_help(self):
        """顯示幫助"""
        help_text = """
📚 命令列表:
  add <text>        - 添加待標註文本
  list             - 列出所有範例
  annotate <id>    - 標註指定範例
  export           - 導出訓練數據
  stats            - 顯示統計信息
  help             - 顯示此幫助
  quit             - 退出
        """
        print(help_text)
    
    def _list_examples(self):
        """列出範例"""
        if not self.examples:
            print("📭 沒有範例")
            return
        
        print(f"\n📋 範例列表 (共 {len(self.examples)} 個):")
        for ex in self.examples[-10:]:  # 只顯示最後10個
            status = "✅" if ex.metadata.get('annotated', False) else "⏳"
            print(f"  {status} {ex.id}: {ex.text[:50]}...")
    
    def _interactive_annotate(self, example_id: str):
        """互動式標註"""
        example = None
        for ex in self.examples:
            if ex.id == example_id:
                example = ex
                break
        
        if not example:
            print(f"❌ 找不到範例: {example_id}")
            return
        
        print(f"\n📝 標註範例: {example_id}")
        print(f"文本: {example.text}")
        print("\n請輸入分段信息 (格式: start,end,label,text)")
        print("例如: 0,5,CALL,Hello")
        print("輸入 'done' 完成標註")
        
        segments = []
        while True:
            try:
                line = input("分段 > ").strip()
                
                if line == 'done':
                    break
                elif line == 'cancel':
                    print("❌ 取消標註")
                    return
                
                parts = line.split(',', 3)
                if len(parts) != 4:
                    print("❌ 格式錯誤，請使用: start,end,label,text")
                    continue
                
                start, end, label, text = parts
                start, end = int(start), int(end)
                
                segment = {
                    'start': start,
                    'end': end,
                    'label': label.upper(),
                    'text': text,
                    'confidence': 1.0
                }
                
                segments.append(segment)
                print(f"✅ 添加分段: {segment}")
                
            except ValueError:
                print("❌ 位置必須是數字")
            except Exception as e:
                print(f"❌ 錯誤: {e}")
        
        if segments:
            if self.annotate_example(example_id, segments):
                print("✅ 標註完成")
            else:
                print("❌ 標註失敗")
        else:
            print("❌ 沒有有效的分段")


def main():
    """主函數 - 演示用法"""
    tool = AnnotationTool()
    
    # 示例：添加一些文本
    examples = [
        "Hello, are you there? I was thinking about the weather today.",
        "Hi UEP! How has your day been? Please set a reminder for my meeting.",
        "Hey there, the weather is beautiful today. Can you check my calendar?",
        "Hello! I just finished watching a great movie. Could you help me find more movies like it?"
    ]
    
    for text in examples:
        tool.add_raw_text(text, source="demo", scenario="daily_chat")
    
    print(f"📋 添加了 {len(examples)} 個待標註範例")
    print("💡 運行 tool.interactive_annotation() 開始標註")
    
    return tool

if __name__ == "__main__":
    tool = main()
    # 可以取消註釋下面這行來啟動互動式標註
    # tool.interactive_annotation()
