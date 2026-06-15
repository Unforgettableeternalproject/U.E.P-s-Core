"""
ReflectionActionQueue — 受控反思行動佇列

反思模組產生的行動不應直接修改記憶狀態，而是進入
有權限、稽核與 rollback 的行動佇列。

行動分三類：
  - AUTO：安全行動，自動執行（如標記概念、調整 salience）
  - CONFIRM：需要宿主確認才執行（如合併概念、歸檔 episode）
  - DENY：禁止執行（如刪除記憶、修改身份）

每個行動保存完整的稽核日誌：
  - 誰發起（reflection / dream / manual）
  - 對誰操作（episode_id / concept_id）
  - 操作前狀態快照
  - 操作結果
  - rollback 資訊
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger("echo_memory.affect.action_queue")


class ActionPermission(Enum):
    """行動權限等級。"""
    AUTO = "auto"          # 自動執行
    CONFIRM = "confirm"    # 需要確認
    DENY = "deny"          # 禁止


class ActionStatus(Enum):
    """行動狀態。"""
    PENDING = "pending"        # 等待執行/確認
    EXECUTED = "executed"      # 已執行
    CONFIRMED = "confirmed"    # 已確認（等待執行）
    REJECTED = "rejected"      # 被拒絕
    ROLLED_BACK = "rolled_back"  # 已回滾
    FAILED = "failed"          # 執行失敗


@dataclass
class ReflectionAction:
    """一個反思行動。"""
    id: str                              # 唯一 ID
    action_type: str                     # 行動類型（如 adjust_salience, merge_concepts 等）
    source: str = "reflection"           # 發起者
    target_id: str = ""                  # 操作目標
    params: dict[str, Any] = field(default_factory=dict)
    reasoning: str = ""                  # 行動理由
    permission: ActionPermission = ActionPermission.AUTO
    status: ActionStatus = ActionStatus.PENDING
    pre_state: dict[str, Any] = field(default_factory=dict)    # 操作前快照
    post_state: dict[str, Any] = field(default_factory=dict)   # 操作後快照
    error: str = ""                      # 錯誤訊息
    created_at: float = field(default_factory=time.time)
    executed_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action_type": self.action_type,
            "source": self.source,
            "target_id": self.target_id,
            "params": self.params,
            "reasoning": self.reasoning,
            "permission": self.permission.value,
            "status": self.status.value,
            "pre_state": self.pre_state,
            "post_state": self.post_state,
            "error": self.error,
            "created_at": self.created_at,
            "executed_at": self.executed_at,
        }


# 行動類型 → 權限映射
_ACTION_PERMISSIONS: dict[str, ActionPermission] = {
    # AUTO：安全行動
    "adjust_salience": ActionPermission.AUTO,
    "tag_concept": ActionPermission.AUTO,
    "add_note": ActionPermission.AUTO,
    # CONFIRM：需要確認
    "merge_concepts": ActionPermission.CONFIRM,
    "archive_episode": ActionPermission.CONFIRM,
    "create_concept": ActionPermission.CONFIRM,
    # DENY：禁止
    "delete_episode": ActionPermission.DENY,
    "delete_concept": ActionPermission.DENY,
    "modify_identity": ActionPermission.DENY,
}


class ReflectionActionQueue:
    """
    受控的反思行動佇列。

    接收 ReflectionModule 產生的行動，根據權限等級決定
    自動執行、等待確認、或拒絕。所有操作保存完整稽核日誌。
    """

    def __init__(
        self,
        audit_path: Path | None = None,
        max_pending: int = 20,
    ):
        """
        Args:
            audit_path: 稽核日誌路徑。為 None 時不持久化。
            max_pending: 最大待處理佇列長度。
        """
        self._pending: deque[ReflectionAction] = deque(maxlen=max_pending)
        self._history: list[ReflectionAction] = []
        self._audit_path = audit_path
        self._action_counter = 0

    def enqueue(
        self,
        action_type: str,
        target_id: str = "",
        params: dict[str, Any] | None = None,
        reasoning: str = "",
        source: str = "reflection",
    ) -> ReflectionAction:
        """
        將行動加入佇列。

        根據 action_type 自動分配權限等級。

        Args:
            action_type: 行動類型
            target_id: 操作目標 ID
            params: 行動參數
            reasoning: 行動理由
            source: 發起者

        Returns:
            建立的 ReflectionAction
        """
        self._action_counter += 1
        permission = _ACTION_PERMISSIONS.get(
            action_type, ActionPermission.CONFIRM
        )

        action = ReflectionAction(
            id=f"act_{self._action_counter:04d}_{int(time.time())}",
            action_type=action_type,
            source=source,
            target_id=target_id,
            params=params or {},
            reasoning=reasoning,
            permission=permission,
            status=(
                ActionStatus.REJECTED
                if permission == ActionPermission.DENY
                else ActionStatus.PENDING
            ),
        )

        if permission == ActionPermission.DENY:
            logger.info(
                "[ActionQueue] 行動被拒絕（權限禁止）: %s on %s",
                action_type,
                target_id,
            )
            self._history.append(action)
            self._audit(action)
            return action

        self._pending.append(action)
        logger.debug(
            "[ActionQueue] 行動入隊: %s on %s (permission=%s)",
            action_type,
            target_id,
            permission.value,
        )
        return action

    def get_pending(self) -> list[ReflectionAction]:
        """取得所有待處理行動。"""
        return list(self._pending)

    def get_auto_actions(self) -> list[ReflectionAction]:
        """取得所有可自動執行的待處理行動。"""
        return [
            a for a in self._pending
            if a.permission == ActionPermission.AUTO
            and a.status == ActionStatus.PENDING
        ]

    def get_confirm_actions(self) -> list[ReflectionAction]:
        """取得所有需要確認的待處理行動。"""
        return [
            a for a in self._pending
            if a.permission == ActionPermission.CONFIRM
            and a.status == ActionStatus.PENDING
        ]

    def confirm(self, action_id: str) -> bool:
        """確認一個待處理行動。"""
        for a in self._pending:
            if a.id == action_id and a.status == ActionStatus.PENDING:
                a.status = ActionStatus.CONFIRMED
                logger.info("[ActionQueue] 行動已確認: %s", action_id)
                return True
        return False

    def reject(self, action_id: str) -> bool:
        """拒絕一個待處理行動。"""
        for a in self._pending:
            if a.id == action_id and a.status == ActionStatus.PENDING:
                a.status = ActionStatus.REJECTED
                self._history.append(a)
                self._pending.remove(a)
                self._audit(a)
                logger.info("[ActionQueue] 行動被拒絕: %s", action_id)
                return True
        return False

    def execute(
        self,
        action: ReflectionAction,
        executor_fn: Any = None,
    ) -> bool:
        """
        執行一個行動。

        只有 AUTO（PENDING）和 CONFIRM（CONFIRMED）的行動可以執行。

        Args:
            action: 要執行的行動
            executor_fn: 執行函式，接收 (action_type, target_id, params) → dict
                         回傳 dict 作為 post_state。
                         為 None 時行動標記為 executed 但不實際操作。

        Returns:
            是否成功執行
        """
        # 權限檢查
        if action.permission == ActionPermission.DENY:
            logger.warning("[ActionQueue] 嘗試執行被禁止的行動: %s", action.id)
            return False

        if action.permission == ActionPermission.CONFIRM and action.status != ActionStatus.CONFIRMED:
            logger.warning(
                "[ActionQueue] CONFIRM 行動未經確認: %s (status=%s)",
                action.id,
                action.status.value,
            )
            return False

        if action.status not in (ActionStatus.PENDING, ActionStatus.CONFIRMED):
            return False

        # 執行
        try:
            if executor_fn is not None:
                result = executor_fn(
                    action.action_type,
                    action.target_id,
                    action.params,
                )
                action.post_state = result if isinstance(result, dict) else {}

            action.status = ActionStatus.EXECUTED
            action.executed_at = time.time()
            logger.info(
                "[ActionQueue] 行動已執行: %s %s on %s",
                action.action_type,
                action.id,
                action.target_id,
            )

        except Exception as e:
            action.status = ActionStatus.FAILED
            action.error = str(e)
            logger.error(
                "[ActionQueue] 行動執行失敗: %s — %s",
                action.id,
                e,
            )

        # 從 pending 移到 history
        if action in self._pending:
            self._pending.remove(action)
        self._history.append(action)
        self._audit(action)

        return action.status == ActionStatus.EXECUTED

    def process_auto_actions(
        self,
        executor_fn: Any = None,
    ) -> list[ReflectionAction]:
        """
        批次執行所有 AUTO 行動。

        Returns:
            已執行的行動列表
        """
        executed = []
        for action in self.get_auto_actions():
            if self.execute(action, executor_fn):
                executed.append(action)
        return executed

    def rollback(self, action_id: str) -> bool:
        """
        回滾一個已執行的行動。

        只標記狀態，實際回滾由外部根據 pre_state 執行。

        Args:
            action_id: 行動 ID

        Returns:
            是否成功標記為回滾
        """
        for a in self._history:
            if a.id == action_id and a.status == ActionStatus.EXECUTED:
                a.status = ActionStatus.ROLLED_BACK
                self._audit(a)
                logger.info("[ActionQueue] 行動已回滾: %s", action_id)
                return True
        return False

    def get_history(self) -> list[ReflectionAction]:
        """取得歷史行動記錄。"""
        return list(self._history)

    def _audit(self, action: ReflectionAction) -> None:
        """寫入稽核日誌。"""
        if self._audit_path is None:
            return

        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(action.to_dict(), ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("[ActionQueue] 稽核日誌寫入失敗: %s", e)
