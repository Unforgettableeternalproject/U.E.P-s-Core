from modules.llm_module.llm_module import LLMModule

def test_handle():
    mod = LLMModule(config={})
    result = mod.handle({})
    assert isinstance(result, dict)
