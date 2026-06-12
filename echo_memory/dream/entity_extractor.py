"""
EntityExtractor - 從 Episode 提取實體

主路徑：透過注入的 LLM 函式呼叫語言模型提取具名實體。
降級路徑：無 LLM 時使用簡單正則規則，品質較低但不報錯。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Callable

from echo_memory.schemas.entity import Entity
from echo_memory.schemas.episode import Episode

logger = logging.getLogger("echo_memory.entity_extractor")

# 有效的實體類型集合
_VALID_TYPES = {"person", "place", "concept", "product", "event", "other"}

# LLM 提取 prompt 模板
_EXTRACT_PROMPT = """\
從以下對話中提取所有具體實體（人名、地名、產品名、事件名等）。

使用者: {user_input}
助手: {agent_response}

輸出 JSON 陣列:
[{{"name": "...", "entity_type": "person|place|product|event|other", "description": "一句話描述"}}]
如果沒有具體實體，輸出 []
"""


class EntityExtractor:
    """
    從 Episode 自動提取實體。

    優先使用注入的 LLM 函式（高品質），
    無 LLM 時降級到正則規則（低品質但不報錯）。
    """

    def __init__(self, llm_fn: Callable[[str], str] | None = None):
        """
        初始化提取器。

        Args:
            llm_fn: LLM 呼叫函式 (prompt: str) -> str。
                    若為 None，降級到規則提取。
        """
        self._llm_fn = llm_fn
        mode = "LLM" if llm_fn is not None else "規則（降級）"
        logger.info(f"[EntityExtractor] 初始化完成，提取模式: {mode}")

    def extract(self, episode: Episode) -> list[Entity]:
        """
        從 Episode 提取實體列表。

        Args:
            episode: 要提取實體的 Episode

        Returns:
            Entity 列表（可能為空）
        """
        if self._llm_fn is not None:
            return self._extract_by_llm(episode)
        return self._extract_by_rules(episode.user_input + " " + episode.agent_response)

    def _extract_by_llm(self, episode: Episode) -> list[Entity]:
        """
        使用 LLM 提取實體（主路徑）。

        呼叫 llm_fn 取得 JSON 輸出，解析後建立 Entity 物件。
        若解析失敗，回退到規則提取。

        Args:
            episode: 要提取實體的 Episode

        Returns:
            Entity 列表
        """
        prompt = _EXTRACT_PROMPT.format(
            user_input=episode.user_input,
            agent_response=episode.agent_response,
        )

        try:
            raw_output = self._llm_fn(prompt)
            entities = self._parse_llm_output(raw_output, episode.id)
            logger.debug(
                f"[EntityExtractor] LLM 提取完成 — "
                f"Episode {episode.id[:8]}: {len(entities)} 個實體"
            )
            return entities
        except Exception as e:
            logger.warning(
                f"[EntityExtractor] LLM 提取失敗，回退到規則提取: {e}"
            )
            return self._extract_by_rules(
                episode.user_input + " " + episode.agent_response,
                episode_id=episode.id,
            )

    def _parse_llm_output(
        self,
        raw_output: str,
        episode_id: str,
    ) -> list[Entity]:
        """
        解析 LLM 輸出的 JSON。

        從輸出中提取第一個 JSON 陣列（允許前後有其他文字）。

        Args:
            raw_output: LLM 原始輸出字串
            episode_id: 關聯的 Episode ID（用於初始化 source_episode_ids）

        Returns:
            Entity 列表
        """
        # 嘗試從輸出中提取 JSON 陣列
        match = re.search(r"\[.*?\]", raw_output, re.DOTALL)
        if not match:
            logger.debug("[EntityExtractor] LLM 輸出中未找到 JSON 陣列")
            return []

        try:
            items = json.loads(match.group())
        except json.JSONDecodeError as e:
            logger.warning(f"[EntityExtractor] JSON 解析失敗: {e}")
            return []

        if not isinstance(items, list):
            return []

        now = datetime.now(timezone.utc)
        entities: list[Entity] = []
        seen_names: set[str] = set()

        for item in items:
            if not isinstance(item, dict):
                continue

            name = str(item.get("name", "")).strip()
            if not name or name.lower() in seen_names:
                continue
            seen_names.add(name.lower())

            # 正規化 entity_type
            raw_type = str(item.get("entity_type", "other")).lower()
            entity_type = raw_type if raw_type in _VALID_TYPES else "other"

            description = str(item.get("description", "")).strip()

            entity = Entity(
                name=name,
                entity_type=entity_type,
                description=description,
                mention_count=1,
                source_episode_ids=[episode_id],
                first_seen=now,
                last_seen=now,
            )
            entities.append(entity)

        return entities

    def _extract_by_rules(
        self,
        text: str,
        episode_id: str = "",
    ) -> list[Entity]:
        """
        規則型實體提取（降級路徑）。

        使用簡單正則匹配：
        - 引號內的詞語（"..." 或 '...'）
        - 連續大寫字母開頭的詞（英文）
        - 常見中文品牌/產品名（簡單啟發式）

        品質較低，但在無 LLM 時保證不報錯。

        Args:
            text: 要提取的文字（通常是 user_input + agent_response）
            episode_id: 關聯的 Episode ID

        Returns:
            Entity 列表
        """
        candidates: set[str] = set()

        # 規則 1：引號內的詞語（中英文引號）
        for m in re.finditer(r'["「『]([\w\s]{2,20})["」』]', text):
            candidates.add(m.group(1).strip())

        # 規則 2：連續大寫開頭的英文詞（至少兩詞）
        for m in re.finditer(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b', text):
            candidates.add(m.group(1))

        # 規則 3：全大寫英文縮寫（2-6 字元，非常見詞）
        _common_upper = {"I", "OK", "AI", "IT", "API", "UI", "UX"}
        for m in re.finditer(r'\b([A-Z]{2,6})\b', text):
            word = m.group(1)
            if word not in _common_upper:
                candidates.add(word)

        if not candidates:
            return []

        now = datetime.now(timezone.utc)
        entities: list[Entity] = []
        seen: set[str] = set()

        for name in list(candidates)[:10]:  # 限制最多 10 個，避免噪音過多
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)

            entity = Entity(
                name=name,
                entity_type="other",  # 規則提取無法判斷類型
                description="",
                mention_count=1,
                source_episode_ids=[episode_id] if episode_id else [],
                first_seen=now,
                last_seen=now,
            )
            entities.append(entity)

        logger.debug(
            f"[EntityExtractor] 規則提取完成: {len(entities)} 個候選實體"
        )
        return entities
