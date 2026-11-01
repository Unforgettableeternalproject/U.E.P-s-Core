"""Dream 排程累積門檻：pending_count、idle／daydream／cron 觸發、readiness（M-14）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from echo_memory import MemoryEngine
from echo_memory.config import get_config
from echo_memory.dream.distillation import KnowledgeDistillation
from echo_memory.dream.engine import DreamEngine, DreamReport
from echo_memory.schemas.episode import Episode

HIGH = 0.9
LOW = 0.1


class FakeScheduler:
    """取代 APScheduler BackgroundScheduler，不啟動真執行緒。"""

    def __init__(self) -> None:
        self.running = False
        self.jobs: dict[str, object] = {}

    def add_job(self, func, trigger=None, id=None, **kwargs):
        self.jobs[id] = func

    def start(self) -> None:
        self.running = True

    def shutdown(self, wait: bool = True) -> None:
        self.running = False


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """固定 dream_* 設定（不受本機 .env 影響），並換掉排程器。"""
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
    monkeypatch.setattr(cfg, "dream_idle_minutes", 30)
    monkeypatch.setattr(cfg, "dream_min_pending", 5)
    monkeypatch.setattr(cfg, "dream_daydream_pending", 20)
    monkeypatch.setattr(cfg, "dream_cron_require_pending", True)
    eng = MemoryEngine(storage_path=tmp_path)
    eng._fake_scheduler = fake
    return eng


def _seed(eng, n: int, salience: float = HIGH, dreamed: bool = False) -> list[str]:
    """直接寫入 episode（不經評估器），可指定 salience 與 is_dreamed。"""
    eng.ensure_initialized()
    ids = []
    for i in range(n):
        ep = Episode(
            user_input=f"u{i}", agent_response=f"a{i}", session_id="s",
            content_summary=f"摘要 {i}", salience_score=salience, is_dreamed=dreamed,
        )
        eng.episodic.prepare_embedding(ep)
        ids.append(eng.episodic.write(ep))
    return ids


def _recorder(dream_engine, monkeypatch, error: str | None = None, skipped: str | None = None):
    """攔截 dream_now，記錄觸發來源。"""
    calls: list[str] = []

    def fake_dream_now(triggered_by: str = "manual") -> DreamReport:
        calls.append(triggered_by)
        return DreamReport(triggered_by=triggered_by, error=error, skipped_reason=skipped)

    monkeypatch.setattr(dream_engine, "dream_now", fake_dream_now)
    return calls


def _idle_for(dream_engine, minutes: float) -> datetime:
    dream_engine._last_interaction = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return dream_engine._last_interaction


# ── pending_count ─────────────────────────────────────────────────────


def test_pending_count_excludes_dreamed_and_low_salience(engine):
    _seed(engine, 3, HIGH)
    _seed(engine, 2, LOW)
    _seed(engine, 4, HIGH, dreamed=True)
    de = engine._ensure_dream_engine()
    assert de.pending_count() == 3
    # 門檻邊界：剛好等於蒸餾門檻算入
    _seed(engine, 1, KnowledgeDistillation.MIN_SALIENCE_THRESHOLD)
    assert de.pending_count() == 4


def test_pending_count_beyond_200(engine):
    """不受 list_episodes 200 筆分頁上限影響。"""
    _seed(engine, 230)
    assert engine._ensure_dream_engine().pending_count() == 230


def test_low_salience_still_stored(engine):
    """門檻只決定何時做夢，低 salience 照常存儲。"""
    _seed(engine, 3, LOW)
    assert engine.episodic.count() == 3


# ── idle ──────────────────────────────────────────────────────────────


def test_idle_without_material_does_not_run_and_resets_timer(engine, monkeypatch):
    _seed(engine, 2)  # < 5
    de = engine._ensure_dream_engine()
    calls = _recorder(de, monkeypatch)
    _idle_for(de, 31)
    assert de.scheduler_tick() is None
    assert calls == []
    assert (datetime.now(timezone.utc) - de._last_interaction).total_seconds() < 60
    # 計時已重置，下一個 tick 不再撞
    assert de.scheduler_tick() is None


def test_idle_with_material_runs(engine, monkeypatch):
    _seed(engine, 5)
    de = engine._ensure_dream_engine()
    calls = _recorder(de, monkeypatch)
    _idle_for(de, 31)
    assert de.scheduler_tick() == "idle"
    assert calls == ["idle"]


def test_idle_not_elapsed_does_not_run(engine, monkeypatch):
    _seed(engine, 10)
    de = engine._ensure_dream_engine()
    calls = _recorder(de, monkeypatch)
    _idle_for(de, 5)
    assert de.scheduler_tick() is None
    assert calls == []


def test_idle_min_pending_zero_runs_without_material(engine, monkeypatch):
    engine.ensure_initialized()
    de = engine._ensure_dream_engine()
    de.min_pending = 0
    calls = _recorder(de, monkeypatch)
    _idle_for(de, 31)
    assert de.scheduler_tick() == "idle"
    assert calls == ["idle"]


# ── daydream ──────────────────────────────────────────────────────────


def test_daydream_does_not_wait_for_idle(engine, monkeypatch):
    _seed(engine, 20)
    de = engine._ensure_dream_engine()
    calls = _recorder(de, monkeypatch)
    de.notify_interaction()  # 剛互動，完全沒閒置
    assert de.scheduler_tick() == "daydream"
    assert calls == ["daydream"]


def test_daydream_below_threshold_or_disabled(engine, monkeypatch):
    _seed(engine, 19)
    de = engine._ensure_dream_engine()
    calls = _recorder(de, monkeypatch)
    assert de.scheduler_tick() is None
    _seed(engine, 5)
    de.daydream_pending = 0  # 停用
    assert de.scheduler_tick() is None
    assert calls == []


def test_daydream_failure_backs_off_one_idle_period(engine, monkeypatch):
    _seed(engine, 25)
    de = engine._ensure_dream_engine()
    calls = _recorder(de, monkeypatch, error="boom")
    assert de.scheduler_tick() == "daydream"
    # 退避期內不再撞
    assert de.scheduler_tick(datetime.now(timezone.utc) + timedelta(minutes=29)) is None
    assert calls == ["daydream"]
    # 一個 idle 週期後再試
    assert de.scheduler_tick(datetime.now(timezone.utc) + timedelta(minutes=31)) == "daydream"
    assert calls == ["daydream", "daydream"]


def test_daydream_lease_skip_backs_off(engine, monkeypatch):
    _seed(engine, 25)
    de = engine._ensure_dream_engine()
    calls = _recorder(de, monkeypatch, skipped="lease_held")
    assert de.scheduler_tick() == "daydream"
    assert de.scheduler_tick(datetime.now(timezone.utc) + timedelta(minutes=1)) is None
    assert calls == ["daydream"]


def test_daydream_runs_real_cycle_with_lease_semantics(engine):
    """daydream 走真實 dream_now：租約語意同 idle（非 cron 窗口），連續兩次都能取得。"""
    _seed(engine, 3)
    de = engine._ensure_dream_engine()
    first = de.dream_now(triggered_by="daydream")
    second = de.dream_now(triggered_by="daydream")
    assert first.skipped_reason is None and first.triggered_by == "daydream"
    assert second.skipped_reason is None


# ── cron ──────────────────────────────────────────────────────────────


def test_cron_require_pending_skips_without_material(engine, monkeypatch):
    engine.ensure_initialized()
    de = engine._ensure_dream_engine()
    calls = _recorder(de, monkeypatch)
    de._cron_tick()
    assert calls == []
    _seed(engine, 1)
    de._cron_tick()
    assert calls == ["cron"]


def test_cron_not_require_pending_always_runs(engine, monkeypatch):
    engine.ensure_initialized()
    de = engine._ensure_dream_engine()
    de.cron_require_pending = False
    calls = _recorder(de, monkeypatch)
    de._cron_tick()
    assert calls == ["cron"]


def test_scheduler_jobs_registered_and_config_wired(engine):
    assert engine.start_dream_scheduler()["started"] is True
    de = engine._dream_engine
    assert (de.min_pending, de.daydream_pending, de.cron_require_pending) == (5, 20, True)
    assert engine._fake_scheduler.jobs["idle_check"] == de.scheduler_tick
    assert engine._fake_scheduler.jobs["cron_dream"] == de._cron_tick


# ── 手動不受限 ─────────────────────────────────────────────────────────


def test_manual_dream_ignores_thresholds(engine):
    engine.ensure_initialized()  # 0 個 pending
    result = engine.dream(triggered_by="manual")
    assert result["skipped_reason"] is None and result["error"] is None
    assert result["triggered_by"] == "manual"


# ── dream_readiness ───────────────────────────────────────────────────


def test_dream_readiness_shape_and_suggestions(engine):
    _seed(engine, 6)
    de = engine._ensure_dream_engine()
    _idle_for(de, 5)
    r = engine.dream_readiness()
    assert set(r) == {
        "pending", "salience_threshold", "min_pending", "daydream_pending",
        "suggested_trigger", "idle_seconds", "idle_threshold_seconds", "is_dreaming",
        "lease",
    }
    assert r["pending"] == 6
    assert r["salience_threshold"] == KnowledgeDistillation.MIN_SALIENCE_THRESHOLD
    assert (r["min_pending"], r["daydream_pending"]) == (5, 20)
    assert r["idle_threshold_seconds"] == 1800
    assert 290 <= r["idle_seconds"] <= 400
    assert r["suggested_trigger"] is None  # 累積夠但未閒置

    _idle_for(de, 31)
    assert engine.dream_readiness()["suggested_trigger"] == "idle"

    _seed(engine, 14)
    de.notify_interaction()
    assert engine.dream_readiness()["suggested_trigger"] == "daydream"


def test_dream_readiness_is_read_only(engine):
    _seed(engine, 25)
    engine.dream_readiness()
    assert engine._dream_engine.get_last_report() is None
    assert engine.episodic.count() == 25
