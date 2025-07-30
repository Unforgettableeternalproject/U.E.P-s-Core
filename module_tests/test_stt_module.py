import pytest
from modules.stt_module.stt_module import STTModule

@pytest.fixture
def stt():
    stt = STTModule(config={"device_index": 1})
    stt.initialize()
    yield stt
    stt.shutdown()

def test_stt_handle_returns_text(stt):
    # 使用新版 STT API - 手動模式
    result = stt.handle({
        "mode": "manual",
        "language": "en-US",
        "enable_speaker_id": False,
        "duration": 3
    })
    assert isinstance(result, dict)
    assert "text" in result
    assert isinstance(result["text"], str)
    assert "error" in result or result["text"] != ""
