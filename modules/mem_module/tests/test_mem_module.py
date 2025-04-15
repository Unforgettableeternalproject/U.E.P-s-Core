import pytest
from modules.mem_module.mem_module import MEMModule

@pytest.fixture(scope="module")
def mem():
    config = {
        "embedding_model": "all-MiniLM-L6-v2",
        "index_file": "memory/mem_test_index",
        "metadata_file": "memory/mem_test_metadata.json",
        "max_distance": 1,
    }
    module = MEMModule(config=config)
    module.initialize()
    yield module

    # 測試結束後清理檔案
    import os
    if os.path.exists(config["index_file"]):
        os.remove(config["index_file"])
    if os.path.exists(config["metadata_file"]):
        os.remove(config["metadata_file"])

@pytest.mark.order(1)
def test_store_and_fetch_simple(mem):
    entry = {
        "user": "What's the capital of France?",
        "response": "The capital of France is Paris."
    }
    store_result = mem.handle({"mode": "store", "entry": entry})
    assert store_result.get("status") == "stored"

    result = mem.handle({"mode": "fetch", "text": "France capital", "top_k": 1})
    assert isinstance(result, dict)
    assert "results" in result
    assert any("Paris" in r["response"] for r in result["results"])

@pytest.mark.order(2)
def test_multi_conversation(mem):
    conversations = [
        {"user": "What are we doing today?", "response": "We're working on the MEM module."},
        {"user": "What comes after MEM?", "response": "We'll handle the LLM integration next."},
        {"user": "Did we finish the STT part?", "response": "Yes, it's already tested."}
    ]
    for entry in conversations:
        mem.handle({"mode": "store", "entry": entry})

    result = mem.handle({"mode": "fetch", "text": "after MEM"})
    assert any("LLM" in r["response"] for r in result["results"])


@pytest.mark.order(3)
def test_empty_result(mem):
    result = mem.handle({"mode": "fetch", "text": "Is Bernie a cat?"})
    assert isinstance(result, dict)
    assert result.get("results") == [] or len(result["results"]) == 0
    assert result.get("status") == "empty"

@pytest.mark.order(4)
def test_memory_count(mem):
    # Check length for debug purposes only
    count = len(mem.metadata)
    print(f"[TEST] Memory total entries: {count}")
    assert count >= 4  # Should include test cases above
