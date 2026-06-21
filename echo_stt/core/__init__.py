"""核心辨識引擎 — Whisper pipeline 包裝與統一介面"""

from echo_stt.core import config, schemas


def __getattr__(name: str):
    if name == "STTEngine":
        from echo_stt.core.engine import STTEngine
        return STTEngine
    raise AttributeError(f"module 'echo_stt.core' has no attribute {name!r}")


__all__ = ["STTEngine", "config", "schemas"]
