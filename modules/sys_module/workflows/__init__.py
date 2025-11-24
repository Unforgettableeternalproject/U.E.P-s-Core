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

# Export core classes from workflows first (so step_templates can import them)
WorkflowType = workflows_module.WorkflowType
WorkflowMode = workflows_module.WorkflowMode
StepResult = workflows_module.StepResult
WorkflowEngine = workflows_module.WorkflowEngine
WorkflowDefinition = workflows_module.WorkflowDefinition
WorkflowStep = workflows_module.WorkflowStep

# Now we can use standard import for step_templates since WorkflowStep/StepResult are available
from modules.sys_module.step_templates import StepTemplate

# Import workflow creators
from .test_workflows import create_test_workflow, get_available_test_workflows
from .file_workflows import create_file_workflow, get_available_file_workflows
from .text_workflows import create_text_workflow, get_available_text_workflows
from .analysis_workflows import create_analysis_workflow, get_available_analysis_workflows
from .info_workflows import create_info_workflow, get_available_info_workflows
from .utility_workflows import create_utility_workflow, get_available_utility_workflows

__all__ = [
    'WorkflowType', 'WorkflowMode', 'StepResult', 'WorkflowEngine', 'WorkflowDefinition',
    'WorkflowStep', 'StepTemplate',
    'create_test_workflow', 'get_available_test_workflows',
    'create_file_workflow', 'get_available_file_workflows',
    'create_text_workflow', 'get_available_text_workflows',
    'create_analysis_workflow', 'get_available_analysis_workflows',
    'create_info_workflow', 'get_available_info_workflows',
    'create_utility_workflow', 'get_available_utility_workflows'
]
