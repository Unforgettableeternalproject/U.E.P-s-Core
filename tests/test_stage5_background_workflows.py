"""
階段五：背景工作流整合測試套件

測試場景：
1. 提交背景任務 - 驗證 task_id 返回
2. 查詢任務狀態 - 驗證進度更新
3. 取消運行中任務 - 驗證狀態變更為 CANCELLED
4. 並行執行 - 驗證多任務同時運行
5. 事件發布 - 驗證 Controller 接收事件
6. 任務清理 - 驗證舊任務移除
7. 整合測試 - 背景執行時主循環繼續
8. 持久化測試 - 驗證任務儲存/載入
9. 通知測試 - 驗證 TTS 通知發送
"""

import os
import sys
import time
import unittest
import tempfile
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path

# 添加項目根目錄到 Python 路徑
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from modules.ui_module.debug.background_worker import (
    BackgroundWorkerManager, 
    BackgroundWorkflowTask, 
    TaskStatus
)
from core.controller import UnifiedController
from core.event_bus import EventBus, SystemEvent
from core.sessions.session_manager import session_manager, SessionStatus
from modules.sys_module.workflows import (
    WorkflowDefinition, 
    WorkflowStep, 
    WorkflowMode, 
    StepResult,
    WorkflowEngine
)


class SimpleTestStep(WorkflowStep):
    """簡單測試步驟"""
    
    def __init__(self, session, step_id="test_step", delay=0.1):
        super().__init__(session)
        self.step_id = step_id
        self.delay = delay
        self.set_step_type(self.STEP_TYPE_INTERACTIVE)
    
    def get_prompt(self) -> str:
        return f"請輸入任何內容以完成步驟 {self.step_id}"
    
    def execute(self, user_input=None) -> StepResult:
        """執行步驟（帶延遲模擬耗時操作）"""
        time.sleep(self.delay)
        return StepResult.success(f"步驟 {self.step_id} 完成", data={"result": f"processed_{user_input}"})


def create_simple_background_workflow(session, step_count=3, step_delay=0.1):
    """
    創建簡單的背景工作流程（用於測試）
    
    Args:
        session: 工作流程會話
        step_count: 步驟數量
        step_delay: 每步驟延遲時間（秒）
    
    Returns:
        WorkflowEngine 實例
    """
    # 創建工作流程定義（背景模式）
    workflow_def = WorkflowDefinition(
        workflow_type="test_background",
        name="測試背景工作流程",
        description="用於測試背景執行的簡單工作流程",
        workflow_mode=WorkflowMode.BACKGROUND,
        requires_llm_review=False
    )
    
    # 添加步驟
    steps = []
    for i in range(step_count):
        step = SimpleTestStep(session, step_id=f"step_{i+1}", delay=step_delay)
        step.set_id(f"step_{i+1}")  # 使用 set_id() 方法而不是直接賦值
        workflow_def.add_step(step)
        steps.append(step)
    
    # 設置轉換
    for i in range(len(steps) - 1):
        workflow_def.add_transition(steps[i].id, steps[i+1].id)
    
    # 最後一步到 END
    workflow_def.add_transition(steps[-1].id, "END")
    
    # 設置入口點
    workflow_def.set_entry_point(steps[0].id)
    
    # 創建引擎
    engine = WorkflowEngine(workflow_def, session)
    
    return engine


class TestBackgroundWorkflowSubmission(unittest.TestCase):
    """測試背景任務提交"""
    
    def setUp(self):
        """測試前設置"""
        self.worker_manager = BackgroundWorkerManager()
    
    def test_submit_workflow_returns_task_id(self):
        """測試：提交背景工作流程返回 task_id"""
        # 創建會話和引擎
        session_id = session_manager.create_session(
            workflow_type="test_background",
            command="test",
            initial_data={}
        )
        session = session_manager.get_workflow_session(session_id)
        engine = create_simple_background_workflow(session, step_count=1, step_delay=0.05)
        
        # 提交背景任務
        task_id = self.worker_manager.submit_workflow(
            workflow_engine=engine,
            workflow_type="test_background",
            session_id=session_id,
            metadata={"test": "data"}
        )
        
        # 驗證：返回有效的 task_id
        self.assertIsNotNone(task_id)
        self.assertIsInstance(task_id, str)
        self.assertTrue(len(task_id) > 0)
        
        # 清理
        session_manager.end_session(session_id)
    
    def test_submit_workflow_creates_task_record(self):
        """測試：提交任務後創建任務記錄"""
        # 創建會話和引擎
        session_id = session_manager.create_session(
            workflow_type="test_background",
            command="test",
            initial_data={}
        )
        session = session_manager.get_workflow_session(session_id)
        engine = create_simple_background_workflow(session, step_count=1, step_delay=0.05)
        
        # 提交背景任務
        task_id = self.worker_manager.submit_workflow(
            workflow_engine=engine,
            workflow_type="test_background",
            session_id=session_id
        )
        
        # 查詢任務狀態
        task = self.worker_manager.get_task_status(task_id)
        
        # 驗證：任務記錄存在且初始狀態正確
        self.assertIsNotNone(task)
        self.assertEqual(task.task_id, task_id)
        self.assertEqual(task.workflow_type, "test_background")
        self.assertEqual(task.session_id, session_id)
        self.assertIn(task.status, [TaskStatus.QUEUED, TaskStatus.RUNNING])
        
        # 清理
        session_manager.end_session(session_id)


class TestBackgroundWorkflowExecution(unittest.TestCase):
    """測試背景工作流程執行"""
    
    def setUp(self):
        """測試前設置"""
        self.worker_manager = BackgroundWorkerManager()
    
    def test_workflow_completes_successfully(self):
        """測試：背景工作流程成功完成"""
        # 創建會話和引擎
        session_id = session_manager.create_session(
            workflow_type="test_background",
            command="test",
            initial_data={}
        )
        session = session_manager.get_workflow_session(session_id)
        engine = create_simple_background_workflow(session, step_count=2, step_delay=0.05)
        
        # 提交背景任務
        task_id = self.worker_manager.submit_workflow(
            workflow_engine=engine,
            workflow_type="test_background",
            session_id=session_id
        )
        
        # 等待任務完成（最多等待 3 秒）
        max_wait = 3.0
        start_time = time.time()
        while time.time() - start_time < max_wait:
            task = self.worker_manager.get_task_status(task_id)
            if task and task.status == TaskStatus.COMPLETED:
                break
            time.sleep(0.1)
        
        # 獲取最終狀態
        task = self.worker_manager.get_task_status(task_id)
        
        # 驗證：任務成功完成
        self.assertIsNotNone(task)
        self.assertEqual(task.status, TaskStatus.COMPLETED)
        self.assertIsNone(task.error_message)
        self.assertIsNotNone(task.end_time)
        
        # 清理
        session_manager.end_session(session_id)
    
    def test_workflow_status_progression(self):
        """測試：工作流程狀態正確轉換 QUEUED -> RUNNING -> COMPLETED"""
        # 創建會話和引擎
        session_id = session_manager.create_session(
            workflow_type="test_background",
            command="test",
            initial_data={}
        )
        session = session_manager.get_workflow_session(session_id)
        engine = create_simple_background_workflow(session, step_count=1, step_delay=0.2)
        
        # 提交背景任務
        task_id = self.worker_manager.submit_workflow(
            workflow_engine=engine,
            workflow_type="test_background",
            session_id=session_id
        )
        
        # 記錄狀態變化
        observed_statuses = set()
        
        # 輪詢狀態 2 秒
        max_wait = 2.0
        start_time = time.time()
        while time.time() - start_time < max_wait:
            task = self.worker_manager.get_task_status(task_id)
            if task:
                observed_statuses.add(task.status)
                if task.status == TaskStatus.COMPLETED:
                    break
            time.sleep(0.05)
        
        # 驗證：至少經過 RUNNING 和 COMPLETED 狀態
        self.assertIn(TaskStatus.RUNNING, observed_statuses)
        self.assertIn(TaskStatus.COMPLETED, observed_statuses)
        
        # 清理
        session_manager.end_session(session_id)


class TestBackgroundWorkflowCancellation(unittest.TestCase):
    """測試背景任務取消"""
    
    def setUp(self):
        """測試前設置"""
        self.worker_manager = BackgroundWorkerManager()
    
    def test_cancel_running_task(self):
        """測試：取消運行中的任務"""
        # 創建會話和引擎（長時間運行）
        session_id = session_manager.create_session(
            workflow_type="test_background",
            command="test",
            initial_data={}
        )
        session = session_manager.get_workflow_session(session_id)
        engine = create_simple_background_workflow(session, step_count=10, step_delay=0.5)
        
        # 提交背景任務
        task_id = self.worker_manager.submit_workflow(
            workflow_engine=engine,
            workflow_type="test_background",
            session_id=session_id
        )
        
        # 等待任務開始運行
        time.sleep(0.3)
        
        # 取消任務
        success = self.worker_manager.cancel_task(task_id)
        
        # 驗證：取消成功
        self.assertTrue(success)
        
        # 等待取消生效
        time.sleep(0.2)
        
        # 獲取任務狀態
        task = self.worker_manager.get_task_status(task_id)
        
        # 驗證：狀態為 CANCELLED
        self.assertIsNotNone(task)
        self.assertEqual(task.status, TaskStatus.CANCELLED)
        
        # 清理
        session_manager.end_session(session_id)


class TestBackgroundWorkflowParallelExecution(unittest.TestCase):
    """測試並行執行多個背景工作流程"""
    
    def setUp(self):
        """測試前設置"""
        self.worker_manager = BackgroundWorkerManager()
    
    def test_parallel_workflow_execution(self):
        """測試：並行執行多個工作流程"""
        task_ids = []
        session_ids = []
        
        # 提交 3 個並行任務
        for i in range(3):
            session_id = session_manager.create_session(
                workflow_type="test_background",
                command=f"test_{i}",
                initial_data={}
            )
            session_ids.append(session_id)
            session = session_manager.get_workflow_session(session_id)
            engine = create_simple_background_workflow(session, step_count=2, step_delay=0.1)
            
            task_id = self.worker_manager.submit_workflow(
                workflow_engine=engine,
                workflow_type="test_background",
                session_id=session_id
            )
            task_ids.append(task_id)
        
        # 等待所有任務完成
        max_wait = 3.0
        start_time = time.time()
        while time.time() - start_time < max_wait:
            all_completed = True
            for task_id in task_ids:
                task = self.worker_manager.get_task_status(task_id)
                if not task or task.status not in [TaskStatus.COMPLETED, TaskStatus.FAILED]:
                    all_completed = False
                    break
            
            if all_completed:
                break
            
            time.sleep(0.1)
        
        # 驗證：所有任務都完成
        for task_id in task_ids:
            task = self.worker_manager.get_task_status(task_id)
            self.assertIsNotNone(task)
            self.assertEqual(task.status, TaskStatus.COMPLETED)
        
        # 清理
        for session_id in session_ids:
            session_manager.end_session(session_id)


class TestBackgroundWorkflowEvents(unittest.TestCase):
    """測試背景工作流程事件發布"""
    
    def setUp(self):
        """測試前設置"""
        self.worker_manager = BackgroundWorkerManager()
        self.event_bus = EventBus()
        self.received_events = []
        
        # 訂閱事件
        def event_handler(event_data):
            self.received_events.append(event_data)
        
        self.event_bus.subscribe(SystemEvent.BACKGROUND_WORKFLOW_COMPLETED, event_handler)
        self.event_bus.subscribe(SystemEvent.BACKGROUND_WORKFLOW_FAILED, event_handler)
    
    def test_completion_event_published(self):
        """測試：工作流程完成時發布事件"""
        # 創建會話和引擎
        session_id = session_manager.create_session(
            workflow_type="test_background",
            command="test",
            initial_data={}
        )
        session = session_manager.get_workflow_session(session_id)
        engine = create_simple_background_workflow(session, step_count=1, step_delay=0.05)
        
        # 提交背景任務
        task_id = self.worker_manager.submit_workflow(
            workflow_engine=engine,
            workflow_type="test_background",
            session_id=session_id
        )
        
        # 等待任務完成
        time.sleep(0.5)
        
        # 驗證：接收到完成事件
        self.assertGreater(len(self.received_events), 0)
        
        # 驗證事件內容
        completion_events = [e for e in self.received_events if e.get('task_id') == task_id]
        self.assertGreater(len(completion_events), 0)
        
        # 清理
        session_manager.end_session(session_id)


class TestControllerBackgroundTaskMonitoring(unittest.TestCase):
    """測試 Controller 背景任務監控"""
    
    def setUp(self):
        """測試前設置"""
        # 創建 Controller 實例（不需要參數）
        self.controller = UnifiedController()
        
    def test_controller_tracks_background_tasks(self):
        """測試：Controller 追蹤背景任務"""
        # 模擬註冊背景任務
        task_id = "test_task_123"
        task_info = {
            "workflow_type": "test_background",
            "session_id": "session_456"
        }
        
        self.controller.register_background_task(task_id, task_info)
        
        # 驗證：任務已註冊
        self.assertIn(task_id, self.controller.background_tasks)
        self.assertEqual(self.controller.background_tasks[task_id]['workflow_type'], "test_background")
        self.assertEqual(self.controller.background_tasks[task_id]['status'], 'running')
    
    def test_controller_get_task_status(self):
        """測試：Controller 查詢任務狀態"""
        # 註冊任務
        task_id = "test_task_789"
        task_info = {
            "workflow_type": "test_background",
            "session_id": "session_123"
        }
        self.controller.register_background_task(task_id, task_info)
        
        # 查詢狀態
        status = self.controller.get_background_task_status(task_id)
        
        # 驗證：返回正確的狀態
        self.assertIsNotNone(status)
        self.assertEqual(status['task_id'], task_id)
        self.assertEqual(status['workflow_type'], "test_background")


class TestBackgroundTaskPersistence(unittest.TestCase):
    """測試背景任務持久化"""
    
    def setUp(self):
        """測試前設置"""
        # 創建臨時文件用於測試
        self.temp_file = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json')
        self.temp_file.close()
        
        # 創建 Controller 實例並設置測試文件路徑
        self.controller = UnifiedController()
        self.controller.background_tasks_file = self.temp_file.name
        
    def tearDown(self):
        """測試後清理"""
        # 刪除臨時文件
        if os.path.exists(self.temp_file.name):
            os.unlink(self.temp_file.name)
    
    def test_save_and_load_background_tasks(self):
        """測試：儲存和載入背景任務"""
        # 添加一些任務歷史
        self.controller.background_task_history = [
            {
                "task_id": "task_001",
                "workflow_type": "test_workflow",
                "status": "completed",
                "start_time": time.time() - 100,
                "end_time": time.time() - 50
            },
            {
                "task_id": "task_002",
                "workflow_type": "test_workflow_2",
                "status": "failed",
                "start_time": time.time() - 80,
                "end_time": time.time() - 30,
                "error": "Test error"
            }
        ]
        
        # 儲存
        self.controller._save_background_tasks()
        
        # 驗證文件存在
        self.assertTrue(os.path.exists(self.temp_file.name))
        
        # 創建新的 Controller 實例並載入
        new_controller = UnifiedController()
        new_controller.background_tasks_file = self.temp_file.name
        new_controller._load_background_tasks()
        
        # 驗證：歷史記錄正確載入
        self.assertEqual(len(new_controller.background_task_history), 2)
        self.assertEqual(new_controller.background_task_history[0]['task_id'], "task_001")
        self.assertEqual(new_controller.background_task_history[1]['task_id'], "task_002")
        self.assertEqual(new_controller.background_task_history[1]['status'], "failed")


def run_all_tests():
    """運行所有測試"""
    # 創建測試套件
    test_suite = unittest.TestSuite()
    
    # 添加測試類
    test_suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestBackgroundWorkflowSubmission))
    test_suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestBackgroundWorkflowExecution))
    test_suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestBackgroundWorkflowCancellation))
    test_suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestBackgroundWorkflowParallelExecution))
    test_suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestBackgroundWorkflowEvents))
    test_suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestControllerBackgroundTaskMonitoring))
    test_suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestBackgroundTaskPersistence))
    
    # 運行測試
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(test_suite)
    
    # 返回結果
    return result


if __name__ == "__main__":
    print("=" * 80)
    print("階段五：背景工作流整合測試套件")
    print("=" * 80)
    
    result = run_all_tests()
    
    print("\n" + "=" * 80)
    print("測試完成")
    print(f"總測試數: {result.testsRun}")
    print(f"成功: {result.testsRun - len(result.failures) - len(result.errors)}")
    print(f"失敗: {len(result.failures)}")
    print(f"錯誤: {len(result.errors)}")
    print("=" * 80)
    
    # 返回退出碼
    sys.exit(0 if result.wasSuccessful() else 1)
