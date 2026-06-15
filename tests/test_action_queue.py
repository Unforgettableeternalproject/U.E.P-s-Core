"""
ReflectionActionQueue 測試

驗收標準：
  1. 行動權限分級正確（AUTO/CONFIRM/DENY）
  2. DENY 行動自動拒絕，不入隊
  3. AUTO 行動可直接執行
  4. CONFIRM 行動未確認前不能執行
  5. 執行失敗保留錯誤訊息
  6. rollback 標記正確
  7. 稽核日誌寫入
  8. process_auto_actions 批次執行
"""

from __future__ import annotations

import json

from echo_memory.affect.action_queue import (
    ActionPermission,
    ActionStatus,
    ReflectionActionQueue,
)


class TestActionPermissions:
    """行動權限分級測試。"""

    def test_auto_actions_pending(self):
        """AUTO 行動入隊後狀態為 PENDING。"""
        q = ReflectionActionQueue()
        a = q.enqueue("adjust_salience", target_id="ep_001", reasoning="太低")
        assert a.permission == ActionPermission.AUTO
        assert a.status == ActionStatus.PENDING

    def test_confirm_actions_pending(self):
        """CONFIRM 行動入隊後狀態為 PENDING。"""
        q = ReflectionActionQueue()
        a = q.enqueue("merge_concepts", target_id="c_001", reasoning="重複")
        assert a.permission == ActionPermission.CONFIRM
        assert a.status == ActionStatus.PENDING

    def test_deny_actions_rejected(self):
        """DENY 行動直接被拒絕，不入隊。"""
        q = ReflectionActionQueue()
        a = q.enqueue("delete_episode", target_id="ep_001")
        assert a.permission == ActionPermission.DENY
        assert a.status == ActionStatus.REJECTED
        assert len(q.get_pending()) == 0
        assert len(q.get_history()) == 1

    def test_unknown_type_defaults_confirm(self):
        """未知行動類型預設為 CONFIRM。"""
        q = ReflectionActionQueue()
        a = q.enqueue("unknown_action", target_id="x")
        assert a.permission == ActionPermission.CONFIRM


class TestAutoExecution:
    """AUTO 行動執行測試。"""

    def test_execute_auto_action(self):
        """AUTO 行動可直接執行。"""
        q = ReflectionActionQueue()
        a = q.enqueue("adjust_salience", target_id="ep_001", params={"new_salience": 0.8})

        executed = []
        def mock_executor(action_type, target_id, params):
            executed.append((action_type, target_id, params))
            return {"old_salience": 0.5, "new_salience": 0.8}

        assert q.execute(a, mock_executor)
        assert a.status == ActionStatus.EXECUTED
        assert a.post_state["new_salience"] == 0.8
        assert len(executed) == 1
        assert len(q.get_pending()) == 0

    def test_process_auto_actions_batch(self):
        """process_auto_actions 批次執行所有 AUTO 行動。"""
        q = ReflectionActionQueue()
        q.enqueue("adjust_salience", target_id="ep_001")
        q.enqueue("tag_concept", target_id="c_001")
        q.enqueue("merge_concepts", target_id="c_002")  # CONFIRM，不會被自動執行

        results = q.process_auto_actions(lambda *_: {})
        assert len(results) == 2  # 只有 2 個 AUTO 被執行
        assert len(q.get_pending()) == 1  # CONFIRM 仍在佇列

    def test_execute_no_executor_fn_rejected(self):
        """executor_fn 為 None 時，拒絕執行。"""
        q = ReflectionActionQueue()
        a = q.enqueue("add_note", target_id="n_001")
        assert not q.execute(a, executor_fn=None)
        assert a.status == ActionStatus.PENDING  # 仍在佇列中


class TestConfirmExecution:
    """CONFIRM 行動確認/拒絕測試。"""

    def test_confirm_then_execute(self):
        """CONFIRM 行動確認後可執行。"""
        q = ReflectionActionQueue()
        a = q.enqueue("merge_concepts", target_id="c_001")

        # 未確認前不能執行
        assert not q.execute(a, lambda *_: {})
        assert a.status == ActionStatus.PENDING

        # 確認
        assert q.confirm(a.id)
        assert a.status == ActionStatus.CONFIRMED

        # 確認後可執行
        assert q.execute(a, lambda *_: {"merged": True})
        assert a.status == ActionStatus.EXECUTED

    def test_reject_confirm_action(self):
        """拒絕 CONFIRM 行動。"""
        q = ReflectionActionQueue()
        a = q.enqueue("archive_episode", target_id="ep_001")
        assert q.reject(a.id)
        assert a.status == ActionStatus.REJECTED
        assert len(q.get_pending()) == 0


class TestFailureAndRollback:
    """失敗處理與回滾測試。"""

    def test_execute_failure_preserves_error(self):
        """執行失敗時保留錯誤訊息。"""
        q = ReflectionActionQueue()
        a = q.enqueue("adjust_salience", target_id="ep_001")

        def failing_executor(action_type, target_id, params):
            raise RuntimeError("episode 不存在")

        assert not q.execute(a, failing_executor)
        assert a.status == ActionStatus.FAILED
        assert "episode 不存在" in a.error

    def test_rollback_executed_action(self):
        """已執行的行動可以回滾。"""
        q = ReflectionActionQueue()
        a = q.enqueue("adjust_salience", target_id="ep_001")
        q.execute(a, lambda *_: {})
        assert a.status == ActionStatus.EXECUTED

        assert q.rollback(a.id)
        assert a.status == ActionStatus.ROLLED_BACK

    def test_rollback_non_executed_fails(self):
        """未執行的行動不能回滾。"""
        q = ReflectionActionQueue()
        a = q.enqueue("adjust_salience", target_id="ep_001")
        assert not q.rollback(a.id)  # 還在 PENDING，不能回滾


class TestAuditLog:
    """稽核日誌測試。"""

    def test_audit_log_written(self, tmp_path):
        """稽核日誌在 enqueue + execute 後各寫入一筆。"""
        audit_file = tmp_path / "audit.jsonl"
        q = ReflectionActionQueue(audit_path=audit_file)

        a = q.enqueue("adjust_salience", target_id="ep_001", reasoning="測試稽核")
        q.execute(a, lambda *_: {"result": "ok"})

        assert audit_file.exists()
        lines = audit_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2  # enqueue (pending) + execute (executed)

        # 第一筆是 enqueue 時的 pending 記錄
        record0 = json.loads(lines[0])
        assert record0["status"] == "pending"
        # 第二筆是 execute 後的 executed 記錄
        record1 = json.loads(lines[1])
        assert record1["action_type"] == "adjust_salience"
        assert record1["status"] == "executed"
        assert record1["target_id"] == "ep_001"

    def test_deny_action_audited(self, tmp_path):
        """DENY 行動也要寫入稽核日誌。"""
        audit_file = tmp_path / "audit.jsonl"
        q = ReflectionActionQueue(audit_path=audit_file)

        q.enqueue("delete_episode", target_id="ep_001")

        lines = audit_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["status"] == "rejected"


class TestQueueLimits:
    """佇列容量測試。"""

    def test_max_pending_enforced(self):
        """超過 max_pending 時，舊的行動被擠出。"""
        q = ReflectionActionQueue(max_pending=3)
        ids = []
        for i in range(5):
            a = q.enqueue("adjust_salience", target_id=f"ep_{i:03d}")
            ids.append(a.id)

        pending = q.get_pending()
        assert len(pending) == 3
        # 最舊的兩個被擠出
        pending_ids = [a.id for a in pending]
        assert ids[0] not in pending_ids
        assert ids[1] not in pending_ids
