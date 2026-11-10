"""D41 journal 安全水位（JournalTailer，需 PostgreSQL；未設 UEP_TEST_DATABASE_URL 時 skip）。

重點：交錯提交（後號先提交）時 tailing 不漏、不重；游標可持久化後續讀。
"""
import os
import time
from datetime import timedelta

import pytest
from alembic import command
from sqlalchemy import inspect, select, text

from tests.uep.test_journal_db import EXTERNAL
from uep.events import journal
from uep.events.bus import EventBus
from uep.events.envelope import EventDraft
from uep.events.models import EventRow
from uep.events.publisher import EventPublisher
from uep.events.tailer import JournalTailer, TailCursor, current_xmin

pytestmark = pytest.mark.usefixtures("session_factory")


@pytest.fixture
def publisher(session_factory):
    return EventPublisher(session_factory, EventBus())


def _record(publisher, session, key):
    """於指定交易內落 journal（不提交）。"""
    return publisher.record(session, EventDraft(event_type="input.text_final", idempotency_key=key), EXTERNAL).event


def _keys(events):
    return [e.idempotency_key for e in events]


def _settle(session_factory):
    """等水位越過已提交事件的 txid。

    xmin 是叢集範圍：別的 schema、別的資料庫、別的 pytest 行程裡任何一個未結束的交易都會把它釘住，
    讓 tailer 暫時什麼都讀不到（設計如此，見 tailer 模組「運作限制」）。測試用條件等待取代「寫完立刻 poll」的假設；
    逾時（預設 60 秒，``UEP_TEST_WATERMARK_TIMEOUT`` 可調）代表叢集上有長交易，直接點名占住水位的連線。
    """
    timeout = float(os.environ.get("UEP_TEST_WATERMARK_TIMEOUT", "60"))
    deadline = time.monotonic() + timeout
    while True:
        with session_factory() as s:
            top = s.scalar(text("SELECT coalesce(max(txid), 0) FROM events"))
            if current_xmin(s) > top:
                return
            holders = s.execute(
                text(
                    "SELECT pid, datname, state, now() - xact_start FROM pg_stat_activity "
                    "WHERE pid <> pg_backend_pid() AND (backend_xmin IS NOT NULL OR backend_xid IS NOT NULL)"
                )
            ).all()
        if time.monotonic() > deadline:
            pytest.fail(f"水位 {timeout:.0f} 秒內未越過 txid {top}；叢集上有未結束的交易：{holders}")
        time.sleep(0.05)


def _drain(tailer):
    _settle(tailer._session_factory)
    out = []
    while batch := tailer.poll(limit=2):
        out.extend(batch)
    return out


# ---- migration ----

def test_migration_0006_adds_txid_and_is_reversible(alembic_cfg, engine, session_factory):
    assert "txid" in {c["name"] for c in inspect(engine).get_columns("events")}
    try:
        command.downgrade(alembic_cfg, "0005_state_stop")
        assert "txid" not in {c["name"] for c in inspect(engine).get_columns("events")}
    finally:
        command.upgrade(alembic_cfg, "head")
    assert "txid" in {c["name"] for c in inspect(engine).get_columns("events")}


def test_new_rows_record_writing_transaction_id(session_factory, publisher):
    publisher.publish(EventDraft(event_type="input.text_final", idempotency_key="a"), EXTERNAL)
    _settle(session_factory)
    with session_factory() as s:
        txid, xmin = s.execute(
            text("SELECT (SELECT txid FROM events), pg_snapshot_xmin(pg_current_snapshot())::text::bigint")
        ).one()
    assert 0 < txid < xmin  # 已提交且水位已越過它


# ---- 基本 tailing ----

def test_tailer_reads_committed_events_in_order_without_duplicates(session_factory, publisher):
    for key in ("a", "b", "c", "d", "e"):
        publisher.publish(EventDraft(event_type="input.text_final", idempotency_key=key), EXTERNAL)
    tailer = JournalTailer(session_factory)
    assert _keys(_drain(tailer)) == ["a", "b", "c", "d", "e"]
    assert tailer.poll() == []
    publisher.publish(EventDraft(event_type="input.text_final", idempotency_key="f"), EXTERNAL)
    _settle(session_factory)
    assert _keys(tailer.poll()) == ["f"]
    assert tailer.poll() == []


def test_tailer_from_now_skips_history(session_factory, publisher):
    publisher.publish(EventDraft(event_type="input.text_final", idempotency_key="old"), EXTERNAL)
    _settle(session_factory)  # from_now 以目前水位為起點：水位須先越過 old
    tailer = JournalTailer.from_now(session_factory)
    publisher.publish(EventDraft(event_type="input.text_final", idempotency_key="new"), EXTERNAL)
    assert _keys(_drain(tailer)) == ["new"]


def test_cursor_persists_and_resumes(session_factory, publisher):
    for key in ("a", "b", "c"):
        publisher.publish(EventDraft(event_type="input.text_final", idempotency_key=key), EXTERNAL)
    _settle(session_factory)
    first = JournalTailer(session_factory)
    got = _keys(first.poll(limit=2))  # 區間內處理到一半
    saved = first.cursor
    assert saved.high_txid is not None  # 區間處理中
    publisher.publish(EventDraft(event_type="input.text_final", idempotency_key="d"), EXTERNAL)
    _settle(session_factory)
    resumed = JournalTailer(session_factory, cursor=TailCursor(**vars(saved)))
    got += _keys(_drain(resumed))
    assert got == ["a", "b", "c", "d"]


def test_legacy_rows_with_zero_txid_are_included(session_factory, publisher):
    publisher.publish(EventDraft(event_type="input.text_final", idempotency_key="legacy"), EXTERNAL)
    with session_factory() as s, s.begin():
        # 模擬 migration 前寫入的列：txid 預設補 0（trigger 只允許改墓碑欄位，故暫停 trigger 做測試資料）
        s.execute(text("ALTER TABLE events DISABLE TRIGGER events_append_only"))
        s.execute(text("UPDATE events SET txid = 0"))
        s.execute(text("ALTER TABLE events ENABLE TRIGGER events_append_only"))
    assert _keys(_drain(JournalTailer(session_factory))) == ["legacy"]


# ---- 並行：交錯提交 ----

def test_interleaved_commits_later_seq_committed_first_not_lost_or_duplicated(session_factory, publisher):
    """A 先開、先取得較小的 journal_seq 與 txid，但較晚提交；B 較晚開、較早提交。"""
    sa, sb = session_factory(), session_factory()
    try:
        ta, tb = sa.begin(), sb.begin()
        ev_a = _record(publisher, sa, "A")  # seq 小、txid 小，未提交
        ev_b = _record(publisher, sb, "B")  # seq 大，先提交
        assert ev_a.journal_seq < ev_b.journal_seq
        tb.commit()

        tailer = JournalTailer(session_factory)
        assert tailer.poll() == []  # A 還在，B 雖已可見也不得越過水位（否則游標跳過 A）

        ta.commit()
        got = _drain(tailer)
        assert _keys(got) == ["A", "B"]  # 兩者都在、依 journal_seq、無重複
        assert tailer.poll() == []
    finally:
        sa.close()
        sb.close()


def test_lower_seq_in_later_transaction_is_not_skipped(session_factory, publisher):
    """txid 較小的 T1 先提交、seq 卻較大；T2 (txid 較大) 的 seq 較小且較晚提交：游標不得因 T1 而跳過 T2。"""
    s1, s2 = session_factory(), session_factory()
    try:
        t1, t2 = s1.begin(), s2.begin()
        s1.execute(text("SELECT txid_current()"))  # T1 先取得較小的 txid
        s2.execute(text("SELECT txid_current()"))
        ev_2 = _record(publisher, s2, "T2")  # 先取 seq（較小），晚提交
        ev_1 = _record(publisher, s1, "T1")  # 後取 seq（較大），先提交
        assert ev_2.journal_seq < ev_1.journal_seq
        t1.commit()

        tailer = JournalTailer(session_factory)
        first = _drain(tailer)  # T2 的 txid 還在水位之上
        assert _keys(first) == ["T1"]

        t2.commit()
        second = _drain(tailer)
        assert _keys(second) == ["T2"]  # 不漏（以 journal_seq 游標會永遠漏掉它）
        assert tailer.poll() == []
        assert sorted(_keys(first + second)) == ["T1", "T2"]  # 不重
    finally:
        s1.close()
        s2.close()


def test_rolled_back_transaction_leaves_gap_without_blocking(session_factory, publisher):
    s1 = session_factory()
    try:
        t1 = s1.begin()
        _record(publisher, s1, "gone")
        t1.rollback()
        publisher.publish(EventDraft(event_type="input.text_final", idempotency_key="kept"), EXTERNAL)
        assert _keys(_drain(JournalTailer(session_factory))) == ["kept"]
    finally:
        s1.close()


def test_open_old_transaction_holds_watermark_until_it_ends(session_factory, publisher):
    holder = session_factory()
    try:
        tx = holder.begin()
        holder.execute(text("SELECT txid_current()"))  # 長交易占著水位，什麼都沒寫
        publisher.publish(EventDraft(event_type="input.text_final", idempotency_key="x"), EXTERNAL)
        tailer = JournalTailer(session_factory)
        assert tailer.poll() == []  # 保守：水位被卡住時不處理
        tx.commit()
        assert _keys(_drain(tailer)) == ["x"]
    finally:
        holder.close()


# ---- 延遲容忍 ----

def test_safety_lag_delays_visibility_but_never_loses(session_factory, publisher):
    now = [0.0]
    tailer = JournalTailer(session_factory, safety_lag=timedelta(seconds=10), clock=lambda: now[0])
    publisher.publish(EventDraft(event_type="input.text_final", idempotency_key="a"), EXTERNAL)
    _settle(session_factory)  # 第一次觀察到的 xmin 必須已越過 a，否則 clock 推進後仍讀不到
    assert tailer.poll() == []  # 水位的觀察還不夠舊
    now[0] = 5.0
    assert tailer.poll() == []
    now[0] = 10.0
    assert _keys(tailer.poll()) == ["a"]
    publisher.publish(EventDraft(event_type="input.text_final", idempotency_key="b"), EXTERNAL)
    _settle(session_factory)
    now[0] = 12.0
    assert tailer.poll() == []
    now[0] = 25.0
    assert _keys(tailer.poll()) == ["b"]
    assert tailer.poll() == []


def test_tailer_returns_tombstoned_events_with_metadata_only(session_factory, publisher):
    event = publisher.publish(
        EventDraft(event_type="input.text_final", idempotency_key="p", payload_blob=b"private"), EXTERNAL
    ).event
    with session_factory() as s, s.begin():
        journal.delete_blob(s, event.payload_ref, "刪除")
    (got,) = _drain(JournalTailer(session_factory))
    assert got.tombstone_at is not None and got.payload_hash == event.payload_hash
    with session_factory() as s:
        assert s.scalar(select(EventRow.txid).where(EventRow.event_id == event.event_id)) > 0


def test_poll_rejects_bad_limit(session_factory):
    with pytest.raises(ValueError):
        JournalTailer(session_factory).poll(limit=0)
