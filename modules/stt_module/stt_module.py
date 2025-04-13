from core.module_base import BaseModule

class STTModule(BaseModule):
    def __init__(self, config: dict):
        self.config = config

    def initialize(self):
        print("Initializing STT Module with config:", self.config)

    def handle(self, data: dict) -> dict:
        # 假設這裡進行語音識別處理
        print("Handling STT with data:", data)
        # 返回統一格式的資料
        return {"text": "識別的文本", "confidence": 0.95}

    def shutdown(self):
        print("Shutting down STT Module")
        # 釋放資源的邏輯
        pass
