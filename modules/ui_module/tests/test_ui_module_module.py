from modules.ui_module_module.ui_module_module import UiModule

def test_handle():
    mod = UiModule(config={})
    result = mod.handle({})
    assert isinstance(result, dict)
