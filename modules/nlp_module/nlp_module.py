# modules/nlp_module/nlp_module.py

from doctest import debug
import torch
import os
from transformers import DistilBertForSequenceClassification, DistilBertTokenizer
from core.module_base import BaseModule
from utils.debug_helper import debug_log, debug_log_e, info_log, error_log
from .schemas import NLPInput, NLPOutput

class NLPModule(BaseModule):
    def __init__(self, config=None):
        self.config = config or {}
        self.model_dir = self.config.get("model_dir", "./models/command_chat_classifier")
        self.model = None
        self.tokenizer = None
        self.label_mapping = {0: "command", 1: "chat", 2: "non-sense"}

    def debug(self):
        # Debug level = 1
        debug_log(1, "[NLP] Debug 模式啟用")
        # Debug level = 2
        debug_log(2, f"[NLP] 模組設定: {self.config}")
        # Debug level = 3
        debug_log(3, f"[NLP] 模型資料夾: {self.model_dir}")
        debug_log(3, f"[NLP] 標籤對應: {self.label_mapping}")

    def initialize(self):
        debug_log(1, "[NLP] 初始化中...")
        self.debug()

        info_log(f"[NLP] 載入模型中（來自 {self.model_dir}）...")

        # Debug 1: 檢查模型目錄是否存在
        if not os.path.exists(self.model_dir):
            error_log(f"[NLP] 模型資料夾不存在：{self.model_dir}")
            return

        expected_files = ["config.json", "pytorch_model.bin", "tokenizer_config.json"]
        missing = [f for f in expected_files if not os.path.exists(os.path.join(self.model_dir, f))]

        if missing:
            error_log(f"[NLP] 模型資料夾內缺少檔案：{missing}")
            return

        self.model = DistilBertForSequenceClassification.from_pretrained(self.model_dir)
        self.tokenizer = DistilBertTokenizer.from_pretrained(self.model_dir)
        info_log("[NLP] 初始化完成")

    def handle(self, data: dict = {}) -> dict:
        validated = NLPInput(**data)
        debug_log(1, f"[NLP] 接收到的資料: {validated}")
        if not validated.text:
            error_log("[NLP] 輸入文本為空")
            return {"text": "", "intent": "ignore", "label": "unknown"}

        text = validated.text
        inputs = self.tokenizer(text, return_tensors="pt", truncation=True)
        outputs = self.model(**inputs)
        prediction = torch.argmax(outputs.logits, dim=1).item()
        label = self.label_mapping.get(prediction, "unknown")

        result = NLPOutput(
            text=text,
            intent=label if label in ["command", "chat"] else "ignore",
            label=label
        ).dict()

        debug_log_e(1, f"[NLP] 預測結果: {result['text']} 對應 {result['intent']}")
        debug_log_e(2, f"[NLP] 完整預測結果: {result}")
        return result

    def shutdown(self):
        info_log("[NLP] 模組關閉")
