"""Dream 排程：明確啟動、總開關、互動重置閒置、停止、跨行程租約與 cron 窗口去重。"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from echo_memory import MemoryEngine
from echo_memory.config import get_config
from echo_memory.dream.engine import DreamEngine
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


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """建立 MemoryEngine，並把 DreamEngine 內的排程器換成 FakeScheduler。"""
    fake = FakeScheduler()
    original_init = DreamEngine.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._scheduler = fake

    monkeypatch.setattr(DreamEngine, "__init__", patched_init)
    # 固定排程開關，不受本機 .env（例如 DREAM_ENABLED=false）影響
    cfg = get_config()
    monkeypatch.setattr(cfg, "dream_enabled", True)
    monkeypatch.setattr(cfg, "dream_enable_idle", True)
    monkeypatch.setattr(cfg, "dream_enable_cron", True)
    eng = MemoryEngine(storage_path=tmp_path)
    eng._fake_scheduler = fake
    return eng


def test_no_start_no_schedule(engine):
    """沒呼叫 start_dream_scheduler，即使 dream() 也不會排程。"""
    result = engine.dream(triggered_by="manual")
    assert result["error"] is None
    assert engine._dream_engine is not None
    assert engine._fake_scheduler.start_calls == 0
    assert engine._fake_scheduler.jobs == {}


def test_start_registers_jobs(engine):
    result = engine.start_dream_scheduler()
    assert result == {"started": True, "reason": "started"}
    fake = engine._fake_scheduler
    assert fake.running and fake.start_calls == 1
    assert {"idle_check", "cron_dream"} <= set(fake.jobs)
    # 再呼叫不重複啟動
    assert engine.start_dream_scheduler()["reason"] == "already_running"
    assert fake.start_calls == 1


def test_dream_enabled_false_blocks_start(engine, monkeypatch):
    monkeypatch.setattr(get_config(), "dream_enabled", False)
    result = engine.start_dream_scheduler()
    assert result == {"started": False, "reason": "disabled"}
    assert engine._dream_engine is None
    assert engine._fake_scheduler.start_calls == 0


def test_stop_shuts_down(engine):
    engine.start_dream_scheduler()
    engine.stop_dream_scheduler()
    assert engine._fake_scheduler.shutdown_calls == 1
    assert not engine._fake_scheduler.running
    # 未建立 DreamEngine 時 stop 無動作
    MemoryEngine(storage_path=engine.storage_root, namespace_id="other").stop_dream_scheduler()


def test_interaction_resets_idle(engine):
    """store_episode／retrieve 會重置閒置計時；DreamEngine 未建時先自記，建立後帶入。"""
    engine.ensure_initialized()
    assert engine._last_interaction is None
    engine.store_episode("你好", "嗨", session_id="s1")
    assert engine._last_interaction is not None
    first = engine._last_interaction

    dream_engine = engine._ensure_dream_engine()
    assert dream_engine._last_interaction == first  # 建構時帶入自記的互動時間

    # 把閒置計時撥舊，retrieve 後應被重置
    dream_engine._last_interaction = datetime.now(timezone.utc) - timedelta(hours=2)
    engine.retrieve("你好")
    idle = (datetime.now(timezone.utc) - dream_engine._last_interaction).total_seconds()
    assert idle < 60


def test_idle_check_uses_notified_time(engine, monkeypatch):
    engine.ensure_initialized()
    dream_engine = engine._ensure_dream_engine()
    calls = []
    monkeypatch.setattr(dream_engine, "_trigger_dream", lambda by: calls.append(by))
    dream_engine._last_interaction = datetime.now(timezone.utc) - timedelta(
        minutes=dream_engine.idle_minutes + 1
    )
    dream_engine._idle_check()
    assert calls == ["idle"]
    dream_engine.notify_interaction()
    dream_engine._idle_check()
    assert calls == ["idle"]


# ── 租約 ──────────────────────────────────────────────────────────────


def test_lease_mutual_exclusion(tmp_path):
    a = FileLease(tmp_path, "dream", ttl_seconds=60)
    b = FileLease(tmp_path, "dream", ttl_seconds=60)
    assert a.try_acquire("idle") == (True, "")
    ok, reason = b.try_acquire("manual")
    assert not ok and "租約" in reason
    a.release(completed=True)
    assert b.try_acquire("manual")[0]
    state = b.read_state()
    assert state["holder_pid"] > 0 and state["last_completed_at"] > 0


def test_lease_mutual_exclusion_across_processes(tmp_path):
    """另一個行程持有租約時，本行程取不到。"""
    script = textwrap.dedent(
        """
        import sys, time
        from echo_memory.storage.locking import FileLease
        lease = FileLease(sys.argv[1], "dream", ttl_seconds=60)
        assert lease.try_acquire("child")[0]
        print("held", flush=True)
        time.sleep(float(sys.argv[2]))
        """
    )
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), "5"],
        stdout=subprocess.PIPE, text=True, cwd=root,
    )
    try:
        assert proc.stdout.readline().strip() == "held"
        mine = FileLease(tmp_path, "dream", ttl_seconds=60)
        ok, reason = mine.try_acquire("idle")
        assert not ok and "持有" in reason
    finally:
        proc.kill()
        proc.wait()


def test_lease_expired_takeover(tmp_path):
    a = FileLease(tmp_path, "dream", ttl_seconds=10)
    b = FileLease(tmp_path, "dream", ttl_seconds=10)
    t0 = time.time()
    assert a.try_acquire("idle", now=t0)[0]
    assert not b.try_acquire("idle", now=t0 + 5)[0]
    assert b.try_acquire("idle", now=t0 + 11)[0]  # a 崩潰、租約過期後可接手
    assert b.read_state()["holder_token"] == b.token
    # 原持有者遲到釋放不應清掉新持有者
    a.release(completed=True)
    assert b.read_state()["holder_token"] == b.token


def test_cron_same_window_skipped(tmp_path):
    a = FileLease(tmp_path, "dream", ttl_seconds=60)
    b = FileLease(tmp_path, "dream", ttl_seconds=60)
    t0 = time.time()
    assert a.try_acquire(
        "cron", now=t0, skip_if_completed_within=900,
        completion_key="last_cron_completed_at",
    )[0]
    a.release(
        completed=True,
        completion_keys=("last_completed_at", "last_cron_completed_at"),
        now=t0 + 30,
    )
    ok, reason = b.try_acquire(
        "cron", now=t0 + 60, skip_if_completed_within=900,
        completion_key="last_cron_completed_at",
    )
    assert not ok and "last_cron_completed_at" in reason
    # 窗口外（隔天）可再執行
    assert b.try_acquire(
        "cron", now=t0 + 86400, skip_if_completed_within=900,
        completion_key="last_cron_completed_at",
    )[0]


def test_dream_skips_when_lease_held(engine):
    """手動 dream() 取不到租約：回報 skipped_reason，不阻塞也不當作錯誤。"""
    engine.ensure_initialized()
    other = FileLease(engine.storage_path, "dream", ttl_seconds=60)
    assert other.try_acquire("other")[0]
    result = engine.dream(triggered_by="manual")
    assert result["skipped_reason"]
    assert result["error"] is None
    other.release()
    result = engine.dream(triggered_by="manual")
    assert result["skipped_reason"] is None
    # 完成後租約已釋放並記錄完成時間
    state = other.read_state()
    assert not state.get("holder_token") and state["last_completed_at"] > 0


def test_cron_cycle_dedup_in_dream_engine(engine):
    """同一窗口內第二次 cron 觸發被跳過，idle 觸發不受窗口限制。"""
    engine.ensure_initialized()
    first = engine.dream(triggered_by="cron")
    assert first["skipped_reason"] is None
    second = engine.dream(triggered_by="cron")
    assert second["skipped_reason"]
    third = engine.dream(triggered_by="idle")
    assert third["skipped_reason"] is None
