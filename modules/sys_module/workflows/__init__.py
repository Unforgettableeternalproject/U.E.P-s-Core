"""
modules/sys_module/workflows/__init__.py
Workflow definitions package
"""

# Import core workflow classes from the main workflows.py file
import sys
import os
import importlib.util

# Load workflows.py directly to avoid circular imports
workflows_path = os.path.join(os.path.dirname(__file__), '..', 'workflows.py')
spec = importlib.util.spec_from_file_location("workflows_core", workflows_path)
workflows_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflows_module)

# Export core classes
WorkflowType = workflows_module.WorkflowType
StepResult = workflows_module.StepResult
WorkflowEngine = workflows_module.WorkflowEngine
WorkflowDefinition = workflows_module.WorkflowDefinition
WorkflowStep = workflows_module.WorkflowStep
FileSelectionStep = workflows_module.FileSelectionStep
ActionSelectionStep = workflows_module.ActionSelectionStep
ConfirmationStep = workflows_module.ConfirmationStep
StepTemplate = workflows_module.StepTemplate

# Import workflow creators
from .test_workflows import create_test_workflow, get_available_test_workflows
from .file_workflows import create_file_workflow, get_available_file_workflows

__all__ = [
    'WorkflowType', 'StepResult', 'WorkflowEngine', 'WorkflowDefinition',
    'WorkflowStep', 'FileSelectionStep', 'ActionSelectionStep', 'ConfirmationStep',
    'StepTemplate',
    'create_test_workflow', 'get_available_test_workflows',
    'create_file_workflow', 'get_available_file_workflows'
]
