#!/usr/bin/env python3
"""
BIO標註模型訓練腳本
基於transformers的微調訓練

不依賴已刪除的 legacy 模組（modules／utils）；標籤表與 models/nlp/bio_tagger/config.json 的 id2label 一致。
用法（repo 根目錄）：
    python train/nlp/train_bio_model.py --help
    python train/nlp/train_bio_model.py --dry-run     # 只檢查資料與標籤，不載入模型、不訓練
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger("train.nlp.train_bio_model")

# BIO 標籤（順序即 id；與 models/nlp/bio_tagger/config.json 的 id2label 一致）
BIO_LABELS = [
    "O",
    "B-CALL", "I-CALL",
    "B-CHAT", "I-CHAT",
    "B-DIRECT_WORK", "I-DIRECT_WORK",
    "B-BACKGROUND_WORK", "I-BACKGROUND_WORK",
    "B-UNKNOWN", "I-UNKNOWN",
]
LABEL2ID = {label: i for i, label in enumerate(BIO_LABELS)}
ID2LABEL = {i: label for i, label in enumerate(BIO_LABELS)}
DEFAULT_DATA = Path(__file__).parent / "nlp_training_data.jsonl"
DEFAULT_OUTPUT = Path(__file__).parent.parent.parent / "models" / "nlp" / "bio_tagger"


def info_log(msg: str) -> None:
    logger.info(msg)


def error_log(msg: str) -> None:
    logger.error(msg)


@dataclass
class TrainingConfig:
    """訓練配置"""
    model_name: str = "distilbert-base-uncased"
    output_dir: str = str(DEFAULT_OUTPUT)
    training_data_path: str = str(DEFAULT_DATA)
    max_length: int = 512
    train_batch_size: int = 16
    eval_batch_size: int = 16
    num_epochs: int = 3
    learning_rate: float = 2e-5
    weight_decay: float = 0.01
    warmup_steps: int = 500
    save_strategy: str = "epoch"
    eval_strategy: str = "epoch"
    load_best_model_at_end: bool = True
    metric_for_best_model: str = "eval_f1"
    early_stopping_patience: int = 3


class BIODataset:
    """BIO標註數據集（torch Dataset 介面；torch 在取樣時才 import，讓 --help／--dry-run 不需要它）"""

    def __init__(self, encodings: Dict[str, Any], labels: List[List[int]]):
        self.encodings = encodings
        self.labels = labels

    def __getitem__(self, idx):
        import torch
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

        # 標籤映射
        self.label2id = LABEL2ID
        self.id2label = ID2LABEL

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
                import pandas as pd
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
        from transformers import AutoTokenizer

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
            from sklearn.model_selection import train_test_split
            from transformers import (
                AutoModelForTokenClassification, DataCollatorForTokenClassification,
                EarlyStoppingCallback, Trainer, TrainingArguments,
            )

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

            # 設定訓練參數（transformers 4.48：evaluation_strategy 已更名為 eval_strategy）
            training_args = TrainingArguments(
                output_dir=self.config.output_dir,
                num_train_epochs=self.config.num_epochs,
                per_device_train_batch_size=self.config.train_batch_size,
                per_device_eval_batch_size=self.config.eval_batch_size,
                warmup_steps=self.config.warmup_steps,
                weight_decay=self.config.weight_decay,
                learning_rate=self.config.learning_rate,
                logging_dir=f"{self.config.output_dir}/logs",
                eval_strategy=self.config.eval_strategy,
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
                processing_class=self.tokenizer,
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
        import numpy as np
        from sklearn.metrics import f1_score

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


def dry_run(config: TrainingConfig) -> int:
    """只檢查資料檔與標籤是否合法，不載入模型、不訓練、不寫檔。"""
    path = Path(config.training_data_path)
    if not path.exists():
        error_log(f"[dry-run] 找不到訓練資料：{path}")
        return 1
    if path.suffix != ".jsonl":
        error_log(f"[dry-run] 目前只檢查 .jsonl（bio_labels／tokens 欄位）：{path}")
        return 1
    total, bad, label_counts = 0, 0, {}
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            if not line.strip():
                continue
            total += 1
            row = json.loads(line)
            labels = row.get('bio_labels', [])
            if len(labels) != len(row.get('tokens', [])) or any(lab not in LABEL2ID for lab in labels):
                bad += 1
                continue
            for lab in labels:
                label_counts[lab] = label_counts.get(lab, 0) + 1
    info_log(f"[dry-run] 資料 {path}：{total} 筆，無效 {bad} 筆；標籤分佈 {label_counts}")
    info_log(f"[dry-run] 模型 {config.model_name}，輸出 {config.output_dir}（未訓練、未寫檔）")
    return 0 if bad == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="訓練 BIO 標註模型（DistilBERT token classification）")
    parser.add_argument("--data", default=str(DEFAULT_DATA), help="訓練資料（.jsonl／.tsv／.json）")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT),
                        help="輸出目錄（預設是 models/nlp/bio_tagger，會被覆寫，請先備份）")
    parser.add_argument("--model-name", default="distilbert-base-uncased")
    parser.add_argument("--epochs", type=int, default=5)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--learning-rate", type=float, default=2e-5)
    parser.add_argument("--dry-run", action="store_true", help="只檢查資料與標籤，不載入模型、不訓練")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """主函數"""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config = TrainingConfig(
        model_name=args.model_name,
        output_dir=args.output_dir,
        training_data_path=args.data,
        num_epochs=args.epochs,
        train_batch_size=args.batch_size,
        eval_batch_size=args.batch_size,
        learning_rate=args.learning_rate,
    )
    if args.dry_run:
        return dry_run(config)
    if not Path(config.training_data_path).exists():
        error_log(f"[Main] 沒有找到訓練數據：{config.training_data_path}")
        return 1
    info_log(f"[Main] 使用訓練數據: {config.training_data_path}")

    # 創建輸出目錄
    Path(config.output_dir).mkdir(parents=True, exist_ok=True)
    if BIOTrainer(config).train():
        info_log("[Main] BIO標註模型訓練完成！")
        return 0
    error_log("[Main] 訓練失敗")
    return 1


if __name__ == "__main__":
    sys.exit(main())
