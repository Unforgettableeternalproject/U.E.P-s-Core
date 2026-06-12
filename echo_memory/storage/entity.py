"""
EntityStore - 實體持久化存儲

以 JSON 檔案作為後端，無需向量索引。
使用 temp file + rename 原子寫入，與 SemanticMemory 相同模式。

存儲檔案：{storage_path}/entities.json
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from echo_memory.schemas.entity import Entity

logger = logging.getLogger("echo_memory.entity_store")


class EntityStore:
    """
    實體持久化存儲管理器。

    以 JSON 作為存儲後端，不維護向量索引（實體查詢以精確名稱為主）。
    所有寫入操作使用 temp file + rename 原子替換模式，防止資料損毀。
    """

    def __init__(self, storage_path: str | Path):
        """
        初始化實體存儲。

        Args:
            storage_path: 存儲目錄（若不存在會自動建立）
        """
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)

        self.entities_file = self.storage_path / "entities.json"

        # 記憶體快取：id -> Entity, name（小寫）-> Entity
        self._by_id: dict[str, Entity] = {}
        self._by_name: dict[str, Entity] = {}  # name.lower() -> Entity

        self._load()

        logger.info(
            f"[EntityStore] 初始化完成 — 已載入 {len(self._by_id)} 個實體"
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 寫入 API
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def upsert(self, entity: Entity) -> str:
        """
        插入或更新實體。

        若同名實體已存在（name 忽略大小寫比對），則更新其 description、
        entity_type、last_seen 及 metadata；否則新增。

        Args:
            entity: Entity 物件

        Returns:
            entity_id（新建或現有的 ID）
        """
        key = entity.name.lower()
        existing = self._by_name.get(key)

        if existing is not None:
            # 更新現有實體
            existing.description = entity.description or existing.description
            existing.entity_type = entity.entity_type
            existing.last_seen = datetime.now(timezone.utc)
            existing.metadata.update(entity.metadata)
            if entity.metadata:
                existing.metadata.update(entity.metadata)
            result_id = existing.id
            logger.debug(f"[EntityStore] 更新實體: {existing.name} (id={result_id})")
        else:
            # 新增實體
            self._by_id[entity.id] = entity
            self._by_name[key] = entity
            result_id = entity.id
            logger.debug(f"[EntityStore] 新增實體: {entity.name} (id={result_id})")

        self._save()
        return result_id

    def increment_mention(self, entity_id: str, episode_id: str) -> None:
        """
        遞增實體的提及次數，並記錄來源 Episode。

        Args:
            entity_id: 實體 ID
            episode_id: 提及此實體的 Episode ID
        """
        entity = self._by_id.get(entity_id)
        if entity is None:
            logger.warning(f"[EntityStore] 找不到實體: {entity_id}")
            return

        entity.mention_count += 1
        if episode_id not in entity.source_episode_ids:
            entity.source_episode_ids.append(episode_id)
        entity.last_seen = datetime.now(timezone.utc)

        self._save()
        logger.debug(
            f"[EntityStore] 遞增提及: {entity.name} "
            f"(count={entity.mention_count}, episode={episode_id[:8]}...)"
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 查詢 API
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def get(self, entity_id: str) -> Entity | None:
        """根據 ID 取得實體。"""
        return self._by_id.get(entity_id)

    def get_by_name(self, name: str) -> Entity | None:
        """根據名稱取得實體（忽略大小寫）。"""
        return self._by_name.get(name.lower())

    def list_entities(
        self,
        entity_type: str | None = None,
        limit: int = 50,
    ) -> list[Entity]:
        """
        列出實體，支援類型篩選。

        Args:
            entity_type: 若提供，只返回該類型（person/place/concept/product/event/other）
            limit: 最多返回筆數（按 mention_count 降序）

        Returns:
            Entity 列表
        """
        entities = list(self._by_id.values())

        if entity_type is not None:
            entities = [e for e in entities if e.entity_type == entity_type]

        # 按提及次數降序排列
        entities.sort(key=lambda e: e.mention_count, reverse=True)
        return entities[:limit]

    def count(self) -> int:
        """返回實體總數。"""
        return len(self._by_id)

    def clear(self) -> None:
        """清空所有實體（測試用）。"""
        self._by_id.clear()
        self._by_name.clear()
        if self.entities_file.exists():
            self.entities_file.unlink()
        logger.info("[EntityStore] 已清空所有實體")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 持久化
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _load(self) -> None:
        """從磁盤載入實體資料。"""
        if not self.entities_file.exists():
            return

        try:
            with open(self.entities_file, "r", encoding="utf-8") as f:
                data: list[dict[str, Any]] = json.load(f)

            for item in data:
                entity = Entity.from_dict(item)
                self._by_id[entity.id] = entity
                self._by_name[entity.name.lower()] = entity

            logger.debug(f"[EntityStore] 從磁盤載入 {len(self._by_id)} 個實體")
        except Exception as e:
            logger.error(f"[EntityStore] 載入 entities.json 失敗: {e}")

    def _save(self) -> None:
        """
        保存實體資料到磁盤。

        使用 temp file + rename 原子替換模式，避免寫入中斷導致資料損毀。
        """
        tmp_file = self.entities_file.with_suffix(".json.tmp")
        try:
            data = [e.to_dict() for e in self._by_id.values()]
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp_file.replace(self.entities_file)
            logger.debug(f"[EntityStore] 保存 {len(data)} 個實體到磁盤")
        except Exception as e:
            logger.error(f"[EntityStore] 保存 entities.json 失敗: {e}")
            if tmp_file.exists():
                tmp_file.unlink()
