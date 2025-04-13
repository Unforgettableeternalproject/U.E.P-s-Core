from ..stt_module import STTModule

def test_handle():
    mod = STTModule()
    mod.initialize()
    result = mod.handle()
    assert "text" in result
    assert isinstance(result["text"], str)

# 使用 pytest 進行測試: pytest test_stt_module.py
