from modules.nlp_module_module.nlp_module_module import NlpModule

def test_handle():
    mod = NlpModule(config={})
    result = mod.handle({})
    assert isinstance(result, dict)
