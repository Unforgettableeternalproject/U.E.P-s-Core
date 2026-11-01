"""
HypothesisStore - 假說存儲（M5-D）

<namespace>/hypotheses.json，atomic_write_json 原子寫入；寫入在 storage_lock 內。

讀寫分離（仿 ShareAdmin 的「物理上拿不到」做法）：
  - HypothesisStore：唯讀。Dream、審核器、Reflection、engine、MCP 一律只拿這個。
  - HypothesisWriter：寫入 handle，只由 ConceptInhibitor（Gate）持有；
    其他模組不得 import／建構。每次狀態轉移都必須經 Gate 的 hypothesis patch
    （規則層 approve、audit-before-mutate、可 rollback）。

檔案損毀時：讀取端視為空（fail-closed：不產生任何有效假說），寫入端拒絕寫入，
避免覆蓋可能可救援的原檔。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from echo_memory.schemas.hypothesis import (
    OPEN_STATUSES,
    Hypothesis,
    make_pair_key,
)
from echo_memory.storage.atomic import atomic_write_json
from echo_memory.storage.locking import storage_lock

logger = logging.getLogger("echo_memory.storage.hypothesis")

FILENAME = "hypotheses.json"
_SCHEMA_VERSION = 1


class HypothesisStoreError(RuntimeError):
    """假說檔無法安全讀寫（損毀、pair 衝突、版本衝突）。"""


class HypothesisStore:
    """假說唯讀存儲。每次讀取依檔案 mtime／大小快取，別的行程／實例寫入後下次讀取即可見。"""

    def __init__(self, namespace_root: str | Path):
        self._root = Path(namespace_root)
        self._path = self._root / FILENAME
        self._cache_sig: tuple[int, int] | None = None
        self._cache: dict[str, Hypothesis] = {}
        self._corrupt = False
        self._mutex = threading.RLock()

    # ── 內部讀取 ─────────────────────────────────────────────

    def _read(self) -> dict[str, Hypothesis]:
        with self._mutex:
            try:
                st = self._path.stat()
                sig = (st.st_mtime_ns, st.st_size)
            except FileNotFoundError:
                self._cache_sig, self._cache, self._corrupt = None, {}, False
                return self._cache
            except OSError as exc:
                logger.warning("[HypothesisStore] 無法讀取檔案資訊: %s", exc)
                return dict(self._cache)
            if sig == self._cache_sig:
                return self._cache
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                items = raw.get("records") if isinstance(raw, dict) else raw
                if not isinstance(items, list):
                    raise ValueError("records 不是 list")
            except (OSError, ValueError) as exc:
                logger.warning("[HypothesisStore] 假說檔損毀（視為空，寫入拒絕）: %s", exc)
                self._cache_sig, self._cache, self._corrupt = sig, {}, True
                return self._cache
            records: dict[str, Hypothesis] = {}
            seen_pairs: set[str] = set()
            for item in items:
                h = Hypothesis.from_dict(item)
                if h is None or h.pair_key in seen_pairs:
                    continue  # 壞列或重複 pair 略過（pair_key 唯一）
                seen_pairs.add(h.pair_key)
                records[h.id] = h
            self._cache_sig, self._cache, self._corrupt = sig, records, False
            return self._cache

    @property
    def path(self) -> Path:
        return self._path

    @property
    def is_corrupt(self) -> bool:
        self._read()
        return self._corrupt

    # ── 查詢（皆回傳複本，呼叫端修改不影響存儲）──────────────────

    def get(self, hypothesis_id: str) -> Hypothesis | None:
        h = self._read().get(hypothesis_id)
        return Hypothesis.from_dict(h.to_dict()) if h is not None else None

    def get_by_pair(self, concept_a: str, concept_b: str) -> Hypothesis | None:
        key = make_pair_key(concept_a, concept_b)
        for h in self._read().values():
            if h.pair_key == key:
                return Hypothesis.from_dict(h.to_dict())
        return None

    def list(self, statuses: tuple[str, ...] | None = None) -> list[Hypothesis]:
        out = [
            Hypothesis.from_dict(h.to_dict()) for h in self._read().values()
            if statuses is None or h.status in statuses
        ]
        return sorted(out, key=lambda h: (h.created_at, h.id))

    def list_for_concepts(
        self, concept_ids: set[str] | frozenset[str], statuses: tuple[str, ...] | None = None,
    ) -> list[Hypothesis]:
        """端點之一落在 concept_ids 內的假說。"""
        out: list[Hypothesis] = []
        for h in self.list(statuses):
            ends = h.endpoints
            if ends and (ends[0] in concept_ids or ends[1] in concept_ids):
                out.append(h)
        return out

    def open_count(self) -> int:
        return sum(1 for h in self._read().values() if h.status in OPEN_STATUSES)

    def has_open_for_pair(self, concept_a: str, concept_b: str) -> bool:
        h = self.get_by_pair(concept_a, concept_b)
        return h is not None and h.is_open

    def is_pair_refuted(self, concept_a: str, concept_b: str) -> bool:
        """該對是否有（仍保留的）refuted 假說；供 GraphPruning 的 bridge_suppressed 回呼。"""
        h = self.get_by_pair(concept_a, concept_b)
        return h is not None and h.status == "refuted"

    def is_blocked(self, concept_a: str, concept_b: str, pair_digest: str) -> bool:
        """同 pair_digest 是否不該再提案：已 refuted，或已因 insufficient 耗盡而 expired。"""
        h = self.get_by_pair(concept_a, concept_b)
        if h is None or h.pair_digest != pair_digest:
            return False
        if h.status == "refuted":
            return True
        return h.status == "expired" and h.outcome.expire_reason == "insufficient_exhausted"

    def count(self) -> int:
        return len(self._read())


class HypothesisWriter:
    """假說寫入 handle。只由 ConceptInhibitor 建構與持有（結構測試會檢查）。

    不做商業規則（規則在 Gate 的 process_hypothesis_patch）；這裡只保證：
    pair_key 唯一、版本單調、原子寫入、檔案損毀時拒絕寫入。
    """

    def __init__(self, store: HypothesisStore):
        self._store = store

    @property
    def store(self) -> HypothesisStore:
        return self._store

    def put(self, hypothesis: Hypothesis, *, expected_version: int | None = None) -> Hypothesis:
        """新增或覆寫一筆。expected_version 不符（被他人改過）拋 HypothesisStoreError。

        寫入時 version 以「目前存儲版本 + 1」為準（新增為 1），updated_at 設為現在。
        """
        with storage_lock(self._store._root):
            records = self._load_for_write()
            current = records.get(hypothesis.id)
            if current is None:
                if expected_version is not None:
                    raise HypothesisStoreError("假說不存在，無法依版本更新")
                hypothesis.version = 1
            else:
                if expected_version is not None and current.version != expected_version:
                    raise HypothesisStoreError(
                        f"假說版本衝突: 預期 {expected_version}, 實際 {current.version}"
                    )
                hypothesis.version = current.version + 1
            for other in records.values():
                if other.pair_key == hypothesis.pair_key and other.id != hypothesis.id:
                    raise HypothesisStoreError(f"pair_key 已存在: {hypothesis.pair_key}")
            hypothesis.updated_at = datetime.now(timezone.utc)
            records[hypothesis.id] = Hypothesis.from_dict(hypothesis.to_dict())  # type: ignore[assignment]
            self._flush(records)
            return hypothesis

    def remove(self, hypothesis_ids: list[str]) -> int:
        """移除指定假說；回傳實際移除數。"""
        with storage_lock(self._store._root):
            records = self._load_for_write()
            removed = 0
            for hid in hypothesis_ids:
                if records.pop(hid, None) is not None:
                    removed += 1
            if removed:
                self._flush(records)
            return removed

    def restore(self, hypothesis_id: str, prior: dict[str, Any] | None) -> None:
        """回滾用：prior 為 None 代表原本不存在（移除），否則還原成 prior（保留較新的 version 單調）。"""
        with storage_lock(self._store._root):
            records = self._load_for_write()
            current = records.get(hypothesis_id)
            if prior is None:
                if records.pop(hypothesis_id, None) is not None:
                    self._flush(records)
                return
            restored = Hypothesis.from_dict(prior)
            if restored is None:
                raise HypothesisStoreError("回滾資料無法解析")
            restored.version = (current.version + 1) if current is not None else restored.version
            for other in records.values():
                if other.pair_key == restored.pair_key and other.id != restored.id:
                    raise HypothesisStoreError(f"pair_key 已存在: {restored.pair_key}")
            records[restored.id] = restored
            self._flush(records)

    # ── 內部 ────────────────────────────────────────────────

    def _load_for_write(self) -> dict[str, Hypothesis]:
        records = self._store._read()
        if self._store._corrupt:
            raise HypothesisStoreError("假說檔損毀，拒絕寫入以免覆蓋原檔")
        return {k: Hypothesis.from_dict(v.to_dict()) for k, v in records.items()}  # type: ignore[misc]

    def _flush(self, records: dict[str, Hypothesis]) -> None:
        payload = {
            "version": _SCHEMA_VERSION,
            "records": [h.to_dict() for h in sorted(records.values(), key=lambda x: (x.created_at, x.id))],
        }
        self._store._root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self._store._path, payload)
        self._store._cache_sig = None  # 下次讀取重新載入
