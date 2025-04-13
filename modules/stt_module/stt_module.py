from core.module_base import BaseModule

class STTModule(BaseModule):
    def __init__(self, config: dict):
        self.config = config

    def initialize(self):
        print("Initializing STT Module with config:", self.config)

    def handle(self, data: dict) -> dict:
        # ���]�o�̶i��y���ѧO�B�z
        print("Handling STT with data:", data)
        # ��^�Τ@�榡�����
        return {"text": "�ѧO���奻", "confidence": 0.95}

    def shutdown(self):
        print("Shutting down STT Module")
        # ����귽���޿�
        pass
