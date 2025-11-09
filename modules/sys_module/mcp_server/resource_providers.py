"""
Resource Providers - 工作流資源提供者

提供工作流狀態、步驟結果等資源給 LLM 查詢。
"""

from typing import Dict, Any, Optional
from pydantic import BaseModel, Field


class WorkflowResource(BaseModel):
    """工作流資源"""
    session_id: str
    workflow_type: str
    current_step: Optional[str] = None
    status: str
    progress: float  # 0.0 - 1.0
    metadata: Dict[str, Any] = Field(default_factory=dict)


class StepResultResource(BaseModel):
    """步驟結果資源"""
    session_id: str
    step_id: str
    status: str
    message: str
    data: Dict[str, Any] = Field(default_factory=dict)
    timestamp: str


class WorkflowResourceProvider:
    """
    工作流資源提供者
    
    提供工作流狀態查詢、步驟結果查詢等功能。
    """
    
    def __init__(self):
        self._workflows: Dict[str, WorkflowResource] = {}
        self._step_results: Dict[str, Dict[str, StepResultResource]] = {}
    
    def register_workflow(self, resource: WorkflowResource):
        """註冊工作流資源"""
        self._workflows[resource.session_id] = resource
    
    def update_workflow(self, session_id: str, updates: Dict[str, Any]):
        """更新工作流資源"""
        if session_id in self._workflows:
            workflow = self._workflows[session_id]
            for key, value in updates.items():
                if hasattr(workflow, key):
                    setattr(workflow, key, value)
    
    def get_workflow(self, session_id: str) -> Optional[WorkflowResource]:
        """取得工作流資源"""
        return self._workflows.get(session_id)
    
    def list_workflows(self) -> list[WorkflowResource]:
        """列出所有工作流"""
        return list(self._workflows.values())
    
    def remove_workflow(self, session_id: str):
        """移除工作流資源"""
        if session_id in self._workflows:
            del self._workflows[session_id]
        if session_id in self._step_results:
            del self._step_results[session_id]
    
    def add_step_result(self, session_id: str, result: StepResultResource):
        """新增步驟結果"""
        if session_id not in self._step_results:
            self._step_results[session_id] = {}
        self._step_results[session_id][result.step_id] = result
    
    def get_step_result(self, session_id: str, step_id: str) -> Optional[StepResultResource]:
        """取得步驟結果"""
        if session_id in self._step_results:
            return self._step_results[session_id].get(step_id)
        return None
    
    def get_all_step_results(self, session_id: str) -> list[StepResultResource]:
        """取得工作流所有步驟結果"""
        if session_id in self._step_results:
            return list(self._step_results[session_id].values())
        return []
    
    def clear(self):
        """清空所有資源"""
        self._workflows.clear()
        self._step_results.clear()
