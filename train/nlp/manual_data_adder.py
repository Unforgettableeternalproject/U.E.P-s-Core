#!/usr/bin/env python3
"""
手動數據添加工具
幫助用戶正確格式化和添加新的訓練數據
"""

import json
import uuid
import re
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Tuple
import sys

# 添加項目根目錄到路徑
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from utils.debug_helper import debug_log, info_log, error_log

class ManualDataAdder:
    """手動數據添加工具"""
    
    def __init__(self):
        self.data_dir = Path("./data")
        self.annotated_dir = self.data_dir / "annotated"
        self.backup_dir = self.data_dir / "backup"
        
        # 確保目錄存在
        self.backup_dir.mkdir(exist_ok=True)
    
    def tokenize_text(self, text: str) -> List[str]:
        """將文本分詞"""
        # 簡單的分詞：按空格分割，保留標點
        tokens = []
        current_token = ""
        
        for char in text:
            if char.isspace():
                if current_token:
                    tokens.append(current_token)
                    current_token = ""
            elif char in ".,!?;:":
                if current_token:
                    tokens.append(current_token)
                    current_token = ""
                tokens.append(char)
            else:
                current_token += char
        
        if current_token:
            tokens.append(current_token)
        
        return tokens
    
    def calculate_segment_positions(self, text: str, segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """計算分段在原文中的準確位置"""
        updated_segments = []
        
        for segment in segments:
            segment_text = segment['text']
            label = segment['label']
            
            # 在原文中查找分段位置
            start_pos = text.find(segment_text)
            if start_pos == -1:
                # 如果找不到完全匹配，嘗試模糊匹配
                error_log(f"⚠️  無法在原文中找到分段: '{segment_text}'")
                error_log(f"   原文: '{text}'")
                # 使用用戶提供的位置或估算
                start_pos = segment.get('start', 0)
                end_pos = segment.get('end', len(segment_text))
            else:
                end_pos = start_pos + len(segment_text)
            
            updated_segments.append({
                'text': segment_text,
                'label': label.upper(),
                'start': start_pos,
                'end': end_pos,
                'confidence': segment.get('confidence', 1.0),
                'annotator_notes': segment.get('annotator_notes', '')
            })
        
        return updated_segments
    
    def generate_bio_labels(self, tokens: List[str], segments: List[Dict[str, Any]], text: str) -> List[str]:
        """生成BIO標籤"""
        bio_labels = ['O'] * len(tokens)
        
        # 計算每個token在原文中的位置
        token_positions = []
        char_pos = 0
        
        for token in tokens:
            # 跳過空白字符
            while char_pos < len(text) and text[char_pos].isspace():
                char_pos += 1
            
            token_start = char_pos
            token_end = char_pos + len(token)
            token_positions.append((token_start, token_end))
            char_pos = token_end
        
        # 為每個分段分配BIO標籤
        for segment in segments:
            seg_start = segment['start']
            seg_end = segment['end']
            label = segment['label']
            
            first_token = True
            for i, (token_start, token_end) in enumerate(token_positions):
                # 檢查token是否在分段範圍內
                if (token_start >= seg_start and token_end <= seg_end) or \
                   (token_start < seg_end and token_end > seg_start):
                    
                    if first_token:
                        bio_labels[i] = f'B-{label}'
                        first_token = False
                    else:
                        bio_labels[i] = f'I-{label}'
        
        return bio_labels
    
    def create_training_example(self, text: str, segments: List[Dict[str, Any]], 
                              metadata: Dict[str, Any] = None) -> Dict[str, Any]:
        """創建訓練範例"""
        # 生成唯一ID
        example_id = f"manual_{uuid.uuid4().hex[:8]}"
        
        # 分詞
        tokens = self.tokenize_text(text)
        
        # 計算分段位置
        updated_segments = self.calculate_segment_positions(text, segments)
        
        # 生成BIO標籤
        bio_labels = self.generate_bio_labels(tokens, updated_segments, text)
        
        # 預設metadata
        default_metadata = {
            "source": "manual_annotation",
            "scenario": "user_input",
            "created_date": datetime.now().isoformat(),
            "annotated": True,
            "quality_checked": True,
            "annotator": "human",
            "annotation_date": datetime.now().isoformat()
        }
        
        if metadata:
            default_metadata.update(metadata)
        
        return {
            "id": example_id,
            "text": text,
            "tokens": tokens,
            "bio_labels": bio_labels,
            "segments": updated_segments,
            "metadata": default_metadata
        }
    
    def validate_example(self, example: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """驗證訓練範例的正確性"""
        errors = []
        
        # 檢查必要欄位
        required_fields = ['id', 'text', 'tokens', 'bio_labels', 'segments']
        for field in required_fields:
            if field not in example:
                errors.append(f"缺少必要欄位: {field}")
        
        if errors:
            return False, errors
        
        # 檢查長度一致性
        if len(example['tokens']) != len(example['bio_labels']):
            errors.append(f"tokens和bio_labels長度不一致: {len(example['tokens'])} vs {len(example['bio_labels'])}")
        
        # 檢查分段位置
        text = example['text']
        for i, segment in enumerate(example['segments']):
            start, end = segment['start'], segment['end']
            if start >= end:
                errors.append(f"分段{i+1}位置無效: start({start}) >= end({end})")
            elif end > len(text):
                errors.append(f"分段{i+1}結束位置超出文本範圍: {end} > {len(text)}")
            else:
                actual_text = text[start:end]
                expected_text = segment['text']
                if actual_text != expected_text:
                    errors.append(f"分段{i+1}文本不匹配: 期望'{expected_text}', 實際'{actual_text}'")
        
        # 檢查BIO標籤格式
        valid_labels = ['O', 'B-CALL', 'I-CALL', 'B-CHAT', 'I-CHAT', 'B-COMMAND', 'I-COMMAND']
        for i, label in enumerate(example['bio_labels']):
            if label not in valid_labels:
                errors.append(f"無效的BIO標籤: {label} (位置 {i})")
        
        return len(errors) == 0, errors
    
    def backup_existing_data(self):
        """備份現有數據"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        for split in ['train', 'dev', 'test']:
            jsonl_file = self.annotated_dir / f"{split}.jsonl"
            conllu_file = self.annotated_dir / f"{split}.conllu"
            
            if jsonl_file.exists():
                backup_jsonl = self.backup_dir / f"{split}_{timestamp}.jsonl"
                backup_jsonl.write_text(jsonl_file.read_text(encoding='utf-8'), encoding='utf-8')
                info_log(f"備份: {jsonl_file} -> {backup_jsonl}")
            
            if conllu_file.exists():
                backup_conllu = self.backup_dir / f"{split}_{timestamp}.conllu"
                backup_conllu.write_text(conllu_file.read_text(encoding='utf-8'), encoding='utf-8')
                info_log(f"備份: {conllu_file} -> {backup_conllu}")
    
    def add_examples_to_training_set(self, examples: List[Dict[str, Any]], 
                                   split: str = "train") -> bool:
        """將範例添加到訓練集"""
        try:
            # 備份現有數據
            self.backup_existing_data()
            
            # 載入現有數據
            jsonl_file = self.annotated_dir / f"{split}.jsonl"
            existing_examples = []
            
            if jsonl_file.exists():
                with open(jsonl_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        if line.strip():
                            existing_examples.append(json.loads(line))
            
            # 添加新範例
            all_examples = existing_examples + examples
            
            # 保存更新的數據
            with open(jsonl_file, 'w', encoding='utf-8') as f:
                for example in all_examples:
                    f.write(json.dumps(example, ensure_ascii=False) + '\n')
            
            info_log(f"成功添加 {len(examples)} 個範例到 {split}.jsonl")
            info_log(f"總計 {len(all_examples)} 個訓練範例")
            
            # 重新生成CoNLL-U格式
            self._generate_conllu_format(all_examples, split)
            
            return True
            
        except Exception as e:
            error_log(f"添加數據失敗: {e}")
            return False
    
    def _generate_conllu_format(self, examples: List[Dict[str, Any]], split: str):
        """生成CoNLL-U格式數據"""
        conllu_file = self.annotated_dir / f"{split}.conllu"
        
        with open(conllu_file, 'w', encoding='utf-8') as f:
            for example in examples:
                f.write(f"# sent_id = {example['id']}\n")
                f.write(f"# text = {example['text']}\n")
                
                for i, (token, label) in enumerate(zip(example['tokens'], example['bio_labels'])):
                    f.write(f"{i+1}\t{token}\t_\t_\t_\t_\t_\t_\t_\t{label}\n")
                
                f.write("\n")
        
        info_log(f"生成CoNLL-U格式: {conllu_file}")
    
    def interactive_add_example(self):
        """互動式添加範例"""
        info_log("🛠️  互動式數據添加工具")
        info_log("請按照提示輸入新的訓練範例")
        
        # 輸入文本
        print("\n請輸入要標註的文本:")
        text = input("文本: ").strip()
        
        if not text:
            error_log("文本不能為空")
            return False
        
        # 輸入分段
        segments = []
        print(f"\n文本: '{text}'")
        print("請輸入分段資訊 (輸入空行結束):")
        
        segment_num = 1
        while True:
            print(f"\n=== 分段 {segment_num} ===")
            segment_text = input("分段文本: ").strip()
            
            if not segment_text:
                break
            
            print("意圖類別: 1=CALL, 2=CHAT, 3=COMMAND")
            intent_choice = input("選擇 (1-3): ").strip()
            
            intent_map = {'1': 'CALL', '2': 'CHAT', '3': 'COMMAND'}
            intent = intent_map.get(intent_choice, 'CHAT')
            
            segments.append({
                'text': segment_text,
                'label': intent,
                'confidence': 1.0,
                'annotator_notes': ''
            })
            
            segment_num += 1
        
        if not segments:
            error_log("至少需要一個分段")
            return False
        
        # 創建範例
        try:
            example = self.create_training_example(text, segments)
            
            # 驗證範例
            is_valid, errors = self.validate_example(example)
            
            if not is_valid:
                error_log("範例驗證失敗:")
                for error in errors:
                    error_log(f"  - {error}")
                return False
            
            # 顯示預覽
            print("\n📋 範例預覽:")
            print(f"ID: {example['id']}")
            print(f"文本: {example['text']}")
            print(f"Tokens: {example['tokens']}")
            print(f"BIO標籤: {example['bio_labels']}")
            print(f"分段:")
            for i, seg in enumerate(example['segments'], 1):
                print(f"  {i}. [{seg['start']}:{seg['end']}] {seg['label']}: '{seg['text']}'")
            
            # 確認添加
            confirm = input("\n確認添加此範例? (y/N): ").strip().lower()
            
            if confirm == 'y':
                success = self.add_examples_to_training_set([example])
                if success:
                    info_log("✅ 範例添加成功!")
                    return True
                else:
                    error_log("❌ 範例添加失敗!")
                    return False
            else:
                info_log("已取消添加")
                return False
                
        except Exception as e:
            error_log(f"創建範例失敗: {e}")
            return False
    
    def add_batch_examples(self, examples_data: List[Tuple[str, List[Dict[str, Any]]]]) -> bool:
        """批量添加範例"""
        info_log(f"🔄 批量添加 {len(examples_data)} 個範例...")
        
        all_examples = []
        
        for i, (text, segments) in enumerate(examples_data, 1):
            try:
                example = self.create_training_example(text, segments)
                
                # 驗證範例
                is_valid, errors = self.validate_example(example)
                
                if not is_valid:
                    error_log(f"範例 {i} 驗證失敗:")
                    for error in errors:
                        error_log(f"  - {error}")
                    continue
                
                all_examples.append(example)
                info_log(f"✅ 範例 {i}: '{text[:50]}{'...' if len(text) > 50 else ''}'")
                
            except Exception as e:
                error_log(f"❌ 範例 {i} 處理失敗: {e}")
                continue
        
        if all_examples:
            success = self.add_examples_to_training_set(all_examples)
            if success:
                info_log(f"🎉 成功添加 {len(all_examples)} 個範例!")
                return True
        
        return False

def main():
    """主函數"""
    adder = ManualDataAdder()
    
    print("📝 手動數據添加工具")
    print("="*50)
    print("1. 互動式添加單個範例")
    print("2. 批量添加範例 (程式碼中定義)")
    print("3. 查看現有數據統計")
    
    choice = input("\n請選擇操作 (1-3): ").strip()
    
    if choice == '1':
        adder.interactive_add_example()
    
    elif choice == '2':
        # 示例批量數據
        batch_examples = [
            ("Hey UEP, how's the weather today?", [
                {'text': 'Hey UEP', 'label': 'CALL'},
                {'text': "how's the weather today?", 'label': 'COMMAND'}
            ]),
            ("I'm really excited about this project!", [
                {'text': "I'm really excited about this project!", 'label': 'CHAT'}
            ]),
            ("System wake up, please save my work", [
                {'text': 'System wake up', 'label': 'CALL'},
                {'text': 'please save my work', 'label': 'COMMAND'}
            ])
        ]
        
        adder.add_batch_examples(batch_examples)
    
    elif choice == '3':
        # 顯示統計
        for split in ['train', 'dev', 'test']:
            jsonl_file = adder.annotated_dir / f"{split}.jsonl"
            if jsonl_file.exists():
                with open(jsonl_file, 'r', encoding='utf-8') as f:
                    count = sum(1 for line in f if line.strip())
                info_log(f"{split}.jsonl: {count} 個範例")
            else:
                info_log(f"{split}.jsonl: 不存在")

if __name__ == "__main__":
    main()
