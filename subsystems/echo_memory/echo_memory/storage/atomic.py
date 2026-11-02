"""
原子寫入與檔案替換 helper

供各 store 共用：
  - replace_with_retry：os.replace 遇 Windows 暫時性 PermissionError
    （防毒 / 索引器短暫佔用）時短退避重試，持續失敗才拋出。
  - atomic_write：寫入唯一命名的同目錄暫存檔 → flush/fsync → 原子替換，
    失敗時清理暫存檔並將例外往外拋（不吞錯）。

暫存檔名沿用 locking.temporary_sibling（含 pid + uuid），並行寫入不會撞名。
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from echo_memory.storage.locking import temporary_sibling

logger = logging.getLogger("echo_memory.atomic")

# 退避參數：總等待上限約 0.05+0.1+0.2+0.4+0.8 = 1.55 秒
REPLACE_RETRIES = 6
REPLACE_BASE_DELAY = 0.05


def replace_with_retry(
    src: str | os.PathLike,
    dst: str | os.PathLike,
    *,
    retries: int = REPLACE_RETRIES,
    base_delay: float = REPLACE_BASE_DELAY,
) -> None:
    """os.replace 加上 PermissionError 短退避重試；用盡次數後拋出最後一次的例外。"""
    attempt = 0
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            attempt += 1
            if attempt >= retries:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            logger.debug(
                "[atomic] os.replace 被拒絕（第 %d 次），%.2fs 後重試: %s", attempt, delay, exc
            )
            time.sleep(delay)


def atomic_write(
    path: str | os.PathLike,
    writer: Callable[[IO[Any]], None],
    *,
    binary: bool = False,
    encoding: str = "utf-8",
) -> None:
    """
    原子寫入 path：writer(f) 寫入唯一暫存檔，fsync 後 replace 到目標。

    失敗（含 replace 持續被拒）時清理暫存檔並重新拋出例外。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = temporary_sibling(target)
    try:
        if binary:
            with open(tmp, "wb") as f:
                writer(f)
                f.flush()
                os.fsync(f.fileno())
        else:
            with open(tmp, "w", encoding=encoding) as f:
                writer(f)
                f.flush()
                os.fsync(f.fileno())
        replace_with_retry(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_json(path: str | os.PathLike, data: Any, *, indent: int | None = 2) -> None:
    """原子寫入 JSON（ensure_ascii=False 保留中文）。"""
    atomic_write(path, lambda f: json.dump(data, f, ensure_ascii=False, indent=indent))
