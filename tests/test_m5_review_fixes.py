# ruff: noqa: F811
"""M5 審查修正的回歸測試（update_edge_weights 逐條紀錄、MCP 排程、bridge 撤銷、回滾、租約、Hypothesis）。"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest

from echo_memory.dream.engine import DreamEngine
from echo_memory.dream.pruning import GraphPruning
from echo_memory.storage.graph import MemoryGraph
from echo_memory.storage.locking import FileLease
from tests.hypothesis_support import build_env, make_bridge, make_concept, user_ep
from tests.test_dream_graph_provenance import (  # noqa: F401  (fixtures)
    DAY,
    _legacy_bridge,
    _make_similar,
    _mutations,
    audit_path,
    bad_coord,
    coord,
    env,
)


# ─── 1. update_edge_weights 逐條紀錄 ─────────────────────────────────────────


def _precedes(graph, a, b):
    graph.graph.add_edge(
        a, b, edge_type="PRECEDES", weight=0.9, recency_score=1.0, frequency=1,
        avg_salience=0.5, created_at=time.time() - 30 * DAY,
    )


def test_update_edge_weights_records_prior_per_edge_and_rollbacks(env, coord, audit_path):
    _, _, graph, ids = env
    _precedes(graph, ids["X"], ids["Y"])
    gp = GraphPruning(graph, patch_coordinator=coord)
    assert gp.update_edge_weights() == 1
    recs = _mutations(audit_path, "edge_update")
    assert len(recs) == 1
    r = recs[0]
    assert r["stage"] == "pruning"
    assert r["prior"]["weight"] == pytest.approx(0.9)
    assert r["prior"]["recency_score"] == pytest.approx(1.0)
    assert r["new"]["weight"] == pytest.approx(graph.graph[ids["X"]][ids["Y"]]["weight"])
    assert graph.graph[ids["X"]][ids["Y"]]["weight"] != pytest.approx(0.9)
    assert coord.rollback_mutation(r, graph=graph) is True
    restored = graph.graph[ids["X"]][ids["Y"]]
    assert restored["weight"] == pytest.approx(0.9)
    assert restored["recency_score"] == pytest.approx(1.0)


def test_update_edge_weights_blocked_when_record_fails(env, bad_coord):
    _, _, graph, ids = env
    _precedes(graph, ids["X"], ids["Y"])
    gp = GraphPruning(graph, patch_coordinator=bad_coord)
    assert gp.update_edge_weights() == 0
    assert graph.graph[ids["X"]][ids["Y"]]["weight"] == pytest.approx(0.9)


# ─── 3. bridge 取代語意 ──────────────────────────────────────────────────────


def test_contradicts_replacement_records_expired_revoke(env, coord, audit_path):
    _, _, graph, ids = env
    _legacy_bridge(graph, ids["X"], ids["Y"])
    assert graph.add_contradicts_edge(
        ids["X"], ids["Y"], 0.8, on_bridge_displaced=coord.record_bridge_displaced,
    )
    assert _mutations(audit_path, "bridge_degrade") == []
    recs = _mutations(audit_path, "bridge_revoke")
    assert recs and all(r["new"]["state"] == "expired" for r in recs)
    assert graph.iter_latent_bridges() == []  # bridge 已被取代，不留 degraded 殘影


# ─── 4. rollback_mutation ────────────────────────────────────────────────────


def test_rollback_unsupported_kind_writes_no_audit(env, coord, audit_path):
    _, _, graph, _ = env
    rec = {"mutation": "procedure_write", "stage": "procedure", "mutation_id": "m1",
           "target": {}, "prior": None}
    assert coord.rollback_mutation(rec, graph=graph) is False
    assert _mutations(audit_path, "rollback") == []
    # 缺少所需 handle 的 kind 同樣不寫 audit
    rec2 = {"mutation": "episode_salience", "stage": "replay", "mutation_id": "m2",
            "target": {"episode_id": "e"}, "prior": {"salience_score": 0.1}}
    assert coord.rollback_mutation(rec2, graph=graph, episodic=None) is False
    assert _mutations(audit_path, "rollback") == []


def test_rollback_edge_persists_graph(env, coord, audit_path, monkeypatch):
    _, _, graph, ids = env
    graph.graph.add_edge(ids["X"], ids["Y"], edge_type="RELATES_TO", weight=0.01)
    gp = GraphPruning(graph, patch_coordinator=coord)
    gp._remove_edges([(ids["X"], ids["Y"])])
    rec = _mutations(audit_path, "edge_remove")[0]
    saves = []
    monkeypatch.setattr(graph, "save", lambda: saves.append(1))
    assert coord.rollback_mutation(rec, graph=graph) is True
    assert saves == [1]
    assert graph.graph.has_edge(ids["X"], ids["Y"])


# ─── 8. 無 coordinator 時 confirm_count 跨週期累計 ───────────────────────────


def test_confirm_count_accumulates_without_coordinator(env):
    _, semantic, graph, ids = env
    _make_similar(semantic, ids)
    gp = GraphPruning(graph, semantic=semantic)
    gp.run()
    assert graph.graph[ids["X"]][ids["Y"]]["confirm_count"] == 1
    assert graph.graph[ids["X"]][ids["Y"]]["created_cycle_id"]
    gp.run()
    assert graph.graph[ids["X"]][ids["Y"]]["confirm_count"] == 2


# ─── 5/6/7/9. DreamEngine 租約與階段 ─────────────────────────────────────────


def _engine(tmp_path, lease=None):
    graph = MemoryGraph(storage_path=tmp_path / "g")
    return DreamEngine(graph, lease=lease)


def test_lease_renew_only_by_holder(tmp_path):
    a = FileLease(tmp_path, "dream", ttl_seconds=60)
    b = FileLease(tmp_path, "dream", ttl_seconds=60)
    assert a.try_acquire("idle")[0]
    assert a.renew(now=time.time() + 30) is True
    assert a.read_state()["expires_at"] > time.time() + 60
    # a 失租（過期被 b 接手）後不能續租
    assert b.try_acquire("manual", now=time.time() + 3600)[0]
    assert a.renew() is False


def test_dream_aborts_when_lease_lost_between_stages(tmp_path):
    lease = FileLease(tmp_path, "dream", ttl_seconds=60)
    other = FileLease(tmp_path, "dream", ttl_seconds=60)
    eng = _engine(tmp_path, lease)
    saves = []
    eng.graph.save = lambda: saves.append(1)  # type: ignore[method-assign]
    real_run = eng._pruning.run

    def steal():
        out = real_run()
        # 模擬租約逾期並被他行程接手
        state = lease._read()
        state["expires_at"] = 1.0
        lease._write(state)
        assert other.try_acquire("manual")[0]
        return out

    eng._pruning.run = steal  # type: ignore[method-assign]
    report = eng.dream_now("manual")
    assert report.error and "租約" in report.error
    assert saves == [1]  # 僅修剪自身的 save；最終 save 未執行
    # 他行程持有的租約不被原持有者釋放
    assert other.read_state().get("holder_token") == other.token


def test_lease_release_retried_once(tmp_path):
    class Flaky(FileLease):
        calls = 0

        def release(self, **kw):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise OSError("boom")
            super().release(**kw)

    lease = Flaky(tmp_path, "dream", ttl_seconds=60)
    eng = _engine(tmp_path, lease)
    report = eng.dream_now("manual")
    assert report.error is None
    assert Flaky.calls == 2
    assert "holder_token" not in lease.read_state()


@pytest.mark.parametrize("by,reset", [("idle", True), ("cron", False)])
def test_blocked_trigger_resets_idle_timer_only_for_idle(tmp_path, by, reset):
    lease = FileLease(tmp_path, "dream", ttl_seconds=60)
    assert FileLease(tmp_path, "dream", ttl_seconds=60).try_acquire("x")[0]
    eng = _engine(tmp_path, lease)
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    eng._last_interaction = old
    eng._trigger_dream(by)
    assert (eng._last_interaction != old) is reset


def test_stage5_has_no_preaudit(tmp_path):
    eng = _engine(tmp_path)

    class Spy:
        cycle_id = ""
        called = 0

        def audit_procedure_extraction(self, *a):
            Spy.called += 1
            return True

        def begin_cycle(self, *a):
            return None

        def review_held(self, **k):
            return None

    class Ep:
        def list_episodes(self, **k):
            return ["ep"]

    class Extractor:
        ran = 0

        def run(self, cands):
            Extractor.ran += 1
            return "report"

    eng._patch_coordinator = Spy()
    eng._episodic_ref = Ep()
    eng._procedure_extractor = Extractor()
    report = eng.dream_now("manual")
    assert report.procedure_extraction == "report"
    assert Spy.called == 0 and Extractor.ran == 1


# ─── 2. MCP 排程啟動移出 registry 鎖 ─────────────────────────────────────────


@pytest.fixture
def srv(monkeypatch):
    """載入 mcp_server.server；環境缺 starlette 等依賴時以最小假 SDK 取代 mcp.server。"""
    import importlib
    import sys
    import types

    try:
        return importlib.import_module("mcp_server.server")
    except ImportError:
        pass
    for name in [m for m in sys.modules if m == "mcp_server" or m.startswith("mcp_server.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    # CI 未安裝 mcp SDK：連頂層套件與 mcp.types 一併以最小假模組取代
    try:
        import mcp  # noqa: F401
    except ImportError:
        fake_pkg = types.ModuleType("mcp")
        fake_pkg.__path__ = []
        monkeypatch.setitem(sys.modules, "mcp", fake_pkg)
    class _AnyAttrModule(types.ModuleType):
        """任何屬性都回傳一個可建構的空類別（供 mcp.types 的各種型別 import）。"""

        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            cls = type(name, (), {"__init__": lambda self, *a, **k: None})
            setattr(self, name, cls)
            return cls

    fake_types = _AnyAttrModule("mcp.types")
    fake = types.ModuleType("mcp.server")
    fake.__path__ = []
    fake.Server = type("Server", (), {"__init__": lambda self, *a, **k: None})
    fake_stdio = types.ModuleType("mcp.server.stdio")
    fake_stdio.stdio_server = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "mcp.types", fake_types)
    monkeypatch.setitem(sys.modules, "mcp.server", fake)
    monkeypatch.setitem(sys.modules, "mcp.server.stdio", fake_stdio)
    module = importlib.import_module("mcp_server.server")
    for name in [m for m in sys.modules if m == "mcp_server" or m.startswith("mcp_server.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    return module


def test_mcp_scheduler_started_in_background_once_and_failure_safe(srv, monkeypatch):
    started = threading.Event()
    calls = []

    class FakeEngine:
        def start_dream_scheduler(self):
            calls.append(threading.current_thread().name)
            started.set()
            return {"started": True, "reason": ""}

    class Boom:
        def start_dream_scheduler(self):
            raise RuntimeError("embedding 載入失敗")

    monkeypatch.setattr(srv, "_scheduled_engines", [])
    monkeypatch.setattr(srv, "_schedule_requested", set())
    eng = FakeEngine()
    srv._ensure_dream_scheduler(eng, "alpha")
    srv._ensure_dream_scheduler(eng, "alpha")
    assert started.wait(2)
    time.sleep(0.1)
    assert len(calls) == 1 and calls[0].startswith("dream-scheduler-")  # 背景執行緒，且只一次
    assert srv._scheduled_engines == [eng]
    # 唯讀場域 namespace 不啟動
    srv._ensure_dream_scheduler(FakeEngine(), "scope-g42")
    time.sleep(0.1)
    assert len(calls) == 1
    # 啟動失敗只記 log，不拋例外
    srv._start_dream_scheduler_safe(Boom(), "beta")


def test_create_engine_does_not_start_scheduler(srv, monkeypatch):
    class Fake:
        started = 0

        def __init__(self, *a, **k):
            pass

        def start_dream_scheduler(self):
            Fake.started += 1
            return {"started": True, "reason": ""}

    monkeypatch.setattr(srv, "MemoryEngine", Fake)
    srv._create_engine("alpha")
    assert Fake.started == 0


# ─── 10. Hypothesis ──────────────────────────────────────────────────────────


def _pair(e, tag):
    ep_a = user_ep(e.engine, f"{tag} 甲", speaker="u1")
    ep_b = user_ep(e.engine, f"{tag} 乙", speaker="u1")
    a = make_concept(e, f"{tag}甲", [ep_a])
    b = make_concept(e, f"{tag}乙", [ep_b])
    make_bridge(e, a, b, confirm=2)
    return a, b


def test_open_hypothesis_expires_directly_on_contradicts(tmp_path):
    e = build_env(tmp_path)
    a, b = _pair(e, "矛盾")
    e.phase.propose()
    hid = e.store.get_by_pair(a, b).id
    # 沒有 CONTRADICTS 時 Gate 不放行
    ok, _ = e.gate.process_hypothesis_patch(
        "expire", {"hypothesis_id": hid, "reason": "pair_contradicts"},
    )
    assert not ok
    assert e.graph.add_contradicts_edge(a, b, 0.8) is True
    report = e.phase.maintain()
    h = e.store.get(hid)
    assert report.expired == 1 and h.status == "expired"
    assert h.outcome.expire_reason == "pair_contradicts"


def test_explicit_namespace_id_overrides_path_inference(tmp_path):
    e = build_env(tmp_path, llm=lambda _p: "{}")  # 路徑上的 namespace 是 ns1（非共享）
    a, b = _pair(e, "注入")
    e.phase._namespace_id = "scope-g9"
    assert e.phase.run().skipped_reason == "shared_namespace"
    e.reviewer._namespace_id = "scope-g9"
    assert e.reviewer.review("hyp_x").reason == "shared_namespace"
    e.gate.namespace_id = "scope-g9"
    from echo_memory.storage.conflict_ledger import compute_pair_digest

    digest = compute_pair_digest(e.engine.semantic.get(a), e.engine.semantic.get(b))
    ok, _ = e.gate.process_hypothesis_patch(
        "propose", {"concept_a": a, "concept_b": b, "pair_digest": digest},
    )
    assert not ok


def test_engine_injects_namespace_id_into_gate(tmp_path):
    e = build_env(tmp_path, namespace_id="scope-g42")
    assert e.gate.namespace_id == "scope-g42"
