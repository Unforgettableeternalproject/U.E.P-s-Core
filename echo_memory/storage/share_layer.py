"""
ScopeShareLayer - 場域共享層（M10 包 B）

每個場域（guild）一個共享 namespace（``scope-g<guild_id>``）。Dream 把「可分享概念」投影成
只含摘要的概念寫進來，保留 provenance（擁有者 uid、來源概念 id、來源 digest），
物理上不存 episode、不存逐字證據。

撤回（即時消失）兩段式，順序固定：
  1. 先把撤回紀錄 append 到 ``revocations.jsonl``（撤回索引，持 scope 鎖，單檔 fsync）。
     這一步完成後，所有讀取端都會過濾掉該投影 -> 即時消失。
  2. 再刪除投影本體、寫 audit。第二步崩潰不影響隱私，reconcile 補完。

fail-closed：
  - 撤回索引損毀（壞行）或遺失（檔案不存在但共享 store 已有投影）-> 整個 scope 檢索回空、
    拒絕新投影。
  - 擁有者被 block（退出場域）-> 拒絕該擁有者新投影，直到 restore_owner。
  - 缺 scope／無效 scope／敏感度不可分享／摘要空白或過長／沒有 embedding -> 拒絕投影。

鎖序：永遠先擁有者、後 scope，且不巢狀持有。本模組只持 scope 鎖（storage_lock(scope_path)），
從不取擁有者鎖；cascade hook 與 LLM 一律在 scope 鎖之外呼叫。

架構禁令：本模組只做結構判斷（id、digest、索引成員），不從文字內容推測敏感度或隱私意圖。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from echo_memory.namespace import resolve_namespace_path
from echo_memory.schemas.evidence_review import SP_PREFIX, make_sp_ref, parse_sp_ref  # noqa: F401
from echo_memory.schemas.sharing import (
    SHAREABLE_SENSITIVITIES,
    namespace_to_scope,
    parse_guild_scope,
    scope_to_namespace_id,
)
from echo_memory.storage.atomic import atomic_write_json
from echo_memory.storage.locking import storage_lock

logger = logging.getLogger("echo_memory.storage.share_layer")

REVOCATIONS_FILENAME = "revocations.jsonl"
AUDIT_FILENAME = "share_audit_log.jsonl"
CONSUMPTIONS_FILENAME = "consumptions.jsonl"
CASCADE_PENDING_FILENAME = "cascade_pending.json"
SEMANTIC_DIRNAME = "semantic"

PROJECTION_KIND = "projection"
PROJECTION_NAME_PREFIX = "proj:"
SUMMARY_MAX_CHARS = 120

_INDEX_RECORD_TYPES = ("init", "revoke", "owner_block", "owner_unblock")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# scope／參考字串工具
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def normalize_scope(scope: Any) -> tuple[str, str]:
    """``guild:<id>`` 或 ``scope-g<id>`` -> (scope_id, namespace_id)；無效拋 ValueError。"""
    text = str(scope).strip() if scope is not None else ""
    from_ns = namespace_to_scope(text)
    if from_ns is not None:
        return from_ns, text
    if parse_guild_scope(text) is not None:
        return text, scope_to_namespace_id(text)
    raise ValueError(f"無效的 scope: {scope!r}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 資料結構
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class ProjectionView:
    """共享層投影的讀取視圖（只含概念摘要與 provenance，無逐字證據）。"""

    projection_id: str
    scope_id: str
    owner_uid: str
    source_concept_id: str
    source_digest: str
    summary: str
    claim_kind: str = "fact"
    support_strength: float = 0.0
    review_status: str = "insufficient"
    sensitivity: str = "none"
    learned_at: str = ""
    similarity: float = 0.0


@dataclass
class ProjectResult:
    ok: bool
    projection_id: str = ""
    reason: str = ""
    replaced_ids: list[str] = field(default_factory=list)


@dataclass
class RevokeResult:
    """撤回結果。revoked_ids＝本次新寫入撤回索引者；pending_delete＝本體尚待 reconcile 刪除者。"""

    ok: bool = True
    revoked_ids: list[str] = field(default_factory=list)
    pending_delete: list[str] = field(default_factory=list)
    index_ok: bool = True
    reason: str = ""


@dataclass(frozen=True)
class CascadeEvent:
    """撤回級聯事件：消費端（B）某輪帶入了被撤回的投影，其 agent unit 需遮蔽。

    hook 的執行屬 engine 端（包 C）：依 consumer_uid 取得 B 的 engine，對 agent_unit_ids
    做證據 tombstone（保留 B 自己的話）。hook 回傳 True 代表已處理；否則進待辦、由 reconcile 重送。
    """

    scope_id: str
    projection_id: str
    owner_uid: str
    consumer_uid: str
    episode_id: str
    agent_unit_ids: tuple[str, ...] = ()
    reason_code: str = ""

    def key(self) -> str:
        return f"{self.projection_id}|{self.consumer_uid}|{self.episode_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "projection_id": self.projection_id,
            "owner_uid": self.owner_uid,
            "consumer_uid": self.consumer_uid,
            "episode_id": self.episode_id,
            "agent_unit_ids": list(self.agent_unit_ids),
            "reason_code": self.reason_code,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CascadeEvent:
        return cls(
            scope_id=str(data.get("scope_id", "")),
            projection_id=str(data.get("projection_id", "")),
            owner_uid=str(data.get("owner_uid", "")),
            consumer_uid=str(data.get("consumer_uid", "")),
            episode_id=str(data.get("episode_id", "")),
            agent_unit_ids=tuple(str(x) for x in (data.get("agent_unit_ids") or [])),
            reason_code=str(data.get("reason_code", "")),
        )


@dataclass
class ReconcileReport:
    revoked: list[str] = field(default_factory=list)
    completed_deletes: int = 0
    unhealthy_scopes: list[str] = field(default_factory=list)
    cascades_redelivered: int = 0

    def summary(self) -> str:
        return (
            f"共享層對帳: 撤回 {len(self.revoked)}, 補刪 {self.completed_deletes}, "
            f"索引異常場域 {len(self.unhealthy_scopes)}, 級聯重送 {self.cascades_redelivered}"
        )


@dataclass
class _Index:
    healthy: bool = True
    revoked: set[str] = field(default_factory=set)
    blocked_owners: set[str] = field(default_factory=set)
    reason: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ScopeShareLayer
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class ScopeShareLayer:
    """場域共享層：投影、撤回、查詢、對帳。只暴露概念摘要，不暴露 episode／evidence。

    Args:
        storage_root: 與 MemoryEngine 相同的 storage root（共享 namespace 位於其下 namespaces/）。
        embed_fn: 摘要 -> 向量；project 在 scope 鎖外呼叫。None 時 project 必須自帶 embedding。
        semantic_provider: ``(scope_namespace_id) -> SemanticMemory``。宿主若用
            ``registry.get("scope-g<id>").semantic`` 取得共享 store，請傳入以共用同一個實例；
            缺省時本層自行建立並快取（路徑與 MemoryEngine 的 semantic 相同）。
        owner_digest_resolver: ``(owner_uid, source_concept_id) -> digest | None``。
            查詢時用來驗證 source_digest；回傳 None 代表未知（不過濾）。
        lock_timeout: scope 鎖逾時秒數。
    """

    def __init__(
        self,
        storage_root: str | Path,
        *,
        embed_fn: Callable[[str], list[float]] | None = None,
        semantic_provider: Callable[[str], Any] | None = None,
        owner_digest_resolver: Callable[[str, str], str | None] | None = None,
        lock_timeout: float = 30.0,
    ):
        self._root = Path(storage_root)
        self._embed_fn = embed_fn
        self._provider = semantic_provider
        self._digest_resolver = owner_digest_resolver
        self._lock_timeout = lock_timeout
        self._sem_cache: dict[str, Any] = {}
        self._sem_sigs: dict[tuple[str, int], tuple] = {}
        self._cache_lock = threading.Lock()
        self._index_cache: dict[str, tuple[Any, _Index]] = {}
        self._cascade_hook: Callable[[CascadeEvent], bool] | None = None

    # ── 路徑與 store ────────────────────────────────────────────

    def scope_path(self, scope: Any) -> Path:
        _, ns = normalize_scope(scope)
        return resolve_namespace_path(self._root, ns)

    def _semantic(self, ns: str) -> Any:
        """共享 store 的 SemanticMemory；每次取用前依檔案簽章重載，使多個實例交錯讀寫安全。"""
        if self._provider is not None:
            sem = self._provider(ns)
        else:
            with self._cache_lock:
                sem = self._sem_cache.get(ns)
                if sem is None:
                    from echo_memory.storage.semantic import SemanticMemory

                    sem = SemanticMemory(
                        storage_path=resolve_namespace_path(self._root, ns) / SEMANTIC_DIRNAME,
                    )
                    self._sem_cache[ns] = sem
                    self._sem_sigs[(ns, id(sem))] = self._file_sig(sem)
        self._sync(ns, sem)
        return sem

    @staticmethod
    def _file_sig(sem: Any) -> tuple:
        sig = []
        for attr in ("metadata_file", "embeddings_file"):
            try:
                st = Path(getattr(sem, attr)).stat()
                sig.append((st.st_mtime_ns, st.st_size))
            except (OSError, AttributeError):
                sig.append(None)
        return tuple(sig)

    def _sync(self, ns: str, sem: Any) -> None:
        """檔案簽章與上次所見不同（別的實例寫過）-> 清空記憶體快取並從磁碟重載。"""
        key = (ns, id(sem))
        sig = self._file_sig(sem)
        with self._cache_lock:
            known = self._sem_sigs.get(key)
            self._sem_sigs[key] = sig
        if known is None or known == sig:
            return
        try:
            with sem._write():
                sem._concepts.clear()
                sem._concepts_by_id.clear()
                sem._embeddings = None
                sem._id_to_index.clear()
                sem._idx_to_id.clear()
                sem._load()
        except Exception as e:  # noqa: BLE001 - 重載失敗：保留舊快取，撤回索引仍把關可見度
            logger.warning("[ScopeShareLayer] 共享 store 重載失敗: %s", e)

    def _remember(self, ns: str) -> None:
        """自己寫入後記錄新的檔案簽章（記憶體已是最新，不必重載）。"""
        sem = self._provider(ns) if self._provider is not None else self._sem_cache.get(ns)
        if sem is not None:
            with self._cache_lock:
                self._sem_sigs[(ns, id(sem))] = self._file_sig(sem)

    def list_scopes(self) -> list[str]:
        """磁碟上存在的共享 namespace 對應的 scope id。"""
        base = self._root / "namespaces"
        if not base.is_dir():
            return []
        out: list[str] = []
        for child in sorted(base.iterdir()):
            scope = namespace_to_scope(child.name) if child.is_dir() else None
            if scope is not None:
                out.append(scope)
        return out

    @staticmethod
    def _projection_concepts(sem: Any) -> list[Any]:
        return [
            c for c in sem.list_concepts(limit=None)
            if (c.metadata or {}).get("kind") == PROJECTION_KIND
        ]

    def _has_projections(self, ns: str, ns_path: Path) -> bool:
        if not (ns_path / SEMANTIC_DIRNAME / "concepts.json").exists():
            return False
        return bool(self._projection_concepts(self._semantic(ns)))

    # ── 撤回索引 ────────────────────────────────────────────────

    def _index_file(self, ns_path: Path) -> Path:
        return ns_path / REVOCATIONS_FILENAME

    def _load_index(self, ns: str, ns_path: Path) -> _Index:
        """讀撤回索引（mtime 快取）。損毀或遺失（但已有投影）-> healthy=False。"""
        if not ns_path.exists():
            return _Index()
        path = self._index_file(ns_path)
        if not path.exists():
            if self._has_projections(ns, ns_path):
                return _Index(healthy=False, reason="index_missing")
            return _Index()
        try:
            st = path.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            return _Index(healthy=False, reason="index_unreadable")
        with self._cache_lock:
            cached = self._index_cache.get(str(path))
            if cached is not None and cached[0] == sig:
                return cached[1]
        idx = _Index()
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                if not raw.strip():
                    continue
                rec = json.loads(raw)
                if not isinstance(rec, dict) or rec.get("type") not in _INDEX_RECORD_TYPES:
                    raise ValueError("unknown record")
                kind = rec["type"]
                if kind == "revoke":
                    pid = str(rec.get("projection_id") or "")
                    if not pid:
                        raise ValueError("revoke without projection_id")
                    idx.revoked.add(pid)
                elif kind == "owner_block":
                    idx.blocked_owners.add(str(rec.get("owner_uid") or ""))
                elif kind == "owner_unblock":
                    idx.blocked_owners.discard(str(rec.get("owner_uid") or ""))
        except Exception as e:  # noqa: BLE001 - 任何損毀都 fail-closed
            logger.warning("[ScopeShareLayer] 撤回索引損毀 %s: %s", path, e)
            return _Index(healthy=False, reason="index_corrupt")
        with self._cache_lock:
            self._index_cache[str(path)] = (sig, idx)
        return idx

    def _append_jsonl(self, path: Path, records: Iterable[dict[str, Any]]) -> None:
        payload = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
        if not payload:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())

    def _ensure_index(self, ns: str, ns_path: Path) -> _Index | None:
        """（須持 scope 鎖）確保索引檔存在；索引異常回傳 None。"""
        idx = self._load_index(ns, ns_path)
        if not idx.healthy:
            return None
        path = self._index_file(ns_path)
        if not path.exists():
            self._append_jsonl(path, [{"type": "init", "at": _now()}])
            idx = self._load_index(ns, ns_path)
        return idx if idx.healthy else None

    def is_healthy(self, scope: Any) -> bool:
        """撤回索引是否可用（不可用時整個 scope 檢索回空）。"""
        try:
            _, ns = normalize_scope(scope)
        except ValueError:
            return False
        return self._load_index(ns, resolve_namespace_path(self._root, ns)).healthy

    def is_owner_blocked(self, scope: Any, owner_uid: str) -> bool:
        """擁有者是否被 block；索引異常一律視為 blocked（fail-closed）。"""
        try:
            _, ns = normalize_scope(scope)
        except ValueError:
            return True
        idx = self._load_index(ns, resolve_namespace_path(self._root, ns))
        return (not idx.healthy) or str(owner_uid) in idx.blocked_owners

    def _audit(self, ns_path: Path, action: str, **fields: Any) -> bool:
        """audit 只記 id／動作／原因碼，不含文字。"""
        try:
            self._append_jsonl(
                ns_path / AUDIT_FILENAME,
                [{"at": _now(), "action": action, **fields}],
            )
            return True
        except OSError as e:
            logger.warning("[ScopeShareLayer] audit 寫入失敗: %s", e)
            return False

    # ── 投影 ────────────────────────────────────────────────────

    def project(
        self,
        scope: Any,
        *,
        owner_uid: str,
        source_concept_id: str,
        source_digest: str,
        summary: str,
        claim_kind: str = "fact",
        support_strength: float = 0.0,
        review_status: str = "insufficient",
        sensitivity: str = "none",
        cited_unit_count: int = 0,
        embedding: list[float] | None = None,
        projection_id: str | None = None,
    ) -> ProjectResult:
        """寫入一筆投影（只存摘要與 provenance）。同擁有者同來源概念的舊投影會先被撤回。

        embedding 與摘要長度驗證都在 scope 鎖之外；鎖內只做索引檢查、單一概念寫入與 audit。
        """
        try:
            scope_id, ns = normalize_scope(scope)
        except ValueError:
            return ProjectResult(False, reason="invalid_scope")
        owner = str(owner_uid or "").strip()
        if not owner:
            return ProjectResult(False, reason="missing_owner")
        if not source_concept_id or not source_digest:
            return ProjectResult(False, reason="missing_provenance")
        if sensitivity not in SHAREABLE_SENSITIVITIES:
            return ProjectResult(False, reason="sensitivity_not_shareable")
        text = (summary or "").strip()
        if not text:
            return ProjectResult(False, reason="empty_summary")
        if len(text) > SUMMARY_MAX_CHARS:
            return ProjectResult(False, reason="summary_too_long")

        vec = embedding
        if vec is None and self._embed_fn is not None:
            try:
                vec = self._embed_fn(text)
            except Exception as e:  # noqa: BLE001
                logger.warning("[ScopeShareLayer] 摘要 embedding 失敗: %s", e)
                return ProjectResult(False, reason="embedding_error")
        if vec is None or len(vec) == 0:
            return ProjectResult(False, reason="embedding_unavailable")

        pid = projection_id or f"proj_{uuid.uuid4().hex[:12]}"
        ns_path = resolve_namespace_path(self._root, ns)
        replaced: list[str] = []
        try:
            with storage_lock(ns_path, timeout=self._lock_timeout):
                idx = self._ensure_index(ns, ns_path)
                if idx is None:
                    return ProjectResult(False, reason="index_unhealthy")
                if owner in idx.blocked_owners:
                    return ProjectResult(False, reason="owner_blocked")
                sem = self._semantic(ns)
                stale = [
                    c for c in self._projection_concepts(sem)
                    if c.metadata.get("owner_uid") == owner
                    and c.metadata.get("source_concept_id") == source_concept_id
                    and c.metadata.get("projection_id") != pid
                ]
                # audit-before-mutate：audit 失敗則不寫入投影
                if not self._audit(
                    ns_path, "project", projection_id=pid, owner_uid=owner,
                    source_concept_id=source_concept_id, reason_code="project",
                ):
                    return ProjectResult(False, reason="audit_failed")
                live_stale = [c for c in stale if c.metadata.get("projection_id") not in idx.revoked]
                self._append_jsonl(self._index_file(ns_path), [
                    {
                        "type": "revoke",
                        "projection_id": c.metadata.get("projection_id"),
                        "owner_uid": owner,
                        "source_concept_id": source_concept_id,
                        "at": _now(),
                        "reason_code": "replaced",
                    }
                    for c in live_stale
                ])
                replaced = [str(c.metadata.get("projection_id")) for c in live_stale]
                sem.upsert_concept(
                    name=f"{PROJECTION_NAME_PREFIX}{pid}",
                    description=text,
                    concept_type="abstract",
                    embedding=list(vec),
                    metadata={
                        "kind": PROJECTION_KIND,
                        "projection_id": pid,
                        "scope_id": scope_id,
                        "owner_uid": owner,
                        "source_concept_id": source_concept_id,
                        "source_digest": source_digest,
                        "claim_kind": claim_kind,
                        "support_strength": round(float(support_strength), 4),
                        "review_status": review_status,
                        "sensitivity": sensitivity,
                        "cited_unit_count": int(cited_unit_count),
                        "learned_at": _now(),
                    },
                    producer="share",
                )
                for c in stale:
                    try:
                        sem.remove_concept(c.name)
                    except Exception as e:  # noqa: BLE001 - 已撤回，本體由 reconcile 補刪
                        logger.warning("[ScopeShareLayer] 舊投影本體刪除失敗（reconcile 補）: %s", e)
                self._remember(ns)
        except Exception as e:  # noqa: BLE001
            logger.warning("[ScopeShareLayer] 投影寫入失敗: %s", e)
            return ProjectResult(False, reason=f"io_error: {e}")
        return ProjectResult(True, pid, replaced_ids=replaced)

    # ── 撤回 ────────────────────────────────────────────────────

    def set_cascade_hook(self, hook: Callable[[CascadeEvent], bool] | None) -> None:
        """註冊撤回級聯 hook（engine 端執行，見 CascadeEvent）。None 取消。"""
        self._cascade_hook = hook

    def revoke(
        self,
        scope: Any,
        projection_ids: Iterable[str],
        *,
        reason_code: str = "revoked",
        owner_uid: str | None = None,
        source_concept_id: str | None = None,
    ) -> RevokeResult:
        """撤回指定投影。先寫撤回索引（即時消失），再刪本體；未知 id 也會寫入索引（冪等、安全）。"""
        ids = list(dict.fromkeys(str(p) for p in projection_ids if p))
        return self._revoke_impl(
            scope, ids, None, reason_code, owner_uid, source_concept_id, block_owner=None,
        )

    def revoke_owner(
        self, scope: Any, owner_uid: str, *, reason_code: str = "member_left", block: bool = True,
    ) -> RevokeResult:
        """成員退出場域：撤回該擁有者在此 scope 的所有投影。

        block=True 另寫 owner_block：在 restore_owner 之前，該擁有者的新投影一律被拒
        （否則其自己的 Dream 會立刻重新投影）。
        """
        owner = str(owner_uid or "").strip()
        if not owner:
            return RevokeResult(ok=False, reason="missing_owner")
        return self._revoke_impl(
            scope, None, owner, reason_code, owner, None, block_owner=owner if block else None,
        )

    def restore_owner(self, scope: Any, owner_uid: str) -> bool:
        """成員回到場域：解除 block。舊投影維持撤回，下次 Dream 重判後才會重新投影。"""
        try:
            _, ns = normalize_scope(scope)
        except ValueError:
            return False
        ns_path = resolve_namespace_path(self._root, ns)
        if not ns_path.exists():
            return True
        try:
            with storage_lock(ns_path, timeout=self._lock_timeout):
                if self._ensure_index(ns, ns_path) is None:
                    return False
                self._append_jsonl(self._index_file(ns_path), [
                    {"type": "owner_unblock", "owner_uid": str(owner_uid), "at": _now()},
                ])
            self._audit(ns_path, "owner_unblock", owner_uid=str(owner_uid), reason_code="member_rejoined")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("[ScopeShareLayer] restore_owner 失敗: %s", e)
            return False

    def revoke_owner_all_scopes(
        self, owner_uid: str, *, reason_code: str = "owner_cleared", block: bool = True,
    ) -> dict[str, RevokeResult]:
        """擁有者要求清除所有分享：撤回其在每個場域的投影（有投影的場域同時 block）。"""
        out: dict[str, RevokeResult] = {}
        owner = str(owner_uid or "").strip()
        if not owner:
            return out
        for scope_id in self.list_scopes():
            try:
                views = self.list_projections(scope_id, owner_uid=owner)
            except Exception:  # noqa: BLE001
                views = []
            if not views:
                continue
            out[scope_id] = self.revoke_owner(scope_id, owner, reason_code=reason_code, block=block)
        return out

    def _revoke_impl(
        self,
        scope: Any,
        ids: list[str] | None,
        owner_filter: str | None,
        reason_code: str,
        owner_uid: str | None,
        source_concept_id: str | None,
        *,
        block_owner: str | None,
    ) -> RevokeResult:
        try:
            scope_id, ns = normalize_scope(scope)
        except ValueError:
            return RevokeResult(ok=False, reason="invalid_scope")
        ns_path = resolve_namespace_path(self._root, ns)
        result = RevokeResult()
        removed: dict[str, dict[str, Any]] = {}
        if not ns_path.exists() and not block_owner:
            return result  # 場域尚無共享 namespace：沒有可撤回的東西（block 例外：要先記下）
        try:
            with storage_lock(ns_path, timeout=self._lock_timeout):
                idx = self._ensure_index(ns, ns_path)
                sem = self._semantic(ns)
                bodies = (
                    {
                        str(c.metadata.get("projection_id")): c
                        for c in self._projection_concepts(sem)
                    }
                    if (ns_path / SEMANTIC_DIRNAME / "concepts.json").exists() else {}
                )
                if ids is None:
                    ids = [
                        pid for pid, c in bodies.items()
                        if c.metadata.get("owner_uid") == owner_filter
                    ]
                if idx is None:
                    # 索引異常：無法再寫撤回紀錄，但本體直接物理刪除（檢索本來就整個回空）
                    result.index_ok = False
                    result.reason = "index_unhealthy"
                else:
                    fresh = [pid for pid in ids if pid not in idx.revoked]
                    records: list[dict[str, Any]] = []
                    if block_owner:
                        records.append({
                            "type": "owner_block", "owner_uid": block_owner, "at": _now(),
                            "reason_code": reason_code,
                        })
                    for pid in fresh:
                        body = bodies.get(pid)
                        meta = body.metadata if body is not None else {}
                        records.append({
                            "type": "revoke",
                            "projection_id": pid,
                            "owner_uid": meta.get("owner_uid") or owner_uid or "",
                            "source_concept_id": meta.get("source_concept_id") or source_concept_id or "",
                            "at": _now(),
                            "reason_code": reason_code,
                        })
                    # 步驟 1：撤回索引（這一步完成即「即時消失」）
                    self._append_jsonl(self._index_file(ns_path), records)
                    result.revoked_ids = list(fresh)
                # 步驟 2：刪本體（失敗不影響隱私，reconcile 補完）
                for pid in ids:
                    body = bodies.get(pid)
                    if body is None:
                        continue
                    removed[pid] = dict(body.metadata)
                    try:
                        sem.remove_concept(body.name)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("[ScopeShareLayer] 投影本體刪除失敗（reconcile 補）: %s", e)
                        result.pending_delete.append(pid)
                self._remember(ns)
                for pid in ids:
                    self._audit(
                        ns_path, "revoke", projection_id=pid,
                        owner_uid=(removed.get(pid) or {}).get("owner_uid") or owner_uid or "",
                        reason_code=reason_code,
                    )
        except Exception as e:  # noqa: BLE001
            logger.warning("[ScopeShareLayer] 撤回失敗: %s", e)
            return RevokeResult(ok=False, index_ok=False, reason=f"io_error: {e}")

        # 級聯（scope 鎖之外）：B 端含 A 內容的 agent unit 遮蔽
        for pid in result.revoked_ids:  # 只對「本次新撤回」者級聯（已撤回的早已處理或在待辦）
            owner = (removed.get(pid) or {}).get("owner_uid") or owner_uid or ""
            self._cascade(scope_id, ns_path, pid, owner, reason_code)
        result.ok = result.index_ok
        return result

    # ── 級聯 ────────────────────────────────────────────────────

    def record_consumption(
        self,
        scope: Any,
        projection_ids: Iterable[str],
        *,
        consumer_uid: str,
        episode_id: str,
        agent_unit_ids: Iterable[str] = (),
    ) -> bool:
        """（包 C 在 store_episode 後呼叫）記錄某輪對話帶入了哪些投影、哪些 agent unit 含其內容。"""
        ids = [str(p) for p in projection_ids if p]
        if not ids:
            return True
        try:
            _, ns = normalize_scope(scope)
            ns_path = resolve_namespace_path(self._root, ns)
            with storage_lock(ns_path, timeout=self._lock_timeout):
                self._append_jsonl(ns_path / CONSUMPTIONS_FILENAME, [{
                    "projection_ids": ids,
                    "consumer_uid": str(consumer_uid),
                    "episode_id": str(episode_id),
                    "agent_unit_ids": [str(u) for u in agent_unit_ids],
                    "at": _now(),
                }])
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("[ScopeShareLayer] 消費紀錄寫入失敗: %s", e)
            return False

    def _read_consumptions(self, ns_path: Path) -> list[dict[str, Any]]:
        path = ns_path / CONSUMPTIONS_FILENAME
        if not path.exists():
            return []
        out: list[dict[str, Any]] = []
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                if not raw.strip():
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
        except OSError:
            return []
        return out

    def consumers_of(self, scope: Any, projection_id: str) -> set[str]:
        """曾被帶入此投影的消費端 uid（交叉佐證的循環防護用）。"""
        try:
            _, ns = normalize_scope(scope)
        except ValueError:
            return set()
        ns_path = resolve_namespace_path(self._root, ns)
        return {
            str(r.get("consumer_uid"))
            for r in self._read_consumptions(ns_path)
            if projection_id in (r.get("projection_ids") or [])
        }

    def _cascade(
        self, scope_id: str, ns_path: Path, projection_id: str, owner_uid: str, reason_code: str,
    ) -> None:
        events = [
            CascadeEvent(
                scope_id=scope_id,
                projection_id=projection_id,
                owner_uid=str(owner_uid),
                consumer_uid=str(r.get("consumer_uid", "")),
                episode_id=str(r.get("episode_id", "")),
                agent_unit_ids=tuple(str(u) for u in (r.get("agent_unit_ids") or [])),
                reason_code=reason_code,
            )
            for r in self._read_consumptions(ns_path)
            if projection_id in (r.get("projection_ids") or [])
        ]
        for event in events:
            if not self._deliver(event):
                self._add_pending(ns_path, event)

    def _deliver(self, event: CascadeEvent) -> bool:
        hook = self._cascade_hook
        if hook is None:
            return False
        try:
            return bool(hook(event))
        except Exception as e:  # noqa: BLE001 - hook 失敗進待辦，不拖垮撤回
            logger.warning("[ScopeShareLayer] 級聯 hook 失敗（進待辦）: %s", e)
            return False

    def _read_pending(self, ns_path: Path) -> dict[str, dict[str, Any]]:
        path = ns_path / CASCADE_PENDING_FILENAME
        try:
            if not path.exists():
                return {}
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _add_pending(self, ns_path: Path, event: CascadeEvent) -> None:
        try:
            with storage_lock(ns_path, timeout=self._lock_timeout):
                pending = self._read_pending(ns_path)
                pending[event.key()] = event.to_dict()
                atomic_write_json(ns_path / CASCADE_PENDING_FILENAME, pending)
        except Exception as e:  # noqa: BLE001
            logger.warning("[ScopeShareLayer] 級聯待辦寫入失敗: %s", e)

    def pending_cascades(self, scope: Any) -> list[CascadeEvent]:
        _, ns = normalize_scope(scope)
        ns_path = resolve_namespace_path(self._root, ns)
        return [CascadeEvent.from_dict(v) for v in self._read_pending(ns_path).values()]

    def run_pending_cascades(self, scope: Any | None = None) -> int:
        """重送尚未成功的級聯事件（hook 在 scope 鎖外呼叫）；回傳成功筆數。"""
        scopes = [scope] if scope is not None else self.list_scopes()
        done = 0
        for sc in scopes:
            try:
                _, ns = normalize_scope(sc)
            except ValueError:
                continue
            ns_path = resolve_namespace_path(self._root, ns)
            delivered: list[str] = []
            for key, raw in list(self._read_pending(ns_path).items()):
                if self._deliver(CascadeEvent.from_dict(raw)):
                    delivered.append(key)
            if not delivered:
                continue
            try:
                with storage_lock(ns_path, timeout=self._lock_timeout):
                    pending = self._read_pending(ns_path)
                    for key in delivered:
                        pending.pop(key, None)
                    atomic_write_json(ns_path / CASCADE_PENDING_FILENAME, pending)
                done += len(delivered)
            except Exception as e:  # noqa: BLE001
                logger.warning("[ScopeShareLayer] 級聯待辦清除失敗: %s", e)
        return done

    # ── 查詢 ────────────────────────────────────────────────────

    def _view(self, concept: Any, scope_id: str, similarity: float = 0.0) -> ProjectionView:
        m = concept.metadata or {}
        try:
            strength = float(m.get("support_strength", 0.0))
        except (TypeError, ValueError):
            strength = 0.0
        return ProjectionView(
            projection_id=str(m.get("projection_id", "")),
            scope_id=scope_id,
            owner_uid=str(m.get("owner_uid", "")),
            source_concept_id=str(m.get("source_concept_id", "")),
            source_digest=str(m.get("source_digest", "")),
            summary=concept.description,
            claim_kind=str(m.get("claim_kind", "fact")),
            support_strength=strength,
            review_status=str(m.get("review_status", "insufficient")),
            sensitivity=str(m.get("sensitivity", "none")),
            learned_at=str(m.get("learned_at", "")),
            similarity=similarity,
        )

    def _visible(self, concept: Any, idx: _Index) -> bool:
        m = concept.metadata or {}
        if m.get("kind") != PROJECTION_KIND or concept.status != "active":
            return False
        if str(m.get("projection_id", "")) in idx.revoked:
            return False
        if str(m.get("owner_uid", "")) in idx.blocked_owners:
            return False
        if m.get("sensitivity") not in SHAREABLE_SENSITIVITIES:
            return False
        if self._digest_resolver is not None:
            try:
                current = self._digest_resolver(
                    str(m.get("owner_uid", "")), str(m.get("source_concept_id", "")),
                )
            except Exception:  # noqa: BLE001 - 無法驗證 -> 視為未知，不過濾
                current = None
            if current is not None and current != m.get("source_digest"):
                return False
        return True

    def list_projections(
        self, scope: Any, *, owner_uid: str | None = None,
    ) -> list[ProjectionView]:
        """目前可見（未撤回、擁有者未 block）的投影；索引異常回傳空。"""
        try:
            scope_id, ns = normalize_scope(scope)
        except ValueError:
            return []
        ns_path = resolve_namespace_path(self._root, ns)
        idx = self._load_index(ns, ns_path)
        if not idx.healthy or not ns_path.exists():
            return []
        if not (ns_path / SEMANTIC_DIRNAME / "concepts.json").exists():
            return []
        sem = self._semantic(ns)
        views = [
            self._view(c, scope_id)
            for c in self._projection_concepts(sem)
            if self._visible(c, idx)
        ]
        if owner_uid is not None:
            views = [v for v in views if v.owner_uid == str(owner_uid)]
        return views

    def get_projection(self, scope: Any, projection_id: str) -> ProjectionView | None:
        for v in self.list_projections(scope):
            if v.projection_id == projection_id:
                return v
        return None

    def is_live(self, scope: Any, projection_id: str) -> bool:
        return bool(projection_id) and self.get_projection(scope, projection_id) is not None

    def search_projections(
        self,
        scope: Any,
        query_embedding: list[float],
        *,
        exclude_owner: str | None = None,
        top_k: int = 3,
        min_similarity: float = 0.45,
    ) -> list[ProjectionView]:
        """向量搜尋可見投影。撤回索引異常、scope 無效、無向量 -> 回空（fail-closed）。"""
        try:
            scope_id, ns = normalize_scope(scope)
        except ValueError:
            return []
        ns_path = resolve_namespace_path(self._root, ns)
        idx = self._load_index(ns, ns_path)
        if not idx.healthy or not ns_path.exists() or not query_embedding or top_k <= 0:
            return []
        if not (ns_path / SEMANTIC_DIRNAME / "concepts.json").exists():
            return []
        sem = self._semantic(ns)
        pool = max(top_k * 4, 20)
        out: list[ProjectionView] = []
        for concept, sim in sem.search_by_vector(
            list(query_embedding), top_k=pool, min_similarity=min_similarity,
        ):
            if not self._visible(concept, idx):
                continue
            if exclude_owner is not None and concept.metadata.get("owner_uid") == str(exclude_owner):
                continue
            out.append(self._view(concept, scope_id, similarity=float(sim)))
            if len(out) >= top_k:
                break
        return out

    def validate_sp_ref(self, ref: str) -> bool:
        """交叉佐證參考是否仍有效：投影存在、未撤回、擁有者未 block、digest 相符。"""
        parsed = parse_sp_ref(ref)
        if parsed is None:
            return False
        view = self.get_projection(parsed.scope, parsed.projection_id)
        return view is not None and view.source_digest == parsed.source_digest

    def stats(self, scope: Any) -> dict[str, int]:
        """只回計數（開發者管理介面用），不含任何文字。"""
        scope_id, ns = normalize_scope(scope)
        ns_path = resolve_namespace_path(self._root, ns)
        idx = self._load_index(ns, ns_path)
        total = 0
        if (ns_path / SEMANTIC_DIRNAME / "concepts.json").exists():
            total = len(self._projection_concepts(self._semantic(ns)))
        live = len(self.list_projections(scope_id))
        return {
            "healthy": int(idx.healthy),
            "projections_live": live,
            "projections_stored": total,
            "revoked": len(idx.revoked),
            "blocked_owners": len(idx.blocked_owners),
        }

    # ── 對帳 ────────────────────────────────────────────────────

    def reconcile_owner(
        self,
        owner_uid: str,
        owner_lookup: Callable[[str, str], dict[str, Any] | None],
        *,
        scopes: Iterable[Any] | None = None,
    ) -> ReconcileReport:
        """Dream 開頭的對帳（批次）：

        (a) 該擁有者的投影，其來源概念不存在、owner 端 projection_id 不符、level 不是 scope_shared、
            或 digest 已變 -> 撤回；
        (c) 已撤回但本體仍在（兩段式第二步崩潰）-> 補刪；
        (d) 重送待辦的級聯。

        owner_lookup(scope_id, source_concept_id) -> {"exists": bool, "projection_id": str,
        "level": str, "digest": str} | None。None＝無法判斷（不撤回）；拋例外同 None。
        不碰索引異常的 scope（回報在 unhealthy_scopes，需人工 quarantine_and_reset）。
        """
        report = ReconcileReport()
        owner = str(owner_uid)
        targets = list(scopes) if scopes is not None else self.list_scopes()
        for sc in targets:
            try:
                scope_id, ns = normalize_scope(sc)
            except ValueError:
                continue
            ns_path = resolve_namespace_path(self._root, ns)
            if not ns_path.exists():
                continue
            idx = self._load_index(ns, ns_path)
            if not idx.healthy:
                report.unhealthy_scopes.append(scope_id)
                continue
            if not (ns_path / SEMANTIC_DIRNAME / "concepts.json").exists():
                continue
            sem = self._semantic(ns)
            report.completed_deletes += self._complete_deletes(ns, ns_path, idx)
            to_revoke: dict[str, str] = {}
            for c in self._projection_concepts(sem):
                m = c.metadata
                pid = str(m.get("projection_id", ""))
                if pid in idx.revoked or str(m.get("owner_uid", "")) != owner:
                    continue
                try:
                    info = owner_lookup(scope_id, str(m.get("source_concept_id", "")))
                except Exception as e:  # noqa: BLE001
                    logger.warning("[ScopeShareLayer] owner_lookup 失敗，略過: %s", e)
                    info = None
                if info is None:
                    continue
                if not info.get("exists"):
                    to_revoke[pid] = "owner_concept_missing"
                elif str(info.get("projection_id") or "") != pid:
                    to_revoke[pid] = "projection_id_mismatch"
                elif info.get("level") != "scope_shared":
                    to_revoke[pid] = "level_not_shared"
                elif str(info.get("digest") or "") != str(m.get("source_digest", "")):
                    to_revoke[pid] = "digest_changed"
            for reason in sorted(set(to_revoke.values())):
                ids = [p for p, r in to_revoke.items() if r == reason]
                res = self.revoke(scope_id, ids, reason_code=reason, owner_uid=owner)
                report.revoked.extend(res.revoked_ids)
        report.cascades_redelivered = self.run_pending_cascades()
        return report

    def _complete_deletes(self, ns: str, ns_path: Path, idx: _Index) -> int:
        """已在撤回索引、本體仍在的投影補刪。"""
        done = 0
        try:
            with storage_lock(ns_path, timeout=self._lock_timeout):
                sem = self._semantic(ns)
                for c in self._projection_concepts(sem):
                    if str(c.metadata.get("projection_id", "")) in idx.revoked:
                        try:
                            if sem.remove_concept(c.name):
                                done += 1
                        except Exception as e:  # noqa: BLE001
                            logger.warning("[ScopeShareLayer] 補刪失敗: %s", e)
                self._remember(ns)
        except Exception as e:  # noqa: BLE001
            logger.warning("[ScopeShareLayer] 補刪取鎖失敗: %s", e)
        return done

    def quarantine_and_reset(self, scope: Any) -> int:
        """撤回索引異常時的人工復原：刪光該 scope 全部投影並重建索引（擁有者之後重新投影）。

        從舊索引逐行搶救仍可解析的紀錄：owner_block／owner_unblock／revoke 都保留（不丟 block），
        被刪的投影補寫 revoke，並對其觸發級聯（失敗進待辦）。回傳刪除的投影數。
        """
        from echo_memory.storage.atomic import atomic_write

        scope_id, ns = normalize_scope(scope)
        ns_path = resolve_namespace_path(self._root, ns)
        removed_meta: dict[str, dict[str, Any]] = {}

        with storage_lock(ns_path, timeout=self._lock_timeout):
            path = self._index_file(ns_path)
            kept: list[dict[str, Any]] = []
            seen_revoked: set[str] = set()
            if path.exists():
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except OSError:
                    lines = []
                for raw in lines:
                    try:
                        rec = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(rec, dict) or rec.get("type") not in _INDEX_RECORD_TYPES:
                        continue
                    if rec["type"] == "init":
                        continue
                    if rec["type"] == "revoke":
                        if not rec.get("projection_id"):
                            continue
                        seen_revoked.add(str(rec["projection_id"]))
                    kept.append(rec)
            if (ns_path / SEMANTIC_DIRNAME / "concepts.json").exists():
                sem = self._semantic(ns)
                for c in self._projection_concepts(sem):
                    pid = str(c.metadata.get("projection_id", ""))
                    removed_meta[pid] = dict(c.metadata)
                    if sem.remove_concept(c.name) and pid not in seen_revoked:
                        kept.append({
                            "type": "revoke", "projection_id": pid,
                            "owner_uid": c.metadata.get("owner_uid", ""),
                            "source_concept_id": c.metadata.get("source_concept_id", ""),
                            "at": _now(), "reason_code": "quarantine",
                        })
                self._remember(ns)
            records = [{"type": "init", "at": _now(), "reason_code": "reset"}, *kept]
            atomic_write(path, lambda f: f.write(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
            ))
            with self._cache_lock:
                self._index_cache.pop(str(path), None)
        self._audit(ns_path, "quarantine_reset", reason_code="index_unhealthy", count=len(removed_meta))
        for pid, meta in removed_meta.items():
            self._cascade(scope_id, ns_path, pid, str(meta.get("owner_uid", "")), "quarantine")
        return len(removed_meta)
