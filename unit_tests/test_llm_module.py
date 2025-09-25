import pytest
from modules.llm_module.llm_module import LLMModule

# 重要: 這個測試目前被棄用，還沒有適應新版構造，請不要使用

@pytest.fixture(scope="module")
def llm():
    module = LLMModule()
    module.initialize()
    yield module
    module.shutdown()

def test_llm_chat_response(llm):
    input_data = {
        "text": "Do you know what day is it?",
        "intent": "chat",
        "memory": "You've mentioned today is your birthday."
    }

    result = llm.handle(input_data)
    print("🧠 Gemini 回覆：", result)

    assert "text" in result
    assert isinstance(result["text"], str)
    assert "mood" in result
    assert result.get("status") != "error"
