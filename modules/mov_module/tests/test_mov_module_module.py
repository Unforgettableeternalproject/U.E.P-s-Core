from modules.mov_module_module.mov_module_module import MovModule

def test_handle():
    mod = MovModule(config={})
    result = mod.handle({})
    assert isinstance(result, dict)
