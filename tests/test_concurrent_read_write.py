"""
M-1b：episodic 寫入的 embedding 移出鎖，且讀路徑對同程序並行寫入安全。

驗收重點：
  1. store_episode 的 embedding（慢網路）進行中，storage_lock 與記憶體鎖皆空閒，
     retrieve 不被擋住（量測時間上界）。
  2. 多執行緒並行 store_episode 與 retrieve：不拋例外、索引結構一致、數量正確。
  3. embedding 失敗仍存佔位 episode（零向量＋embedding_pending）。
  4. backfill_pending_embeddings 的 embedding 同樣在鎖外。
  5. 圖譜並行讀寫（含 reload／save／dream 直接改圖處）不拋例外。
  6. semantic／procedural 寫入的 embedding 不擋 search_by_vector。
"""

from __future__ import annotations

import threading
import time
import zlib
from pathlib import Path

import numpy as np
import pytest

from echo_memory import MemoryEngine
from echo_memory.schemas.episode import Episode
from echo_memory.schemas.procedure import Procedure
from echo_memory.storage import locking
from echo_memory.storage.episodic import EmbeddingUnavailable
from echo_memory.storage.graph import MemoryGraph
from echo_memory.storage.locking import ReleasableRLock, storage_lock
from echo_memory.storage.procedural import ProceduralMemory
from echo_memory.storage.semantic import SemanticMemory

# retrieve 的時間上界：遠小於慢 embedding 的延遲（SLOW_DELAY），留足 CI 抖動空間
RETRIEVE_BOUND = 0.8
SLOW_DELAY = 2.0


def _vec(text: str, dim: int = 512) -> list[float]:
    """共同基底向量加文字相關的小擾動：任兩段文字的 cosine 約 0.99，檢索必有命中。"""
    base = np.random.RandomState(0).randn(dim).astype(np.float32)
    noise = np.random.RandomState(zlib.crc32(text.encode("utf-8"))).randn(dim).astype(np.float32)
    v = base + 0.05 * noise
    return (v / np.linalg.norm(v)).tolist()


def _lock_is_free(storage_path: Path, wait: float = 0.5) -> bool:
    """從另一條執行緒探測 storage_lock 的執行緒鎖是否空閒。"""
    lock = locking._thread_lock(Path(storage_path) / ".echo-memory.lock")
    result: dict[str, bool] = {}

    def _probe() -> None:
        got = lock.acquire(timeout=wait)
        result["free"] = got
        if got:
            lock.release()

    t = threading.Thread(target=_probe)
    t.start()
    t.join(timeout=wait + 5)
    return result.get("free", False)


def _mem_lock_is_free(episodic, wait: float = 0.5) -> bool:
    result: dict[str, bool] = {}

    def _probe() -> None:
        got = episodic._mem_lock.acquire(timeout=wait)
        result["free"] = got
        if got:
            episodic._mem_lock.release()

    t = threading.Thread(target=_probe)
    t.start()
    t.join(timeout=wait + 5)
    return result.get("free", False)


class _SlowEmbedder:
    """慢網路替身：進入時發訊號並睡眠，可指定哪些文字變慢。"""

    def __init__(self, delay: float, slow_prefix: str = "SLOW"):
        self.delay = delay
        self.slow_prefix = slow_prefix
        self.entered = threading.Event()
        self.calls = 0

    def __call__(self, text: str) -> list[float]:
        if text.startswith(self.slow_prefix):
            self.calls += 1
            self.entered.set()
            time.sleep(self.delay)
        return _vec(text)


@pytest.fixture
def engine(tmp_path: Path) -> MemoryEngine:
    eng = MemoryEngine(storage_path=tmp_path)
    # 查詢向量走快速路徑；寫入向量由各測試決定快慢（conftest 的查詢包裝會呼叫
    # _generate_embedding，若不另外覆蓋，慢寫入替身會連查詢一起變慢）
    eng.episodic.generate_query_embedding = lambda text: _vec(text)
    eng.episodic._generate_embedding = lambda text: _vec(text)
    return eng


def _seed(engine: MemoryEngine, n: int = 5) -> None:
    for i in range(n):
        engine.store_episode(f"種子對話 {i}", f"回覆 {i}", "seed", salience=0.1)


# ─── 1. embedding 在鎖外，retrieve 不被擋 ──────────────────────────────────────


def test_retrieve_not_blocked_by_slow_store_embedding(engine: MemoryEngine):
    _seed(engine)
    slow = _SlowEmbedder(SLOW_DELAY)
    engine.episodic._generate_embedding = slow

    errors: list[BaseException] = []

    def _writer(i: int) -> None:
        try:
            engine.store_episode(f"SLOW 慢寫入 {i}", "好", f"w{i}", salience=0.1)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    writers = [threading.Thread(target=_writer, args=(i,)) for i in range(2)]
    for t in writers:
        t.start()
    try:
        assert slow.entered.wait(timeout=10), "慢 embedding 應已開始"

        # embedding 進行中：跨程序檔案鎖與記憶體鎖都必須空閒
        assert _lock_is_free(engine.storage_path), "embedding 期間不應持有 storage_lock"
        assert _mem_lock_is_free(engine.episodic), "embedding 期間不應持有 _mem_lock"

        started = time.perf_counter()
        ctx = engine.retrieve("種子對話", top_k=5)
        elapsed = time.perf_counter() - started
        assert elapsed < RETRIEVE_BOUND, f"retrieve 被寫入的 embedding 延遲擋住: {elapsed:.2f}s"
        assert not ctx.is_empty()
        assert all(engine.episodic.get(ep.id) is not None for ep in ctx.episodes)
    finally:
        for t in writers:
            t.join(timeout=30)

    assert not errors, errors
    assert engine.episodic.count() == 5 + 2


# ─── 2. 並行 store + retrieve 一致性 ───────────────────────────────────────────


def test_concurrent_store_and_retrieve_are_consistent(engine: MemoryEngine):
    _seed(engine, 4)
    engine.episodic._generate_embedding = _SlowEmbedder(0.03, slow_prefix="W")

    n_writers, per_writer = 3, 4
    errors: list[BaseException] = []
    stop = threading.Event()
    retrieves = [0]

    def _writer(w: int) -> None:
        try:
            for i in range(per_writer):
                engine.store_episode(f"W{w} 寫入 {i}", "好", f"s{w}", salience=0.1)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def _reader() -> None:
        try:
            while not stop.is_set():
                ctx = engine.retrieve("種子對話", top_k=5)
                retrieves[0] += 1
                for ep in ctx.episodes:
                    assert ep.id
                    assert engine.episodic.get(ep.id) is not None
                engine.episodic.list_episodes(limit=50)
                engine.episodic.count()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    writers = [threading.Thread(target=_writer, args=(w,)) for w in range(n_writers)]
    readers = [threading.Thread(target=_reader) for _ in range(2)]
    for t in readers + writers:
        t.start()
    for t in writers:
        t.join(timeout=60)
    stop.set()
    for t in readers:
        t.join(timeout=30)

    assert not errors, errors
    assert retrieves[0] > 0

    epi = engine.episodic
    total = 4 + n_writers * per_writer
    assert epi.count() == total
    # 索引結構一致：矩陣列數 = episode 數，id 對照涵蓋全部且唯一
    assert epi._embeddings.shape[0] == total
    assert sorted(epi._id_to_index.values()) == list(range(total))
    assert set(epi._id_to_index) == set(epi._episodes)


# ─── 3. embedding 失敗仍存佔位 ─────────────────────────────────────────────────


def test_failed_embedding_still_stores_placeholder(engine: MemoryEngine):
    _seed(engine, 2)

    def _down(text: str) -> list[float]:
        raise EmbeddingUnavailable("後端離線")

    engine.episodic._generate_embedding = _down
    eid = engine.store_episode("離線時的對話", "好", "off", salience=0.1)

    ep = engine.episodic.get(eid)
    assert ep is not None
    assert ep.metadata.get("embedding_pending") is True
    assert len(ep.embedding) == engine.episodic._embeddings.shape[1]
    assert not any(ep.embedding)
    assert engine.episodic.count() == 3

    # 持久化後重載仍在，且之後可補齊
    engine.episodic._generate_embedding = lambda text: _vec(text)
    assert engine.episodic.backfill_pending_embeddings() == 1
    healed = engine.episodic.get(eid)
    assert not healed.metadata.get("embedding_pending")
    assert any(healed.embedding)


# ─── 4. backfill 的 embedding 在鎖外 ───────────────────────────────────────────


def test_backfill_embeds_outside_locks(engine: MemoryEngine):
    _seed(engine, 2)

    def _down(text: str) -> list[float]:
        raise EmbeddingUnavailable("後端離線")

    engine.episodic._generate_embedding = _down
    eid = engine.store_episode("SLOW 待補", "好", "bf", salience=0.1)
    assert engine.episodic.get(eid).metadata.get("embedding_pending")

    slow = _SlowEmbedder(1.0)
    engine.episodic._generate_embedding = slow
    result: dict[str, int] = {}
    t = threading.Thread(
        target=lambda: result.update(done=engine.episodic.backfill_pending_embeddings())
    )
    t.start()
    try:
        assert slow.entered.wait(timeout=10)
        assert _lock_is_free(engine.storage_path), "backfill 的 embedding 不應持有 storage_lock"
        assert _mem_lock_is_free(engine.episodic)
        started = time.perf_counter()
        engine.retrieve("種子對話", top_k=3)
        assert time.perf_counter() - started < RETRIEVE_BOUND
    finally:
        t.join(timeout=30)
    assert result.get("done") == 1


# ─── 5. 圖譜並行讀寫 ───────────────────────────────────────────────────────────


def test_graph_concurrent_read_write(tmp_path: Path):
    graph = MemoryGraph(tmp_path)
    errors: list[BaseException] = []
    stop = threading.Event()
    published: list[str] = []

    def _writer() -> None:
        try:
            prev = None
            for i in range(300):
                ep = Episode(
                    user_input="u", agent_response="a", session_id="s",
                    content_summary=f"c{i}",
                )
                graph.add_episode_node(ep)
                published.append(ep.id)
                if prev is not None:
                    graph.add_precedes_edge(prev, ep.id, 1.0)
                prev = ep.id
                if i % 50 == 0:
                    graph.save()
                if i % 100 == 99:
                    graph.reload()
                    prev = None
                # dream 直接改圖處的寫法：以 graph.lock 包住結構變動
                with graph.lock:
                    graph.graph.add_node(f"extra{i}", node_type="Concept")
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
        finally:
            stop.set()

    def _safe_reader() -> None:
        # 只走公開方法，不手持 graph.lock；種子取自寫入者公布的 id
        try:
            while not stop.is_set():
                seeds = list(published[-5:])
                graph.expand_neighbors(seeds, max_hops=2, min_weight=0.0)
                graph.get_stats()
                graph.get_node_count()
                for n in seeds:
                    graph.has_node(n)
                    graph.get_episode_entities(n)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_writer)] + [
        threading.Thread(target=_safe_reader) for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, errors


def test_episodic_graph_survives_concurrent_search_and_write(engine: MemoryEngine):
    """episodic 寫入改圖（含 reload 換圖）時，retrieve 的 expand_neighbors 不拋例外。"""
    _seed(engine, 6)
    errors: list[BaseException] = []
    stop = threading.Event()

    def _writer() -> None:
        try:
            for i in range(12):
                engine.store_episode(f"圖並行 {i}", "好", "g", salience=0.1)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
        finally:
            stop.set()

    def _reader() -> None:
        try:
            while not stop.is_set():
                engine.retrieve("種子對話", top_k=6)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_writer)] + [
        threading.Thread(target=_reader) for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors


# ─── 6. semantic／procedural 寫入的 embedding 不擋讀 ────────────────────────────


def test_semantic_upsert_embedding_does_not_block_search(tmp_path: Path):
    slow = _SlowEmbedder(SLOW_DELAY)
    sem = SemanticMemory(storage_path=tmp_path / "sem", embed_fn=slow)
    sem.upsert_concept("快概念", "描述", embedding=_vec("快概念"))

    t = threading.Thread(target=lambda: sem.upsert_concept("SLOW概念", "描述"))
    t.start()
    try:
        assert slow.entered.wait(timeout=10)
        started = time.perf_counter()
        results = sem.search_by_vector(_vec("快概念"), top_k=3, min_similarity=0.0)
        elapsed = time.perf_counter() - started
        assert elapsed < RETRIEVE_BOUND, f"search_by_vector 被 upsert 的 embedding 擋住: {elapsed:.2f}s"
        assert results
    finally:
        t.join(timeout=30)
    assert sem.count() == 2


def test_procedural_write_embedding_does_not_block_search(tmp_path: Path):
    slow = _SlowEmbedder(SLOW_DELAY)
    proc = ProceduralMemory(storage_path=tmp_path / "proc", embed_fn=slow)
    quick = Procedure(name="快程序", description="d", steps=["a"], domain="x")
    proc.write(quick)

    t = threading.Thread(
        target=lambda: proc.write(
            Procedure(name="SLOW程序", description="d", steps=["b"], domain="x")
        )
    )
    t.start()
    try:
        assert slow.entered.wait(timeout=10)
        started = time.perf_counter()
        results = proc.search_by_vector(_vec("快程序: d. 步驟: a"), top_k=3, min_similarity=0.0)
        elapsed = time.perf_counter() - started
        assert elapsed < RETRIEVE_BOUND, f"procedural 搜尋被寫入的 embedding 擋住: {elapsed:.2f}s"
        assert results
        assert proc.count() >= 1
    finally:
        t.join(timeout=30)
    assert proc.count() == 2


# ─── 7. 刪除／淘汰期間讀者不被 I/O 擋、圖不競態 ────────────────────────────────


def _slow_save(episodic, entered: threading.Event, delay: float):
    original = episodic._save_unlocked

    def _wrapped() -> None:
        entered.set()
        time.sleep(delay)
        original()

    episodic._save_unlocked = _wrapped


def test_delete_episode_save_does_not_block_readers(engine: MemoryEngine):
    _seed(engine, 5)
    victim = engine.episodic.list_episodes(limit=1)[0].id
    entered = threading.Event()
    _slow_save(engine.episodic, entered, 1.5)

    t = threading.Thread(target=lambda: engine.episodic.delete_episode(victim))
    t.start()
    try:
        assert entered.wait(timeout=10)
        assert _mem_lock_is_free(engine.episodic), "存檔 I/O 期間不應持有 _mem_lock"
        started = time.perf_counter()
        engine.retrieve("種子對話", top_k=3)
        assert time.perf_counter() - started < RETRIEVE_BOUND
    finally:
        t.join(timeout=30)
    assert engine.episodic.get(victim) is None
    assert engine.episodic.count() == 4


def test_evict_oldest_save_does_not_block_readers(tmp_path: Path):
    eng = MemoryEngine(storage_path=tmp_path, max_episodes=3)
    eng.episodic.generate_query_embedding = lambda text: _vec(text)
    eng.episodic._generate_embedding = lambda text: _vec(text)
    _seed(eng, 3)
    entered = threading.Event()
    _slow_save(eng.episodic, entered, 1.5)

    t = threading.Thread(target=lambda: eng.store_episode("SLOW 觸發淘汰", "好", "ev", salience=0.1))
    t.start()
    try:
        assert entered.wait(timeout=10)
        assert _mem_lock_is_free(eng.episodic)
        started = time.perf_counter()
        eng.retrieve("種子對話", top_k=3)
        assert time.perf_counter() - started < RETRIEVE_BOUND
    finally:
        t.join(timeout=30)
    assert eng.episodic.count() == 3


def test_concurrent_delete_and_retrieve_graph_is_race_free(engine: MemoryEngine):
    _seed(engine, 12)
    ids = [e.id for e in engine.episodic.list_episodes(limit=50)]
    errors: list[BaseException] = []
    stop = threading.Event()

    def _deleter() -> None:
        try:
            for eid in ids[:8]:
                engine.episodic.delete_episode(eid)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
        finally:
            stop.set()

    def _reader() -> None:
        try:
            while not stop.is_set():
                engine.retrieve("種子對話", top_k=6)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_deleter)] + [
        threading.Thread(target=_reader) for _ in range(2)
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=60)
    assert not errors, errors
    assert engine.episodic.count() == 4
    assert not any(engine.episodic.graph.has_node(i) for i in ids[:8])


# ─── 8. 冷啟動並行初始化只建一份 ───────────────────────────────────────────────


def test_concurrent_cold_start_builds_each_component_once(tmp_path: Path, monkeypatch):
    from echo_memory.storage.episodic import EpisodicMemory

    built: list[int] = []
    real_init = EpisodicMemory.__init__

    def _counting_init(self, *a, **k):
        built.append(1)
        time.sleep(0.2)  # 拉寬競態窗口
        real_init(self, *a, **k)

    monkeypatch.setattr(EpisodicMemory, "__init__", _counting_init)
    eng = MemoryEngine(storage_path=tmp_path)

    seen: dict[str, list[object]] = {"episodic": [], "semantic": [], "retrieval": []}
    barrier = threading.Barrier(4)

    def _touch() -> None:
        barrier.wait()
        for name in seen:
            seen[name].append(getattr(eng, name))

    threads = [threading.Thread(target=_touch) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=30)

    assert len(built) == 1
    for name, objs in seen.items():
        assert len(objs) == 4 and len({id(o) for o in objs}) == 1, name


def test_ensure_initialized_builds_everything(tmp_path: Path):
    eng = MemoryEngine(storage_path=tmp_path)
    assert eng._episodic is None
    eng.ensure_initialized()
    for attr in ("_episodic", "_semantic", "_working", "_procedural", "_retrieval",
                 "_entity_store", "_entity_extractor", "_evidence", "_idempotency", "_activity"):
        assert getattr(eng, attr) is not None, attr


# ─── 9. semantic／procedural 存檔不擋 search ───────────────────────────────────


def test_semantic_save_does_not_block_search(tmp_path: Path, monkeypatch):
    from echo_memory.storage import semantic as sem_mod

    sem = SemanticMemory(storage_path=tmp_path / "sem", embed_fn=lambda text: _vec(text))
    sem.upsert_concept("既有概念", "描述")

    entered = threading.Event()
    real = sem_mod.atomic_write_json

    def _slow_write(*a, **k):
        entered.set()
        time.sleep(1.5)
        return real(*a, **k)

    monkeypatch.setattr(sem_mod, "atomic_write_json", _slow_write)
    t = threading.Thread(target=lambda: sem.upsert_concept("新概念", "描述"))
    t.start()
    try:
        assert entered.wait(timeout=10)
        started = time.perf_counter()
        results = sem.search_by_vector(_vec("既有概念: 描述"), top_k=3, min_similarity=0.0)
        assert time.perf_counter() - started < RETRIEVE_BOUND, "存檔 I/O 擋住了 search_by_vector"
        assert results
    finally:
        t.join(timeout=30)
    assert sem.count() == 2

    # 存檔順序：最後一次寫入的內容必須是最終檔案內容
    monkeypatch.setattr(sem_mod, "atomic_write_json", real)
    sem.upsert_concept("第三概念", "描述")
    reloaded = SemanticMemory(storage_path=tmp_path / "sem")
    assert reloaded.count() == 3


def test_procedural_save_does_not_block_search(tmp_path: Path, monkeypatch):
    from echo_memory.storage import procedural as proc_mod

    proc = ProceduralMemory(storage_path=tmp_path / "proc", embed_fn=lambda text: _vec(text))
    proc.write(Procedure(name="既有", description="d", steps=["a"], domain="x"))

    entered = threading.Event()
    real = proc_mod.atomic_write_json

    def _slow_write(*a, **k):
        entered.set()
        time.sleep(1.5)
        return real(*a, **k)

    monkeypatch.setattr(proc_mod, "atomic_write_json", _slow_write)
    t = threading.Thread(
        target=lambda: proc.write(Procedure(name="新", description="d", steps=["b"], domain="x"))
    )
    t.start()
    try:
        assert entered.wait(timeout=10)
        started = time.perf_counter()
        proc.search_by_vector(_vec("既有: d. 步驟: a"), top_k=3, min_similarity=0.0)
        proc.count()
        assert time.perf_counter() - started < RETRIEVE_BOUND
    finally:
        t.join(timeout=30)
    assert proc.count() == 2


# ─── 10. 維度不合回空；冪等預判不白算 embedding ────────────────────────────────


def test_dimension_mismatch_returns_empty(engine: MemoryEngine):
    _seed(engine, 3)
    epi = engine.episodic
    eid = epi.list_episodes(limit=1)[0].id
    assert epi.search([1.0, 0.0, 0.0], top_k=3, min_similarity=0.0) == []
    assert epi.similarity_to_query([1.0, 0.0, 0.0], eid) is None


def test_idempotent_resend_skips_embedding(engine: MemoryEngine):
    calls = []

    def _counting(text: str) -> list[float]:
        calls.append(text)
        return _vec(text)

    engine.episodic._generate_embedding = _counting
    first = engine.store_episode("冪等測試", "好", "idem", salience=0.1, idempotency_key="k1")
    n = len(calls)
    again = engine.store_episode("冪等測試", "好", "idem", salience=0.1, idempotency_key="k1")
    assert again == first
    assert len(calls) == n, "冪等命中不應再算 embedding"


# ─── 11. 冷建構與 storage_lock 不死鎖（ABBA） ──────────────────────────────────


def _slow_episodic_init(monkeypatch, delay: float = 0.3):
    """拉寬 EpisodicMemory 建構窗口（建構內會取 storage_lock）。"""
    from echo_memory.storage.episodic import EpisodicMemory

    real_init = EpisodicMemory.__init__

    def _slow(self, *a, **k):
        time.sleep(delay)
        real_init(self, *a, **k)

    monkeypatch.setattr(EpisodicMemory, "__init__", _slow)


def _run_all(targets, timeout: float = 30.0) -> list[BaseException]:
    errors: list[BaseException] = []

    def _wrap(fn):
        def _inner():
            try:
                fn()
            except BaseException as e:  # noqa: BLE001
                errors.append(e)
        return _inner

    threads = [threading.Thread(target=_wrap(fn), daemon=True) for fn in targets]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=timeout)
    hung = [th for th in threads if th.is_alive()]
    assert not hung, "出現死鎖：執行緒逾時未結束"
    return errors


def test_cold_init_while_holding_storage_lock_does_not_deadlock(tmp_path: Path, monkeypatch):
    _slow_episodic_init(monkeypatch)
    eng = MemoryEngine(storage_path=tmp_path)

    def _holder():
        # 持 storage_lock 再冷存取 idempotency／evidence（不會建 episodic）
        with storage_lock(eng.storage_path):
            time.sleep(0.1)
            eng.idempotency
            eng.evidence

    def _builder():
        eng.retrieval  # 冷建 retrieval -> semantic -> episodic（建構內取 storage_lock）

    assert not _run_all([_holder, _builder])


def test_cold_record_observation_and_retrieval_do_not_deadlock(tmp_path: Path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    _slow_episodic_init(monkeypatch)
    eng = MemoryEngine(storage_path=tmp_path)
    now = datetime.now(timezone.utc)

    def _observe():
        eng.record_observation(
            kind="game_activity", subject="某遊戲",
            started_at=now - timedelta(minutes=30), ended_at=now,
            idempotency_key="obs-1",
        )

    def _build():
        eng.retrieval

    def _store():
        eng.store_episode("冷啟動並行寫入", "好", "cold", salience=0.1, idempotency_key="cold-1")

    assert not _run_all([_observe, _build, _store])
    assert eng.episodic.count() == 1


# ─── 12. ReleasableRLock 單元測試 ──────────────────────────────────────────────


def test_releasable_rlock_reentrant_and_exclusive():
    lock = ReleasableRLock()
    with lock:
        with lock:
            pass
        got: list[bool] = []
        th = threading.Thread(target=lambda: got.append(lock.acquire(timeout=0.2)))
        th.start()
        th.join()
        assert got == [False], "持有期間他執行緒不應取得"
    got.clear()
    th = threading.Thread(target=lambda: (got.append(lock.acquire(timeout=0.5)), lock.release()))
    th.start()
    th.join()
    assert got == [True]


def test_releasable_rlock_released_preserves_depth():
    lock = ReleasableRLock()
    with lock:
        with lock:
            with lock.released():
                # 讓出期間他執行緒可取得
                got: list[bool] = []

                def _other():
                    ok = lock.acquire(timeout=1.0)
                    got.append(ok)
                    if ok:
                        lock.release()

                th = threading.Thread(target=_other)
                th.start()
                th.join()
                assert got == [True]
            # 取回後保留原本的 2 層重入深度
            assert lock._depth == 2
        assert lock._depth == 1
    assert lock._depth == 0 and lock._owner is None


def test_releasable_rlock_restores_on_exception():
    lock = ReleasableRLock()
    with lock:
        with pytest.raises(ValueError):
            with lock.released():
                raise ValueError("boom")
        assert lock._owner == threading.get_ident() and lock._depth == 1
    assert lock._owner is None


def test_releasable_rlock_released_without_ownership_is_noop():
    lock = ReleasableRLock()
    with lock.released():
        assert lock._owner is None
    with pytest.raises(RuntimeError):
        lock.release()
