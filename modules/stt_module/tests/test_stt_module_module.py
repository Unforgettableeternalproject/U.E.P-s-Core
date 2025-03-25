from modules.stt_module_module.stt_module_module import SttModule

def test_handle():
    mod = SttModule(config={})
    result = mod.handle({})
    assert isinstance(result, dict)
