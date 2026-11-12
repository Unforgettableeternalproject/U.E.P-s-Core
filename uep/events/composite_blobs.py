"""composite 引用 text 的 DB blob 實作（D110）：沿用 journal 的 ``payload_blobs``，不新增表。

- ref：``cblob:<run_id>:<20 位十六進位>``（<= 64 字元，可進 checkpoint）。以 Run 前綴區分用途與歸屬（journal 的 blob 是 ``blob_<ULID>``），
  終態刪除與 TTL 清除都只動這個前綴，不碰 journal 事件的 blob。
- 雜湊：沿用 ``PayloadHasher`` 的 HMAC（D42）；沒有密鑰則拒絕寫入（呼叫端視為未持久化，續跑時 fail-closed）。
  cursor 要過 checkpoint 的 64 字元短碼限制，所以回傳給呼叫端的 hash 只取 HMAC 的 64 位十六進位摘要
  （完整格式仍存在 ``payload_blobs.content_hash``）；驗證時以現行密鑰重算比對，密鑰已換則不符（fail-closed）。
- 刪除：內容設 NULL、保留雜湊與 ``deleted_at``（與 journal 的 ``delete_blob`` 同一語意）；冪等。
- TTL 不得短於核准有效期（見 ``uep.agent.composite_blobs``）。讀取時超過 ``created_at + ttl_s`` 視同不存在；每次寫入與 ``delete_run`` 順手清除已過期者（兜底，Run 終態刪除才是主路徑；沒有背景排程，
  所以過期原文最晚在下一次寫入或任一 Run 終態刪除時被清掉）。
"""
from __future__ import annotations

import hmac
import logging
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, update
from sqlalchemy.orm import Session, sessionmaker

from uep.events.models import PayloadBlob
from uep.events.payload_hash import PayloadHasher, PayloadHashKeyMissing, resolve_hasher

logger = logging.getLogger(__name__)

REF_PREFIX = "cblob:"


def _run_prefix(run_id: str) -> str:
    return f"{REF_PREFIX}{run_id}:"


class DbCompositeBlobStore:
    def __init__(
        self, session_factory: sessionmaker[Session], *, ttl_s: float, hasher: PayloadHasher | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl_s 必須 > 0")
        self._sessions = session_factory
        self._ttl = timedelta(seconds=ttl_s)
        self._hasher = hasher
        self._clock = clock

    def put(self, run_id: str, data: bytes) -> tuple[str, str]:
        digest = resolve_hasher(self._hasher).hash(data)  # 先取密鑰：失敗時不留下任何列
        ref = f"{_run_prefix(run_id)}{secrets.token_hex(10)}"
        with self._sessions.begin() as session:
            session.add(PayloadBlob(payload_ref=ref, content=data, content_hash=digest))
        self._purge_expired()
        return ref, digest.rsplit(":", 1)[-1]

    def get(self, ref: str) -> bytes | None:
        if not ref.startswith(REF_PREFIX):
            return None  # 只讀本 store 的 blob，不經它讀 journal 的私人 payload
        with self._sessions() as session:
            blob = session.get(PayloadBlob, ref)
            if blob is None or blob.content is None or blob.deleted_at is not None:
                return None
            created = blob.created_at
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if created is not None and self._clock() - created > self._ttl:
                return None
            return bytes(blob.content)

    def verify(self, data: bytes, stored_hash: str) -> bool:
        try:
            current = resolve_hasher(self._hasher).hash(data).rsplit(":", 1)[-1]
        except PayloadHashKeyMissing:
            return False
        return hmac.compare_digest(current, stored_hash)

    def delete_run(self, run_id: str) -> int:
        with self._sessions.begin() as session:
            result = session.execute(
                update(PayloadBlob)
                .where(PayloadBlob.payload_ref.startswith(_run_prefix(run_id), autoescape=True), PayloadBlob.deleted_at.is_(None))
                .values(content=None, deleted_at=func.now())
            )
            deleted = result.rowcount or 0
        self._purge_expired()  # 過期者即使沒人再寫入也要在這裡清掉原文
        return deleted

    def _purge_expired(self) -> None:
        """TTL 兜底：清除已過期的 composite blob；失敗不影響寫入。"""
        cutoff = self._clock() - self._ttl
        try:
            with self._sessions.begin() as session:
                session.execute(
                    update(PayloadBlob)
                    .where(
                        PayloadBlob.payload_ref.startswith(REF_PREFIX, autoescape=True), PayloadBlob.deleted_at.is_(None),
                        PayloadBlob.created_at < cutoff,
                    )
                    .values(content=None, deleted_at=func.now())
                )
        except Exception:
            logger.warning("composite blob TTL 清除失敗", exc_info=True)
