"""宿主整合介面（S2 MemoryHost）：Gate 開關、事件開關、per-engine dream 設定、close、
scope namespace、dream listener、租約狀態，以及維持現狀的 scope_ref／冪等 key 契約。"""

from __future__ import annotations

import os
import time

import pytest

from echo_memory import MemoryEngine
from echo_memory.affect.concept_inhibitor import ConceptInhibitor
from echo_memory.affect.event_emitter import MemoryEventEmitter
from echo_memory.config import get_config
from echo_memory.dream.engine import DreamEngine
from echo_memory.schemas.events import scope_ref_for
from echo_memory.storage.locking import FileLease


class FakeScheduler:
    """取代 APScheduler BackgroundScheduler，不啟動真執行緒。"""

    def __init__(self) -> None:
        self.running = False
        self.jobs: dict[str, object] = {}
        self.start_calls = 0
        self.shutdown_calls = 0

    def add_job(self, func, trigger=None, id=None, **kwargs):
        self.jobs[id] = func

    def start(self) -> None:
        self.running = True
        self.start_calls += 1

    def shutdown(self, wait: bool = True) -> None:
        self.running = False
        self.shutdown_calls += 1


class CollectSink:
    def __init__(self) -> None:
        self.drafts: list = []

    def emit(self, draft) -> None:
        self.drafts.append(draft)


@pytest.fixture
def fake_scheduler(monkeypatch):
    fake = FakeScheduler()
    original_init = DreamEngine.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._scheduler = fake

    monkeypatch.setattr(DreamEngine, "__init__", patched_init)
    cfg = get_config()
    monkeypatch.setattr(cfg, "dream_enabled", True)
    monkeypatch.setattr(cfg, "dream_enable_idle", True)
    monkeypatch.setattr(cfg, "dream_enable_cron", True)
    return fake


@pytest.fixture
def engine(tmp_path, fake_scheduler):
    eng = MemoryEngine(storage_path=tmp_path)
    eng._fake_scheduler = fake_scheduler
    return eng


# ─── B-1：set_concept_inhibitor opt_out ───────────────────────────────────────


def test_inhibitor_none_default_is_opt_out(engine):
    engine.set_concept_inhibitor(None)
    engine.ensure_initialized()
    assert engine._concept_inhibitor is None  # 刻意無 Gate，不自動補預設


def test_inhibitor_none_explicit_opt_out_true(engine):
    engine.set_concept_inhibitor(None, opt_out=True)
    engine.ensure_initialized()
    assert engine._concept_inhibitor is None


def test_inhibitor_none_opt_out_false_recreates_default(engine):
    engine.ensure_initialized()
    first = engine._concept_inhibitor
    assert isinstance(first, ConceptInhibitor)  # 預設 Gate

    engine.set_concept_inhibitor(None, opt_out=False)
    assert engine._concept_inhibitor is None  # 只是清掉
    engine.ensure_initialized()
    second = engine._concept_inhibitor
    assert isinstance(second, ConceptInhibitor) and second is not first


def test_inhibitor_opt_out_false_before_init_still_builds_default(engine):
    engine.set_concept_inhibitor(None, opt_out=False)
    engine.ensure_initialized()
    assert isinstance(engine._concept_inhibitor, ConceptInhibitor)


def test_inhibitor_explicit_instance_unchanged(engine):
    engine.ensure_initialized()
    gate = ConceptInhibitor(semantic=engine.semantic, llm_fn=None)
    engine.set_concept_inhibitor(gate)
    assert engine._concept_inhibitor is gate
    assert gate.event_emitter is engine.event_emitter


# ─── B-2：事件開關 ───────────────────────────────────────────────────────────


def test_emitter_default_off_without_global_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(get_config(), "self_model_events_enabled", False)
    eng = MemoryEngine(storage_path=tmp_path)
    sink = CollectSink()
    eng.set_candidate_sink(sink)
    eng.event_emitter.emit_dream_completed("c1")
    assert eng.event_emitter.drain()
    assert sink.drafts == []


def test_emitter_enabled_fn_via_constructor_overrides_global(tmp_path, monkeypatch):
    monkeypatch.setattr(get_config(), "self_model_events_enabled", False)
    eng = MemoryEngine(storage_path=tmp_path, event_emitter_enabled_fn=lambda: True)
    sink = CollectSink()
    eng.set_candidate_sink(sink)
    eng.event_emitter.emit_dream_completed("c1")
    assert eng.event_emitter.drain()
    assert len(sink.drafts) == 1
    assert sink.drafts[0].event_type == "memory.dream_completed"


def test_emitter_enabled_fn_can_turn_off_despite_global_on(tmp_path, monkeypatch):
    monkeypatch.setattr(get_config(), "self_model_events_enabled", True)
    eng = MemoryEngine(storage_path=tmp_path, event_emitter_enabled_fn=lambda: False)
    sink = CollectSink()
    eng.set_candidate_sink(sink)
    eng.event_emitter.emit_dream_completed("c1")
    assert eng.event_emitter.drain()
    assert sink.drafts == []


def test_emitter_set_enabled_fn_runtime_and_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(get_config(), "self_model_events_enabled", False)
    em = MemoryEventEmitter("ns1")
    sink = CollectSink()
    em.set_sink(sink)
    flag = {"on": False}
    em.set_enabled_fn(lambda: flag["on"])
    em.emit_dream_completed("c1")
    em.drain()
    assert sink.drafts == []
    flag["on"] = True
    em.emit_dream_completed("c2")
    em.drain()
    assert len(sink.drafts) == 1
    # 還原為全域設定（False）
    em.set_enabled_fn(None)
    em.emit_dream_completed("c3")
    em.drain()
    assert len(sink.drafts) == 1


def test_emitter_enabled_fn_exception_means_off():
    em = MemoryEventEmitter("ns1")
    sink = CollectSink()
    em.set_sink(sink)

    def boom() -> bool:
        raise RuntimeError("x")

    em.set_enabled_fn(boom)
    em.emit_dream_completed("c1")
    em.drain()
    assert sink.drafts == []


def test_emitter_scope_namespace_never_emits_even_if_enabled(tmp_path):
    eng = MemoryEngine(
        storage_path=tmp_path, namespace_id="scope-g1", event_emitter_enabled_fn=lambda: True,
    )
    sink = CollectSink()
    eng.set_candidate_sink(sink)
    eng.event_emitter.emit_dream_completed("c1")
    eng.event_emitter.drain()
    assert sink.drafts == []


# ─── B-3：configure_dream ────────────────────────────────────────────────────


def test_configure_dream_applies_per_engine_and_keeps_global(engine):
    cfg = get_config()
    before = (cfg.dream_enable_idle, cfg.dream_min_pending, cfg.dream_cron)
    result = engine.configure_dream(
        dream_enable_idle=False, min_pending=2, dream_cron="5 4 * * *",
        daydream_pending=0, idle_minutes=7, cron_require_pending=False,
    )
    assert result["applied"] is True
    assert engine.start_dream_scheduler() == {"started": True, "reason": "started"}
    de = engine._dream_engine
    assert de.enable_idle is False
    assert de.min_pending == 2
    assert de.cron_schedule == "5 4 * * *"
    assert de.daydream_pending == 0
    assert de.idle_minutes == 7
    assert de.cron_require_pending is False
    assert de.enable_cron is cfg.dream_enable_cron  # 沒給的鍵沿用全域
    assert (cfg.dream_enable_idle, cfg.dream_min_pending, cfg.dream_cron) == before
    jobs = engine._fake_scheduler.jobs
    assert "cron_dream" in jobs and "idle_check" not in jobs


def test_configure_dream_enabled_false_blocks_start(engine):
    engine.configure_dream(enabled=False)
    assert engine.start_dream_scheduler() == {"started": False, "reason": "disabled"}
    assert engine._dream_engine is None


def test_configure_dream_no_trigger(engine):
    engine.configure_dream(enable_idle=False, enable_cron=False, daydream_pending=0)
    assert engine.start_dream_scheduler() == {"started": False, "reason": "no_trigger_enabled"}


def test_configure_dream_is_independent_between_engines(tmp_path, fake_scheduler):
    a = MemoryEngine(storage_path=tmp_path, namespace_id="a")
    b = MemoryEngine(storage_path=tmp_path, namespace_id="b")
    a.configure_dream(min_pending=1)
    b.configure_dream(min_pending=9)
    assert a.dream_readiness()["min_pending"] == 1
    assert b.dream_readiness()["min_pending"] == 9


def test_configure_dream_unknown_key_raises_and_applies_nothing(engine):
    with pytest.raises(ValueError):
        engine.configure_dream(min_pending=3, no_such_key=1)
    assert engine._dream_overrides == {}
    with pytest.raises(ValueError):
        engine.configure_dream(dream_enabled="yes")  # 型別錯誤
    with pytest.raises(ValueError):
        engine.configure_dream(min_pending="abc")


def test_configure_dream_none_withdraws_override(engine):
    engine.configure_dream(min_pending=3)
    result = engine.configure_dream(min_pending=None)
    assert result["overrides"] == {}


def test_configure_dream_too_late_is_not_applied(engine, caplog):
    engine.start_dream_scheduler()  # DreamEngine 已建立
    with caplog.at_level("WARNING", logger="echo_memory.engine"):
        result = engine.configure_dream(min_pending=1)
    assert result["applied"] is False
    assert engine._dream_overrides == {}
    assert engine._dream_engine.min_pending == get_config().dream_min_pending
    assert any("太晚" in r.getMessage() for r in caplog.records)


# ─── B-4：close ──────────────────────────────────────────────────────────────


def test_close_is_idempotent_and_blocks_further_use(engine):
    engine.store_episode("你好", "嗨", session_id="s1")
    engine.start_dream_scheduler()
    engine.close()
    engine.close()  # 冪等
    assert engine._fake_scheduler.shutdown_calls == 1
    with pytest.raises(RuntimeError):
        engine.store_episode("再一則", "好", session_id="s1")
    with pytest.raises(RuntimeError):
        engine.retrieve("你好")
    with pytest.raises(RuntimeError):
        engine.start_dream_scheduler()
    with pytest.raises(RuntimeError):
        engine.configure_dream(min_pending=1)
    engine.stop_dream_scheduler()  # 關閉後仍可安全呼叫


def test_close_without_initialization(engine):
    engine.close()
    assert engine._closed is True


def test_close_drains_events_and_persists_data(tmp_path, fake_scheduler, monkeypatch):
    eng = MemoryEngine(storage_path=tmp_path)
    eng.store_episode("記得這件事", "好的", session_id="s1", idempotency_key="k-close")
    order: list[str] = []
    monkeypatch.setattr(eng, "stop_dream_scheduler", lambda: order.append("stop"))
    monkeypatch.setattr(eng.event_emitter, "drain", lambda timeout=2.0: order.append("drain") or True)
    monkeypatch.setattr(eng, "_save_stores_on_close", lambda: order.append("save"))
    eng.close()
    assert order == ["stop", "drain", "save"]


def test_close_roundtrip_reload(tmp_path, fake_scheduler):
    eng = MemoryEngine(storage_path=tmp_path)
    eid = eng.store_episode("記得這件事", "好的", session_id="s1")
    eng.close()
    again = MemoryEngine(storage_path=tmp_path)
    assert eid
    assert again.get_stats()["total_episodes"] == 1
    assert (again.storage_path / "neurochem_state.json").exists()


def test_close_step_failure_does_not_block_others(engine, monkeypatch):
    called: list[str] = []

    def bad_stop():
        raise RuntimeError("stop 壞了")

    monkeypatch.setattr(engine, "stop_dream_scheduler", bad_stop)
    monkeypatch.setattr(engine, "_save_neurochem", lambda: called.append("nc"))
    engine.close()
    assert called == ["nc"]
    assert engine._closed


# ─── B-5：scope namespace 不排程 ─────────────────────────────────────────────


def test_scope_namespace_does_not_start_scheduler(tmp_path, fake_scheduler):
    eng = MemoryEngine(storage_path=tmp_path, namespace_id="scope-g123")
    assert eng.start_dream_scheduler() == {"started": False, "reason": "scope_namespace"}
    assert eng._dream_engine is None
    assert fake_scheduler.start_calls == 0


# ─── B-6：dream listener ─────────────────────────────────────────────────────


def _collect(engine):
    reports: list[dict] = []
    engine.set_dream_listener(reports.append)
    return reports


def test_listener_manual_completed(engine):
    reports = _collect(engine)
    result = engine.dream(triggered_by="manual")
    assert result["error"] is None
    assert len(reports) == 1
    r = reports[0]
    assert set(r) == {"triggered_by", "started_at", "finished_at", "skipped_reason", "error", "status"}
    assert r["status"] == "completed" and r["triggered_by"] == "manual"
    assert r["skipped_reason"] is None and r["error"] is None
    assert r["started_at"] and r["finished_at"] and r["started_at"] <= r["finished_at"]


def test_listener_skipped_when_lease_held_by_other(engine):
    engine.ensure_initialized()
    reports = _collect(engine)
    other = FileLease(engine.storage_path, "dream", ttl_seconds=60)
    ok, _ = other.try_acquire(owner="other")
    assert ok
    result = engine.dream(triggered_by="idle")
    assert result["skipped_reason"]
    assert len(reports) == 1
    assert reports[0]["status"] == "skipped"
    assert reports[0]["triggered_by"] == "idle"
    assert reports[0]["skipped_reason"]
    assert reports[0]["finished_at"]


def test_listener_error_status(engine, monkeypatch):
    engine.ensure_initialized()
    de = engine._ensure_dream_engine()
    reports = _collect(engine)

    def boom():
        raise RuntimeError("修剪壞了")

    monkeypatch.setattr(de._pruning, "update_edge_weights", boom)
    result = engine.dream(triggered_by="manual")
    assert result["error"]
    assert len(reports) == 1
    assert reports[0]["status"] == "error" and "修剪壞了" in reports[0]["error"]


def test_listener_exception_is_swallowed(engine, caplog):
    def bad(_report):
        raise RuntimeError("listener 爆了")

    engine.set_dream_listener(bad)
    with caplog.at_level("WARNING", logger="echo_memory.dream"):
        result = engine.dream(triggered_by="manual")
    assert result["error"] is None
    assert any("listener" in r.getMessage() for r in caplog.records)


def test_listener_none_cancels(engine):
    reports = _collect(engine)
    engine.set_dream_listener(None)
    engine.dream()
    assert reports == []


def test_listener_covers_scheduled_triggers(engine):
    engine.configure_dream(cron_require_pending=False)
    engine.ensure_initialized()
    de = engine._ensure_dream_engine()
    reports = _collect(engine)
    de._cron_tick()  # 排程 cron 路徑
    de._trigger_dream("idle")  # 排程 idle 路徑
    assert [r["triggered_by"] for r in reports] == ["cron", "idle"]
    assert all(r["status"] == "completed" for r in reports)
    # 同 cron 窗口再觸發：被跳過也要通知
    de._cron_tick()
    assert reports[-1]["triggered_by"] == "cron" and reports[-1]["status"] == "skipped"


def test_listener_set_after_dream_engine_built(engine):
    engine.ensure_initialized()
    engine._ensure_dream_engine()
    reports = _collect(engine)  # DreamEngine 已存在後才註冊
    engine.dream()
    assert len(reports) == 1


# ─── B-7：readiness 的租約欄位 ────────────────────────────────────────────────


def test_readiness_lease_free(engine):
    assert engine.dream_readiness()["lease"] == {
        "held_by_other": False, "holder_pid": None, "expires_at": None,
    }


def test_readiness_lease_held_by_other(engine):
    engine.ensure_initialized()
    other = FileLease(engine.storage_path, "dream", ttl_seconds=60)
    assert other.try_acquire(owner="other")[0]
    lease = engine.dream_readiness()["lease"]
    assert lease["held_by_other"] is True
    assert lease["holder_pid"] == os.getpid()
    assert lease["expires_at"] > time.time()


def test_readiness_lease_held_by_self_is_not_other(engine):
    engine.ensure_initialized()
    de = engine._ensure_dream_engine()
    assert de._lease.try_acquire(owner="self")[0]
    lease = engine.dream_readiness()["lease"]
    assert lease["held_by_other"] is False
    assert lease["holder_pid"] == os.getpid()


def test_readiness_lease_expired_is_free(engine):
    engine.ensure_initialized()
    other = FileLease(engine.storage_path, "dream", ttl_seconds=0.05)
    assert other.try_acquire(owner="other")[0]
    time.sleep(0.15)
    lease = engine.dream_readiness()["lease"]
    assert lease == {"held_by_other": False, "holder_pid": None, "expires_at": None}


def test_readiness_lease_is_read_only(engine):
    engine.ensure_initialized()
    other = FileLease(engine.storage_path, "dream", ttl_seconds=60)
    other.try_acquire(owner="other")
    before = other.read_state()
    engine.dream_readiness()
    assert other.read_state() == before


# ─── B-8：scope_ref 只產生 private（維持現狀） ───────────────────────────────


@pytest.mark.parametrize("ns", ["local-default", "echo-stream", "12345", "scope-g123"])
def test_scope_ref_is_always_private(ns):
    assert scope_ref_for(ns) == f"scope_private_{ns}"


@pytest.mark.parametrize("level", ["group", "public", "shared"])
def test_scope_ref_rejects_non_private(level):
    with pytest.raises(ValueError):
        scope_ref_for("ns1", level)


def test_emitted_drafts_carry_private_scope_ref(tmp_path):
    eng = MemoryEngine(storage_path=tmp_path, namespace_id="u1", event_emitter_enabled_fn=lambda: True)
    sink = CollectSink()
    eng.set_candidate_sink(sink)
    eng.event_emitter.emit_dream_completed("c1")
    eng.event_emitter.drain()
    assert [d.scope_ref for d in sink.drafts] == ["scope_private_u1"]


# ─── B-10：同 idempotency_key 回原 episode_id（維持現狀） ─────────────────────


def test_same_idempotency_key_returns_original_id(engine):
    first = engine.store_episode("你好", "嗨", session_id="s1", idempotency_key="host-key-1")
    second = engine.store_episode("你好", "嗨", session_id="s1", idempotency_key="host-key-1")
    assert first == second
    other = engine.store_episode("你好", "嗨", session_id="s1", idempotency_key="host-key-2")
    assert other != first
