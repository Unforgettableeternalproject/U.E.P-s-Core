"""Inbox：consumer 端去重（契約 §1.2、E-2）。

``process_once`` 在呼叫者的交易內先佔用 (consumer_id, event_id)，再執行效果；
效果拋例外時佔用一併回滾，讓下次重送可重新處理。完成標記與效果同交易提交。
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from uep.events.models import InboxRow

MAX_RESULT_BYTES = 1024
MAX_RESULT_STR = 128


def _check_result(result: dict[str, Any]) -> None:
    """inbox.result 只允許 ID／狀態類小值（str 短字串、數字、布林、None）。"""
    for key, value in result.items():
        if not isinstance(key, str) or len(key) > MAX_RESULT_STR:
            raise ValueError("inbox.result 鍵不合法")
        if not (value is None or isinstance(value, (bool, int, float))
                or (isinstance(value, str) and len(value) <= MAX_RESULT_STR)):
            raise ValueError(f"inbox.result[{key!r}] 只允許短字串或數字")
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_RESULT_BYTES:
        raise ValueError("inbox.result 過大")


def process_once(
    session: Session,
    consumer_id: str,
    event_id: str,
    effect: Callable[[], Any],
) -> bool:
    """首次處理回傳 True（並執行 effect）；已處理過回傳 False（不執行）。

    effect 的回傳值若為 dict 會存入 inbox.result（限小型 ID／狀態值，違規則整筆回滾）。
    """
    savepoint = session.begin_nested()
    try:
        claimed = session.execute(
            pg_insert(InboxRow)
            .values(consumer_id=consumer_id, event_id=event_id)
            .on_conflict_do_nothing(index_elements=["consumer_id", "event_id"])
            .returning(InboxRow.event_id)
        ).one_or_none()
        if claimed is None:
            savepoint.rollback()
            return False
        outcome = effect()
        if isinstance(outcome, dict):
            _check_result(outcome)
            row = session.get(InboxRow, (consumer_id, event_id))
            row.result = outcome
            session.flush()
    except BaseException:
        savepoint.rollback()
        raise
    savepoint.commit()
    return True
