"""N5：Core 記憶路徑切 bge-m3（sentence-transformers 本機直載）與 memory_echo／Access Layer 去重。

- 設定預設、舊資料目錄的索引守門（model／維度不符拒絕建 engine，不重建、不改寫舊資料）。
- 去重後的單一來源（member_recall schema、字數上限、簽名檢查、工具宣告）。
- A5 記憶驗收的 fake／CPU 自動化部分：新資料目錄下 寫入 -> recall、重啟後仍可 recall；
  真 bge-m3 的版本只在本機 HF 快取已有權重時才跑（不下載）。
- ``/mem embed`` 指令層（fake embedder，不載模型）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from tests.uep.test_p3_s2_memory_host import PRIVATE, SPECS, make_identity
from uep.console import mem_cmd
from uep.echo.memory_wiring import (
    EchoMemoryToolBackend,
    EmbeddingIndexMismatch,
    build_memory_wiring,
    check_embedding_index,
)
from uep.fakes.memory import FakeGuildMembership
from uep.memory import MemoryHost

REPO = Path(__file__).resolve().parents[2]
BGE_M3 = "BAAI/bge-m3"
BGE_SMALL = "BAAI/bge-small-zh-v1.5"


def _write_index(directory: Path, model: str, dimension: int, *, manifest: bool = True) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    np.save(directory / "embeddings.npy", np.zeros((1, dimension), dtype=np.float32))
    if manifest:
        (directory / "embedding_index.json").write_text(
            json.dumps({"version": 1, "model": model, "dimension": dimension, "ordered_ids": ["e1"]}), encoding="utf-8")


# ---- 設定與索引守門 ----

def test_core_預設_embedding_為_bge_m3_本機直載(monkeypatch):
    from uep.config import UepSettings

    for key in ("UEP_MEMORY_EMBEDDING_BACKEND", "UEP_MEMORY_EMBEDDING_MODEL", "UEP_MEMORY_MIN_SIMILARITY"):
        monkeypatch.delenv(key, raising=False)
    settings = UepSettings(_env_file=None)
    assert (settings.memory_embedding_backend, settings.memory_embedding_model) == ("sentence-transformers", BGE_M3)
    assert settings.memory_min_similarity is None  # 未設＝沿用 EM 預設


def test_空目錄與相符索引放行(tmp_path):
    check_embedding_index(tmp_path / "none", BGE_M3)
    _write_index(tmp_path / "ok", BGE_M3, 1024)
    check_embedding_index(tmp_path / "ok", BGE_M3)


def test_舊_bge_small_manifest_拒絕(tmp_path):
    _write_index(tmp_path / "old", BGE_SMALL, 512)
    before = sorted(p.name for p in (tmp_path / "old").iterdir())
    with pytest.raises(EmbeddingIndexMismatch, match="全新"):
        check_embedding_index(tmp_path / "old", BGE_M3)
    assert sorted(p.name for p in (tmp_path / "old").iterdir()) == before  # 守門只讀，不改寫


def test_沒有_manifest_的舊快照依維度拒絕(tmp_path):
    _write_index(tmp_path / "legacy", BGE_SMALL, 512, manifest=False)
    with pytest.raises(EmbeddingIndexMismatch):
        check_embedding_index(tmp_path / "legacy", BGE_M3)
    check_embedding_index(tmp_path / "legacy", BGE_SMALL)  # 與自己的模型一致仍可用


def test_factory_遇舊目錄拒絕建_engine_且不動舊檔(tmp_path):
    pytest.importorskip("echo_memory")
    from echo_memory.namespace import resolve_namespace_path

    old = resolve_namespace_path(tmp_path / "mem", "local-default")
    _write_index(old, BGE_SMALL, 512)
    snapshot = {p.name: p.read_bytes() for p in old.iterdir()}
    wiring = build_memory_wiring(embedding_backend="sentence-transformers", embedding_model=BGE_M3)
    with pytest.raises(EmbeddingIndexMismatch):
        wiring.engine_factory("local-default", tmp_path / "mem")
    assert {p.name: p.read_bytes() for p in old.iterdir()} == snapshot


def test_min_similarity_設定會套到_EM_全域設定(tmp_path):
    pytest.importorskip("echo_memory")
    from echo_memory.config import get_config

    cfg = get_config()
    previous = cfg.retrieval_min_similarity
    try:
        wiring = build_memory_wiring(embedding_model=BGE_M3, min_similarity=0.5)
        engine = wiring.engine_factory("local-default", tmp_path / "mem")
        assert cfg.retrieval_min_similarity == 0.5
        close = getattr(engine, "close", None)
        if callable(close):
            close()
    finally:
        cfg.retrieval_min_similarity = previous


# ---- 去重 ----

def test_member_recall_schema_與常數只有一份():
    from uep.memory import access
    from uep.stream.adapters import memory_echo
    from uep.stream.contracts import memory_tools

    assert access.MEMBER_RECALL_SCHEMA is memory_tools.MEMBER_RECALL_SCHEMA
    assert memory_echo.MEMBER_RECALL_SCHEMA is memory_tools.MEMBER_RECALL_SCHEMA
    assert access.MEMBER_RECALL == memory_echo.MEMBER_RECALL == "member_recall"
    assert memory_echo.MEMBER_NAME_MAX_CHARS == access._MEMBER_LABEL_MAX
    assert memory_echo.DEFAULT_TOOL_MAX_CHARS == memory_tools.DEFAULT_TOOL_MAX_CHARS
    assert access._declares_param is memory_tools.declares_param is memory_echo._declares_param


def test_工具宣告_backend_與_adapter_共用同一個轉換():
    pytest.importorskip("llm_integration")
    from uep.stream.adapters.memory_echo import FORCE_REMEMBER_DESCRIPTION, EchoMemoryAdapter

    backend_defs = {d["function"]["name"]: d for d in EchoMemoryToolBackend().definitions()}
    assert set(backend_defs) == {"deep_recall", "force_remember"}
    assert backend_defs["force_remember"]["function"]["description"] == FORCE_REMEMBER_DESCRIPTION
    adapter = EchoMemoryAdapter(object())
    adapter.enabled_tools = ["deep_recall", "force_remember"]
    assert {d["function"]["name"]: d for d in adapter.tool_definitions()} == backend_defs


def test_adapters_config_已移除_F1():
    assert not (REPO / "uep" / "stream" / "adapters" / "config.py").exists()
    for path in (REPO / "uep").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "ensure_subsystem(" not in text, path
        assert "from . import config" not in text and "adapters.config" not in text, path


def test_adapter_仍讀環境變數覆寫(monkeypatch):
    from uep.stream.adapters.memory_echo import EchoMemoryAdapter

    monkeypatch.setenv("ECHO_STREAM_MEMORY_TOP_K", "7")
    monkeypatch.setenv("ECHO_STREAM_MEMORY_MIN_MATCH", "0.6")
    adapter = EchoMemoryAdapter(object())
    assert adapter.top_k == 7 and adapter.min_match == 0.6


# ---- A5：寫入 -> recall（新資料目錄）----

def _hash_embed(text: str, dim: int = 1024) -> list[float]:
    """確定性的字元 bigram 雜湊向量：相同字面越多越相近，不需模型。"""
    vec = np.zeros(dim, dtype=np.float32)
    chars = [c for c in text if not c.isspace()]
    for a, b in zip(chars, chars[1:] or chars):
        vec[int(hashlib.md5((a + b).encode("utf-8")).hexdigest(), 16) % dim] += 1.0
    if not vec.any():
        vec[0] = 1.0
    return (vec / np.linalg.norm(vec)).tolist()


def _make_host(root: Path) -> MemoryHost:
    wiring = build_memory_wiring(
        assess_mode="off", embedding_backend="sentence-transformers", embedding_model=BGE_M3, min_similarity=0.2,
    )
    return MemoryHost(
        data_root=root, namespaces=SPECS, identity=make_identity(),
        guild_membership=FakeGuildMembership({"123": {"actor_a": "111"}}), drain_timeout_s=5.0, **wiring.host_kwargs(),
    )


@pytest.fixture
def hashed_embedding(monkeypatch):
    pytest.importorskip("echo_memory")
    pytest.importorskip("llm_integration")
    from echo_memory.config import get_config
    from echo_memory.storage.episodic import EpisodicMemory

    previous = get_config().retrieval_min_similarity
    monkeypatch.setattr(EpisodicMemory, "_generate_embedding_st", lambda self, text: _hash_embed(text))
    yield
    get_config().retrieval_min_similarity = previous


def test_A5_新資料目錄_寫入後_recall_且重啟仍在(tmp_path, hashed_embedding):
    root = tmp_path / "memory-bgem3"
    host = _make_host(root)
    host.start()
    try:
        stored = host.access.store(actor_id="actor_a", user_text="我最喜歡吃豚骨拉麵", spoken_text="記住了，你喜歡豚骨拉麵",
                                   session_id="s1", idempotency_key="a5-1")
        assert stored.status == "stored"
        hit = host.access.retrieve(PRIVATE, "豚骨拉麵", PRIVATE, actor_id="actor_a", session_id="s2")
        assert hit.memory_status.value == "ok" and "拉麵" in hit.text
    finally:
        host.stop()
    manifests = list(root.rglob("embedding_index.json"))
    assert manifests, "寫入後應有 embedding_index.json"
    meta = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert meta["model"] == BGE_M3 and meta["dimension"] == 1024

    host2 = _make_host(root)  # 同一個新目錄重啟：索引相符、資料仍在
    host2.start()
    try:
        again = host2.access.retrieve(PRIVATE, "豚骨拉麵", PRIVATE, actor_id="actor_a", session_id="s3")
        assert "拉麵" in again.text
    finally:
        host2.stop()


def test_A5_舊目錄不會被誤用(tmp_path, hashed_embedding):
    from echo_memory.namespace import resolve_namespace_path

    root = tmp_path / "memory-old"
    _write_index(resolve_namespace_path(root, SPECS[0].namespace_id), BGE_SMALL, 512)
    host = _make_host(root)
    with pytest.raises(Exception, match="全新|不符"):
        host.start()
    assert not host.started


def _bge_m3_cached() -> bool:
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:
        return False
    return isinstance(try_to_load_from_cache(BGE_M3, "modules.json"), str)


@pytest.mark.skipif(not _bge_m3_cached(), reason="本機 HF 快取沒有 BAAI/bge-m3（不在測試中下載）")
def test_A5_真_bge_m3_CPU_寫入後_recall(tmp_path):
    pytest.importorskip("echo_memory")
    pytest.importorskip("llm_integration")
    pytest.importorskip("sentence_transformers")
    from echo_memory.config import get_config

    previous = get_config().retrieval_min_similarity
    host = _make_host(tmp_path / "memory-bgem3")
    host.start()
    try:
        host.access.store(actor_id="actor_a", user_text="我養了一隻叫小橘的貓", spoken_text="記住了，小橘是你的貓",
                          session_id="s1", idempotency_key="a5-real-1")
        hit = host.access.retrieve(PRIVATE, "你記得我的寵物嗎", PRIVATE, actor_id="actor_a", session_id="s2")
        assert hit.memory_status.value == "ok" and "小橘" in hit.text
    finally:
        host.stop()
        get_config().retrieval_min_similarity = previous


# ---- /mem embed ----

def test_parse_args():
    assert mem_cmd.parse_args("你好 世界 --device CUDA --n 5") == ("你好 世界", "cuda", 5)
    assert mem_cmd.parse_args('"你好"') == ("你好", "cpu", mem_cmd.DEFAULT_RUNS)


def test_percentile():
    assert mem_cmd.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50) == 5
    assert mem_cmd.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10


def test_mem_embed_印載入首查與百分位():
    now = {"t": 0.0}

    class Fake:
        def embed(self, text):
            now["t"] += 0.5 if text == "warmup" else 0.05
            return [0.0] * 4

    lines: list[str] = []
    result = mem_cmd.run_mem_embed("你好 --n 10", lines.append, embedder_factory=lambda device: Fake(), clock=lambda: now["t"])
    text = "\n".join(lines)
    assert result is not None and result["dim"] == 4
    assert abs(result["load_s"] - 0.5) < 1e-9 and abs(result["p95_s"] - 0.05) < 1e-9
    assert "載入" in text and "p50" in text and "低於上限" in text


def test_mem_embed_cuda_未允許時不載入():
    made: list[str] = []
    lines: list[str] = []
    out = mem_cmd.run_mem_embed("你好 --device cuda", lines.append, embedder_factory=lambda d: made.append(d), env={})
    assert out is None and made == [] and "UEP_E2E_ALLOW_GPU" in "\n".join(lines)


def test_mem_embed_失敗只顯示原因():
    def boom(device):
        raise RuntimeError("x")

    lines: list[str] = []
    assert mem_cmd.run_mem_embed("你好", lines.append, embedder_factory=boom) is None
    assert "RuntimeError" in lines[0]
