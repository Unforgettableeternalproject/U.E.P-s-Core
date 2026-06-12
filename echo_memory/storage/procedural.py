"""
ProceduralMemory - 程序記憶存儲

第四層記憶：技能/SOP 的持久化存儲 + 向量索引。

存儲結構（與 SemanticMemory 平行）：
  - procedures.json: 程序元數據
  - procedure_embeddings.npy: L2-normalized 向量矩陣

設計決策：
  - 維持 NumPy+JSON 檔案制（與 SemanticMemory 一致）
  - 向量搜索用 dot product（存儲時已 L2-normalize）
  - embedding 生成委託給外部（MemoryEngine 注入 embedder）
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from echo_memory.schemas.procedure import Procedure

logger = logging.getLogger("echo_memory.procedural")


class ProceduralMemory:
    """
    程序記憶管理器 — 持久化 + 向量索引。

    存儲檔案：
      {storage_path}/procedures.json     — 程序元數據（JSON）
      {storage_path}/procedure_embeddings.npy — L2-normalized 向量矩陣
    """

    def __init__(
        self,
        storage_path: str | Path,
        embed_fn: Callable[[str], list[float]] | None = None,
    ):
        """
        初始化程序記憶。

        Args:
            storage_path: 存儲目錄
            embed_fn: 文本 → embedding 的函式（由 MemoryEngine 注入）。
                      若為 None，write 時必須自行提供 embedding。
        """
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)

        self.metadata_file = self.storage_path / "procedures.json"
        self.embeddings_file = self.storage_path / "procedure_embeddings.npy"

        self._embed_fn = embed_fn

        # 記憶體快取
        self._procedures: dict[str, Procedure] = {}       # name → Procedure
        self._procedures_by_id: dict[str, Procedure] = {}  # id → Procedure
        self._embeddings: np.ndarray | None = None
        self._id_to_index: dict[str, int] = {}
        self._idx_to_id: dict[int, str] = {}

        self._load()

        logger.info(
            f"[ProceduralMemory] 初始化完成 — "
            f"已載入 {len(self._procedures)} 個程序"
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 寫入 API
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def write(self, procedure: Procedure) -> str:
        """
        寫入新程序或更新已有同名程序。

        Args:
            procedure: Procedure 物件

        Returns:
            procedure_id
        """
        is_update = procedure.name in self._procedures

        if is_update:
            existing = self._procedures[procedure.name]
            existing.description = procedure.description
            existing.steps = procedure.steps
            existing.domain = procedure.domain
            existing.trigger_patterns = procedure.trigger_patterns
            existing.last_used = datetime.now(timezone.utc)
            if procedure.metadata:
                existing.metadata.update(procedure.metadata)
            # Major #3：合併新來源 Episode
            for ep_id in procedure.source_episode_ids:
                if ep_id not in existing.source_episode_ids:
                    existing.source_episode_ids.append(ep_id)
            proc = existing
        else:
            self._procedures[procedure.name] = procedure
            self._procedures_by_id[procedure.id] = procedure
            proc = procedure

        # 生成/更新 embedding
        # Major #4 修復：更新時也要重算 embedding（內容變了向量要跟上）
        if self._embed_fn is not None and (is_update or not proc.embedding):
            text = f"{proc.name}: {proc.description}. 步驟: {'; '.join(proc.steps)}"
            vec = self._embed_fn(text)
            proc.embedding = self._normalize_vector(vec).tolist()

        if proc.embedding:
            self._update_embedding_index(proc, is_update=is_update)

        self._save()

        action = "更新" if is_update else "新增"
        logger.debug(f"[ProceduralMemory] {action}程序: {proc.name} (id={proc.id})")
        return proc.id

    def refine(
        self,
        original_id: str,
        new_steps: list[str],
        new_description: str | None = None,
    ) -> str | None:
        """
        精化程序（建立新版本，保留溯源）。

        Args:
            original_id: 原始 Procedure ID
            new_steps: 新的步驟序列
            new_description: 新描述（若 None 則沿用原版）

        Returns:
            新 Procedure ID，若原始不存在回傳 None
        """
        original = self._procedures_by_id.get(original_id)
        if original is None:
            return None

        refined = Procedure(
            name=original.name,
            description=new_description or original.description,
            steps=new_steps,
            domain=original.domain,
            trigger_patterns=original.trigger_patterns,
            source_episode_ids=list(original.source_episode_ids),
            version=original.version + 1,
            parent_id=original.id,
            metadata=dict(original.metadata),
        )

        # 封存舊版
        original.is_archived = True

        # 更新快取（新版取代舊版的 name 映射）
        self._procedures[refined.name] = refined
        self._procedures_by_id[refined.id] = refined

        # 生成 embedding
        if self._embed_fn is not None:
            text = f"{refined.name}: {refined.description}. 步驟: {'; '.join(refined.steps)}"
            refined.embedding = self._normalize_vector(self._embed_fn(text)).tolist()

        self._rebuild_embeddings()
        self._save()

        logger.info(
            f"[ProceduralMemory] 精化程序: {refined.name} "
            f"v{original.version} → v{refined.version}"
        )
        return refined.id

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 查詢 API
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def get(self, procedure_id: str) -> Procedure | None:
        """根據 ID 獲取程序"""
        return self._procedures_by_id.get(procedure_id)

    def get_by_name(self, name: str) -> Procedure | None:
        """根據名稱獲取程序（回傳最新版）"""
        return self._procedures.get(name)

    def search_by_vector(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        min_similarity: float = 0.3,
        domain: str | None = None,
    ) -> list[tuple[Procedure, float]]:
        """
        向量相似度搜索程序。

        Args:
            query_embedding: 查詢向量
            top_k: 返回數量
            min_similarity: 最小相似度閾值
            domain: 篩選領域（None = 全部）

        Returns:
            [(Procedure, similarity_score), ...] 按相似度降序
        """
        if self._embeddings is None or len(self._procedures_by_id) == 0:
            return []

        query_vec = self._normalize_vector(query_embedding)
        if not np.any(query_vec):
            return []

        similarities = np.dot(self._embeddings, query_vec)
        top_indices = np.argsort(similarities)[::-1][:top_k * 2]  # 多取一些再篩選

        results = []
        for idx in top_indices:
            sim = float(similarities[idx])
            if sim < min_similarity:
                continue

            proc_id = self._idx_to_id.get(idx)
            if proc_id is None:
                continue

            proc = self._procedures_by_id.get(proc_id)
            if proc is None or proc.is_archived:
                continue

            if domain is not None and proc.domain != domain:
                continue

            results.append((proc, sim))
            if len(results) >= top_k:
                break

        return results

    def list_procedures(
        self,
        domain: str | None = None,
        include_archived: bool = False,
        sort_by: str = "last_used",
        limit: int = 20,
    ) -> list[Procedure]:
        """
        列出程序。

        Args:
            domain: 篩選領域
            include_archived: 是否包含已封存的
            sort_by: 排序欄位（last_used/success_count/version/created_at）
            limit: 返回數量上限
        """
        procs = list(self._procedures_by_id.values())

        if not include_archived:
            procs = [p for p in procs if not p.is_archived]

        if domain is not None:
            procs = [p for p in procs if p.domain == domain]

        sort_keys = {
            "success_count": lambda p: p.success_count,
            "version": lambda p: p.version,
            "created_at": lambda p: p.created_at,
            "last_used": lambda p: p.last_used,
        }
        key_fn = sort_keys.get(sort_by, sort_keys["last_used"])
        procs.sort(key=key_fn, reverse=True)

        return procs[:limit]

    def record_success(self, procedure_id: str) -> bool:
        """記錄執行成功"""
        proc = self._procedures_by_id.get(procedure_id)
        if proc is None:
            return False
        proc.success_count += 1
        proc.last_used = datetime.now(timezone.utc)
        self._save()
        return True

    def record_failure(self, procedure_id: str) -> bool:
        """記錄執行失敗"""
        proc = self._procedures_by_id.get(procedure_id)
        if proc is None:
            return False
        proc.failure_count += 1
        proc.last_used = datetime.now(timezone.utc)
        self._save()
        return True

    def count(self, include_archived: bool = False) -> int:
        if include_archived:
            return len(self._procedures_by_id)
        return sum(1 for p in self._procedures_by_id.values() if not p.is_archived)

    def clear(self):
        """清空所有程序記憶"""
        self._procedures.clear()
        self._procedures_by_id.clear()
        self._embeddings = None
        self._id_to_index.clear()
        self._idx_to_id.clear()
        self._save()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 內部：向量索引
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _update_embedding_index(self, proc: Procedure, is_update: bool) -> None:
        if not proc.embedding:
            return
        new_vec = np.asarray(proc.embedding, dtype=np.float32)

        if is_update and proc.id in self._id_to_index:
            idx = self._id_to_index[proc.id]
            self._embeddings[idx] = new_vec
        else:
            if self._embeddings is None:
                self._embeddings = new_vec.reshape(1, -1)
                self._id_to_index[proc.id] = 0
                self._idx_to_id[0] = proc.id
            else:
                idx = len(self._embeddings)
                self._embeddings = np.vstack([self._embeddings, new_vec])
                self._id_to_index[proc.id] = idx
                self._idx_to_id[idx] = proc.id

    def _rebuild_embeddings(self) -> None:
        if not self._procedures_by_id:
            self._embeddings = None
            self._id_to_index = {}
            self._idx_to_id = {}
            return

        vectors = []
        new_id_to_index: dict[str, int] = {}
        new_idx_to_id: dict[int, str] = {}

        for proc in self._procedures_by_id.values():
            if proc.embedding:
                idx = len(vectors)
                new_id_to_index[proc.id] = idx
                new_idx_to_id[idx] = proc.id
                vectors.append(np.asarray(proc.embedding, dtype=np.float32))

        if vectors:
            self._embeddings = np.vstack(vectors).astype(np.float32)
        else:
            self._embeddings = None
        self._id_to_index = new_id_to_index
        self._idx_to_id = new_idx_to_id

    @staticmethod
    def _normalize_vector(vector: list[float] | np.ndarray) -> np.ndarray:
        vec = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        if norm == 0.0:
            return np.zeros_like(vec, dtype=np.float32)
        return vec / norm

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 持久化
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _load(self) -> None:
        if self.metadata_file.exists():
            try:
                with open(self.metadata_file, encoding="utf-8") as f:
                    data = json.load(f)
                for p_dict in data:
                    proc = Procedure.from_dict(p_dict)
                    if not proc.is_archived:
                        self._procedures[proc.name] = proc
                    self._procedures_by_id[proc.id] = proc
            except Exception as e:
                logger.error(f"[ProceduralMemory] 載入 procedures.json 失敗: {e}")

        if self.embeddings_file.exists():
            try:
                loaded = np.load(self.embeddings_file)
                if loaded.ndim == 2:
                    self._embeddings = loaded.astype(np.float32)
                else:
                    self._embeddings = None
            except Exception as e:
                logger.error(f"[ProceduralMemory] 載入 embeddings 失敗: {e}")
                self._embeddings = None

        # 重建索引
        if self._embeddings is not None:
            for i, proc in enumerate(self._procedures_by_id.values()):
                if i < len(self._embeddings):
                    self._id_to_index[proc.id] = i
                    self._idx_to_id[i] = proc.id
                else:
                    break

            if len(self._procedures_by_id) != len(self._embeddings):
                self._rebuild_embeddings()

    def _save(self) -> None:
        # JSON（原子寫入）
        tmp_json = self.metadata_file.with_suffix(".json.tmp")
        try:
            data = [p.to_dict() for p in self._procedures_by_id.values()]
            with open(tmp_json, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp_json.replace(self.metadata_file)
        except Exception as e:
            logger.error(f"[ProceduralMemory] 保存 procedures.json 失敗: {e}")
            if tmp_json.exists():
                tmp_json.unlink()

        # NPY（原子寫入）
        tmp_npy = self.storage_path / "procedure_embeddings_tmp.npy"
        try:
            if self._embeddings is not None:
                np.save(tmp_npy, self._embeddings)
                tmp_npy.replace(self.embeddings_file)
            elif self.embeddings_file.exists():
                self.embeddings_file.unlink()
        except Exception as e:
            logger.error(f"[ProceduralMemory] 保存 embeddings 失敗: {e}")
            if tmp_npy.exists():
                tmp_npy.unlink()

    def get_stats(self) -> dict[str, Any]:
        active = [p for p in self._procedures_by_id.values() if not p.is_archived]
        return {
            "total_procedures": len(self._procedures_by_id),
            "active_procedures": len(active),
            "archived_procedures": len(self._procedures_by_id) - len(active),
            "avg_success_rate": (
                sum(p.success_rate for p in active) / len(active)
                if active else 0.0
            ),
            "domain_distribution": self._count_domains(active),
        }

    @staticmethod
    def _count_domains(procs: list[Procedure]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for p in procs:
            counts[p.domain] = counts.get(p.domain, 0) + 1
        return counts
