from modules.sys_module_module.sys_module_module import SysModule

def test_handle():
    mod = SysModule(config={})
    result = mod.handle({})
    assert isinstance(result, dict)
