"""tools.lock.json 的產生入口（§5.5）：``python -m uep.tools.lockfile`` 依目前的註冊表目錄重寫 lockfile。

改 ``input_schema`` 或分類欄時：先升該條目的 ``tool_version``，再執行本指令更新 lockfile；
lockfile 入版控，變更在 PR diff 上可見。CI 測試（test_tool_registry.py）比對 lockfile 與目錄是否同步。
"""
from __future__ import annotations

import json
import logging

from uep.tools.catalog.memory import memory_entries
from uep.tools.catalog.testkit import testkit_entries
from uep.tools.registry import LOCKFILE_PATH, ToolRegistry


def current_registry() -> ToolRegistry:
    """目前要進 lockfile 的全部 builtin 條目（之後的目錄模組在這裡加；測試工具組無條件在內，與執行期開關無關）。"""
    return ToolRegistry.build([*memory_entries(), *testkit_entries()])


def render_lockfile(registry: ToolRegistry) -> str:
    return json.dumps(registry.lock_snapshot(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


logger = logging.getLogger("uep.tools.lockfile")


if __name__ == "__main__":
    LOCKFILE_PATH.write_text(render_lockfile(current_registry()), encoding="utf-8", newline="\n")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger.info("已寫入 %s", LOCKFILE_PATH)
