#!/usr/bin/env python3
"""
BIO標註模型訓練腳本
基於transformers的微調訓練
"""

import os
import json
import pandas as pd
import torch
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass

from transformers import (
    AutoTokenizer, AutoModelForTokenClassification,
    TrainingArguments, Trainer,
    DataCollatorForTokenClassification,
    EarlyStoppingCallback
)
from sklearn.metrics import classification_report, f1_score
from sklearn.model_selection import train_test_split
import numpy as np

# 添加項目根目錄到路徑
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from modules.nlp_module.bio_tagger import BIOTagger
from train.nlp.annotation_tool import AnnotationTool
from utils.debug_helper import debug_log, info_log, error_log


@dataclass
class TrainingConfig:
    """訓練配置"""
    model_name: str = "distilbert-base-uncased"
    output_dir: str = "../../models/nlp/bio_tagger"
    training_data_path: str = "./training_data/bio_training_data.tsv"
    max_length: int = 512
    train_batch_size: int = 16
    eval_batch_size: int = 16
    num_epochs: int = 3
    learning_rate: float = 2e-5
    weight_decay: float = 0.01
    warmup_steps: int = 500
    save_strategy: str = "epoch"
    evaluation_strategy: str = "epoch"
    load_best_model_at_end: bool = True
    metric_for_best_model: str = "eval_f1"
    early_stopping_patience: int = 3


class BIODataset(torch.utils.data.Dataset):
    """BIO標註數據集"""
    
    def __init__(self, encodings: Dict[str, Any], labels: List[List[int]]):
        self.encodings = encodings
        self.labels = labels
    
    def __getitem__(self, idx):
        item = {key: torch.tensor(val[idx]) for key, val in self.encodings.items()}
        item['labels'] = torch.tensor(self.labels[idx])
        return item
    
    def __len__(self):
        return len(self.labels)


class BIOTrainer:
    """BIO標註模型訓練器"""
    
    def __init__(self, config: TrainingConfig):
        self.config = config
        self.model = None
        self.tokenizer = None
        self.bio_tagger = BIOTagger(config.model_name)
        
        # 標籤映射
        self.label2id = self.bio_tagger.label2id
        self.id2label = self.bio_tagger.id2label
        
    def load_training_data(self, data_path: str) -> Tuple[List[str], List[List[str]]]:
        """載入訓練數據"""
        texts = []
        labels = []
        
        try:
            if data_path.endswith('.jsonl'):
                # JSONL格式數據 - 我們的新格式
                with open(data_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        if line.strip():
                            data = json.loads(line)
                            
                            # 直接使用tokens和bio_labels
                            text = ' '.join(data['tokens'])
                            bio_labels = data['bio_labels']
                            
                            texts.append(text)
                            labels.append(bio_labels)
                            
            elif data_path.endswith('.tsv'):
                # BIO格式數據
                df = pd.read_csv(data_path, sep='\t')
                
                current_text = []
                current_labels = []
                
                for _, row in df.iterrows():
                    word = row['word']
                    label = row['label']
                    
                    if pd.isna(word) or word == '':
                        # 句子結束
                        if current_text:
                            texts.append(' '.join(current_text))
                            labels.append(current_labels)
                            current_text = []
                            current_labels = []
                    else:
                        current_text.append(word)
                        current_labels.append(label)
                
                # 添加最後一個句子
                if current_text:
                    texts.append(' '.join(current_text))
                    labels.append(current_labels)
                    
            elif data_path.endswith('.json'):
                # JSON格式數據
                with open(data_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                for example in data:
                    text = example['text']
                    segments = example['segments']
                    
                    # 轉換為BIO標籤
                    bio_labels = self._segments_to_bio_labels(text, segments)
                    
                    texts.append(text)
                    labels.append(bio_labels)
            
            info_log(f"[BIOTrainer] 載入了 {len(texts)} 個訓練範例")
            return texts, labels
            
        except Exception as e:
            error_log(f"[BIOTrainer] 數據載入失敗: {e}")
            return [], []
    
    def _segments_to_bio_labels(self, text: str, segments: List[Dict[str, Any]]) -> List[str]:
        """將分段轉換為BIO標籤序列"""
        words = text.split()
        bio_labels = ['O'] * len(words)
        
        for segment in segments:
            start_char = segment['start']
            end_char = segment['end']
            label = segment['label'].upper()
            
            # 找到對應的詞位置
            char_pos = 0
            for i, word in enumerate(words):
                word_start = char_pos
                word_end = char_pos + len(word)
                
                # 檢查詞是否在segment範圍內
                if word_start >= start_char and word_end <= end_char:
                    if bio_labels[i] == 'O':  # 第一個詞
                        bio_labels[i] = f'B-{label}'
                    else:  # 後續詞
                        bio_labels[i] = f'I-{label}'
                elif word_start < end_char and word_end > start_char:
                    # 詞部分重疊
                    if bio_labels[i] == 'O':
                        bio_labels[i] = f'B-{label}'
                
                char_pos = word_end + 1  # +1 for space
        
        return bio_labels
    
    def prepare_dataset(self, texts: List[str], labels: List[List[str]]) -> BIODataset:
        """準備數據集"""
        # 載入tokenizer
        self.tokenizer = AutoTokenizer.from_pretrained(self.config.model_name)
        
        # Tokenize文本 - 因為我們的數據已經是token級別的，使用is_split_into_words=True
        tokenized_texts = [text.split() for text in texts]
        
        encodings = self.tokenizer(
            tokenized_texts,
            truncation=True,
            padding=True,
            max_length=self.config.max_length,
            is_split_into_words=True,
            return_offsets_mapping=False
        )
        
        # 對齊標籤
        aligned_labels = []
        for i, label_seq in enumerate(labels):
            word_ids = encodings.word_ids(batch_index=i)
            aligned_label = self._align_labels_with_tokens(label_seq, word_ids)
            aligned_labels.append(aligned_label)
        
        return BIODataset(encodings, aligned_labels)
    
    def _align_labels_with_tokens(self, labels: List[str], word_ids: List[Optional[int]]) -> List[int]:
        """對齊標籤與tokens"""
        aligned_labels = []
        previous_word_idx = None
        
        for word_idx in word_ids:
            if word_idx is None:
                # 特殊token
                aligned_labels.append(-100)
            elif word_idx != previous_word_idx:
                # 新詞的第一個subtoken
                if word_idx < len(labels):
                    aligned_labels.append(self.label2id[labels[word_idx]])
                else:
                    aligned_labels.append(self.label2id['O'])
            else:
                # 同一詞的後續subtoken
                aligned_labels.append(-100)
            
            previous_word_idx = word_idx
        
        return aligned_labels
    
    def train(self):
        """訓練模型"""
        try:
            info_log("[BIOTrainer] 開始訓練BIO標註模型...")
            
            # 載入訓練數據
            texts, labels = self.load_training_data(self.config.training_data_path)
            if not texts:
                error_log("[BIOTrainer] 無法載入訓練數據")
                return False
            
            # 切分訓練/驗證集
            train_texts, val_texts, train_labels, val_labels = train_test_split(
                texts, labels, test_size=0.2, random_state=42
            )
            
            # 準備數據集
            train_dataset = self.prepare_dataset(train_texts, train_labels)
            val_dataset = self.prepare_dataset(val_texts, val_labels)
            
            # 載入模型
            self.model = AutoModelForTokenClassification.from_pretrained(
                self.config.model_name,
                num_labels=len(self.label2id),
                id2label=self.id2label,
                label2id=self.label2id
            )
            
            # 設定訓練參數
            training_args = TrainingArguments(
                output_dir=self.config.output_dir,
                num_train_epochs=self.config.num_epochs,
                per_device_train_batch_size=self.config.train_batch_size,
                per_device_eval_batch_size=self.config.eval_batch_size,
                warmup_steps=self.config.warmup_steps,
                weight_decay=self.config.weight_decay,
                learning_rate=self.config.learning_rate,
                logging_dir=f"{self.config.output_dir}/logs",
                evaluation_strategy=self.config.evaluation_strategy,
                save_strategy=self.config.save_strategy,
                load_best_model_at_end=self.config.load_best_model_at_end,
                metric_for_best_model=self.config.metric_for_best_model,
                greater_is_better=True,
                report_to=None  # 不使用wandb等
            )
            
            # 數據collator
            data_collator = DataCollatorForTokenClassification(self.tokenizer)
            
            # 訓練器
            trainer = Trainer(
                model=self.model,
                args=training_args,
                train_dataset=train_dataset,
                eval_dataset=val_dataset,
                tokenizer=self.tokenizer,
                data_collator=data_collator,
                compute_metrics=self._compute_metrics,
                callbacks=[EarlyStoppingCallback(early_stopping_patience=self.config.early_stopping_patience)]
            )
            
            # 開始訓練
            trainer.train()
            
            # 保存模型
            trainer.save_model()
            self.tokenizer.save_pretrained(self.config.output_dir)
            
            info_log(f"[BIOTrainer] 模型已保存至: {self.config.output_dir}")
            
            # 評估
            eval_results = trainer.evaluate()
            info_log(f"[BIOTrainer] 評估結果: {eval_results}")
            
            return True
            
        except Exception as e:
            error_log(f"[BIOTrainer] 訓練失敗: {e}")
            return False
    
    def _compute_metrics(self, eval_pred):
        """計算評估指標"""
        predictions, labels = eval_pred
        predictions = np.argmax(predictions, axis=2)
        
        # 移除特殊tokens的標籤
        true_predictions = [
            [self.id2label[p] for (p, l) in zip(prediction, label) if l != -100]
            for prediction, label in zip(predictions, labels)
        ]
        true_labels = [
            [self.id2label[l] for (p, l) in zip(prediction, label) if l != -100]
            for prediction, label in zip(predictions, labels)
        ]
        
        # 計算F1分數
        all_true_labels = [label for sublist in true_labels for label in sublist]
        all_pred_labels = [label for sublist in true_predictions for label in sublist]
        
        f1 = f1_score(all_true_labels, all_pred_labels, average='weighted')
        
        return {
            'f1': f1
        }


def main():
    """主函數"""
    # 檢查是否有訓練數據
    training_data_path = "./data/annotated/train.jsonl"
    if not Path(training_data_path).exists():
        info_log("[Main] 沒有找到訓練數據，請先運行資料生成器或標註工具")
        return
    
    # 配置訓練參數
    config = TrainingConfig(
        model_name="distilbert-base-uncased",
        output_dir="../../models/nlp/bio_tagger",
        training_data_path=training_data_path,
        num_epochs=3,
        train_batch_size=16,
        eval_batch_size=16,
        learning_rate=2e-5
    )
    
    # 創建輸出目錄
    Path(config.output_dir).mkdir(parents=True, exist_ok=True)
    
    # 創建訓練器
    trainer = BIOTrainer(config)
    
    # 開始訓練
    success = trainer.train()
    
    if success:
        info_log("[Main] BIO標註模型訓練完成！")
        
        # 測試模型
        bio_tagger = BIOTagger()
        if bio_tagger.load_model(config.output_dir):
            test_text = "Hello, are you there? I was thinking about the weather today. Can you help me with my schedule?"
            segments = bio_tagger.predict(test_text)
            info_log(f"[Main] 測試結果: {segments}")
    else:
        error_log("[Main] 訓練失敗")


if __name__ == "__main__":
    main()
