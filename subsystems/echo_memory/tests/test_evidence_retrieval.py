"""EM-005：證據檢索、交付狀態語意、prompt 注入規則。"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from echo_memory.engine import MemoryEngine
from echo_memory.schemas.evidence import (
    EvidenceInput,
    SourceRef,
    TextSpan,
    can_attest_agent_said,
)


@pytest.fixture
def engine(tmp_path):
    return MemoryEngine(storage_path=tmp_path / "data", namespace_id="ns1")


def _ev(user, reply, *, state="delivered", units=None, span=True, speaker="42"):
    return [
        EvidenceInput(
            source_type="user", content=user, speaker_id=speaker,
            source_ref=SourceRef(kind="discord_voice", channel="voice", ref_ids=("m1",)),
            occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        EvidenceInput(
            source_type="agent", content=reply, speaker_id="agent",
            delivery_state=state, delivery_unit="sentence", delivered_units=units,
            text_span=TextSpan(0, len(reply)) if span else None,
        ),
    ]


def _store_mixed(engine):
    """known 已交付、known 被打斷無進度、known 有進度、not_delivered、legacy 各一。"""
    ids = {}
    ids["delivered"] = engine.store_episode(
        "我喜歡手沖咖啡", "咖啡很香", "s1", evidence=_ev("我喜歡手沖咖啡", "咖啡很香"))
    ids["partial_none"] = engine.store_episode(
        "我養了一隻貓", "貓很可愛。牠叫什麼名字？", "s1",
        evidence=_ev("我養了一隻貓", "貓很可愛。牠叫什麼名字？", state="partial", span=False))
    ids["partial_prog"] = engine.store_episode(
        "我住在台北", "台北不錯。", "s1",
        evidence=_ev("我住在台北", "台北不錯。", state="partial", units=1))
    ids["not_delivered"] = engine.store_episode(
        "今天好累", "", "s1",
        evidence=_ev("今天好累", "早點休息吧", state="not_delivered"))
    ids["legacy"] = engine.store_episode("舊介面輸入", "舊介面回覆", "s1")
    return ids


# ─── 交付狀態語意（與 Echo Stream 的對接約定）──────────────────────


def test_interrupted_without_progress_accepts_full_planned_reply(engine):
    reply = "貓很可愛。牠叫什麼名字？"
    ep_id = engine.store_episode(
        "我養了一隻貓", reply, "s1",
        evidence=_ev("我養了一隻貓", reply, state="partial", units=None, span=False))
    agent = next(u for u in engine.get_episode_evidence(ep_id) if u.source_type == "agent")
    assert agent.delivery_state == "partial"
    assert agent.delivered_units is None
    assert agent.text_span is None
    assert agent.content == reply  # 完整預定回覆逐字保留
    assert not can_attest_agent_said(agent)


def test_interrupted_with_progress_holds_played_prefix(engine):
    ep_id = engine.store_episode(
        "我住在台北", "台北不錯。", "s1",
        evidence=_ev("我住在台北", "台北不錯。", state="partial", units=1))
    agent = next(u for u in engine.get_episode_evidence(ep_id) if u.source_type == "agent")
    assert agent.delivered_units == 1 and agent.content == "台北不錯。"
    assert can_attest_agent_said(agent)


# ─── 證據檢索 ──────────────────────────────────────────────────


def test_retrieve_evidence_requires_query_or_ids(engine):
    with pytest.raises(ValueError):
        engine.retrieve_evidence()
    with pytest.raises(ValueError):
        engine.retrieve_evidence(episode_ids=["x"], source_types=["bogus"])


def test_retrieve_evidence_by_episode_ids_returns_units_in_order(engine):
    ids = _store_mixed(engine)
    units = engine.retrieve_evidence(episode_ids=[ids["partial_prog"], ids["delivered"]])
    assert [u.episode_id for u in units] == [ids["partial_prog"]] * 2 + [ids["delivered"]] * 2
    user = units[0]
    assert user.source_type == "user" and user.speaker_id == "42"
    assert user.content == "我住在台北"  # 逐字原文
    assert user.provenance_status == "known"


def test_retrieve_evidence_source_types_filter(engine):
    ids = _store_mixed(engine)
    units = engine.retrieve_evidence(
        episode_ids=list(ids.values()), source_types=["user"])
    assert {u.source_type for u in units} == {"user"}
    assert len(units) == 5


def test_retrieve_evidence_require_delivered(engine):
    ids = _store_mixed(engine)
    units = engine.retrieve_evidence(
        episode_ids=list(ids.values()), source_types=["agent"], require_delivered=True)
    got = {u.episode_id for u in units}
    # 已交付與有進度的 partial 保留；無進度 partial、not_delivered、legacy unknown 排除
    assert got == {ids["delivered"], ids["partial_prog"]}


def test_retrieve_evidence_include_legacy_false(engine):
    ids = _store_mixed(engine)
    units = engine.retrieve_evidence(episode_ids=list(ids.values()), include_legacy=False)
    assert all(u.provenance_status == "known" for u in units)
    assert ids["legacy"] not in {u.episode_id for u in units}
    with_legacy = engine.retrieve_evidence(episode_ids=[ids["legacy"]])
    assert {u.provenance_status for u in with_legacy} == {"legacy_unknown"}


def test_retrieve_evidence_by_query_expands_episode_hits(engine):
    ids = _store_mixed(engine)
    ep = engine.episodic.get(ids["delivered"])
    units = engine.retrieve_evidence(
        ep.content_summary, top_k=3, min_similarity=0.3, source_types=["user", "agent"])
    assert units and units[0].episode_id == ids["delivered"]
    assert {u.source_type for u in units[:2]} == {"user", "agent"}


def test_retrieve_evidence_query_and_ids_intersect(engine):
    ids = _store_mixed(engine)
    ep = engine.episodic.get(ids["delivered"])
    units = engine.retrieve_evidence(
        ep.content_summary, episode_ids=[ids["partial_prog"]], min_similarity=0.3)
    assert units == []


def test_retrieve_evidence_synthesizes_legacy_for_unmigrated_episode(engine):
    ids = _store_mixed(engine)
    ep = engine.episodic.get(ids["legacy"])
    ep.evidence_ids = []  # 模擬遷移前的舊 Episode
    engine.episodic.write(ep)
    units = engine.retrieve_evidence(episode_ids=[ids["legacy"]])
    assert len(units) == 2
    assert {u.provenance_status for u in units} == {"legacy_unknown"}


def test_retrieve_evidence_skips_tombstoned(engine, monkeypatch):
    from dataclasses import replace
    ids = _store_mixed(engine)
    orig = engine.evidence.units_for_episodes

    def _tomb(eps):
        out = orig(eps)
        return {k: [replace(u, tombstoned_at=datetime.now(timezone.utc)) for u in v]
                for k, v in out.items()}

    monkeypatch.setattr(engine.evidence, "units_for_episodes", _tomb)
    assert engine.retrieve_evidence(episode_ids=[ids["delivered"]]) == []


# ─── prompt 注入規則 ────────────────────────────────────────────


def _entries(ctx):
    text = ctx.to_text()
    body = text.split("\n", 2)[2].rsplit("\n</UNTRUSTED_MEMORY_DATA>", 1)[0]
    return json.loads(body)["相關對話記憶"], text


def _ctx_for(engine, ep_id):
    ep = engine.episodic.get(ep_id)
    ctx = engine.retrieve(ep.content_summary, top_k=5, min_similarity=0.3)
    assert ep_id in {e.id for e in ctx.episodes}
    return ctx


def test_prompt_omits_not_delivered_agent_content(engine):
    ids = _store_mixed(engine)
    entries, text = _entries(_ctx_for(engine, ids["not_delivered"]))
    assert "早點休息吧" not in text
    entry = next(e for e in entries if e["user_input"] == "今天好累")
    assert entry["agent_response"] == ""
    assert "未說出口" in entry["agent_delivery"]


def test_prompt_omits_partial_without_progress(engine):
    ids = _store_mixed(engine)
    entries, text = _entries(_ctx_for(engine, ids["partial_none"]))
    assert "牠叫什麼名字" not in text
    entry = next(e for e in entries if e["user_input"] == "我養了一隻貓")
    assert entry["agent_response"] == ""
    assert "未說出口" in entry["agent_delivery"]


def test_prompt_keeps_delivered_prefix_and_delivered(engine):
    ids = _store_mixed(engine)
    for key, user, said in (("partial_prog", "我住在台北", "台北不錯。"),
                            ("delivered", "我喜歡手沖咖啡", "咖啡很香")):
        entries, _ = _entries(_ctx_for(engine, ids[key]))
        entry = next(e for e in entries if e["user_input"] == user)
        assert entry["agent_response"] == said
        assert "agent_delivery" not in entry


def test_prompt_keeps_legacy_unknown_agent_text(engine):
    """舊資料 delivery_state=unknown 無從得知，維持既有顯示行為（不是 not_delivered）。"""
    ids = _store_mixed(engine)
    entries, _ = _entries(_ctx_for(engine, ids["legacy"]))
    entry = next(e for e in entries if e["user_input"] == "舊介面輸入")
    assert entry["agent_response"] == "舊介面回覆"
    assert "agent_delivery" not in entry
