# modules/nlp_module/nlp_module.py

import torch
import os
from transformers import DistilBertForSequenceClassification, DistilBertTokenizer
from core.module_base import BaseModule
from .schemas import NLPInput, NLPOutput

class NLPModule(BaseModule):
    def __init__(self, config=None):
        self.config = config or {}
        self.model_dir = self.config.get("model_dir", "./models/command_chat_classifier")
        self.model = None
        self.tokenizer = None
        self.label_mapping = {0: "command", 1: "chat", 2: "non-sense"}

    def initialize(self):
        print(f"[NLP] 載入模型中（來自 {self.model_dir}）...")

        # Debug 1: 檢查模型目錄是否存在
        if not os.path.exists(self.model_dir):
            print(f"[NLP][ERROR] 模型資料夾不存在：{self.model_dir}")
            return

        expected_files = ["config.json", "pytorch_model.bin", "tokenizer_config.json"]
        missing = [f for f in expected_files if not os.path.exists(os.path.join(self.model_dir, f))]
        if missing:
            print(f"[NLP][ERROR] 模型資料夾內缺少檔案：{missing}")
            return

        self.model = DistilBertForSequenceClassification.from_pretrained(self.model_dir)
        self.tokenizer = DistilBertTokenizer.from_pretrained(self.model_dir)
        print("[NLP] 初始化完成")

    def handle(self, data: dict = {}) -> dict:
        validated = NLPInput(**data)
        text = validated.text

        inputs = self.tokenizer(text, return_tensors="pt", truncation=True)
        outputs = self.model(**inputs)
        prediction = torch.argmax(outputs.logits, dim=1).item()
        label = self.label_mapping.get(prediction, "unknown")

        return NLPOutput(
            text=text,
            intent=label if label in ["command", "chat"] else "ignore",
            label=label
        ).dict()

    def shutdown(self):
        print("[NLP] 模組關閉")
