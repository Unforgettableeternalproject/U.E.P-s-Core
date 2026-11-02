"""會話持久化 adapter：接 SessionControl 的 SessionStore。

## 為什麼用 SessionControl 的 store 而不是自己寫

`echo_thought_core.session_store.SessionStore` 本來就是 U.E.P 的
session 持久化層（JSON 檔 + index，支援 save / load / list / delete）。
Echo Stream 是整合層，在這裡再造一個檔案格式只會讓兩邊的 session
永遠不能互通——之後 ContextManager 的四層壓縮接上來（Phase 4）時，
壓縮後的訊息也要走同一個 store。

## 這一層的範圍

只做**手動控制**：列出、建立、切換、刪除。
「每輪對話用相關性比對自動擷取對應歷史」是之後的方向（艾斯維爾
2026-08-21 定調），到時候改的是選 session 的邏輯，store 介面不動。

**這是唯一 import echo_thought_core.session_store 的地方**，
與 :func:`~uep.stream.adapters.think_llm.build_backend` 的慣例一致。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import config


def build_store(data_dir: str | Path | None = None) -> Any:
    """建一個 SessionControl 的 SessionStore。

    ``data_dir`` 預設是 Echo Stream 專案根目錄的 ``data/``——
    store 自己會加上 ``sessions/`` 子目錄，與 SessionControl 的
    目錄慣例一致。
    """
    config.ensure_subsystem("SESSION", "echo_thought_core")

    from echo_thought_core.session_store import SessionStore

    if data_dir is None:
        data_dir = Path(__file__).parents[2] / "data"
    return SessionStore(data_dir=data_dir)


__all__ = ["build_store"]
