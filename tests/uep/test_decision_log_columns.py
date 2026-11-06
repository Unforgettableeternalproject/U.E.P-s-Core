"""決策日誌新增四欄（migration 0014：sub_kind／trigger／namespace／snapshot_version）。

離線部分驗證 primary_row 取值、不合格值記 None、舊資料（四欄 NULL）相容；DB 部分（無 UEP_TEST_DATABASE_URL 時 skip）
驗證 0013<->0014 往返、DbSink 寫入讀回與 sub_kind 過濾。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from alembic.script import ScriptDirectory

from tests.uep.conftest import alembic_config
from tests.uep.test_decision_log import _P, ListSink, _cand, _mode, _stack
from uep.decision import DecisionRequest
from uep.decision.log import COLUMNS, primary_row, shadow_row
from uep.decision.replay import analyze, filter_rows, format_report, read_jsonl
from uep.decision.turn_reply import KIND_TURN_REPLY
from uep.turn.models import ReplyMode

NEW_COLUMNS = ("sub_kind", "trigger", "namespace", "snapshot_version")
SECRET_TEXT = "這是不該進日誌的原文"


def _row_for(**request_kw) -> dict:
    """走真實中樞決策一次，再以指定 request 欄位重組 primary 日誌列。"""
    hub, engine, writer = _stack(_P(_mode(ReplyMode.RESPOND)))
    try:
        base = DecisionRequest(request_id="req_c", kind=KIND_TURN_REPLY, native=_cand())
        result = hub.decide(KIND_TURN_REPLY, base)
        spec = hub.registry.get(KIND_TURN_REPLY)
        request = DecisionRequest(request_id="req_c", kind=KIND_TURN_REPLY, native=_cand(), **request_kw)
        return primary_row(request, result, spec, 3)
    finally:
        engine.close()
        writer.close()


def test_new_columns_are_appended_to_log_columns():
    assert COLUMNS[-4:] == NEW_COLUMNS


def test_primary_row_fills_new_columns():
    row = _row_for(
        trigger="tick", subject={"namespace": "ns_main"}, facts={"sub_kind": "output_modality"},
        snapshot_versions={"policy": 7},
    )
    assert (row["sub_kind"], row["trigger"], row["namespace"], row["snapshot_version"]) == (
        "output_modality", "tick", "ns_main", 7,
    )
    assert set(row) == set(COLUMNS)


def test_primary_row_defaults_trigger_and_leaves_others_none():
    row = _row_for()
    assert row["trigger"] == "initial"
    assert (row["sub_kind"], row["namespace"], row["snapshot_version"]) == (None, None, None)


@pytest.mark.parametrize("bad", [
    "", "A", "UPPER", "has space", "x" * 65, SECRET_TEXT, "含中文", "line\nbreak", 5, None, True, ["speak"],
])
def test_primary_row_rejects_unsafe_token_values(bad):
    row = _row_for(facts={"sub_kind": bad}, subject={"namespace": bad})
    assert row["sub_kind"] is None and row["namespace"] is None


def test_primary_row_accepts_boundary_token_lengths():
    ok = "a" * 64
    row = _row_for(facts={"sub_kind": ok}, subject={"namespace": "ns.1:a-b_c"})
    assert row["sub_kind"] == ok and row["namespace"] == "ns.1:a-b_c"


@pytest.mark.parametrize("bad", [True, False, "7", 7.5, None])
def test_primary_row_rejects_non_int_snapshot_version(bad):
    row = _row_for(snapshot_versions={"policy": bad})
    assert row["snapshot_version"] is None


def test_primary_row_ignores_other_snapshot_keys_and_never_stores_text():
    row = _row_for(snapshot_versions={"other": 3}, facts={"sub_kind": SECRET_TEXT})
    assert row["snapshot_version"] is None
    assert SECRET_TEXT not in json.dumps(row, default=str)


def test_shadow_row_new_columns_stay_none():
    class Rec:
        decision_id, request_id, kind, primary_decision_id = "dec_s", "req", KIND_TURN_REPLY, "dec_p"
        shadow_id, shadow_mode, shadow_reason, elapsed_ms = "s.1", "pass", "r", 2
        status, agree, candidate_id = "ok", False, "cand1"

    row = shadow_row(Rec())
    assert all(row[c] is None for c in NEW_COLUMNS)
    assert set(row) == set(COLUMNS)


# ---- 舊資料相容（新欄全為 NULL）----

def _legacy_rows() -> list[dict]:
    """沒有新四欄鍵的舊式列（舊 JSONL／舊 DB 版本輸出形狀）。"""
    now = datetime(2026, 11, 1, tzinfo=UTC)
    base = dict.fromkeys(COLUMNS[:-4])
    primary = {**base, "decision_id": "dec_p", "kind": KIND_TURN_REPLY, "created_at": now, "role": "primary",
               "outcome": "respond", "reason_code": "r", "status": "ok", "elapsed_ms": 4, "fallback": False}
    shadow = {**base, "decision_id": "dec_s", "kind": KIND_TURN_REPLY, "created_at": now + timedelta(seconds=1),
              "role": "shadow", "primary_decision_id": "dec_p", "proposer_id": "s.1", "outcome": "pass",
              "reason_code": "r2", "status": "ok", "elapsed_ms": 2, "agree": False}
    return [primary, shadow]


def test_replay_output_identical_for_null_new_columns():
    legacy = _legacy_rows()
    nulled = [{**r, **dict.fromkeys(NEW_COLUMNS)} for r in legacy]
    assert analyze(nulled) == analyze(legacy)
    assert format_report(analyze(nulled)) == format_report(analyze(legacy))
    since = datetime(2026, 10, 1, tzinfo=UTC)
    assert filter_rows(nulled, kind=KIND_TURN_REPLY, since=since) == [
        {**r, **dict.fromkeys(NEW_COLUMNS)} for r in filter_rows(legacy, kind=KIND_TURN_REPLY, since=since)
    ]


def test_replay_reads_jsonl_written_with_null_new_columns(tmp_path):
    from uep.decision.log import JsonlSink

    path = tmp_path / "log.jsonl"
    JsonlSink(path).write([{**r, **dict.fromkeys(NEW_COLUMNS)} for r in _legacy_rows()])
    rows = read_jsonl(path)
    assert all(r[c] is None for r in rows for c in NEW_COLUMNS)
    legacy_jsonl = tmp_path / "legacy.jsonl"
    JsonlSink(legacy_jsonl).write(_legacy_rows())
    assert analyze(rows) == analyze(read_jsonl(legacy_jsonl))


def test_writer_end_to_end_shadow_none_and_primary_trigger_default():
    sink = ListSink()
    shadow = _P(_mode(ReplyMode.PASS, pid="s.x"), pid="s.x")
    hub, engine, writer = _stack(_P(_mode(ReplyMode.RESPOND)), shadows=[shadow], sink=sink)
    try:
        hub.decide(KIND_TURN_REPLY, DecisionRequest(request_id="r", kind=KIND_TURN_REPLY, native=_cand()))
        assert engine.wait_shadow(2) and writer.flush()
    finally:
        engine.close()
        writer.close()
    shadow_rows = [r for r in sink.rows if r["role"] == "shadow"]
    primary_rows = [r for r in sink.rows if r["role"] == "primary"]
    assert shadow_rows and all(r[c] is None for r in shadow_rows for c in NEW_COLUMNS)
    assert primary_rows[0]["trigger"] == "initial"


# ---- migration（無 DB 的基本檢查）----

def test_migration_0014_revision_chain():
    script = ScriptDirectory.from_config(alembic_config("postgresql://x/y_test"))
    rev = script.get_revision("0014_decision_log_columns")
    assert rev is not None and rev.down_revision == "0013_run_supervisor_flags"
    assert callable(rev.module.upgrade) and callable(rev.module.downgrade)
    assert script.get_heads() == ["0014_decision_log_columns"]


def test_orm_model_has_new_columns_and_index():
    import uep.db.models_registry  # noqa: F401
    from uep.db.base import Base

    table = Base.metadata.tables["decision_log"]
    assert [c.name for c in table.columns] == list(COLUMNS)
    assert all(table.columns[c].nullable for c in NEW_COLUMNS)
    assert ("kind", "sub_kind") in {tuple(c.name for c in ix.columns) for ix in table.indexes}


# ---- 需要 PostgreSQL（UEP_TEST_DATABASE_URL；由協調者統一執行）----

def test_db_migration_0014_upgrade_downgrade_roundtrip(alembic_cfg, engine):
    from alembic import command
    from sqlalchemy import inspect

    def columns():
        return {c["name"] for c in inspect(engine).get_columns("decision_log")}

    def indexes():
        return {i["name"] for i in inspect(engine).get_indexes("decision_log")}

    assert set(NEW_COLUMNS) <= columns() and "ix_decision_log_kind_sub_kind" in indexes()
    try:
        command.downgrade(alembic_cfg, "0013_run_supervisor_flags")
        assert not (set(NEW_COLUMNS) & columns())
        assert "ix_decision_log_kind_sub_kind" not in indexes()
    finally:
        command.upgrade(alembic_cfg, "head")
    assert set(NEW_COLUMNS) <= columns() and "ix_decision_log_kind_sub_kind" in indexes()


def test_db_sink_roundtrip_new_columns_and_sub_kind_filter(engine, session_factory):
    from sqlalchemy import text

    from uep.decision.db import DbSink, load_rows

    def row(did, **extra):
        r = dict.fromkeys(COLUMNS)
        r.update(decision_id=did, kind="loop.consult", created_at=datetime.now(UTC), role="primary", status="ok", **extra)
        return r

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM decision_log"))
    try:
        DbSink(session_factory).write([
            row("dec_a", sub_kind="speak", trigger="event", namespace="ns_main", snapshot_version=3),
            row("dec_b", sub_kind="placement"),
            row("dec_legacy"),  # 新欄全 NULL（舊資料形狀）
        ])
        rows = {r["decision_id"]: r for r in load_rows(session_factory, kind="loop.consult")}
        assert len(rows) == 3
        a = rows["dec_a"]
        assert (a["sub_kind"], a["trigger"], a["namespace"], a["snapshot_version"]) == ("speak", "event", "ns_main", 3)
        assert all(rows["dec_legacy"][c] is None for c in NEW_COLUMNS)
        speak = load_rows(session_factory, kind="loop.consult", sub_kind="speak")
        assert [r["decision_id"] for r in speak] == ["dec_a"]  # NULL 列不算符合
        assert load_rows(session_factory, sub_kind="nope") == []
        # 不帶 sub_kind：舊列照常回傳，replay 輸出不受影響
        assert analyze(list(rows.values()))["total_rows"] == 3
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM decision_log"))
