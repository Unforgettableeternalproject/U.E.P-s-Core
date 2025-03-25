from modules.mem_module_module.mem_module_module import MemModule

def test_handle():
    mod = MemModule(config={})
    result = mod.handle({})
    assert isinstance(result, dict)
