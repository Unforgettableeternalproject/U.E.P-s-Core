import pytest
import asyncio
import sys
from modules.tts_module.tts_module import TTSModule

# IDK may create some temp files for tts test

@pytest.fixture(scope="module")
def tts():
    # 暫時清空 sys.argv
    original_argv = sys.argv
    sys.argv = [original_argv[0]]  # 保留腳本名稱
    try:
        module = TTSModule()
        module.initialize()
        yield module
        module.shutdown()
    finally:
        # 恢復 sys.argv
        sys.argv = original_argv

@pytest.mark.asyncio
async def test_tts_handle_single(tts):
    input_data = {
        "text": "Hello, this is a test for TTS module.",
        "mood": "neutral",
        "save": False
    }

    result = await tts.handle(input_data)
    print("🗣️ TTS 回應：", result)

    assert "status" in result
    assert result["status"] == "success"
    assert "message" in result
    assert isinstance(result["message"], str)

@pytest.mark.asyncio
async def test_tts_handle_streaming(tts):
    input_data = {
        "text": "Area 3 is a totalitarian state called Crambell, divided into four quadrants named after the Four Horsemen of the Apocalypse. Each quadrant serves a specific purpose: Famein for the elderly, weak, women, and children; Pestilens as an arms control Zone and rest for the army; Wyar as the industrial and important district with connections to Area 4; and finally, Delth where most citizens live and work, having mining operations and dorms for miners.",
        "mood": "happy",
        "save": False
    }

    result = await tts.handle(input_data)
    print("🗣️ TTS 串流回應：", result)

    assert "status" in result
    assert result["status"] == "success"
    assert "chunk_count" in result
    assert result["chunk_count"] > 1
    assert "output_path" in result
    assert isinstance(result["output_path"], str)

@pytest.mark.asyncio
async def test_tts_invalid_input(tts):
    input_data = {
        "text": "",
        "mood": "neutral",
        "save": False
    }

    result = await tts.handle(input_data)
    print("❌ TTS 錯誤回應：", result)

    assert "status" in result
    assert result["status"] == "error"
    assert "message" in result
    assert "Text is required" in result["message"]
