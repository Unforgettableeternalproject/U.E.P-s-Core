"""Event Journal 與可刪 payload blob（契約 §1.2、E-1、E-11、D33）。

所有函式在呼叫者提供的 Session／交易內運作，不自行 commit，
讓 journal 與 outbox（及呼叫者的狀態變更）能共用同一交易。
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from uep.events.envelope import UEPEvent
from uep.events.models import EventRow, PayloadBlob
from uep.ids import new_id

logger = logging.getLogger("uep.events.journal")

# UEPEvent 欄位中由 DB 決定、插入時不帶的部分
_DB_ASSIGNED = {"journal_seq", "received_at"}


@dataclass(frozen=True)
class AppendResult:
    event: UEPEvent
    duplicate: bool  # True 表示命中 (source, idempotency_key)，回傳的是原事件


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def row_to_event(row: EventRow) -> UEPEvent:
    data = {name: getattr(row, name) for name in UEPEvent.model_fields}
    return UEPEvent(**data)


def put_blob(session: Session, data: bytes) -> tuple[str, str]:
    """寫入可刪 blob，回傳 (payload_ref, payload_hash)。"""
    ref, digest = new_id("blob"), sha256_hex(data)
    session.add(PayloadBlob(payload_ref=ref, content=data, content_hash=digest))
    session.flush()
    return ref, digest


def read_blob(session: Session, payload_ref: str) -> bytes | None:
    """讀 blob；已刪除（墓碑）回傳 None。"""
    blob = session.get(PayloadBlob, payload_ref)
    return None if blob is None else blob.content


def delete_blob(session: Session, payload_ref: str, reason: str) -> int:
    """刪除 blob 內容；journal 只加墓碑，雜湊保留。冪等：重複呼叫回傳 0。回傳新標墓碑的事件數。"""
    session.execute(
        update(PayloadBlob)
        .where(PayloadBlob.payload_ref == payload_ref, PayloadBlob.deleted_at.is_(None))
        .values(content=None, deleted_at=func.now())
    )
    result = session.execute(
        update(EventRow)
        .where(EventRow.payload_ref == payload_ref, EventRow.tombstone_at.is_(None))
        .values(tombstone_at=func.now(), tombstone_reason=reason)
    )
    return result.rowcount or 0


def find_by_idempotency(session: Session, source: str, key: str) -> UEPEvent | None:
    row = session.execute(
        select(EventRow).where(EventRow.source == source, EventRow.idempotency_key == key)
    ).scalar_one_or_none()
    return None if row is None else row_to_event(row)


def _note_resend(existing: UEPEvent, blob: bytes | None) -> None:
    """重送命中時，若帶了 blob 且雜湊與原事件不同，記 warning（只記 ID，不記內容）。"""
    if blob is not None and existing.payload_hash != sha256_hex(blob):
        logger.warning(
            "重送內容與原事件不一致：event_id=%s source=%s", existing.event_id, existing.source
        )


def append(session: Session, event: UEPEvent, blob: bytes | None = None) -> AppendResult:
    """把事件 append 進 journal。

    外部事件以 (source, idempotency_key) 去重：重送回傳原事件、不新增列、不寫 blob。
    其他唯一性衝突（如 aggregate_version）以 IntegrityError 上拋，由呼叫者決定。
    """
    if event.idempotency_key:
        existing = find_by_idempotency(session, event.source, event.idempotency_key)
        if existing is not None:
            _note_resend(existing, blob)
            return AppendResult(existing, duplicate=True)

    savepoint = session.begin_nested()  # 併發重送輸掉競爭時，連 blob 一起撤銷
    try:
        if blob is not None:
            ref, digest = put_blob(session, blob)
            event = event.model_copy(update={"payload_ref": ref, "payload_hash": digest})
        values = event.model_dump(exclude=_DB_ASSIGNED)
        values["delivery_class"] = event.delivery_class.value
        values["priority"] = event.priority.value
        stmt = pg_insert(EventRow).values(**values)
        if event.idempotency_key:
            stmt = stmt.on_conflict_do_nothing(index_elements=["source", "idempotency_key"])
        inserted = session.execute(
            stmt.returning(EventRow.journal_seq, EventRow.received_at)
        ).one_or_none()
    except BaseException:
        savepoint.rollback()
        raise

    if inserted is None:
        savepoint.rollback()
        winner = find_by_idempotency(session, event.source, event.idempotency_key or "")
        if winner is None:  # 理論上不會發生：衝突只可能來自該唯一鍵
            raise RuntimeError("journal 插入無結果且找不到原事件")
        _note_resend(winner, blob)
        return AppendResult(winner, duplicate=True)
    savepoint.commit()

    return AppendResult(
        event.model_copy(
            update={"journal_seq": inserted.journal_seq, "received_at": inserted.received_at}
        ),
        duplicate=False,
    )
