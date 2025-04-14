# modules/stt_module/tests/test_stt_module.py
import pytest
from modules.stt_module.stt_module import STTModule

@pytest.fixture
def stt():
    stt = STTModule(config={"device_index": 1})
    stt.initialize()
    yield stt
    stt.shutdown()

def test_stt_handle_returns_text(stt):
    result = stt.handle()
    assert isinstance(result, dict)
    assert "text" in result
    assert isinstance(result["text"], str)
    assert "error" in result or result["text"] != ""
