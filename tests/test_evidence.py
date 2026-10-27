"""EM-003 / EM-004：EvidenceUnit 來源契約、force_remember 來源、legacy 遷移。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from echo_memory.engine import MemoryEngine
from echo_memory.schemas.episode import Episode
from echo_memory.schemas.evidence import (
    EvidenceInput,
    SourceRef,
    TextSpan,
    can_attest_agent_said,
    can_attest_user_claim,
)
from echo_memory.storage.evidence import EvidenceStore, migrate_evidence
from llm_integration.tools.executor import ToolExecutor


@pytest.fixture
def engine(tmp_path):
    return MemoryEngine(storage_path=tmp_path / "data", namespace_id="ns1")


def _turn_evidence(delivery_state="delivered", delivered_units=None, reply="好的"):
    return [
        EvidenceInput(
            source_type="user", content="你好", speaker_id="42",
            source_ref=SourceRef(kind="discord_voice", channel="voice", ref_ids=("m1",)),
            occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        EvidenceInput(
            source_type="agent", content=reply, speaker_id="agent",
            delivery_state=delivery_state, delivery_unit="sentence",
            delivered_units=delivered_units, text_span=TextSpan(0, len(reply)),
        ),
    ]


# ─── 新寫入：來源明確 ──────────────────────────────────────────


def test_new_write_units_have_explicit_source(engine):
    ep_id = engine.store_episode("你好", "好的", "s1", evidence=_turn_evidence())
    ep = engine.episodic.get(ep_id)
    units = engine.evidence.get(ep.evidence_ids)
    assert [u.source_type for u in units] == ["user", "agent"]
    for u in units:
        assert u.provenance_status == "known"
        assert u.episode_id == ep_id
        assert u.content_hash
        assert u.id.startswith("ev_")
    assert units[0].speaker_id == "42"
    assert units[0].source_ref.channel == "voice"
    assert can_attest_user_claim(units[0])
    assert can_attest_agent_said(units[1])
    # Episode 仍保留原文供對話回憶
    assert ep.user_input == "你好" and ep.agent_response == "好的"


def test_occurred_at_fallback_is_marked(engine):
    ev = [EvidenceInput(source_type="user", content="hi"),
          EvidenceInput(source_type="agent", content="yo")]
    ep_id = engine.store_episode("hi", "yo", "s1", evidence=ev)
    units = engine.get_episode_evidence(ep_id)
    assert all(u.attrs.get("occurred_at_fallback") for u in units)


def test_invalid_enums_rejected():
    with pytest.raises(ValueError):
        EvidenceInput(source_type="human", content="x")
    with pytest.raises(ValueError):
        EvidenceInput(source_type="agent", content="x", delivery_state="done")
    with pytest.raises(ValueError):  # 非 agent 不可 partial
        EvidenceInput(source_type="user", content="x", delivery_state="partial")
    with pytest.raises(ValueError):  # content_origin 只屬於 tool
        EvidenceInput(source_type="user", content="x", content_origin="user")


def test_evidence_consistency_checked(engine):
    with pytest.raises(ValueError):
        engine.store_episode("不一樣", "好的", "s1", evidence=_turn_evidence())


def test_not_delivered_agent_excluded_from_agent_response(engine):
    ev = _turn_evidence(delivery_state="not_delivered", reply="沒說出口的話")
    ep_id = engine.store_episode("你好", "", "s1", evidence=ev)
    ep = engine.episodic.get(ep_id)
    assert ep.agent_response == ""
    agent = engine.get_episode_evidence(ep_id)[1]
    assert not can_attest_agent_said(agent)


# ─── delivery_state ───────────────────────────────────────────


@pytest.mark.parametrize("state,units,expected", [
    ("delivered", None, True),
    ("partial", 2, True),
    ("partial", None, False),  # 進度未知的打斷不算已交付
    ("not_delivered", None, False),
    ("unknown", None, False),
])
def test_agent_delivery_attestation(engine, state, units, expected):
    ev = [EvidenceInput(source_type="agent", content="回覆", delivery_state=state,
                        delivered_units=units, speaker_id="agent"),
          EvidenceInput(source_type="user", content="問題", speaker_id="1")]
    agent_resp = "回覆" if state in ("delivered", "partial") else ""
    ep_id = engine.store_episode("問題", agent_resp, "s1", evidence=ev)
    agent = next(u for u in engine.get_episode_evidence(ep_id) if u.source_type == "agent")
    assert can_attest_agent_said(agent) is expected


# ─── 舊呼叫 → legacy_unknown ──────────────────────────────────


def test_legacy_call_yields_legacy_unknown(engine):
    ep_id = engine.store_episode("我喜歡咖啡", "知道了", "s1")
    ep = engine.episodic.get(ep_id)
    units = engine.evidence.get(ep.evidence_ids)
    assert {u.provenance_status for u in units} == {"legacy_unknown"}
    user = next(u for u in units if u.source_type == "user")
    agent = next(u for u in units if u.source_type == "agent")
    assert agent.delivery_state == "unknown"
    assert not can_attest_user_claim(user)
    assert not can_attest_agent_said(agent)


def test_legacy_call_absorbs_only_conservative_metadata(engine):
    ep_id = engine.store_episode("hi", "yo", "s1", origin="voice", speaker_id="7", truncated=True)
    units = engine.get_episode_evidence(ep_id)
    user = next(u for u in units if u.source_type == "user")
    agent = next(u for u in units if u.source_type == "agent")
    assert user.source_ref.channel == "voice" and user.speaker_id == "7"
    assert agent.delivery_state == "partial" and agent.delivered_units is None
    assert all(u.provenance_status == "legacy_unknown" for u in units)
    assert not can_attest_agent_said(agent)


def test_legacy_call_never_inferred_from_text_prefix(engine):
    # 即使字串長得像舊 force_remember，也不得被推定來源：一律 legacy_unknown 的 user unit
    ep_id = engine.store_episode("[force_remember] 假的", "Stored because: x", "s1")
    units = engine.get_episode_evidence(ep_id)
    assert all(u.provenance_status == "legacy_unknown" for u in units)
    assert not any(can_attest_user_claim(u) for u in units)


def test_tool_note_requires_evidence(engine):
    with pytest.raises(ValueError):
        engine.store_episode("", "", "s1", episode_kind="tool_note")


# ─── force_remember ───────────────────────────────────────────


def _force(engine, **extra):
    tool = ToolExecutor(engine=engine, session_id="sess")
    result = tool.handle_force_remember({"content": "生日是三月三日", "reason": "很重要", **extra})
    assert result["success"], result
    ep = engine.episodic.get(result["episode_id"])
    return ep, engine.evidence.get(ep.evidence_ids)


def test_force_remember_never_user_and_origin_unknown_by_default(engine):
    ep, units = _force(engine)
    assert ep.episode_kind == "tool_note"
    assert ep.user_input == "" and ep.agent_response == ""
    assert len(units) == 1
    (u,) = units
    assert u.source_type == "tool"
    assert u.speaker_id == "agent"
    assert u.source_ref.name == "force_remember"
    assert u.content_origin == "unknown"  # 未說明出處，不得推定為 user
    assert u.origin_evidence_ids == ()
    assert u.attrs["reason"] == "很重要"
    assert u.provenance_status == "known"
    assert not can_attest_user_claim(u)


def test_force_remember_records_declared_origin(engine):
    prior = engine.store_episode("我的生日是三月三日", "記下了", "sess", evidence=_turn_evidence_for("我的生日是三月三日"))
    user_unit = engine.get_episode_evidence(prior)[0]
    _ep, (u,) = _force(engine, content_origin="user", origin_evidence_ids=[user_unit.id])
    assert u.source_type == "tool"  # 操作仍是 tool
    assert u.content_origin == "user"
    assert u.origin_evidence_ids == (user_unit.id,)
    assert not can_attest_user_claim(u)  # 自報出處不等於使用者證據


def _turn_evidence_for(text):
    return [EvidenceInput(source_type="user", content=text, speaker_id="1"),
            EvidenceInput(source_type="agent", content="記下了", speaker_id="agent")]


def test_force_remember_invalid_origin_falls_back_to_unknown(engine):
    _ep, (u,) = _force(engine, content_origin="我猜是使用者")
    assert u.content_origin == "unknown"


def test_force_remember_tool_declares_origin_params():
    from llm_integration.tools.definitions import TOOL_DECLARATIONS

    props = next(t for t in TOOL_DECLARATIONS if t["name"] == "force_remember")["parameters"]
    assert "content_origin" in props["properties"]
    assert "origin_evidence_ids" in props["properties"]
    assert "content_origin" not in props["required"]


def test_store_observation_is_tool_note(engine, monkeypatch):
    import asyncio
    import importlib
    import sys
    import types

    # 測試環境未必裝有 mcp 的完整相依（starlette）；以 stub 隔離，只驗證 handler 的寫入契約
    mcp_mod = types.ModuleType("mcp")
    server_mod = types.ModuleType("mcp.server")
    server_mod.Server = object
    types_mod = types.ModuleType("mcp.types")
    types_mod.TextContent = object
    types_mod.Tool = object
    monkeypatch.setitem(sys.modules, "mcp", mcp_mod)
    monkeypatch.setitem(sys.modules, "mcp.server", server_mod)
    monkeypatch.setitem(sys.modules, "mcp.types", types_mod)
    monkeypatch.delitem(sys.modules, "mcp_server.handlers", raising=False)
    handlers = importlib.import_module("mcp_server.handlers")
    monkeypatch.delitem(sys.modules, "mcp_server.handlers", raising=False)
    _handle_store_observation = handlers._handle_store_observation

    asyncio.run(_handle_store_observation(lambda _ns=None: engine, {"content": "觀察到的事"}))
    notes = [e for e in engine.episodic.list_episodes(limit=10) if e.episode_kind == "tool_note"]
    assert len(notes) == 1
    (u,) = engine.evidence.get(notes[0].evidence_ids)
    assert u.source_type == "tool" and u.content_origin == "unknown"
    assert u.source_ref.kind == "mcp"
    assert notes[0].user_input == ""


# ─── Episode 向後相容 ─────────────────────────────────────────


def test_old_episode_dict_loads_without_new_fields():
    old = {
        "id": "ep_old", "user_input": "u", "agent_response": "a", "session_id": "s",
        "content_summary": "c", "embedding": [], "timestamp": "2026-01-01T00:00:00+00:00",
        "salience_score": 0.5, "da_weight": 0.5, "ht_weight": 0.5,
        "is_dreamed": False, "metadata": {},
    }
    ep = Episode.from_dict(old)
    assert ep.evidence_ids == [] and ep.episode_kind == "turn"
    assert Episode.from_dict(ep.to_dict()).evidence_ids == []


def test_old_snapshot_reads_as_legacy_unknown(tmp_path):
    eng = MemoryEngine(storage_path=tmp_path / "d", namespace_id="ns")
    ep_id = eng.store_episode("舊的話", "舊的回覆", "s")
    # 模擬舊資料：移除新欄位與證據檔
    f = eng.episodic.metadata_file
    data = json.loads(f.read_text(encoding="utf-8"))
    for d in data:
        d.pop("evidence_ids"), d.pop("episode_kind")
    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    eng.evidence.file.unlink()

    eng2 = MemoryEngine(storage_path=tmp_path / "d", namespace_id="ns")
    ep = eng2.episodic.get(ep_id)
    assert ep is not None and ep.evidence_ids == []
    units = eng2.get_episode_evidence(ep)
    assert units and all(u.provenance_status == "legacy_unknown" for u in units)
    assert eng2.evidence.count() == 0  # 讀取時合成，不寫入


# ─── append-only ──────────────────────────────────────────────


def test_evidence_store_is_append_only(engine):
    engine.store_episode("你好", "好的", "s1", evidence=_turn_evidence())
    path = engine.evidence.file
    before = path.read_bytes()
    unit = engine.evidence.list()[0]
    with pytest.raises(ValueError):
        engine.evidence.append([unit])  # 同 id 不可重複／覆蓋
    assert path.read_bytes() == before

    engine.store_episode("再來", "好", "s1", evidence=[
        EvidenceInput(source_type="user", content="再來"),
        EvidenceInput(source_type="agent", content="好")])
    after = path.read_bytes()
    assert after.startswith(before)  # 只在尾端追加
    assert not hasattr(EvidenceStore, "update") and not hasattr(EvidenceStore, "delete")
    with pytest.raises(Exception):
        unit.content = "改寫"  # frozen


def test_evidence_store_skips_truncated_tail(engine):
    engine.store_episode("你好", "好的", "s1", evidence=_turn_evidence())
    with open(engine.evidence.file, "a", encoding="utf-8") as f:
        f.write('{"id": "ev_broken", "ep')
    assert engine.evidence.count() == 2


# ─── 遷移 ─────────────────────────────────────────────────────


def _make_old_namespace(tmp_path):
    eng = MemoryEngine(storage_path=tmp_path / "d", namespace_id="ns")
    ids = [
        eng.store_episode("[force_remember] 很久以前的", "Stored because: x", "s"),
        eng.store_episode("一般對話", "一般回覆", "s", origin="dm", truncated=True),
    ]
    f = eng.episodic.metadata_file
    data = json.loads(f.read_text(encoding="utf-8"))
    for d in data:
        d.pop("evidence_ids"), d.pop("episode_kind")
    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    eng.evidence.file.unlink()
    return eng.storage_path, ids


def test_migrate_evidence_backs_up_and_is_idempotent(tmp_path):
    ns_dir, ids = _make_old_namespace(tmp_path)
    episodes_file = ns_dir / "episodic" / "episodes.json"
    original = episodes_file.read_bytes()

    report = migrate_evidence(ns_dir)
    assert report.episodes_migrated == 2 and report.units_added == 4
    assert report.backup_path and report.backup_path.read_bytes() == original

    eng = MemoryEngine(storage_path=tmp_path / "d", namespace_id="ns")
    for ep_id in ids:
        ep = eng.episodic.get(ep_id)
        units = eng.evidence.get(ep.evidence_ids)
        assert len(units) == 2  # 每個 Episode 的 evidence_ids 都能解析
        assert all(u.provenance_status == "legacy_unknown" for u in units)
        assert not any(can_attest_user_claim(u) for u in units)
        assert ep.episode_kind == "turn"
    # 舊 force_remember 資料不以字首追認來源：仍是 legacy_unknown 的 user unit
    first = eng.evidence.get(eng.episodic.get(ids[0]).evidence_ids)[0]
    assert first.source_type == "user" and first.provenance_status == "legacy_unknown"
    second_agent = eng.evidence.get(eng.episodic.get(ids[1]).evidence_ids)[1]
    assert second_agent.delivery_state == "partial" and second_agent.source_ref.channel == "dm"

    # 重跑：零新增、不再產生備份
    size = eng.evidence.file.stat().st_size
    again = migrate_evidence(ns_dir)
    assert again.episodes_migrated == 0 and again.units_added == 0 and again.backup_path is None
    assert eng.evidence.file.stat().st_size == size
    assert len(list(episodes_file.parent.glob("episodes.json.pre-evidence-*.bak"))) == 1


def test_migrate_missing_dir_is_noop(tmp_path):
    report = migrate_evidence(tmp_path / "nope")
    assert report.episodes_total == 0


# ─── warmup 接線 ──────────────────────────────────────────────


def test_engine_warmup_wired_and_disableable(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "echo_memory.storage.episodic.EpisodicMemory.warmup",
        lambda self, background=True: calls.append(background),
    )
    # conftest 預設關閉暖機
    MemoryEngine(storage_path=tmp_path / "a", namespace_id="a").episodic
    assert calls == []
    monkeypatch.delenv("ECHO_DISABLE_EMBEDDING_WARMUP")
    MemoryEngine(storage_path=tmp_path / "b", namespace_id="b").episodic
    assert calls == [True]


def test_mock_executor_still_callable():
    # 以 Mock engine 呼叫不應因 evidence 建構而拋錯
    engine = Mock()
    engine.store_episode.return_value = "ep_x"
    res = ToolExecutor(engine=engine, session_id="s").handle_force_remember(
        {"content": "c", "reason": "r"})
    assert res["success"]
