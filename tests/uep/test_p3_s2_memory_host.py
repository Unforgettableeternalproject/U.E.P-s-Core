"""P3 S2：MemoryHost（P3接入方案 §3、§5 S2 驗收）。

以 fake MemoryEngine 驗證：啟動順序（先注入再 start）、scope-g* 不啟排程、
private 記憶不進 public audience（VC-2-1）、memory.* 事件冪等、階段 X 目錄不相交核對。
純邏輯案例不需資料庫；journal／Activity 案例需 PostgreSQL（未設 UEP_TEST_DATABASE_URL 時 skip）。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.uep.memory_translate_support import load_samples
from uep.context.snapshot import MemoryStatus
from uep.events.bus import EventBus
from uep.events.publisher import EventPublisher
from uep.fakes.memory import FakeDraft, FakeEngineFactory, FakeGuildMembership
from uep.memory import (
    ForeignClaim,
    IdentityDirectory,
    MemoryHost,
    MemoryHostError,
    MemoryPort,
    MemorySink,
    NamespaceSpec,
)
from uep.memory.metrics import MemoryMetrics
from uep.tasks.models import ActivityRow

OWNER_NS = "core_owner"
SCOPE_NS = "scope-g123"
SPECS = (NamespaceSpec(OWNER_NS, owner_id="owner"), NamespaceSpec(SCOPE_NS))
PRIVATE = "scope_private_owner"
PUBLIC = "scope_public"
GROUP = "scope_group_g123"
SAMPLES = load_samples()


def make_identity() -> IdentityDirectory:
    return IdentityDirectory(
        platform_to_actor={"discord:111": "actor_a", "discord:222": "actor_b", "discord:333": "actor_c"},
        actor_to_owner={"actor_a": "owner", "actor_c": "other"},  # actor_b 沒有映射 -> 訪客
    )


def make_host(tmp_path: Path, factory: FakeEngineFactory | None = None, **over) -> tuple[MemoryHost, FakeEngineFactory]:
    factory = factory or FakeEngineFactory()
    kwargs = {
        "engine_factory": factory, "data_root": tmp_path / "core_memory", "namespaces": SPECS,
        "identity": make_identity(), "llm_fn": lambda prompt: "ok", "drain_timeout_s": 1.0,
        "guild_membership": FakeGuildMembership({"123": {"actor_a": "111"}}),  # actor_c 不是成員
        "enable_dream_scheduler": True,  # 舊路徑（engine 內建排程）供既有案例沿用；新預設見 test_p3_s6_memory_wiring
    }
    kwargs.update(over)
    return MemoryHost(**kwargs), factory


@pytest.fixture
def host_pair(tmp_path):
    host, factory = make_host(tmp_path)
    host.start()
    yield host, factory
    host.stop()


# ---- 啟動順序 ----

def test_inject_before_start_without_inhibitor_never_passes_none(tmp_path):
    host, factory = make_host(tmp_path)
    host.start()
    calls = factory.calls(OWNER_NS)
    assert calls.index("set_llm_fn") < calls.index("set_candidate_sink") < calls.index("start_dream_scheduler")
    # 真 engine 把 set_concept_inhibitor(None) 當「刻意無 Gate」：沒有 inhibitor 就不呼叫
    assert "set_concept_inhibitor" not in calls
    host.stop()


def test_inject_order_with_per_namespace_inhibitor_factory(tmp_path):
    made: list[tuple[str, object]] = []

    def inhibitor_factory(namespace_id):
        inhibitor = object()
        made.append((namespace_id, inhibitor))
        return inhibitor

    host, factory = make_host(
        tmp_path, concept_inhibitor_factory=inhibitor_factory,
        namespaces=(NamespaceSpec(OWNER_NS, owner_id="owner"), NamespaceSpec("core_two", owner_id="other"), NamespaceSpec(SCOPE_NS)),
    )
    host.start()
    calls = factory.calls(OWNER_NS)
    assert calls.index("set_llm_fn") < calls.index("set_concept_inhibitor") < calls.index("set_candidate_sink")
    assert calls.index("set_candidate_sink") < calls.index("start_dream_scheduler")
    # 每個非共享 engine 各自一個實例；scope-g* 不注入
    assert [ns for ns, _ in made] == [OWNER_NS, "core_two"]
    assert factory.engines[OWNER_NS].inhibitor is made[0][1] and factory.engines["core_two"].inhibitor is made[1][1]
    assert factory.engines[OWNER_NS].inhibitor is not factory.engines["core_two"].inhibitor
    assert "set_concept_inhibitor" not in factory.calls(SCOPE_NS)
    host.stop()


def test_shared_or_missing_inhibitor_instance_rejected(tmp_path):
    shared = object()
    host, factory = make_host(
        tmp_path, concept_inhibitor_factory=lambda ns: shared,
        namespaces=(NamespaceSpec(OWNER_NS, owner_id="owner"), NamespaceSpec("core_two", owner_id="other")),
    )
    with pytest.raises(MemoryHostError, match="各一個"):
        host.start()
    assert "stop_dream_scheduler" in factory.calls(OWNER_NS) and not host.started
    host2, _ = make_host(tmp_path, concept_inhibitor_factory=lambda ns: None)
    with pytest.raises(MemoryHostError, match="各一個"):
        host2.start()


def test_scope_namespace_never_starts_scheduler(host_pair):
    host, factory = host_pair
    assert "start_dream_scheduler" not in factory.calls(SCOPE_NS)
    assert host.start_report.namespaces[SCOPE_NS]["scheduler"] == {"started": False, "reason": "scope_namespace"}
    assert host.start_report.namespaces[OWNER_NS]["scheduler"]["started"] is True


def test_scheduler_can_be_disabled_by_host(tmp_path):
    host, factory = make_host(tmp_path, enable_dream_scheduler=False)
    host.start()
    assert "start_dream_scheduler" not in factory.calls(OWNER_NS)
    assert host.start_report.namespaces[OWNER_NS]["scheduler"]["reason"] == "disabled_by_host"
    host.stop()


def test_stop_order_is_scheduler_then_drain_then_close_and_idempotent(tmp_path):
    host, factory = make_host(tmp_path, factory=FakeEngineFactory(with_close=True))
    host.start()
    host.stop()
    calls = factory.calls(OWNER_NS)
    assert calls.index("start_dream_scheduler") < calls.index("stop_dream_scheduler") < calls.index("close")
    engine = factory.engines[OWNER_NS]
    assert engine.event_emitter.drained and engine.closed
    host.stop()  # 冪等
    assert not host.started


def test_close_is_required_and_called_for_every_engine_with_timeout(host_pair):
    """B-4：close 是 engine 必要介面（不再用 getattr 容忍沒有 close），逐個以 drain_timeout 呼叫。"""
    host, factory = host_pair
    host.stop()
    for ns in (OWNER_NS, SCOPE_NS):
        assert factory.engines[ns].closed and factory.engines[ns].close_timeouts == [1.0]


def test_start_failure_stops_already_started_engines(tmp_path):
    factory = FakeEngineFactory()

    def flaky(namespace_id, data_root):
        if namespace_id == SCOPE_NS:
            raise RuntimeError("engine 建立失敗")
        return factory(namespace_id, data_root)

    host, _ = make_host(tmp_path, engine_factory=flaky)
    with pytest.raises(RuntimeError):
        host.start()
    assert "stop_dream_scheduler" in factory.calls(OWNER_NS)
    assert not host.started


def test_scheduler_start_error_is_reported_not_raised(tmp_path):
    factory = FakeEngineFactory()

    def failing_scheduler(namespace_id, data_root):
        engine = factory(namespace_id, data_root)
        engine.scheduler_result = {"started": False, "reason": "error: boom"}
        return engine

    host, _ = make_host(tmp_path, engine_factory=failing_scheduler)
    report = host.start()
    assert report.namespaces[OWNER_NS]["scheduler"]["reason"].startswith("error")
    assert host.metrics.get("memory.dream.scheduler_error") == 1
    host.stop()


# ---- 設定核對與階段 X ----

def test_default_namespace_rejected(tmp_path):
    host, _ = make_host(tmp_path, namespaces=(NamespaceSpec("local-default", owner_id="owner"),))
    with pytest.raises(MemoryHostError, match="local-default"):
        host.start()


@pytest.mark.parametrize("specs, message", [
    ((NamespaceSpec("a1", owner_id="o1"), NamespaceSpec("a1", owner_id="o2")), "重複"),
    ((NamespaceSpec("a1", owner_id="o1"), NamespaceSpec("a2", owner_id="o1")), "多個 namespace"),
    ((NamespaceSpec("a1"),), "owner_id"),
    ((NamespaceSpec("scope-g9", owner_id="o1"),), "共享層"),
    ((NamespaceSpec("a1", owner_id="o-1"),), "英數"),
    ((), "沒有任何"),
])
def test_bad_namespace_config_rejected(tmp_path, specs, message):
    host, _ = make_host(tmp_path, namespaces=specs)
    with pytest.raises(MemoryHostError, match=message):
        host.start()


def test_phase_x_foreign_root_equal_overlaps(tmp_path):
    root = tmp_path / "core_memory"
    host, _ = make_host(tmp_path, foreign_claims=(ForeignClaim(root),))
    with pytest.raises(MemoryHostError, match="階段 X"):
        host.start()


def test_phase_x_foreign_root_ancestor_or_descendant_overlaps(tmp_path):
    for foreign in (tmp_path, tmp_path / "core_memory" / "namespaces" / OWNER_NS / "semantic"):
        host, _ = make_host(tmp_path, foreign_claims=(ForeignClaim(foreign),))
        with pytest.raises(MemoryHostError, match="階段 X"):
            host.start()


def test_phase_x_same_root_same_namespace_overlaps(tmp_path):
    root = tmp_path / "core_memory"
    host, _ = make_host(tmp_path, foreign_claims=(ForeignClaim(root, namespaces=["1234", OWNER_NS]),))
    with pytest.raises(MemoryHostError, match="階段 X"):
        host.start()


def test_phase_x_same_root_disjoint_namespaces_passes(tmp_path):
    root = tmp_path / "core_memory"
    host, _ = make_host(tmp_path, foreign_claims=(ForeignClaim(root, namespaces=["1234", "scope-g999"]),))
    host.start()
    assert host.started
    host.stop()


def test_phase_x_disjoint_roots_pass(tmp_path):
    host, _ = make_host(tmp_path, foreign_claims=(ForeignClaim(tmp_path / "bot_data" / "memory"),))
    host.start()
    assert host.started
    host.stop()


def test_engine_with_unexpected_directory_rejected(tmp_path):
    factory = FakeEngineFactory()

    def wrong_dir(namespace_id, data_root):
        engine = factory(namespace_id, data_root)
        engine.storage_path = tmp_path / "elsewhere"
        return engine

    host, _ = make_host(tmp_path, engine_factory=wrong_dir)
    with pytest.raises(MemoryHostError, match="不符"):
        host.start()
    assert not host.started
    assert "stop_dream_scheduler" in factory.calls(OWNER_NS)  # 目錄不符的 engine 也在清理範圍內


# ---- Access Layer：VC-2-1、身分（D77）----

def seeded(host_pair):
    host, factory = host_pair
    factory.engines[OWNER_NS].episodes.append("私人的小秘密")
    factory.engines[SCOPE_NS].shared_summaries.append("共享摘要")
    return host, factory


def test_access_layer_is_a_memory_port(host_pair):
    assert isinstance(host_pair[0].access, MemoryPort)


def test_private_memory_reaches_private_audience(host_pair):
    host, factory = seeded(host_pair)
    result = host.access.retrieve(PRIVATE, "私人的小秘密", PRIVATE, actor_id="actor_a")
    assert result.text == "私人的小秘密" and result.memory_status is MemoryStatus.OK
    assert not result.excluded_summary
    assert factory.engines[OWNER_NS].retrieve_calls[0]["top_k"] == 3


def test_private_memory_never_enters_public_audience(host_pair):
    """VC-2-1：private 記憶不進 public audience；只留原因碼計數，不呼叫 engine、不帶內容。"""
    host, factory = seeded(host_pair)
    for audience in (PUBLIC, GROUP):
        result = host.access.retrieve(PRIVATE, "私人的小秘密", audience, actor_id="actor_a")
        assert result.text == "" and result.excluded_summary == {"scope_denied": 1}
    assert factory.engines[OWNER_NS].retrieve_calls == []
    composite = host.access.retrieve_for_audience("私人的小秘密", PUBLIC, actor_id="actor_a")
    assert composite.text == "" and composite.excluded_summary == {"scope_denied": 1}
    assert factory.engines[OWNER_NS].retrieve_calls == []
    assert host.access.profile(actor_id="actor_a", audience=PUBLIC) == ""


def test_group_audience_gets_shared_summary_but_not_private(host_pair):
    host, factory = seeded(host_pair)
    factory.engines[OWNER_NS].profile_text = "喜歡貓"
    result = host.access.retrieve_for_audience("私人的小秘密", GROUP, actor_id="actor_a")
    assert result.text == "共享摘要" and result.excluded_summary == {"scope_denied": 1}
    assert result.shared_ids == ("proj_0",)
    # viewer_uid 來自身分映射，不是呼叫端傳的字串
    assert factory.engines[SCOPE_NS].shared_calls[0] == {
        "query": "私人的小秘密", "scope_id": "guild:123", "viewer_uid": "111", "present_uids": (),
        "owner_uid": None,  # 一般檢索不帶 owner_uid
    }
    assert host.access.profile(actor_id="actor_a", audience=PRIVATE) == "喜歡貓"


def test_unmapped_actor_is_guest_everywhere(host_pair):
    host, factory = seeded(host_pair)
    for actor in ("actor_b", None, "actor_unknown"):
        assert host.access.retrieve(PRIVATE, "私人的小秘密", PRIVATE, actor_id=actor).excluded_summary == {"guest": 1}
        assert host.access.retrieve(GROUP, "共享", GROUP, actor_id=actor).excluded_summary == {"guest": 1}
        assert host.access.retrieve_for_audience("私人", PRIVATE, actor_id=actor).excluded_summary == {"guest": 1}
        stored = host.access.store(actor_id=actor, user_text="我是訪客", spoken_text="嗯")
        assert (stored.status, stored.reason) == ("rejected", "guest")
        assert host.access.profile(actor_id=actor, audience=PRIVATE) == ""
    assert factory.engines[OWNER_NS].retrieve_calls == [] and factory.engines[OWNER_NS].store_calls == 0


def test_speaker_ref_in_payload_cannot_make_an_owner():
    directory = make_identity()
    assert directory.resolve_platform("discord:111").owner_id == "owner"  # 映射表命中才算
    for spoofed in ("discord:999", "owner", "discord:111 ", None):
        assert directory.resolve_platform(spoofed).guest


def test_actor_cannot_read_other_owners_scope(host_pair):
    host, factory = seeded(host_pair)
    result = host.access.retrieve("scope_private_other", "x", "scope_private_other", actor_id="actor_a")
    assert result.excluded_summary == {"not_owner": 1}
    assert factory.engines[OWNER_NS].retrieve_calls == []


def test_unknown_and_bad_scopes_are_denied_not_raised(host_pair):
    host, _ = seeded(host_pair)
    assert host.access.retrieve("garbage", "x", PRIVATE, actor_id="actor_a").excluded_summary == {"unknown_scope": 1}
    assert host.access.retrieve(PRIVATE, "x", "garbage", actor_id="actor_a").excluded_summary == {"bad_audience": 1}
    assert host.access.retrieve(PUBLIC, "x", PUBLIC, actor_id="actor_a").text == ""
    with pytest.raises(TypeError):
        host.access.retrieve(PRIVATE, "x", PRIVATE, actor_id="actor_a", unknown_kw=1)


def test_degraded_engine_reports_status_without_asserting_content(host_pair):
    host, factory = seeded(host_pair)
    factory.engines[OWNER_NS].degraded = True
    result = host.access.retrieve(PRIVATE, "私人的小秘密", PRIVATE, actor_id="actor_a")
    assert result.memory_status is MemoryStatus.DEGRADED_NO_MEMORY
    assert result.degraded_reason == "embedding_unavailable"


def test_slow_retrieve_times_out_with_empty_text(host_pair):
    host, factory = seeded(host_pair)
    factory.engines[OWNER_NS].retrieve_delay_s = 0.4
    result = host.access.retrieve(PRIVATE, "私人的小秘密", PRIVATE, actor_id="actor_a", timeout_s=0.05)
    assert result.memory_status is MemoryStatus.TIMEOUT and result.text == ""
    assert host.metrics.get("memory.retrieve.timeout") == 1


def test_engine_calls_hold_engine_lock_and_expired_queued_call_never_runs(host_pair):
    host, factory = seeded(host_pair)
    engine = factory.engines[OWNER_NS]
    lock = host.engine_lock(OWNER_NS)
    lock.acquire()  # 模擬 S3 的 EchoMemoryAdapter 正持有同一把鎖
    try:
        result = host.access.retrieve(PRIVATE, "私人的小秘密", PRIVATE, actor_id="actor_a", timeout_s=0.1)
        assert result.memory_status is MemoryStatus.TIMEOUT
        assert engine.retrieve_calls == []
    finally:
        lock.release()
    time.sleep(0.2)  # 鎖放開後，排隊中已逾時的呼叫不得才開始執行
    assert engine.retrieve_calls == []


def test_store_requires_owner_and_is_idempotent_by_key(host_pair):
    host, factory = host_pair
    first = host.access.store(actor_id="actor_a", user_text="今天吃了拉麵", spoken_text="聽起來不錯", idempotency_key="k1")
    again = host.access.store(actor_id="actor_a", user_text="今天吃了拉麵", spoken_text="聽起來不錯", idempotency_key="k1")
    assert first.status == again.status == "stored" and first.episode_id == again.episode_id
    assert len(factory.engines[OWNER_NS].episodes) == 1
    assert first.event_duplicate is None  # 沒有 publisher：不發事件


def test_memory_tools_are_whitelisted_and_gated(tmp_path):
    class Backend:
        def definitions(self):
            return [{"type": "function", "function": {"name": "deep_recall"}}, {"name": "trigger_dream"}]

        def execute(self, engine, name, args, session_id):
            return {"success": True, "result": f"{name}:{session_id}"}

    host, _ = make_host(tmp_path, tool_backend=Backend())
    host.start()
    try:
        # member_recall 由 Access Layer 自己提供 schema（有成員資格 Port 才曝光）；trigger_dream 不在白名單
        assert [d["function"]["name"] for d in host.access.tool_definitions()] == ["deep_recall", "member_recall"]
        host.access.set_session("seg_1")
        ok = host.access.execute_tool("deep_recall", {}, actor_id="actor_a", audience=PRIVATE)
        assert ok == {"success": True, "result": "deep_recall:seg_1"}
        for kwargs in (
            {"name": "trigger_dream", "actor_id": "actor_a", "audience": PRIVATE},
            {"name": "deep_recall", "actor_id": "actor_b", "audience": PRIVATE},
            {"name": "deep_recall", "actor_id": "actor_a", "audience": PUBLIC},
        ):
            name = kwargs.pop("name")
            assert host.access.execute_tool(name, {}, **kwargs)["success"] is False
    finally:
        host.stop()


# ---- 做夢（手動入口；排程在記憶端）----

# ---- 需要 PostgreSQL：事件冪等、Activity、health、組裝 ----

@pytest.fixture
def wired(session_factory, tmp_path):
    bus = EventBus()
    publisher = EventPublisher(session_factory, bus)
    host, factory = make_host(tmp_path, publisher=publisher, session_factory=session_factory)
    host.start()
    yield host, factory, bus, session_factory
    host.stop()


def count_events(session_factory, event_type: str) -> int:
    with session_factory() as s:
        return s.execute(text("SELECT count(*) FROM events WHERE event_type = :t"), {"t": event_type}).scalar_one()


def fixture_draft(index: int = 0, kind: str = "memory") -> FakeDraft:
    raw = SAMPLES[kind][index]
    return FakeDraft(raw["event_type"], raw["payload"], raw["idempotency_key"], raw["scope_ref"])


def test_memory_events_idempotent_through_sink(wired):
    host, factory, bus, sf = wired
    engine = factory.engines[OWNER_NS]
    draft = fixture_draft(0)
    before = bus.get_stats()["published"]  # 啟動時已有一筆 health ready
    engine.emit(draft)
    engine.emit(draft)  # 記憶端重送同一把 key
    assert count_events(sf, draft.event_type) == 1
    assert bus.get_stats()["published"] == before + 1  # 重送不再投遞 Bus
    assert host.metrics.get("memory.sink.published") == 1 and host.metrics.get("memory.sink.duplicate") == 1


def test_candidate_summary_goes_to_blob_not_journal(wired):
    _, factory, _, sf = wired
    raw = SAMPLES["candidate"][0]
    factory.engines[OWNER_NS].emit(fixture_draft(0, "candidate"))
    with sf() as s:
        payload = s.execute(text("SELECT payload FROM events WHERE event_type = 'proactive.trigger_observed'")).scalar_one()
        blobs = s.execute(text("SELECT count(*) FROM payload_blobs")).scalar_one()
    assert "summary" not in payload and "expires_at" not in payload
    assert blobs == 1
    assert raw["payload"]["summary"] not in str(payload)


def test_sink_rebuilds_scope_ref_and_ignores_echo_scope_ref(wired):
    """echo 的 scope_ref 含 namespace 字元（可有連字號）；轉譯器改用 owner 映射重建，不被它帶著走。"""
    _, factory, _, sf = wired
    raw = SAMPLES["memory"][1]
    factory.engines[OWNER_NS].emit(FakeDraft(raw["event_type"], raw["payload"], raw["idempotency_key"], "scope_private_echo-stream"))
    with sf() as s:
        scope = s.execute(text("SELECT scope_ref FROM events WHERE event_type = :t"), {"t": raw["event_type"]}).scalar_one()
    assert scope == "scope_private_owner"


def test_sink_drops_untrusted_drafts_and_counts(session_factory):
    metrics = MemoryMetrics()
    publisher = EventPublisher(session_factory, EventBus())
    sink = MemorySink(publisher, "ns", None, metrics=metrics)  # 沒有 owner 映射
    sink.emit(fixture_draft(0))
    owned = MemorySink(publisher, "ns", "owner", metrics=metrics)
    raw = SAMPLES["memory"][0]
    owned.emit(FakeDraft("memory.not_a_kind", raw["payload"], "k1", raw["scope_ref"]))
    owned.emit(FakeDraft(raw["event_type"], {**raw["payload"], "privacy_scope": "public"}, "k2", raw["scope_ref"]))
    owned.emit(FakeDraft(raw["event_type"], raw["payload"], "", raw["scope_ref"]))
    without_privacy = {k: v for k, v in raw["payload"].items() if k != "privacy_scope"}
    owned.emit(FakeDraft(raw["event_type"], without_privacy, "k3", raw["scope_ref"]))  # 缺欄位也丟棄（fail-closed）
    owned.emit(object())  # 不是草稿：不得拋出（emit 在記憶端 daemon 執行緒上呼叫）
    snap = metrics.snapshot()
    assert snap["memory.sink.dropped.no_owner"] == 1
    assert snap["memory.sink.dropped.privacy_scope"] == 2 and snap["memory.sink.dropped.no_idempotency_key"] == 1
    assert snap["memory.sink.dropped.unknown_type"] == 2  # 未知種類與「不是草稿的物件」都被擋
    assert count_events(session_factory, raw["event_type"]) == 0


def test_sink_publish_failure_is_swallowed(session_factory):
    class Boom:
        def publish(self, *args, **kwargs):
            raise RuntimeError("db down")

    metrics = MemoryMetrics()
    sink = MemorySink(Boom(), "ns", "owner", metrics=metrics)  # type: ignore[arg-type]
    sink.emit(fixture_draft(0))
    assert metrics.get("memory.sink.publish_failed") == 1


def test_store_publishes_record_created_once_with_ids_only(wired):
    host, _, _, sf = wired
    kwargs = {"actor_id": "actor_a", "user_text": "今天吃了拉麵", "spoken_text": "聽起來不錯", "idempotency_key": "turn-1"}
    first = host.access.store(**kwargs)
    again = host.access.store(**kwargs)
    assert (first.status, first.event_duplicate) == ("stored", False)
    assert again.event_duplicate is True and again.episode_id == first.episode_id
    assert count_events(sf, "memory.record_created") == 1
    with sf() as s:
        payload, scope = s.execute(text("SELECT payload, scope_ref FROM events WHERE event_type = 'memory.record_created'")).one()
    assert payload == {"ref_id": first.episode_id} and scope == PRIVATE
    assert "拉麵" not in str(payload)


def test_dream_listener_report_adds_finished_activity_and_counts(wired):
    """B-6：Activity 與計數由 set_dream_listener 的報告負責（sink 只轉事件，不再補 Activity，避免重複）；
    skipped 不補 Activity，租約衝突單獨計數（E4）。"""
    host, factory, _, sf = wired
    engine = factory.engines[OWNER_NS]
    engine.emit(fixture_draft(1))  # 事件本身不產生 Activity
    assert fixture_draft(1).event_type == "memory.dream_completed"
    with sf() as s:
        assert s.query(ActivityRow).filter(ActivityRow.kind == "maintenance").count() == 0
    engine.report_dream({"status": "completed", "triggered_by": "scheduler"})
    engine.report_dream({"status": "skipped", "triggered_by": "scheduler", "skipped_reason": "租約由 pid=1 持有"})
    with sf() as s:
        rows = list(s.query(ActivityRow).filter(ActivityRow.kind == "maintenance"))
    assert len(rows) == 1 and rows[0].origin_ref["triggered_by"] == "scheduler" and rows[0].status == "ENDED"
    assert host.metrics.get("memory.dream.runs.scheduler") == 1
    assert host.metrics.get("memory.dream.skipped_lease") == 1


@pytest.mark.parametrize("fail_on", ["set_llm_fn", "start_dream_scheduler"])
def test_engine_exception_during_start_cleans_up_everything(tmp_path, fail_on):
    factory = FakeEngineFactory()

    def factory_with_failure(namespace_id, data_root):
        engine = factory(namespace_id, data_root)
        if namespace_id == OWNER_NS:
            engine.fail_on = {fail_on}
        return engine

    host, _ = make_host(
        tmp_path, engine_factory=factory_with_failure,
        namespaces=(NamespaceSpec(SCOPE_NS), NamespaceSpec(OWNER_NS, owner_id="owner")),
    )
    with pytest.raises(RuntimeError, match=fail_on):
        host.start()
    assert not host.started
    assert "stop_dream_scheduler" in factory.calls(OWNER_NS) and "stop_dream_scheduler" in factory.calls(SCOPE_NS)
    with pytest.raises(MemoryHostError):  # 已清空：不再提供 engine
        host.engine(OWNER_NS)


def test_drain_timeout_is_logged(tmp_path, caplog):
    host, factory = make_host(tmp_path)
    host.start()
    factory.engines[OWNER_NS].drain_ok = False
    with caplog.at_level("WARNING", logger="uep.memory.host"):
        host.stop()
    assert any("排空逾時" in r.message for r in caplog.records)


# ---- 共享層成員資格（fail-closed）----

def test_group_read_denied_without_membership_port(tmp_path):
    host, factory = make_host(tmp_path, guild_membership=None)
    host.start()
    try:
        factory.engines[SCOPE_NS].shared_summaries.append("共享摘要")
        result = host.access.retrieve(GROUP, "共享", GROUP, actor_id="actor_a")
        assert result.text == "" and result.excluded_summary == {"no_membership": 1}
        assert factory.engines[SCOPE_NS].shared_calls == []
    finally:
        host.stop()


def test_non_member_cannot_read_guild_shared_layer(host_pair):
    host, factory = seeded(host_pair)
    result = host.access.retrieve(GROUP, "共享", GROUP, actor_id="actor_c")  # 已映射為 owner，但不是 guild 成員
    assert result.text == "" and result.excluded_summary == {"not_member": 1}
    assert factory.engines[SCOPE_NS].shared_calls == []
    other_guild = host.access.retrieve("scope_group_g999", "共享", "scope_group_g999", actor_id="actor_a")
    assert other_guild.excluded_summary == {"not_member": 1}


def test_present_uids_clipped_to_members(host_pair):
    host, factory = seeded(host_pair)
    host.access.retrieve(GROUP, "共享", GROUP, actor_id="actor_a", present_uids=["111", "999", 333])
    assert factory.engines[SCOPE_NS].shared_calls[0]["present_uids"] == ("111",)  # 非成員被裁掉
    assert host.metrics.get("memory.retrieve.present_clipped") == 1


# ---- 跨 namespace 隔離 ----

def test_blocked_namespace_does_not_stall_another_namespace(tmp_path):
    host, factory = make_host(
        tmp_path, namespaces=(NamespaceSpec(OWNER_NS, owner_id="owner"), NamespaceSpec("core_two", owner_id="other")),
    )
    host.start()
    try:
        factory.engines[OWNER_NS].episodes.append("甲的記憶")
        factory.engines["core_two"].episodes.append("乙的記憶")
        factory.engines[OWNER_NS].retrieve_delay_s = 1.0  # A 卡住
        results: dict[str, object] = {}
        slow = threading.Thread(target=lambda: results.update(a=host.access.retrieve(
            PRIVATE, "甲的記憶", PRIVATE, actor_id="actor_a", timeout_s=2.0)))
        slow.start()
        time.sleep(0.1)
        started = time.monotonic()
        quick = host.access.retrieve(
            "scope_private_other", "乙的記憶", "scope_private_other", actor_id="actor_c", timeout_s=0.5)
        assert quick.text == "乙的記憶" and time.monotonic() - started < 0.5
        # A 的鎖被外部持有時，B 也照常
        lock = host.engine_lock(OWNER_NS)
        lock.acquire()
        try:
            again = host.access.retrieve(
                "scope_private_other", "乙的記憶", "scope_private_other", actor_id="actor_c", timeout_s=0.5)
            assert again.text == "乙的記憶"
        finally:
            lock.release()
        slow.join()
    finally:
        host.stop()


# ---- 需要 PostgreSQL 的補充案例 ----

def test_store_timeout_with_late_completion_still_publishes_record_created(wired):
    host, factory, _, sf = wired
    factory.engines[OWNER_NS].store_delay_s = 0.3
    result = host.access.store(
        actor_id="actor_a", user_text="慢慢寫", spoken_text="嗯", timeout_s=0.05, idempotency_key="slow-1")
    assert result.status == "timeout"
    deadline = time.monotonic() + 3
    while count_events(sf, "memory.record_created") == 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert count_events(sf, "memory.record_created") == 1
    assert host.metrics.get("memory.call.late_completed") == 1


def test_stop_publishes_unavailable_health(session_factory, tmp_path):
    publisher = EventPublisher(session_factory, EventBus())
    host, _ = make_host(tmp_path, publisher=publisher)
    host.start()
    host.stop()
    with session_factory() as s:
        statuses = list(s.execute(text(
            "SELECT payload->>'status' FROM events WHERE event_type = 'core.component_health_changed' "
            "ORDER BY journal_seq"
        )).scalars())
    assert statuses == ["ready", "unavailable"]


def test_start_sweeps_stale_active_maintenance_rows(session_factory, tmp_path):
    from uep.ids import new_id

    stale, unrelated = new_id("actv"), new_id("actv")
    with session_factory() as s, s.begin():
        s.add(ActivityRow(
            activity_id=stale, persona_id="uep", kind="maintenance", presence_bound=False, status="ACTIVE",
            version=1, origin_ref={"component": "memory.dream", "namespace_id": OWNER_NS}))
        s.add(ActivityRow(
            activity_id=unrelated, persona_id="uep", kind="maintenance", presence_bound=False, status="ACTIVE",
            version=1, origin_ref={"component": "other"}))
    host, _ = make_host(tmp_path, session_factory=session_factory)
    host.start()
    host.stop()
    with session_factory() as s:
        swept, other = s.get(ActivityRow, stale), s.get(ActivityRow, unrelated)
        assert (swept.status, swept.origin_ref["outcome"]) == ("ENDED", "interrupted")
        assert swept.ended_at is not None
        assert other.status == "ACTIVE"


def test_assembly_requires_same_publisher(session_factory, tmp_path):
    from tests.uep.test_assembly import kwargs as assembly_kwargs
    from uep.assembly import AssemblyError, assemble_core

    host, _ = make_host(tmp_path, publisher=EventPublisher(session_factory, EventBus()))  # 另一個 publisher
    with pytest.raises(AssemblyError, match="publisher"):
        assemble_core(session_factory, **assembly_kwargs(session_factory, tmp_path, memory=host))
    host_without, _ = make_host(tmp_path)  # 沒有 publisher 也不行：事件會被吞掉
    with pytest.raises(AssemblyError, match="publisher"):
        assemble_core(session_factory, **assembly_kwargs(session_factory, tmp_path, memory=host_without))


def assemble_with_host(session_factory, tmp_path, **over):
    from tests.uep.test_assembly import kwargs as assembly_kwargs
    from uep.assembly import assemble_core

    bus = EventBus()
    publisher = EventPublisher(session_factory, bus)
    host, factory = make_host(tmp_path, publisher=publisher)
    core = assemble_core(
        session_factory,
        **assembly_kwargs(session_factory, tmp_path, bus=bus, publisher=publisher, memory=host, **over),
    )
    return core, host, factory


def test_assembly_starts_memory_after_maintenance_before_relay_and_stops_first(session_factory, tmp_path):
    core, host, factory = assemble_with_host(session_factory, tmp_path, relay_starters=(lambda: None,))
    try:
        core.start()
        log = core.start_log
        order = [log.index(x) for x in ("presence.start", "bridge.reconcile", "memory.start", "relay.start")]
        assert order == sorted(order)
        assert host.started and "start_dream_scheduler" in factory.calls(OWNER_NS)
    finally:
        core.shutdown()
    assert not host.started and "stop_dream_scheduler" in factory.calls(OWNER_NS)


def test_assembly_start_failure_after_memory_still_stops_memory(session_factory, tmp_path):
    def boom():
        raise RuntimeError("relay 啟動失敗")

    core, host, factory = assemble_with_host(session_factory, tmp_path, relay_starters=(boom,))
    with pytest.raises(RuntimeError, match="relay"):
        core.start()
    assert not host.started and "stop_dream_scheduler" in factory.calls(OWNER_NS)
