from .stt_module import STTModule

def register():
    instance = STTModule(config={})  # ����i�H�� config_loader �ǤJ�]�w
    instance.initialize()
    return instance
