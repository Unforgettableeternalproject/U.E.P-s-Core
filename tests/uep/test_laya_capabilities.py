"""Laya 的 available_capabilities 改從工具層快照取得（離線，不需資料庫、不需真 laya）。"""
from __future__ import annotations

import logging
import sys
import types
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
    bind_laya_capabilities,
    build_state,
    registry_capabilities_source,
)
from uep.turn.models import SessionContext, TurnCandidate

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


def test_registry_adapter_without_method_warns_once_and_raises(caplog):
    read = registry_capabilities_source(object())
    with caplog.at_level(logging.WARNING, logger=laya_wiring.logger.name):
        for _ in range(3):
            try:
                read()
            except TypeError:
                pass
            else:
                raise AssertionError("應拋例外")
    assert sum("capability_snapshot" in r.getMessage() for r in caplog.records) == 1


def test_bind_helper_reaches_primary_and_inner_laya(monkeypatch):
    p, agent = _proposer(monkeypatch)
    wrapper = SimpleNamespace(laya=p)
    orch = SimpleNamespace(proposer=wrapper, _shadows=[(SimpleNamespace(), None)])
    assert bind_laya_capabilities(orch, _registry([("lore.read", "ready")])) == 1
    p.propose(_cand())
    assert agent.states[-1]["available_capabilities"] == "query"
    # 註冊表缺方法：推論得到 none，不崩
    bind_laya_capabilities(orch, object())
    p.propose(_cand())
    assert agent.states[-1]["available_capabilities"] == "none"
