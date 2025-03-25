from modules.tts_module_module.tts_module_module import TtsModule

def test_handle():
    mod = TtsModule(config={})
    result = mod.handle({})
    assert isinstance(result, dict)
