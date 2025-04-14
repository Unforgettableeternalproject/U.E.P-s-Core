# modules/nlp_module/tests/test_nlp_module.py
import pytest
from modules.nlp_module.nlp_module import NLPModule

@pytest.fixture
def nlp():
    nlp = NLPModule(config={"model_dir": "./models/command_chat_classifier"})
    nlp.initialize()
    yield nlp
    nlp.shutdown()

def test_classify_command(nlp):
    result = nlp.handle({"text": "Open the notepad for me."})
    assert result["intent"] == "command"
    assert result["label"] in ["command", "chat", "non-sense"]

def test_classify_chat(nlp):
    result = nlp.handle({"text": "How are you today ma'am?"})
    assert result["intent"] == "chat"
    assert result["label"] in ["command", "chat", "non-sense"]

def test_invalid_input(nlp):
    result = nlp.handle({"text": "....@#%#^@...."})
    assert result["intent"] in ["ignore", "chat", "command"]
