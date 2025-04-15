import pytest
from modules.mem_module import MEMModule


@pytest.fixture
def mem():
    mem = MEMModule(config={"embedding_model": "all-MiniLM-L6-v2"})
    mem.initialize()
    yield mem
    mem.shutdown()