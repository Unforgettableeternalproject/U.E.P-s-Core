"""
M6 事件契約測試：Echo -> Core 的 EventDraft（memory.*／proactive.trigger_observed）。

全部用假 Core 與 conftest 的確定性 embedding，不載入真模型。
"""

from __future__ import annotations

import dataclasses
import time
from datetime import datetime, timezone

import pytest

from echo_memory.affect.event_emitter import MemoryEventEmitter
from echo_memory.config import Config, get_config
from echo_memory.schemas.events import (
    EVENT_KINDS,
    TRUST_FIELDS,
    CandidateSink,
    EventDraft,
    ScopeRef,
    compute_idempotency_key,
    is_safe_token,
)
from tests.hypothesis_support import (
    ScriptedLLM,
    build_env,
    llm_reply,
    make_bridge,
    propose,
    two_concepts,
)
from tests.mock_core import FakeCore, HostileCore


@pytest.fixture
def events_on(monkeypatch):
    monkeypatch.setattr(get_config(), "self_model_events_enabled", True)


def _scenario(tmp_path, sink, namespace_id="ns1"):
    """兩位說話者各自陳述 -> 準備好審核；sink 先注入，審核留給呼叫端。"""
    env = build_env(tmp_path, namespace_id=namespace_id)
    env.engine.set_candidate_sink(sink)
    d = two_concepts(env, same_speaker=False)
    make_bridge(env, d["a"], d["b"])
    hid = propose(env, d["a"], d["b"])
    env.reviewer._llm_fn = ScriptedLLM(llm_reply("supported", [d["ua"], d["ub"]]))
    return env, d, hid


# ─── 契約形狀 ────────────────────────────────────────────────────────────────

def test_draft_has_only_four_fields_and_no_trust_fields():
    names = {f.name for f in dataclasses.fields(EventDraft)}
    assert names == {"event_type", "payload", "idempotency_key", "scope_ref"}
    assert not (names & TRUST_FIELDS)
    d = EventDraft(event_type="memory.dream_completed", payload={}, idempotency_key="k", scope_ref="s")
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.event_type = "other"  # type: ignore[misc]
    assert set(d.to_dict()) == names


def test_kinds_map_to_independent_event_types(tmp_path):
    assert EVENT_KINDS == {
        "dream_completed", "reflection_completed", "hypothesis_supported",
        "hypothesis_contradicted", "contradiction_found", "episode_flagged",
        "self_observation_proposed",
    }
    from echo_memory.schemas.events import build_memory_draft

    types = {build_memory_draft(k, "ns", ("r",)).event_type for k in EVENT_KINDS}
    assert types == {f"memory.{k}" for k in EVENT_KINDS}
    assert "memory.consolidated" not in types


def test_sink_protocol_is_runtime_checkable():
    assert isinstance(FakeCore(), CandidateSink)


def test_scope_ref_serialization_matches_core_draft():
    assert ScopeRef("private", owner_id="owner").serialize() == "scope_private_owner"
    assert ScopeRef("public").serialize() == "scope_public"
    assert ScopeRef("group", group_id="g1").serialize() == "scope_group_g1"
    with pytest.raises(ValueError):
        ScopeRef("private").serialize()
    with pytest.raises(ValueError):
        ScopeRef("private", owner_id="bad id\n").serialize()


def test_idempotency_key_is_content_hash_and_valid():
    a = compute_idempotency_key("dream_completed", "ns", ["b", "a"], ["d"])
    assert a == compute_idempotency_key("dream_completed", "ns", ["a", "b"], ["d"])
    assert a != compute_idempotency_key("dream_completed", "ns2", ["a", "b"], ["d"])
    assert a != compute_idempotency_key("episode_flagged", "ns", ["a", "b"], ["d"])
    assert is_safe_token(a) and len(a) <= 256


def test_safe_token_rules():
    assert is_safe_token("a:b_c.d-E9")
    assert not is_safe_token("")
    assert not is_safe_token("a b")
    assert not is_safe_token("abc\n")  # 結尾換行不得放行
    assert not is_safe_token("x" * 257)


# ─── 冪等、必填引用、weight 非門檻 ─────────────────────────────────────────────

def test_same_event_is_emitted_once(events_on):
    core = FakeCore()
    em = MemoryEventEmitter("ns1")
    em.set_sink(core)
    em.emit_episode_flagged("ep1", "force_remember", 0.5)
    em.emit_episode_flagged("ep1", "force_remember", 0.9)  # weight 不同也是同一件事
    em.drain()
    assert len(core.events) == 1


def test_empty_evidence_refs_never_emitted(events_on):
    core = FakeCore()
    em = MemoryEventEmitter("ns1")
    em.set_sink(core)
    em.emit_reflection_completed([])
    em.emit_episode_flagged("", "force_remember", 1.0)
    em.emit_dream_completed("")
    em.emit_self_observation_proposed("c1", [])
    em.drain()
    assert core.events == []


def test_weight_is_not_a_threshold(events_on):
    core = FakeCore()
    em = MemoryEventEmitter("ns1")
    em.set_sink(core)
    em.emit_episode_flagged("ep_low", "adjust_importance", 0.0)
    em.emit_episode_flagged("ep_high", "adjust_importance", 1.0)
    em.emit_episode_flagged("ep_wild", "adjust_importance", 7.5)  # 夾到 [0,1]
    em.drain()
    assert sorted(e.payload["weight"] for e in core.events) == [0.0, 1.0, 1.0]


# ─── 開關與 namespace ────────────────────────────────────────────────────────

def test_switches_default_off(monkeypatch):
    monkeypatch.setattr("echo_memory.config.DOTENV_AVAILABLE", False)
    monkeypatch.delenv("SELF_MODEL_ENABLED", raising=False)
    monkeypatch.delenv("SELF_MODEL_EVENTS_ENABLED", raising=False)
    cfg = Config()
    assert cfg.self_model_enabled is False
    assert cfg.self_model_events_enabled is False


def test_default_off_emits_zero_events(tmp_path):
    core = FakeCore()
    assert get_config().self_model_events_enabled is False
    env, d, hid = _scenario(tmp_path, core)
    out = env.reviewer.review(hid)
    assert out.status == "supported"
    env.engine.event_emitter.emit_dream_completed("cyc")
    env.engine.event_emitter.drain()
    assert core.events == [] and core.candidates == []


def test_no_sink_is_noop(tmp_path, events_on):
    env, d, hid = _scenario(tmp_path, None)
    assert env.engine.event_emitter.sink is None
    assert env.reviewer.review(hid).status == "supported"


def test_shared_namespace_never_emits(events_on):
    core = FakeCore()
    em = MemoryEventEmitter("scope-g123")
    em.set_sink(core)
    em.emit_episode_flagged("ep1", "force_remember", 1.0)
    em.emit_hypothesis_transition("h1", True, ["u1", "u2"], 0.9)
    em.drain()
    assert core.events == [] and core.candidates == []


# ─── Hypothesis 昇格 -> 恰一個事件 ────────────────────────────────────────────

def test_hypothesis_promotion_emits_exactly_one_event(tmp_path, events_on):
    core = FakeCore()
    env, d, hid = _scenario(tmp_path, core)
    out = env.reviewer.review(hid)
    assert out.outcome == "reviewed" and out.status == "supported"
    env.engine.event_emitter.drain()

    assert [e.event_type for e in core.events] == ["memory.hypothesis_supported"]
    ev = core.events[0]
    h = env.store.get(hid)
    # evidence_refs 等於 Gate 驗證後記錄的引用
    p = ev.payload
    assert set(p["evidence_refs"]) == set(h.review.cited_evidence_ids) == {d["ua"], d["ub"]}
    assert p["derived_from"] == [f"hypothesis:{hid}"]
    assert p["privacy_scope"] == "private"
    assert ev.scope_ref == "scope_private_ns1"
    assert ev.idempotency_key == compute_idempotency_key(
        "hypothesis_supported", "ns1", p["evidence_refs"], p["derived_from"],
    )
    assert is_safe_token(ev.idempotency_key)
    assert not (set(ev.to_dict()) | set(p)) & TRUST_FIELDS

    # 最小路徑：一個候選，只有描述，不帶證據原文
    assert len(core.candidates) == 1
    cand = core.candidates[0]
    cp = cand.payload
    assert cand.event_type == "proactive.trigger_observed"
    assert cp["trigger_kind"] == "memory_hypothesis_supported"
    assert cand.scope_ref == "scope_private_ns1" and is_safe_token(cand.idempotency_key)
    assert cp["source_idempotency_keys"] == [ev.idempotency_key]
    assert cp["dedupe_key"] == f"hypothesis:{hid}"
    assert set(cp["evidence_refs"]) == {d["ua"], d["ub"]}
    assert datetime.fromisoformat(cp["expires_at"]) > datetime.now(timezone.utc)
    for forbidden in ("urgency", "interruptibility_cost", "desired_location", "goal_ref"):
        assert forbidden not in cp
    assert not (set(cand.to_dict()) | set(cp)) & TRUST_FIELDS
    for text in ("咖啡拉花", "早晨咖啡店", "咖啡"):
        assert text not in cp["summary"]

    # 同一份重審（同引用）不會再發第二個
    env.engine.event_emitter.emit_hypothesis_transition(
        hid, True, [d["ua"], d["ub"]], 0.9,
    )
    env.engine.event_emitter.drain()
    assert len(core.events) == 1

    # Echo 不驅動 Core
    assert (core.task_calls, core.presence_calls, core.persona_writes) == (0, 0, 0)


# ─── Hostile Core 不影響寫入與 Dream ─────────────────────────────────────────

@pytest.mark.parametrize("mode", ["raise", "command", "block"])
def test_hostile_core_does_not_affect_writes(tmp_path, events_on, mode):
    core = HostileCore(mode)
    env, d, hid = _scenario(tmp_path, core)
    t0 = time.perf_counter()
    out = env.reviewer.review(hid)
    elapsed = time.perf_counter() - t0
    for _ in range(100):  # 等背景執行緒真的呼叫到 sink（block 模式下不會結束）
        if core.calls:
            break
        time.sleep(0.01)
    core.release.set()
    assert out.outcome == "reviewed" and out.status == "supported"
    assert env.store.get(hid).status == "supported"
    assert env.graph.edge_types_between(d["a"], d["b"]) == {"RELATES_TO"}
    assert core.calls >= 1
    assert elapsed < 1.5  # 不等接收端：block 模式不得拖慢寫入路徑
    assert (core.task_calls, core.presence_calls, core.persona_writes) == (0, 0, 0)


@pytest.mark.parametrize("mode", ["raise", "command", "block"])
def test_hostile_core_does_not_affect_dream(tmp_path, events_on, mode):
    core = HostileCore(mode)
    env = build_env(tmp_path)
    env.engine.set_candidate_sink(core)
    result = env.engine.dream("manual")
    for _ in range(100):
        if core.calls:
            break
        time.sleep(0.01)
    core.release.set()
    assert result["error"] is None
    assert result["skipped_reason"] is None
    assert core.calls >= 1  # dream_completed 確實嘗試投遞


def test_deliver_never_blocks_caller_and_drops_over_limit(events_on):
    core = HostileCore("block")
    em = MemoryEventEmitter("ns1")
    em.set_sink(core)
    t0 = time.perf_counter()
    for i in range(10):
        em.emit_episode_flagged(f"ep{i}", "force_remember", 0.5)
    elapsed = time.perf_counter() - t0
    time.sleep(0.1)
    core.release.set()
    assert elapsed < 0.5
    assert core.calls <= 4  # 並行上限，超過直接丟棄不排隊


def test_dream_completed_emitted_once_per_cycle(tmp_path, events_on):
    core = FakeCore()
    env = build_env(tmp_path)
    env.engine.set_candidate_sink(core)
    env.engine.dream("manual")
    env.engine.dream("manual")
    env.engine.event_emitter.drain()
    kinds = [e.event_type for e in core.events]
    assert kinds.count("memory.dream_completed") == 2  # 兩個週期各一個（cycle id 不同）
    assert len({e.idempotency_key for e in core.events}) == len(core.events)
    for e in core.events:
        assert all(r.startswith("dream_cycle:") for r in e.payload["evidence_refs"])
