"""
echo_stt — 獨立 STT 模組

從 U.E.P-s-Core 遷出並解耦的語音識別系統。

重型依賴（torch, faster-whisper）使用 lazy import，
讓輕量使用者（如 Discord Bot）只 import channel 模組時不需要 GPU 依賴。
"""

__version__ = "0.1.0"


def __getattr__(name: str):
    """Lazy import — 只在存取 STTEngine 時才載入 torch 等重型依賴。"""
    if name == "STTEngine":
        from echo_stt.core.engine import STTEngine
        return STTEngine
    raise AttributeError(f"module 'echo_stt' has no attribute {name!r}")


__all__ = ["STTEngine", "__version__"]
