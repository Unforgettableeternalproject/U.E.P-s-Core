"""Event Journal 與可刪 payload blob（契約 §1.2、E-1、E-11、D33）。

所有函式在呼叫者提供的 Session／交易內運作，不自行 commit，
讓 journal 與 outbox（及呼叫者的狀態變更）能共用同一交易。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from uep.events.envelope import IdempotencyConflict, UEPEvent
from uep.events.models import EventRow, PayloadBlob
from uep.events.payload_hash import (  # noqa: F401  sha256_hex 供舊呼叫端相容
    PayloadHasher,
    PayloadHashKeyMissing,
    VerifyOutcome,
    resolve_hasher,
    sha256_hex,
)
from uep.ids import new_id

logger = logging.getLogger("uep.events.journal")

# UEPEvent 欄位中由 DB 決定、插入時不帶的部分
_DB_ASSIGNED = {"journal_seq", "received_at"}


@dataclass(frozen=True)
class AppendResult:
    event: UEPEvent
    duplicate: bool  # True 表示命中 (source, idempotency_key)，回傳的是原事件


def row_to_event(row: EventRow) -> UEPEvent:
    data = {name: getattr(row, name) for name in UEPEvent.model_fields}
    return UEPEvent(**data)


def put_blob(session: Session, data: bytes, hasher: PayloadHasher | None = None) -> tuple[str, str]:
    """寫入可刪 blob，回傳 (payload_ref, payload_hash)。雜湊為 HMAC（D42）；沒有密鑰則拒絕寫入。"""
    digest = resolve_hasher(hasher).hash(data)  # 先取密鑰：失敗時不留下任何列
    ref = new_id("blob")
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


def verify_payload(data: bytes, stored_hash: str | None, hasher: PayloadHasher | None = None) -> bool:
    """以已存雜湊驗證候選內容（blob 已刪除後仍可用）；無雜湊回 False。舊 sha256 資料照舊演算法驗證。"""
    if not stored_hash:
        return False
    return resolve_hasher(hasher).verify(data, stored_hash)


def check_payload(data: bytes, stored_hash: str | None, hasher: PayloadHasher | None = None) -> VerifyOutcome:
    """三態驗證：金鑰輪替造成的「無法驗證」與「確定不符」分開（無雜湊視為無法驗證）。"""
    if not stored_hash:
        return VerifyOutcome.UNVERIFIABLE
    return resolve_hasher(hasher).check(data, stored_hash)


def _check_resend(
    existing: UEPEvent, event: UEPEvent, blob: bytes | None, hasher: PayloadHasher | None
) -> None:
    """重送命中時核對一致性（M1）：event_type／scope／aggregate 或 blob 雜湊不符即拋 IdempotencyConflict。

    blob 只在「確定不符」（同一把密鑰 kid 相同卻驗不過、或舊 sha256 不符）時算衝突；密鑰已輪替而無法驗證時
    記 warning 並視為重送（不誤報）。例外訊息與 log 只含欄位名與 ID，不含內容。真重送（全部一致）無動作。
    """
    mismatched = [
        name
        for name, same in (
            ("event_type", existing.event_type == event.event_type),
            ("scope_ref", existing.scope_ref == event.scope_ref),
            (
                "aggregate",
                (existing.aggregate_type, existing.aggregate_id, existing.aggregate_version)
                == (event.aggregate_type, event.aggregate_id, event.aggregate_version),
            ),
        )
        if not same
    ]
    if blob is not None:
        try:
            outcome = check_payload(blob, existing.payload_hash, hasher)
            if outcome is VerifyOutcome.MISMATCH:
                mismatched.append("payload_blob")
            elif outcome is VerifyOutcome.UNVERIFIABLE:
                logger.warning("無法驗證重送內容（密鑰已輪替或無雜湊）：event_id=%s", existing.event_id)
        except PayloadHashKeyMissing:
            logger.warning("無法驗證重送內容（未設定 payload_hash 密鑰）：event_id=%s", existing.event_id)
    if mismatched:
        raise IdempotencyConflict(
            f"idempotency_key 重送內容與原事件不一致：{','.join(mismatched)}（event_id={existing.event_id}）"
        )


def append(
    session: Session, event: UEPEvent, blob: bytes | None = None, hasher: PayloadHasher | None = None
) -> AppendResult:
    """把事件 append 進 journal。

    外部事件以 (source, idempotency_key) 去重：真重送回傳原事件、不新增列、不寫 blob；
    event_type／scope／aggregate 或 blob 雜湊與原事件不一致則拋 IdempotencyConflict。
    其他唯一性衝突（如 aggregate_version）以 IntegrityError 上拋，由呼叫者決定。
    """
    if event.idempotency_key:
        existing = find_by_idempotency(session, event.source, event.idempotency_key)
        if existing is not None:
            _check_resend(existing, event, blob, hasher)
            return AppendResult(existing, duplicate=True)

    savepoint = session.begin_nested()  # 併發重送輸掉競爭時，連 blob 一起撤銷
    try:
        if blob is not None:
            ref, digest = put_blob(session, blob, hasher)
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
        _check_resend(winner, event, blob, hasher)
        return AppendResult(winner, duplicate=True)
    savepoint.commit()

    return AppendResult(
        event.model_copy(
            update={"journal_seq": inserted.journal_seq, "received_at": inserted.received_at}
        ),
        duplicate=False,
    )
