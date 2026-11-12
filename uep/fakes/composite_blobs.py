"""``CompositeBlobStore`` 的離線實作（記憶體）。``lose``／``tamper`` 供測試模擬 blob 遺失或內容被改。"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from pydantic import SecretStr

from uep.events.payload_hash import PayloadHasher

_HASHER = PayloadHasher(SecretStr("fake-composite-blob-key-0123456789"))


@dataclass
class InMemoryCompositeBlobStore:
    blobs: dict[str, bytes] = field(default_factory=dict)
    hashes: dict[str, str] = field(default_factory=dict)
    owners: dict[str, str] = field(default_factory=dict)
    fail_put: bool = False
    _seq: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def put(self, run_id: str, data: bytes) -> tuple[str, str]:
        if self.fail_put:
            raise RuntimeError("put 失敗（測試注入）")
        with self._lock:
            self._seq += 1
            ref = f"cblob:{run_id}:{self._seq}"
            digest = _HASHER.hash(data).rsplit(":", 1)[-1]
            self.blobs[ref], self.hashes[ref], self.owners[ref] = data, digest, run_id
            return ref, digest

    def get(self, ref: str) -> bytes | None:
        return self.blobs.get(ref)

    def verify(self, data: bytes, stored_hash: str) -> bool:
        return _HASHER.hash(data).rsplit(":", 1)[-1] == stored_hash

    def delete_run(self, run_id: str) -> int:
        with self._lock:
            refs = [r for r, owner in self.owners.items() if owner == run_id and r in self.blobs]
            for ref in refs:
                del self.blobs[ref]
            return len(refs)

    def lose(self, ref: str) -> None:
        self.blobs.pop(ref, None)

    def tamper(self, ref: str, data: bytes) -> None:
        self.blobs[ref] = data
