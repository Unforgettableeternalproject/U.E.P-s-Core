"""Laya 的 available_capabilities 改從工具層快照取得（離線，不需資料庫、不需真 laya）。"""
from __future__ import annotations

import logging
import sys
import types
from collections import Counter
from enum import Enum
from types import SimpleNamespace

from uep.adapters.tool import CAPABILITY_CEILING
from uep.decision.laya_schema import (
    CAPABILITY_CATEGORIES,
    NON_USER_TAGS,
    TAG_CATEGORY,
    categories_from_readiness,
    format_capabilities,
)
from uep.echo import laya_wiring
from uep.echo.laya_wiring import (
    LayaProposer,
    LayaRulesProposer,
    bind_laya_capabilities,
    build_state,
    registry_capabilities_source,
)
from uep.turn.decision_config import DecisionConfig
from uep.turn.models import SessionContext, TurnCandidate
from uep.turn.rule_proposer import RuleProposer

CTX = SessionContext(conversation_id="c1", segment_id="s1", audience_scope="aud", session_ref="sess")


def _cand() -> TurnCandidate:
    return TurnCandidate(
        candidate_id="cand1", utterance_refs=("e1",), speakers=("discord:1",), ctx=CTX, interaction_state="IDLE",
        interaction_version=1, presence_active=True, text="hi", participant_count=1, addressed_hint=True,
    )


class _Agent:
    revision = "r"

    def __init__(self):
        self.states: list[dict] = []

    def predict(self, state, questions):
        self.states.append(state)
        return {"answers": {
            "reply_mode": {"choice": "respond", "probabilities": {"respond": 0.9}},
            "route": {"choice": "direct_chat", "probabilities": {}},
            "addressed": {"noul": 0.8},
        }}


def _proposer(monkeypatch, **kw) -> tuple[LayaProposer, _Agent]:
    fake = types.ModuleType("laya")
    fake.__version__ = "0"
    agent = _Agent()
    fake.load = lambda model_id, device=None, **k: agent
    monkeypatch.setitem(sys.modules, "laya", fake)
    monkeypatch.setattr(laya_wiring, "resolve_device", lambda requested, cuda_available=None: requested)
    p = LayaProposer(device="cpu", **kw)
    assert p.wait_ready(5) and p.loaded
    return p, agent


def test_every_ceiling_tag_is_mapped_or_explicitly_excluded():
    tags = set(CAPABILITY_CEILING)
    assert tags <= set(TAG_CATEGORY) | NON_USER_TAGS
    assert not set(TAG_CATEGORY) & NON_USER_TAGS
    assert set(TAG_CATEGORY) <= tags and NON_USER_TAGS <= tags  # 對映表沒有已不存在的標籤
    assert set(TAG_CATEGORY.values()) <= set(CAPABILITY_CATEGORIES)


def test_only_ready_tags_count_and_output_stays_in_vocabulary():
    rows = [
        ("memory.read", "ready"), ("web.search", "warming"), ("chatroom.post", "unavailable"),
        ("lore.write", "ready"), ("decision.consult", "ready"), ("test.read", "ready"), ("unknown.tag", "ready"),
    ]
    assert categories_from_readiness(rows) == {"query", "files"}
    assert format_capabilities(categories_from_readiness(rows)) == "query, files"
    assert categories_from_readiness([]) == frozenset()
    assert format_capabilities(categories_from_readiness([("web.fetch", "warming")])) == "none"


def test_source_is_read_on_every_inference(monkeypatch):
    calls = []
    answers = [{"query"}, {"query", "messaging"}, set()]

    def source():
        calls.append(1)
        return answers[min(len(calls) - 1, len(answers) - 1)]

    p, agent = _proposer(monkeypatch, capabilities_source=source)
    assert len(calls) == 1  # warmup 讀一次
    p.propose(_cand())
    p.propose(_cand())
    assert len(calls) == 3
    assert [s["available_capabilities"] for s in agent.states] == ["query", "query, messaging", "none"]


def test_read_failure_gives_none_not_default(monkeypatch):
    def boom():
        raise RuntimeError("x")

    p, agent = _proposer(monkeypatch, capabilities_source=boom)
    p.propose(_cand())
    assert [s["available_capabilities"] for s in agent.states] == ["none", "none"]  # warmup 與推論
    assert p.metrics["capabilities_read_error"] == 2


def test_without_source_behaviour_is_unchanged(monkeypatch):
    p, agent = _proposer(monkeypatch)
    p.propose(_cand())
    assert agent.states[-1]["available_capabilities"] == build_state(_cand(), "U.E.P")["available_capabilities"]
    assert "device_control" not in agent.states[-1]["available_capabilities"]
    p2, agent2 = _proposer(monkeypatch, capabilities=("query",))
    p2.propose(_cand())
    assert agent2.states[-1]["available_capabilities"] == "query"


def test_bind_after_construction_switches_to_source(monkeypatch):
    p, agent = _proposer(monkeypatch)
    p.bind_capabilities_source(lambda: {"files"})
    p.propose(_cand())
    assert agent.states[-1]["available_capabilities"] == "files"


def _registry(rows):
    return SimpleNamespace(capability_snapshot=lambda: SimpleNamespace(
        revision=1, tags=tuple(SimpleNamespace(tag=t, readiness=r, side_effect_class="internal_read") for t, r in rows)))


def test_registry_adapter_maps_snapshot():
    read = registry_capabilities_source(_registry([("memory.read", "ready"), ("chatroom.post", "warming")]))
    assert read() == {"query"}


def test_registry_adapter_without_method_returns_none():
    assert registry_capabilities_source(object()) is None


def test_bind_helper_reaches_primary_and_inner_laya(monkeypatch):
    p, agent = _proposer(monkeypatch)
    wrapper = SimpleNamespace(laya=p)
    orch = SimpleNamespace(proposer=wrapper, _shadows=[(SimpleNamespace(), None)])
    assert bind_laya_capabilities(orch, _registry([("lore.read", "ready")])) == 1
    p.propose(_cand())
    assert agent.states[-1]["available_capabilities"] == "query"


def test_missing_snapshot_method_does_not_bind_and_keeps_default(monkeypatch, caplog):
    p, agent = _proposer(monkeypatch)
    orch = SimpleNamespace(proposer=SimpleNamespace(laya=p), _shadows=[])
    with caplog.at_level(logging.WARNING, logger=laya_wiring.logger.name):
        assert bind_laya_capabilities(orch, object()) == 0
    assert any("維持靜態能力清單" in r.getMessage() for r in caplog.records)
    assert p.metrics["capabilities_snapshot_unavailable"] == 1
    p.propose(_cand())
    assert agent.states[-1]["available_capabilities"] == build_state(_cand(), "U.E.P")["available_capabilities"]


def test_snapshot_method_raising_gives_none_and_syncs_primary_metric(monkeypatch):
    def boom():
        raise RuntimeError("x")

    p, agent = _proposer(monkeypatch)
    wrapper = LayaRulesProposer(p, RuleProposer(DecisionConfig()))
    sink: Counter[str] = Counter()
    wrapper.bind_metrics(sink)
    orch = SimpleNamespace(proposer=wrapper, _shadows=[])
    assert bind_laya_capabilities(orch, SimpleNamespace(capability_snapshot=boom)) == 1
    p.propose(_cand())
    assert agent.states[-1]["available_capabilities"] == "none"
    assert sink["laya_primary.capabilities_read_error"] == 1


class _Phase(Enum):
    ready = "ready"
    warming = "warming"


def test_enum_readiness_is_accepted():
    assert categories_from_readiness([("memory.read", _Phase.ready), ("web.fetch", _Phase.warming)]) == {"query"}
    read = registry_capabilities_source(_registry([("memory.read", _Phase.ready)]))
    assert read() == {"query"}


def test_malformed_snapshots_give_empty_without_raising():
    for snap in (SimpleNamespace(tags=None), SimpleNamespace(), SimpleNamespace(tags=(SimpleNamespace(),)),
                 SimpleNamespace(tags=(SimpleNamespace(tag="memory.read", readiness="???"),))):
        read = registry_capabilities_source(SimpleNamespace(capability_snapshot=lambda s=snap: s))
        assert read() == frozenset()


def test_all_skipped_metric_only_when_tags_present_but_none_ready():
    seen: list[str] = []
    mk = lambda rows: registry_capabilities_source(_registry(rows), seen.append)
    mk([("memory.read", "warming")])()
    assert seen == ["capabilities_all_skipped"]
    mk([])()
    mk([("memory.read", "ready")])()
    assert seen == ["capabilities_all_skipped"]


def test_repeated_read_failures_log_once(monkeypatch, caplog):
    def boom():
        raise RuntimeError("x")

    p, _ = _proposer(monkeypatch, capabilities_source=boom)  # warmup 已失敗一次
    with caplog.at_level(logging.ERROR, logger=laya_wiring.logger.name):
        for _ in range(5):
            p.propose(_cand())
    assert p.metrics["capabilities_read_error"] == 6
    assert sum("讀取可用能力失敗" in r.getMessage() for r in caplog.records) == 1  # 只有 warmup 那次記了，之後都只計 metric
    later = laya_wiring.time.monotonic() + 100.0
    monkeypatch.setattr(laya_wiring.time, "monotonic", lambda: later)
    with caplog.at_level(logging.ERROR, logger=laya_wiring.logger.name):
        p.propose(_cand())
    assert sum("讀取可用能力失敗" in r.getMessage() for r in caplog.records) == 2  # 超過 60 秒再記一次


def test_shadow_proposer_is_bound(monkeypatch):
    primary, _ = _proposer(monkeypatch)
    shadow, shadow_agent = _proposer(monkeypatch)
    orch = SimpleNamespace(proposer=SimpleNamespace(laya=primary), _shadows=[(shadow, None)])
    assert bind_laya_capabilities(orch, _registry([("chatroom.post", "ready")])) == 2
    shadow.propose(_cand())
    assert shadow_agent.states[-1]["available_capabilities"] == "messaging"
