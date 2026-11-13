"""DL-3：決策日誌寫入器與重放評估工具（無 DB 部分），以及需要 DB 的 migration／sink 測試（無 UEP_TEST_DATABASE_URL 時 skip）。"""
from __future__ import annotations

import json
import threading
import time
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
from alembic.script import ScriptDirectory

from tests.uep.conftest import alembic_config
from uep.decision import (
    DecisionHub,
    DecisionPointRegistry,
    DecisionPointSpec,
    DecisionRequest,
)
from uep.decision.db import DbSink, DecisionLogRow, create_default_writer, load_rows
from uep.decision.log import COLUMNS, DecisionLogWriter, JsonlSink, _primary_status
from uep.decision.replay import (
    analyze,
    filter_rows,
    format_report,
    main,
    percentile,
    read_jsonl,
)
from uep.decision.replay_turn import (
    candidate_from_dict,
    candidate_to_dict,
    rerun_candidates,
)
from uep.decision.turn_reply import KIND_TURN_REPLY, TurnReplyEngine, turn_reply_spec
from uep.turn.models import Proposal, ReplyMode, SessionContext, TurnCandidate

CTX = SessionContext(conversation_id="c1", segment_id="s1", audience_scope="aud", session_ref="sess")
SECRET_TEXT = "這是不該進日誌的原文"


def _cand(cid="cand1", *, addressed=True, count=2, text=SECRET_TEXT) -> TurnCandidate:
    return TurnCandidate(
        candidate_id=cid, utterance_refs=("evt_a", "evt_b"), speakers=("discord:1",), ctx=CTX,
        interaction_state="IDLE", interaction_version=1, presence_active=True, text=text,
        participant_count=count, addressed_hint=addressed,
    )


class _P:
    def __init__(self, fn, pid="t.p"):
        self.fn, self.proposer_id = fn, pid

    def propose(self, candidate):
        return self.fn(candidate)


def _mode(mode: ReplyMode, pid="t.p", reason="r"):
    return lambda _c: Proposal(reply_mode=mode, reason_code=reason, proposer_id=pid, proposer_version="1")


class ListSink:
    def __init__(self):
        self.rows: list[dict] = []
        self.lock = threading.Lock()

    def write(self, rows):
        with self.lock:
            self.rows.extend(dict(r) for r in rows)


def _stack(primary, *, shadows=(), sink=None, budget_ms=300, **writer_kw):
    registry = DecisionPointRegistry()
    hub = DecisionHub(registry)
    metrics: Counter[str] = Counter()
    engine = TurnReplyEngine(primary, registry=registry, metrics=metrics, shadow_proposers=shadows)
    hub.register(turn_reply_spec(budget_ms), engine)
    writer = DecisionLogWriter(sink, flush_interval=0.01, **writer_kw)
    writer.attach(hub)
    return hub, engine, writer


def _decide(hub, cand=None, request_id="req_x"):
    cand = cand or _cand()
    return hub.decide(KIND_TURN_REPLY, DecisionRequest(request_id=request_id, kind=KIND_TURN_REPLY, native=cand))


# ---- 寫入器 ----

def test_primary_row_has_summary_without_text():
    sink = ListSink()
    hub, engine, writer = _stack(_P(_mode(ReplyMode.RESPOND)), sink=sink)
    try:
        result = _decide(hub)
        assert writer.flush()
    finally:
        engine.close()
        writer.close()
    (row,) = sink.rows
    assert set(row) == set(COLUMNS)
    assert row["decision_id"] == result.decision_id and row["role"] == "primary" and row["kind"] == KIND_TURN_REPLY
    assert (row["outcome"], row["status"], row["fallback"], row["proposer_id"]) == ("respond", "ok", False, "t.p")
    assert (row["candidate_id"], row["participant_count"], row["addressed_hint"]) == ("cand1", 2, True)
    assert row["utterance_refs"] == ["evt_a", "evt_b"] and row["request_id"] == "req_x"
    assert SECRET_TEXT not in json.dumps(row, default=str)
    assert isinstance(row["created_at"], datetime) and row["elapsed_ms"] >= 0


def test_shadow_row_points_to_primary_decision():
    sink = ListSink()
    shadow = _P(_mode(ReplyMode.PASS, pid="s.one"), pid="s.one")
    hub, engine, writer = _stack(_P(_mode(ReplyMode.RESPOND)), shadows=[shadow], sink=sink)
    try:
        result = _decide(hub)
        assert engine.wait_shadow(2)
        assert writer.flush()
    finally:
        engine.close()
        writer.close()
    primary = next(r for r in sink.rows if r["role"] == "primary")
    (shadow_row,) = [r for r in sink.rows if r["role"] == "shadow"]
    assert primary["decision_id"] == result.decision_id
    assert shadow_row["primary_decision_id"] == result.decision_id
    assert shadow_row["decision_id"] != result.decision_id and shadow_row["decision_id"].startswith("dec_")
    assert (shadow_row["proposer_id"], shadow_row["outcome"], shadow_row["agree"]) == ("s.one", "pass", False)
    assert shadow_row["request_id"] == "req_x" and shadow_row["candidate_id"] == "cand1"


def test_existing_on_shadow_callback_is_preserved():
    seen = []
    sink = ListSink()
    registry = DecisionPointRegistry()
    hub = DecisionHub(registry)
    engine = TurnReplyEngine(
        _P(_mode(ReplyMode.RESPOND)), registry=registry, metrics=Counter(),
        shadow_proposers=[_P(_mode(ReplyMode.RESPOND, pid="s"), pid="s")], on_shadow=seen.append,
    )
    hub.register(turn_reply_spec(300), engine)
    writer = DecisionLogWriter(sink, flush_interval=0.01)
    writer.attach(hub)
    try:
        _decide(hub)
        assert engine.wait_shadow(2)
        assert writer.flush()
    finally:
        engine.close()
        writer.close()
    assert len(seen) == 1 and seen[0].primary_decision_id is not None
    assert any(r["role"] == "shadow" for r in sink.rows)


def test_slow_sink_does_not_delay_primary():
    class SlowSink:
        def __init__(self):
            self.gate = threading.Event()

        def write(self, rows):
            self.gate.wait(2.0)  # 刻意卡住

    sink = SlowSink()
    hub, engine, writer = _stack(_P(_mode(ReplyMode.RESPOND)), sink=sink, max_queue=8, batch_size=2)
    try:
        started = time.monotonic()
        for i in range(40):  # 遠超過佇列容量
            _decide(hub, _cand(f"c{i}"), request_id=f"req_{i}")
        elapsed = time.monotonic() - started
        assert elapsed < 0.5, f"primary 被日誌拖慢：{elapsed:.3f}s"
        assert writer.metrics["decision_log.dropped"] > 0  # 佇列滿 -> 丟棄而非阻塞
    finally:
        sink.gate.set()
        engine.close()
        writer.close()


def test_failing_sink_degrades_to_dropped_count():
    class BrokenSink:
        calls = 0

        def write(self, rows):
            BrokenSink.calls += 1
            raise RuntimeError("db down")

    hub, engine, writer = _stack(_P(_mode(ReplyMode.RESPOND)), sink=BrokenSink())
    try:
        for i in range(3):
            result = _decide(hub, _cand(f"c{i}"))
            assert result.outcome == "respond" and not result.fallback
        assert writer.flush()
    finally:
        engine.close()
        writer.close()
    assert writer.metrics["decision_log.dropped"] == 3 and writer.metrics["decision_log.written"] == 0
    assert BrokenSink.calls >= 1


def test_sink_recovers_after_failure():
    class FlakySink(ListSink):
        fail = True

        def write(self, rows):
            if self.fail:
                raise RuntimeError("temporary")
            super().write(rows)

    sink = FlakySink()
    hub, engine, writer = _stack(_P(_mode(ReplyMode.RESPOND)), sink=sink)
    try:
        _decide(hub, _cand("c1"))
        assert writer.flush()
        sink.fail = False
        _decide(hub, _cand("c2"))
        assert writer.flush()
    finally:
        engine.close()
        writer.close()
    assert [r["candidate_id"] for r in sink.rows] == ["c2"]
    assert writer.metrics["decision_log.dropped"] == 1 and writer.metrics["decision_log.written"] == 1


def test_disabled_writer_is_inert():
    sink = ListSink()
    off = DecisionLogWriter(sink, enabled=False)
    none = DecisionLogWriter(None)
    assert not off.enabled and not none.enabled
    hub, engine, writer = _stack(_P(_mode(ReplyMode.RESPOND)), sink=sink, enabled=False)
    try:
        _decide(hub)
        assert writer._thread is None  # 停用時連執行緒都不開
    finally:
        engine.close()
    assert sink.rows == []


def test_default_writer_disabled_without_database(monkeypatch):
    from uep.config import get_settings

    monkeypatch.setattr(get_settings(), "database_url", None)
    assert not create_default_writer().enabled
    monkeypatch.setattr(get_settings(), "decision_log", "off")
    assert not create_default_writer().enabled


def test_primary_status_mapping_from_real_failures():
    sink = ListSink()

    def boom(_c):
        raise RuntimeError("x")

    def slow(c):
        time.sleep(0.4)
        return _mode(ReplyMode.RESPOND)(c)

    bad = lambda _c: Proposal(reply_mode="nope", reason_code="x")
    expected = {"boom": (boom, "error"), "slow": (slow, "timeout"), "bad": (bad, "invalid")}
    for name, (fn, status) in expected.items():
        hub, engine, writer = _stack(_P(fn), sink=sink, budget_ms=80)
        try:
            result = _decide(hub, _cand(name))
            assert result.fallback
            assert writer.flush()
        finally:
            engine.close()
            writer.close()
    got = {r["candidate_id"]: (r["status"], r["fallback"]) for r in sink.rows}
    assert got == {k: (v[1], True) for k, v in expected.items()}


def test_primary_status_busy_branch():
    spec = DecisionPointSpec(kind="k", on_busy_reason="proposer_busy", on_timeout_reason="t", on_invalid_reason="i")
    hub, engine, _writer = _stack(_P(_mode(ReplyMode.RESPOND)))
    try:
        result = _decide(hub)
    finally:
        engine.close()
    busy = type(result)(**{**result.__dict__, "fallback": True, "reason_code": "proposer_busy"})
    assert _primary_status(busy, spec) == "busy"


def test_observer_exception_never_breaks_decide():
    hub, engine, _writer = _stack(_P(_mode(ReplyMode.RESPOND)))
    hub.add_observer(lambda *a: (_ for _ in ()).throw(RuntimeError("observer boom")))
    try:
        assert _decide(hub).outcome == "respond"
    finally:
        engine.close()


def test_close_drains_queue():
    sink = ListSink()
    hub, engine, writer = _stack(_P(_mode(ReplyMode.RESPOND)), sink=sink)
    for i in range(5):
        _decide(hub, _cand(f"c{i}"))
    writer.close()
    engine.close()
    assert len(sink.rows) == 5


def test_jsonl_sink_roundtrip_feeds_replay(tmp_path):
    path = tmp_path / "log.jsonl"
    hub, engine, writer = _stack(
        _P(_mode(ReplyMode.RESPOND)), shadows=[_P(_mode(ReplyMode.RESPOND, pid="s"), pid="s")], sink=JsonlSink(path)
    )
    try:
        _decide(hub)
        assert engine.wait_shadow(2)
        assert writer.flush()
    finally:
        engine.close()
        writer.close()
    rows = read_jsonl(path)
    assert len(rows) == 2 and SECRET_TEXT not in path.read_text(encoding="utf-8")
    report = analyze(rows)["kinds"][KIND_TURN_REPLY]
    assert report["primary"]["count"] == 1 and report["shadows"]["s"]["agree_rate"] == 1.0


# ---- 重放評估 ----

def _row(did, role, outcome, *, kind="turn.reply", pid="p", reason="r", status="ok", ms=10, primary=None, agree=None, at=None):
    row = dict.fromkeys(COLUMNS)
    row.update(
        decision_id=did, kind=kind, role=role, proposer_id=pid, outcome=outcome, reason_code=reason, status=status,
        elapsed_ms=ms, primary_decision_id=primary, agree=agree,
        created_at=(at or datetime(2026, 11, 3, 12, 0, tzinfo=UTC)).isoformat(),
    )
    return row


def _synthetic():
    rows = []
    # 10 個 primary：6 respond、3 pass、1 defer；elapsed 1..10 ms；其中一筆 timeout
    outcomes = ["respond"] * 6 + ["pass"] * 3 + ["defer"]
    for i, out in enumerate(outcomes):
        rows.append(_row(f"dec_p{i}", "primary", out, ms=i + 1, reason="wake" if out == "respond" else "quiet",
                         status="timeout" if i == 9 else "ok"))
    # shadow A：對前 6 respond 全部同意；對 3 pass 其中 1 個給 respond（不同意）；defer 給 pass（不同意）
    shadow_modes = ["respond"] * 6 + ["pass", "pass", "respond"] + ["pass"]
    for i, mode in enumerate(shadow_modes):
        rows.append(_row(f"dec_sa{i}", "shadow", mode, pid="A", primary=f"dec_p{i}", agree=mode == outcomes[i],
                         ms=(i + 1) * 10, reason="sr"))
    # shadow A 一筆例外（無 outcome）與一筆孤兒（primary 不在資料內）
    rows.append(_row("dec_saerr", "shadow", None, pid="A", primary="dec_p0", agree=False, status="error", ms=5))
    rows.append(_row("dec_saorph", "shadow", "respond", pid="A", primary="dec_gone", agree=True))
    # 另一個 kind 不應混入
    rows.append(_row("dec_other", "primary", "x", kind="other.kind"))
    return rows


def test_percentile_nearest_rank():
    assert percentile([], 50) is None
    assert percentile([5], 95) == 5.0
    values = list(range(1, 11))
    assert percentile(values, 50) == 5.0 and percentile(values, 95) == 10.0 and percentile(values, 100) == 10.0
    assert percentile([3, 1, 2], 50) == 2.0  # 不要求已排序


def test_analyze_agreement_confusion_status_latency_reason():
    report = analyze(filter_rows(_synthetic(), kind="turn.reply"))
    assert list(report["kinds"]) == ["turn.reply"]
    block = report["kinds"]["turn.reply"]
    primary = block["primary"]
    assert primary["count"] == 10 and primary["status"] == {"ok": 9, "timeout": 1}
    assert primary["elapsed_ms"] == {"n": 10, "p50": 5.0, "p95": 10.0}
    assert primary["by_outcome"] == {"respond": 6, "pass": 3, "defer": 1}
    assert primary["by_reason"] == {"wake": 6, "quiet": 4}
    a = block["shadows"]["A"]
    assert a["count"] == 12 and a["status"] == {"ok": 11, "error": 1}
    assert a["decided"] == 11  # 例外列沒有 outcome，不算進一致率
    assert a["agree"] == 9 and a["agree_rate"] == pytest.approx(9 / 11)  # 以日誌的 agree 欄位為準（含孤兒列）
    assert a["orphans"] == 1
    assert a["confusion"] == {
        "respond": {"respond": 6},
        "pass": {"pass": 2, "respond": 1},
        "defer": {"pass": 1},
    }
    assert a["elapsed_ms"]["n"] == 12 and a["elapsed_ms"]["p50"] == 40.0
    assert a["reason_agree"]["sr"] == {"count": 10, "agree": 8}


def test_filter_by_kind_and_time_range():
    t0 = datetime(2026, 11, 1, tzinfo=UTC)
    rows = [_row(f"d{i}", "primary", "respond", at=t0 + timedelta(days=i)) for i in range(5)]
    rows.append(_row("dx", "primary", "respond", kind="other", at=t0))
    assert len(filter_rows(rows)) == 6
    assert len(filter_rows(rows, kind="turn.reply")) == 5
    picked = filter_rows(rows, kind="turn.reply", since="2026-11-02", until="2026-11-04")
    assert [r["decision_id"] for r in picked] == ["d1", "d2"]  # [since, until)
    assert len(filter_rows(rows, since=t0 + timedelta(days=4))) == 1


def test_cli_stats_from_jsonl(tmp_path, capsys):
    path = tmp_path / "log.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in _synthetic()) + "\n", encoding="utf-8")
    out_json = tmp_path / "report.json"
    assert main(["stats", "--jsonl", str(path), "--kind", "turn.reply", "--json-out", str(out_json)]) == 0
    text = capsys.readouterr().out
    assert "一致率" in text and "混淆矩陣" in text and "shadow A" in text
    saved = json.loads(out_json.read_text(encoding="utf-8"))
    assert saved["kinds"]["turn.reply"]["shadows"]["A"]["decided"] == 11
    assert main(["stats", "--jsonl", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["total_rows"] == len(_synthetic())
    assert "turn.reply" in format_report(saved)


def test_rerun_candidates_through_multiple_proposers(tmp_path, capsys):
    candidates = [
        candidate_to_dict(_cand("c1", addressed=True, count=1, text="U.E.P 你好")),
        candidate_to_dict(_cand("c2", addressed=False, count=3, text="大家午安")),
    ]
    rows = rerun_candidates(candidates, ["respond.minimal", "rules.default"])
    primaries = [r for r in rows if r["role"] == "primary"]
    shadows = [r for r in rows if r["role"] == "shadow"]
    assert len(primaries) == 2 and len(shadows) == 2
    assert all(s["primary_decision_id"] in {p["decision_id"] for p in primaries} for s in shadows)
    assert {p["outcome"] for p in primaries} == {"respond"}  # respond.minimal 恆回 respond
    assert all(s["status"] == "ok" and s["agree"] is (s["outcome"] == "respond") for s in shadows)
    block = analyze(rows)["kinds"]["turn.reply"]
    assert block["primary"]["count"] == 2 and "rules.default" in block["shadows"]
    # CLI
    path = tmp_path / "cands.jsonl"
    path.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in candidates), encoding="utf-8")
    rows_out = tmp_path / "rows.jsonl"
    assert main(["rerun", "--candidates", str(path), "--proposers", "respond.minimal,rules.default",
                 "--rows-out", str(rows_out), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["total_rows"] == 4 and len(read_jsonl(rows_out)) == 4


def test_candidate_dict_roundtrip():
    cand = _cand("c9", text="abc")
    back = candidate_from_dict(candidate_to_dict(cand))
    assert back == cand
    assert "text" not in candidate_to_dict(cand, include_text=False)


# ---- migration（無 DB 的基本檢查）----

def test_migration_0010_revision_chain():
    script = ScriptDirectory.from_config(alembic_config("postgresql://x/y_test"))
    rev = script.get_revision("0010_decision_log")
    assert rev is not None and rev.down_revision == "0009_action_tool_version"
    assert script.get_revision("0009_action_tool_version").down_revision == "0008_receipt_worker_epoch"
    module = rev.module
    assert callable(module.upgrade) and callable(module.downgrade)
    assert len(script.get_heads()) == 1


def test_orm_model_matches_log_columns_and_is_registered():
    import uep.db.models_registry  # noqa: F401
    from uep.db.base import Base

    table = Base.metadata.tables["decision_log"]
    assert [c.name for c in table.columns] == list(COLUMNS)
    assert {c.name for c in table.primary_key} == {"decision_id"}
    assert DecisionLogRow.__tablename__ == "decision_log"
    indexed = {tuple(c.name for c in ix.columns) for ix in table.indexes}
    assert {("kind", "created_at"), ("request_id",), ("created_at",), ("primary_decision_id",)} <= indexed


# ---- 需要 PostgreSQL（UEP_TEST_DATABASE_URL；由協調者統一執行）----

def test_db_migration_0010_upgrade_downgrade(alembic_cfg, engine):
    from alembic import command
    from sqlalchemy import inspect

    def has_table():
        return "decision_log" in inspect(engine).get_table_names()

    assert has_table()
    try:
        command.downgrade(alembic_cfg, "0009_action_tool_version")
        assert not has_table()
    finally:
        command.upgrade(alembic_cfg, "head")
    assert has_table()
    columns = {c["name"] for c in inspect(engine).get_columns("decision_log")}
    assert columns == set(COLUMNS)
    indexes = {i["name"] for i in inspect(engine).get_indexes("decision_log")}
    assert {"ix_decision_log_kind_created_at", "ix_decision_log_request_id", "ix_decision_log_created_at"} <= indexes


def test_db_sink_writer_and_load_rows(engine, session_factory):
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM decision_log"))
    shadow = _P(_mode(ReplyMode.PASS, pid="s.db"), pid="s.db")
    hub, eng, writer = _stack(_P(_mode(ReplyMode.RESPOND)), shadows=[shadow], sink=DbSink(session_factory))
    try:
        result = _decide(hub)
        assert eng.wait_shadow(2)
        assert writer.flush()
    finally:
        eng.close()
        writer.close()
    assert writer.metrics["decision_log.written"] == 2 and writer.metrics["decision_log.dropped"] == 0
    rows = load_rows(session_factory, kind=KIND_TURN_REPLY)
    assert len(rows) == 2
    primary = next(r for r in rows if r["role"] == "primary")
    sh = next(r for r in rows if r["role"] == "shadow")
    assert primary["decision_id"] == result.decision_id and primary["utterance_refs"] == ["evt_a", "evt_b"]
    assert sh["primary_decision_id"] == result.decision_id and sh["agree"] is False
    assert load_rows(session_factory, kind="nope") == []
    future = datetime.now(UTC) + timedelta(days=1)
    assert load_rows(session_factory, since=future) == []
    report = analyze(rows)["kinds"][KIND_TURN_REPLY]
    assert report["shadows"]["s.db"]["confusion"] == {"respond": {"pass": 1}}
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM decision_log"))


def test_db_role_check_constraint(engine, session_factory):
    from sqlalchemy.exc import IntegrityError

    row = dict.fromkeys(COLUMNS)
    row.update(decision_id="dec_bad", kind="k", created_at=datetime.now(UTC), role="bogus", status="ok")
    with pytest.raises(IntegrityError):
        DbSink(session_factory).write([row])
