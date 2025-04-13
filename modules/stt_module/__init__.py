from .stt_module import STTModule

def register():
    instance = STTModule(config={})  # 之後可以用 config_loader 傳入設定
    instance.initialize()
    return instance
