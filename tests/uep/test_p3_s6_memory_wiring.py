"""P3 S6：記憶端真實接線（不需 GPU 的部分）。

以 fake engine／fake adapter 固定生命週期與 EM #619 的約束：單一 adapter、寫入經 adapter、per-user 關自己的排程、
scope-g 掛 resolver 且不啟排程、close 逐個呼叫（在 adapter 排空之後）、dream listener 計數。
真 engine 的煙霧測試在缺 ``echo_memory`` 依賴時跳過（回報會說明缺什麼）。
"""
from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from tests.uep.test_p3_s2_memory_host import (
    OWNER_NS,
    PRIVATE,
    SCOPE_NS,
    SPECS,
    make_identity,
)
from uep.fakes.memory import FakeAdapterFactory, FakeEngineFactory, FakeGuildMembership
from uep.memory import MemoryHost


class RecordingBackend:
    """只提供宣告；有 adapter 時 Access Layer 不會呼叫 ``execute``（呼叫了就失敗）。"""

    def definitions(self) -> list[dict[str, Any]]:
        return [
            {"type": "function", "function": {"name": "deep_recall", "parameters": {"properties": {"query": {}}}}},
            {"type": "function", "function": {"name": "force_remember", "parameters": {"properties": {"content": {}}}}},
        ]

    def execute(self, engine: Any, name: str, args: Mapping[str, Any], session_id: str) -> dict[str, Any]:
        raise AssertionError("有 adapter 時不得走 backend.execute（寫入必須經 adapter）")


def make_wired_host(tmp_path: Path, *, adapters: bool = True, **over: Any) -> tuple[MemoryHost, FakeEngineFactory, FakeAdapterFactory]:
    factory, adapter_factory = FakeEngineFactory(), FakeAdapterFactory()
    kwargs: dict[str, Any] = {
        "engine_factory": factory, "data_root": tmp_path / "core_memory", "namespaces": SPECS,
        "identity": make_identity(), "llm_fn": lambda prompt: "ok", "drain_timeout_s": 1.0,
        "guild_membership": FakeGuildMembership({"123": {"actor_a": "111"}}), "tool_backend": RecordingBackend(),
    }
    if adapters:
        kwargs["adapter_factory"] = adapter_factory
    kwargs.update(over)
    return MemoryHost(**kwargs), factory, adapter_factory


@pytest.fixture
def wired(tmp_path):
    host, factory, adapters = make_wired_host(tmp_path)
    host.start()
    yield host, factory, adapters
    host.stop()


# ---- 做夢：per-user 與 scope-g 都不啟自己的排程 ----

def test_default_host_disables_engine_schedule_explicitly_and_never_starts_it(wired):
    host, factory, _ = wired
    for ns in (OWNER_NS, SCOPE_NS):
        calls = factory.calls(ns)
        assert "start_dream_scheduler" not in calls
        assert factory.engines[ns].dream_overrides == {"dream_enable_idle": False, "dream_enable_cron": False}
        # configure_dream 必須在任何會建立 DreamEngine 的呼叫之前（EM：太晚呼叫不套用）
        assert calls.index("configure_dream") < calls.index("set_llm_fn")
    assert host.start_report.namespaces[OWNER_NS]["scheduler"] == {"started": False, "reason": "disabled_by_host"}
    assert host.start_report.namespaces[SCOPE_NS]["scheduler"]["reason"] == "scope_namespace"


def test_scope_engine_gets_resolver_and_user_engine_does_not(wired):
    _, factory, _ = wired
    assert "set_engine_resolver" in factory.calls(SCOPE_NS)
    assert "set_engine_resolver" not in factory.calls(OWNER_NS)
    resolver = factory.engines[SCOPE_NS].engine_resolver
    assert resolver("owner") is factory.engines[OWNER_NS]   # owner_id
    assert resolver("111") is factory.engines[OWNER_NS]     # discord 平台 uid -> actor -> owner
    assert resolver("999") is None and resolver(SCOPE_NS) is None  # 未知與共享 engine 一律拒絕


def test_dream_listener_installed_on_user_engine_and_counts_reports(wired):
    host, factory, _ = wired
    assert "set_dream_listener" in factory.calls(OWNER_NS)
    engine = factory.engines[OWNER_NS]
    engine.report_dream({"status": "completed", "triggered_by": "host-loop"})
    engine.report_dream({"status": "skipped", "triggered_by": "host-loop", "skipped_reason": "lease held by pid=1"})
    engine.report_dream({"status": "skipped", "triggered_by": "host-loop", "skipped_reason": "pending 不足"})
    engine.report_dream({"status": "error", "triggered_by": "host-loop", "error": "boom"})
    assert host.metrics.get("memory.dream.runs.host_loop") == 1
    assert host.metrics.get("memory.dream.skipped_lease") == 1
    assert host.metrics.get("memory.dream.skipped") == 1
    assert host.metrics.get("memory.dream.error") == 1


def test_dream_listener_never_raises(wired):
    _, factory, _ = wired
    factory.engines[OWNER_NS].report_dream({"status": None, "triggered_by": object()})  # 不合規報告也不得拋出


# ---- 單一 adapter ----

def test_exactly_one_adapter_per_engine_even_after_idempotent_restart(wired):
    host, _, adapters = wired
    host.start()  # 冪等，不重建
    assert {ns: len(v) for ns, v in adapters.created.items()} == {OWNER_NS: 1, SCOPE_NS: 1}
    assert adapters.created[OWNER_NS][0].engine_lock is host.engine_lock(OWNER_NS)  # 與 Host 讀取路徑同一把鎖
    assert host.adapter(OWNER_NS) is adapters.created[OWNER_NS][0]


# ---- 寫入經 adapter ----

def test_store_goes_through_adapter_and_scans_privacy_after_reply(wired):
    host, factory, adapters = wired
    adapter = adapters.created[OWNER_NS][0]
    adapter.unit_ids = ["u1", "u2"]
    result = host.access.store(actor_id="actor_a", user_text="我愛拉麵", spoken_text="好喔", session_id="s1",
                               idempotency_key="k1")
    assert result.status == "stored" and result.episode_id == "ep0001"
    assert factory.engines[OWNER_NS].store_calls == 1 and factory.engines[OWNER_NS].episodes == ["我愛拉麵"]
    deadline = time.monotonic() + 2
    while not adapter.scans and time.monotonic() < deadline:
        time.sleep(0.01)
    assert adapter.scans == [["u1", "u2"]]


def test_store_without_unit_ids_skips_privacy_scan(wired):
    host, _, adapters = wired
    host.access.store(actor_id="actor_a", user_text="嗨", spoken_text="嗨", session_id="s1")
    time.sleep(0.1)
    assert adapters.created[OWNER_NS][0].scans == []


def test_adapter_store_failure_is_reported_as_failed(wired):
    host, factory, adapters = wired
    adapters.created[OWNER_NS][0].store_results_none = True  # 真 adapter 的 store 是旁路：失敗吞例外回 None
    result = host.access.store(actor_id="actor_a", user_text="嗨", spoken_text="嗨", session_id="s1")
    assert result.status == "failed" and factory.engines[OWNER_NS].store_calls == 0
    assert host.metrics.get("memory.store.failed") == 1


def test_adapter_store_timeout_is_bounded(wired):
    host, factory, _ = wired
    factory.engines[OWNER_NS].store_delay_s = 0.5
    result = host.access.store(actor_id="actor_a", user_text="嗨", spoken_text="嗨", session_id="s1", timeout_s=0.05)
    assert result.status == "timeout"


def test_guest_store_never_reaches_adapter(wired):
    host, factory, _ = wired
    assert host.access.store(actor_id="actor_b", user_text="嗨", spoken_text="嗨").status == "rejected"
    assert factory.engines[OWNER_NS].store_calls == 0


def test_force_remember_runs_on_adapter_with_session_and_filtered_args(wired):
    host, _, adapters = wired
    adapter = adapters.created[OWNER_NS][0]
    out = host.access.execute_tool(
        "force_remember", {"content": "他住台北", "evil": "x"}, actor_id="actor_a", audience=PRIVATE, session_id="s9"
    )
    assert out["success"] is True
    assert adapter.tool_calls == [("force_remember", {"content": "他住台北"})]  # 未宣告的引數被過濾
    assert adapter.sessions == ["s9"]


def test_tool_denied_for_public_audience_does_not_touch_adapter(wired):
    host, _, adapters = wired
    out = host.access.execute_tool("deep_recall", {"query": "x"}, actor_id="actor_a", audience="scope_public")
    assert out["code"] == "denied" and adapters.created[OWNER_NS][0].tool_calls == []


def test_without_adapter_factory_store_falls_back_to_engine(tmp_path):
    host, factory, _ = make_wired_host(tmp_path, adapters=False)
    host.start()
    try:
        assert host.access.store(actor_id="actor_a", user_text="嗨", spoken_text="嗨", session_id="s").status == "stored"
        assert host.adapter(OWNER_NS) is None and factory.engines[OWNER_NS].store_calls == 1
    finally:
        host.stop()


# ---- 關閉 ----

def test_stop_drains_adapters_before_closing_every_engine_once(tmp_path):
    host, factory, adapters = make_wired_host(tmp_path)
    host.start()
    host.stop()
    host.stop()  # 冪等
    for ns in (OWNER_NS, SCOPE_NS):
        names = factory.calls(ns)
        assert names.count("close") == 1 and factory.engines[ns].close_timeouts == [1.0]
        assert names.index("stop_dream_scheduler") < names.index("adapter.aclose") < names.index("close")
        assert adapters.created[ns][0].closed
    assert not host.started


def test_one_failing_close_does_not_block_other_engines(tmp_path):
    host, factory, _ = make_wired_host(tmp_path)
    host.start()
    factory.engines[SCOPE_NS].fail_on = {"close"}
    host.stop()
    assert factory.engines[OWNER_NS].closed and factory.engines[SCOPE_NS].closed  # 後者 close 拋例外但已標記，前者仍被關閉


def test_start_failure_closes_started_engines_and_adapters(tmp_path):
    factory, adapters = FakeEngineFactory(), FakeAdapterFactory()

    def flaky(namespace_id: str, data_root: Path) -> Any:
        if namespace_id == SCOPE_NS:
            raise RuntimeError("engine 建立失敗")
        return factory(namespace_id, data_root)

    host, _, _ = make_wired_host(tmp_path, engine_factory=flaky, adapter_factory=adapters)
    with pytest.raises(RuntimeError):
        host.start()
    assert factory.engines[OWNER_NS].closed and adapters.created[OWNER_NS][0].closed and not host.started


# ---- 真 engine（缺依賴時跳過）----

def test_wiring_module_imports_without_memory_deps():
    from uep.echo import memory_wiring

    assert callable(memory_wiring.build_memory_wiring)


def test_real_engine_smoke_through_host(tmp_path):
    pytest.importorskip("echo_memory")
    pytest.importorskip("llm_integration")
    from uep.echo.memory_wiring import build_memory_wiring

    wiring = build_memory_wiring(assess_mode="off")
    host = MemoryHost(
        data_root=tmp_path / "core_memory", namespaces=SPECS, identity=make_identity(),
        guild_membership=FakeGuildMembership({"123": {"actor_a": "111"}}), drain_timeout_s=5.0, **wiring.host_kwargs(),
    )
    host.start()
    try:
        assert host.start_report.namespaces[OWNER_NS]["scheduler"]["started"] is False
        defs = host.access.tool_definitions()
        assert {d["function"]["name"] for d in defs} >= {"deep_recall", "force_remember", "member_recall"}
        stored = host.access.store(actor_id="actor_a", user_text="我喜歡拉麵", spoken_text="記住了", session_id="s1",
                                   idempotency_key="smoke-1")
        assert stored.status == "stored"
        assert host.access.retrieve(PRIVATE, "拉麵", PRIVATE, actor_id="actor_a").memory_status.value in {"ok", "degraded_no_memory"}
    finally:
        host.stop()
    assert host.adapter(OWNER_NS) is None
