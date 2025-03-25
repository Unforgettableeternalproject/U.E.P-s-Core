from modules.ani_module_module.ani_module_module import AniModule

def test_handle():
    mod = AniModule(config={})
    result = mod.handle({})
    assert isinstance(result, dict)
