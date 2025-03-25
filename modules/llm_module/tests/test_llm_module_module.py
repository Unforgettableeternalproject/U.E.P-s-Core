from modules.llm_module_module.llm_module_module import LlmModule

def test_handle():
    mod = LlmModule(config={})
    result = mod.handle({})
    assert isinstance(result, dict)
