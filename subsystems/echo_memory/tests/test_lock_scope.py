"""
鎖範圍測試（M-1 / M-4）

驗收重點：
  1. store_episode 的實體提取（LLM）在鎖外：LLM 慢呼叫期間，同 namespace 的其他寫入不被阻塞。
  2. dream 的 LLM 階段不持鎖。
  3. 排程觸發（_trigger_dream，idle / cron 路徑）與手動 dream 走同一套鎖語意，臨界區內持鎖。
  4. LLM 期間來源被刪除或變動，寫回時丟棄（實體、replay、蒸餾、矛盾偵測）。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from echo_memory import MemoryEngine
from echo_memory.dream.contradiction import ContradictionDetector
from echo_memory.dream.distillation import KnowledgeDistillation
from echo_memory.dream.replay import LiDERReplay
from echo_memory.schemas.concept import Concept
from echo_memory.schemas.entity import Entity
from echo_memory.schemas.episode import Episode
from echo_memory.storage import locking
from echo_memory.storage.episodic import EpisodicMemory
from echo_memory.storage.graph import MemoryGraph
from echo_memory.storage.locking import storage_lock
from echo_memory.storage.semantic import SemanticMemory

# ─── 輔助 ─────────────────────────────────────────────────────────────────────


def _lock_is_free(storage_path: Path, wait: float = 0.5) -> bool:
    """
    從另一條執行緒探測 storage_lock 的執行緒鎖是否空閒。

    storage_lock 以 RLock 做同程序互斥；探測執行緒拿不到代表別的執行緒正持鎖。
    """
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


class _BlockingExtractor:
    """實體提取替身：呼叫時發出訊號並阻塞，直到測試放行。"""

    def __init__(self, entities: list[Entity] | None = None):
        self.started = threading.Event()
        self.release = threading.Event()
        self.entities = entities if entities is not None else []

    def extract(self, episode):
        self.started.set()
        assert self.release.wait(timeout=20), "測試未放行實體提取"
        return list(self.entities)


class _NoopExtractor:
    def extract(self, episode):
        return []


@pytest.fixture
def engine(tmp_path: Path) -> MemoryEngine:
    return MemoryEngine(storage_path=tmp_path)


# ─── M-1：store_episode 實體提取在鎖外 ─────────────────────────────────────────


def test_store_episode_llm_runs_outside_lock(engine: MemoryEngine):
    """實體提取（LLM）進行中，同 namespace 的另一寫入不被阻塞到 LLM 結束。"""
    extractor = _BlockingExtractor()
    engine._entity_extractor = extractor

    first_done = threading.Event()

    def _first():
        engine.store_episode("第一筆需要實體提取", "好", "s1", salience=0.9)
        first_done.set()

    t1 = threading.Thread(target=_first)
    t1.start()
    try:
        assert extractor.started.wait(timeout=10), "實體提取應已開始"

        # LLM 仍阻塞中：鎖必須是空閒的
        assert _lock_is_free(engine.storage_path), "LLM 階段不應持有 storage_lock"

        # 另一條執行緒對同 namespace 寫入（低 salience 不觸發提取）應立即完成
        second_done = threading.Event()

        def _second():
            engine.store_episode("第二筆", "好", "s2", salience=0.1)
            second_done.set()

        t2 = threading.Thread(target=_second)
        t2.start()
        assert second_done.wait(timeout=10), "第二筆寫入被第一筆的 LLM 阻塞"
        assert not first_done.is_set(), "第一筆此時應仍卡在 LLM"
        t2.join(timeout=5)
    finally:
        extractor.release.set()
        t1.join(timeout=20)

    assert first_done.is_set()
    assert engine.episodic.count() == 2


def test_store_episode_stale_entities_discarded(engine: MemoryEngine):
    """LLM 期間 episode 被刪除 → 實體不寫回（過期丟棄）。"""
    extractor = _BlockingExtractor(
        entities=[Entity(name="筑波大學", entity_type="place")],
    )
    engine._entity_extractor = extractor

    holder: dict[str, str] = {}

    def _store():
        holder["id"] = engine.store_episode("我妹妹去筑波大學", "好", "s1", salience=0.9)

    t = threading.Thread(target=_store)
    t.start()
    try:
        assert extractor.started.wait(timeout=10)
        # 以最新的 episode id 刪除（尚未回傳 id，從儲存層取）
        ep_ids = [ep.id for ep in engine.episodic.list_episodes(limit=10)]
        assert len(ep_ids) == 1
        assert engine.episodic.delete_episode(ep_ids[0])
    finally:
        extractor.release.set()
        t.join(timeout=20)

    assert engine.entity_store.count() == 0, "過期的實體不應寫回"


def test_store_episode_entities_applied_when_fresh(engine: MemoryEngine):
    """未過期時實體照常寫回（確認重構未破壞原行為）。"""
    extractor = _BlockingExtractor(
        entities=[Entity(name="筑波大學", entity_type="place")],
    )
    extractor.release.set()
    engine._entity_extractor = extractor

    ep_id = engine.store_episode("我妹妹去筑波大學", "好", "s1", salience=0.9)

    assert engine.entity_store.count() == 1
    assert engine.episodic.graph.has_node(ep_id)


# ─── M-1 / M-4：dream 鎖語意 ──────────────────────────────────────────────────


@pytest.fixture
def dream_engine(tmp_path: Path, monkeypatch):
    """備好未 dream 的高 salience episode，LLM 回呼可注入；排程器不啟動。"""
    monkeypatch.setattr("echo_memory.dream.engine.DreamEngine.start", lambda self: None)
    calls: list[str] = []
    state: dict = {"hook": None}

    def _llm(prompt: str) -> str:
        calls.append(prompt)
        if state["hook"] is not None:
            state["hook"]()
        return "{}"

    eng = MemoryEngine(storage_path=tmp_path, llm_fn=_llm)
    eng._entity_extractor = _NoopExtractor()
    for i, sal in enumerate((0.9, 0.8, 0.7)):
        eng.store_episode(f"對話 {i}", f"回應 {i}", "s1", salience=sal)
    eng._test_llm_calls = calls  # type: ignore[attr-defined]
    eng._test_state = state  # type: ignore[attr-defined]
    return eng


def test_dream_llm_phase_does_not_hold_lock(dream_engine: MemoryEngine):
    """dream 的 LLM 階段（replay / 蒸餾）執行時，storage_lock 必須空閒。"""
    observed: list[bool] = []
    dream_engine._test_state["hook"] = lambda: observed.append(
        _lock_is_free(dream_engine.storage_path)
    )

    dream_engine.dream(triggered_by="manual")

    assert observed, "dream 應至少呼叫過一次 LLM"
    assert all(observed), "LLM 階段不應持鎖"


def test_scheduled_trigger_holds_lock_in_critical_sections(dream_engine: MemoryEngine):
    """
    排程（idle / cron）走 _trigger_dream：臨界區持鎖、LLM 階段不持鎖，
    與手動 dream 同一套鎖語意。
    """
    # 先走一次手動 dream 建立 DreamEngine（排程器已被 fixture 停用）
    dream_engine.dream(triggered_by="manual")
    de = dream_engine._dream_engine
    assert de is not None

    held_in_pruning: list[bool] = []
    original_run = de._pruning.run

    def _probe_pruning_run():
        held_in_pruning.append(not _lock_is_free(dream_engine.storage_path))
        return original_run()

    de._pruning.run = _probe_pruning_run  # type: ignore[method-assign]

    # 讓下一輪還有候選 episode 可進 LLM 階段
    dream_engine.store_episode("新對話", "新回應", "s2", salience=0.95)
    llm_free: list[bool] = []
    dream_engine._test_state["hook"] = lambda: llm_free.append(
        _lock_is_free(dream_engine.storage_path)
    )

    de._trigger_dream("idle")

    assert held_in_pruning == [True], "排程觸發的修剪臨界區必須持有 storage_lock"
    assert llm_free and all(llm_free), "排程觸發的 LLM 階段不應持鎖"
    assert de.get_last_report() is not None


def test_scheduled_trigger_writes_wait_for_critical_section(tmp_path: Path):
    """排程觸發的臨界區持鎖時，另一執行緒的鎖會被擋住（證明真的互斥）。"""
    eng = MemoryEngine(storage_path=tmp_path)
    eng._entity_extractor = _NoopExtractor()
    eng.store_episode("對話", "回應", "s1", salience=0.9)

    from echo_memory.dream.engine import DreamEngine

    de = DreamEngine(
        graph=eng.episodic.graph,
        episodic=eng.episodic,
        enable_idle=False,
        enable_cron=False,
        lock_factory=lambda: storage_lock(eng.storage_path),
    )
    blocked: list[bool] = []
    original = de._pruning.update_edge_weights

    def _probe():
        blocked.append(not _lock_is_free(eng.storage_path))
        return original()

    de._pruning.update_edge_weights = _probe  # type: ignore[method-assign]
    de._trigger_dream("cron")

    assert blocked == [True]


def test_standalone_dream_engine_defaults_to_episodic_lock(tmp_path: Path):
    """獨立使用（未傳 lock_factory）時，預設用 episodic.lock_path 的 storage_lock。"""
    ep_dir = tmp_path / "episodic"
    ep_dir.mkdir()
    episodic = EpisodicMemory(storage_path=ep_dir, lock_path=tmp_path)

    from echo_memory.dream.engine import DreamEngine

    de = DreamEngine(
        graph=episodic.graph, episodic=episodic,
        enable_idle=False, enable_cron=False,
    )
    held: list[bool] = []
    original = de._pruning.run

    def _probe():
        held.append(not _lock_is_free(tmp_path))
        return original()

    de._pruning.run = _probe  # type: ignore[method-assign]
    de.dream_now("manual")

    assert held == [True]


# ─── 過期寫回丟棄：replay / 蒸餾 / 矛盾偵測 ────────────────────────────────────


@pytest.fixture
def storage(tmp_path: Path):
    ep_dir = tmp_path / "episodic"
    ep_dir.mkdir()
    episodic = EpisodicMemory(storage_path=ep_dir)
    return episodic, episodic.graph


def _write_episodes(episodic: EpisodicMemory) -> list[Episode]:
    eps = []
    for text, sal in (("A 對話", 0.9), ("B 對話", 0.8), ("C 對話", 0.7)):
        ep = Episode(
            user_input=text, agent_response="回", session_id="s",
            content_summary=text, salience_score=sal,
        )
        episodic.write(ep)
        eps.append(ep)
    return eps


_REPLAY_JSON = json.dumps({
    "reflections": [
        {"index": 0, "reflection": "r0", "importance": 1.0, "related_indices": [1]},
        {"index": 1, "reflection": "r1", "importance": 1.0, "related_indices": [2]},
        {"index": 2, "reflection": "r2", "importance": 1.0, "related_indices": []},
    ]
})


def test_replay_discards_deleted_episode(storage):
    """LLM 期間候選 episode 被刪除 → 其 salience 更新與相關邊都不套用。"""
    episodic, graph = storage
    eps = _write_episodes(episodic)

    def _llm(prompt: str) -> str:
        episodic.delete_episode(eps[0].id)
        return _REPLAY_JSON

    report = LiDERReplay(episodic, graph, llm_fn=_llm).run()

    assert eps[0].id not in report._replayed_episode_ids
    assert eps[1].id in report._replayed_episode_ids
    # 只有 B-C 這條邊成立；涉及已刪除 A 的邊（A-B）被丟棄
    assert report.edges_created + report.edges_strengthened == 1
    assert not graph.graph.has_edge(eps[0].id, eps[1].id)
    assert episodic.get(eps[0].id) is None


def test_replay_discards_modified_episode(storage):
    """LLM 期間候選 episode 的 salience 被別處改動 → 視為過期，不覆蓋。"""
    episodic, graph = storage
    eps = _write_episodes(episodic)

    def _llm(prompt: str) -> str:
        episodic.update_salience(eps[0].id, 0.1)
        return _REPLAY_JSON

    report = LiDERReplay(episodic, graph, llm_fn=_llm).run()

    assert eps[0].id not in report._replayed_episode_ids
    # 別處寫入的 0.1 不被 replay 的舊快照運算覆蓋
    assert episodic.get(eps[0].id).salience_score == pytest.approx(0.1)


def test_replay_all_stale_skips(storage):
    episodic, graph = storage
    eps = _write_episodes(episodic)

    def _llm(prompt: str) -> str:
        for ep in eps:
            episodic.delete_episode(ep.id)
        return _REPLAY_JSON

    report = LiDERReplay(episodic, graph, llm_fn=_llm).run()

    assert report.skipped_reason == "stale"
    assert report._replayed_episode_ids == []


def test_distillation_discards_concept_with_stale_sources(tmp_path: Path):
    """LLM 期間唯一來源 episode 被刪除 → 概念不寫入 SemanticMemory。"""
    ep_dir = tmp_path / "episodic"
    ep_dir.mkdir()
    episodic = EpisodicMemory(storage_path=ep_dir)
    semantic = SemanticMemory(storage_path=tmp_path / "semantic")
    eps = _write_episodes(episodic)

    response = json.dumps([{
        "name": "過期概念", "description": "來源已消失",
        "concept_type": "abstract", "source_indices": [0],
    }])

    def _llm(prompt: str) -> str:
        episodic.delete_episode(eps[0].id)
        return response

    report = KnowledgeDistillation(
        episodic=episodic, semantic=semantic, graph=episodic.graph, llm_fn=_llm,
    ).run()

    assert report.concepts_stale == 1
    assert report.concepts_created == 0
    assert semantic.get_by_name("過期概念") is None
    assert report._distilled_episode_ids == []


def test_distillation_drops_only_stale_sources(tmp_path: Path):
    """部分來源過期：概念保留，但只連到仍有效的來源，且過期者不進 dreamed 清單。"""
    ep_dir = tmp_path / "episodic"
    ep_dir.mkdir()
    episodic = EpisodicMemory(storage_path=ep_dir)
    semantic = SemanticMemory(storage_path=tmp_path / "semantic")
    eps = _write_episodes(episodic)

    response = json.dumps([{
        "name": "部分有效", "description": "仍有來源",
        "concept_type": "abstract", "source_indices": [0, 1],
    }])

    def _llm(prompt: str) -> str:
        episodic.delete_episode(eps[0].id)
        return response

    report = KnowledgeDistillation(
        episodic=episodic, semantic=semantic, graph=episodic.graph, llm_fn=_llm,
    ).run()

    assert report.concepts_created == 1
    assert eps[0].id not in report._distilled_episode_ids
    assert eps[1].id in report._distilled_episode_ids


def test_contradiction_llm_outside_lock_and_stale_discarded(tmp_path: Path):
    """矛盾偵測的 LLM 在鎖外；LLM 期間候選概念被改動 → 矛盾邊不登記。"""
    semantic = SemanticMemory(storage_path=tmp_path / "semantic")
    graph = MemoryGraph(storage_path=tmp_path / "graph")

    concept_a = Concept(name="A", description="運動有益")
    concept_a.embedding = [1.0, 0.0]
    concept_b = Concept(name="B", description="運動有害")
    concept_b.embedding = [0.99, 0.14]
    for c in (concept_a, concept_b):
        semantic._concepts[c.name] = c
        semantic._concepts_by_id[c.id] = c

    free_during_llm: list[bool] = []

    def _llm(prompt: str) -> str:
        free_during_llm.append(_lock_is_free(tmp_path))
        concept_b.description = "已被改寫的描述"  # 模擬別處在 LLM 期間修改
        return json.dumps({"is_contradiction": True, "strength": 0.9, "reason": "x"})

    detector = ContradictionDetector(
        semantic=semantic, graph=graph, similarity_threshold=0.8, llm_fn=_llm,
        guard=lambda: storage_lock(tmp_path),
    )
    edges = detector.detect_and_register(concept_a, candidate_pool=[concept_b])

    assert free_during_llm == [True], "矛盾偵測的 LLM 不應持鎖"
    assert edges == [], "候選已被改動，矛盾結果應丟棄"
    assert not graph.has_contradicts_edge(concept_a=concept_a.id, concept_b=concept_b.id)
