import os
import pandas as pd
from datasets import Dataset
from transformers import (
    DistilBertForSequenceClassification,
    DistilBertTokenizer,
    Trainer,
    TrainingArguments,
    DataCollatorWithPadding
)
from sklearn.model_selection import train_test_split

os.environ['CUDA_LAUNCH_BLOCKING'] = '1'

class DBertTrainer:
    def __init__(self, dataset_path, model_name="distilbert-base-uncased", output_dir="./results"):
        self.dataset_path = dataset_path
        self.model_name = model_name
        self.output_dir = output_dir
        self.final_model_path = "./models/command_chat_classifier"

    def load_and_preprocess_data(self):
        df = pd.read_csv(self.dataset_path)
        label_mapping = {"command": 0, "chat": 1, "non-sense": 2}
        df["label"] = df["label"].map(label_mapping)

        train_texts, test_texts, train_labels, test_labels = train_test_split(
            df["text"].tolist(), df["label"].tolist(), test_size=0.2
        )
        train_data = Dataset.from_dict({"text": train_texts, "label": train_labels})
        test_data = Dataset.from_dict({"text": test_texts, "label": test_labels})
        return train_data, test_data

    def tokenize_data(self, data, tokenizer):
        def preprocess_function(examples):
            return tokenizer(examples["text"], truncation=True)
        return data.map(preprocess_function, batched=True)

    def load_model(self):
        if os.path.exists(self.final_model_path) and os.path.isfile(os.path.join(self.final_model_path, 'config.json')):
            model = DistilBertForSequenceClassification.from_pretrained(self.final_model_path)
            tokenizer = DistilBertTokenizer.from_pretrained(self.final_model_path)
        else:
            tokenizer = DistilBertTokenizer.from_pretrained(self.model_name)
            model = DistilBertForSequenceClassification.from_pretrained(self.model_name, num_labels=3)
        return model, tokenizer

    def train(self):
        print("[Trainer] 讀取資料集中...")
        train_data, test_data = self.load_and_preprocess_data()

        print("[Trainer] 載入模型...")
        model, tokenizer = self.load_model()

        train_data = self.tokenize_data(train_data, tokenizer)
        test_data = self.tokenize_data(test_data, tokenizer)

        training_args = TrainingArguments(
            output_dir=self.output_dir,
            evaluation_strategy="epoch",
            learning_rate=2e-5,
            per_device_train_batch_size=8,
            per_device_eval_batch_size=8,
            num_train_epochs=250,
            weight_decay=0.01,
            save_strategy="steps",
            save_steps=500,
            logging_dir=f"{self.output_dir}/logs"
        )

        trainer = Trainer(
            model=model,
            args=training_args,
            train_dataset=train_data,
            eval_dataset=test_data,
            tokenizer=tokenizer,
            data_collator=DataCollatorWithPadding(tokenizer=tokenizer),
        )

        print("[Trainer] 開始訓練...")
        trainer.train()

        print(f"[Trainer] 儲存模型到：{self.final_model_path}")
        model.save_pretrained(self.final_model_path)
        tokenizer.save_pretrained(self.final_model_path)

# 執行訓練
if __name__ == "__main__":
    trainer = DBertTrainer(dataset_path="train/nlp/dataset.csv")
    trainer.train()