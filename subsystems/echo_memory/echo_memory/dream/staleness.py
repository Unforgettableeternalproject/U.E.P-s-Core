"""
Dream 過期檢查工具。

dream 的 LLM 階段不持有 storage_lock，期間 episode 可能被刪除或被其他路徑
修改（salience、is_dreamed）。鎖外計算完成、準備寫回前，在鎖內以快照比對，
已不存在或已變動的 episode 一律丟棄並記錄，不寫回過期結果。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager, nullcontext
from typing import Any

logger = logging.getLogger("echo_memory.dream")

# 鎖 guard：呼叫後回傳 context manager（通常是 storage_lock），None 代表不加鎖
Guard = Callable[[], AbstractContextManager[Any]]


def resolve_guard(guard: Guard | None) -> Guard:
    """未提供 guard 時退回 nullcontext（獨立使用 / 單元測試）。"""
    return guard if guard is not None else nullcontext


def snapshot_episodes(episodes: Iterable[Any]) -> dict[str, tuple[float, bool]]:
    """記錄 episode 的版本指紋（salience、is_dreamed）。須在鎖內呼叫。"""
    return {
        ep.id: (float(ep.salience_score), bool(ep.is_dreamed))
        for ep in episodes
    }


def find_stale_episode_ids(
    episodic: Any,
    snapshot: dict[str, tuple[float, bool]],
    label: str = "dream",
) -> set[str]:
    """
    比對快照與目前狀態，回傳已過期的 episode id。須在鎖內呼叫。

    過期條件：episode 已不存在，或 salience / is_dreamed 與快照不同。
    """
    stale: set[str] = set()
    for ep_id, fingerprint in snapshot.items():
        current = episodic.get(ep_id)
        if current is None:
            stale.add(ep_id)
            logger.info("[%s] episode %s 已不存在，丟棄相關結果", label, ep_id[:8])
            continue
        now = (float(current.salience_score), bool(current.is_dreamed))
        if now != fingerprint:
            stale.add(ep_id)
            logger.info("[%s] episode %s 已被修改，丟棄相關結果", label, ep_id[:8])
    return stale
