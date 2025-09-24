"""
Legacy Direct Actions for SYS Module
====================================

This file contains the legacy direct action handlers that were used before
the workflow system was implemented. These are preserved here for reference
and potential future use, but are no longer used in the main SYS module.

These functions bypass the workflow system and call actions directly.
For new development, use the workflow-based approach instead.
"""

from .actions.file_interaction import clean_trash_bin, drop_and_read, intelligent_archive, summarize_tag, translate
from .actions.window_control import push_window, fold_window, switch_workspace, screenshot_and_annotate
from .actions.text_processing import clipboard_tracker, quick_phrases, ocr_extract
from .actions.automation_helper import set_reminder, generate_backup_script, monitor_folder
from .actions.integrations import news_summary, get_weather, get_world_time, code_analysis, media_control

def get_legacy_action_handlers():
    """
    Returns the legacy direct action handlers
    
    These handlers were used in the original SYS module before workflows were implemented.
    They bypass the workflow system and call actions directly.
    
    Returns:
        dict: Dictionary mapping action names to handler functions
    """
    return {
        # File Interaction Actions (now available as workflows)
        "drop_and_read": drop_and_read,
        "intelligent_archive": intelligent_archive,
        "summarize_tag": summarize_tag,
        "translate":translate,
        "clean_trash_bin": clean_trash_bin,
        
        # Window Control Actions
        "push_window": push_window,
        "fold_window": fold_window,
        "switch_workspace": switch_workspace,
        "screenshot_and_annotate": screenshot_and_annotate,
        
        # Clipboard Tools Actions
        "clipboard_tracker": clipboard_tracker,
        "quick_phrases": quick_phrases,
        "ocr_extract": ocr_extract,
        
        # Automation Helper Actions
        "set_reminder": set_reminder,
        "generate_backup_script": generate_backup_script,
        "monitor_folder": monitor_folder,
        
        # External Integration Actions
        "news_summary": news_summary,
        "get_weather": get_weather,
        "get_world_time": get_world_time,
        "code_analysis": code_analysis,
        "media_control": media_control,
    }

def handle_legacy_action(mode: str, params: dict):
    """
    Handle legacy direct actions (deprecated)
    
    This function provides backward compatibility for direct action calls.
    New code should use the workflow system instead.
    
    Args:
        mode: The action mode/type
        params: Parameters for the action
        
    Returns:
        dict: Action result
        
    Raises:
        ValueError: If the action mode is not supported
    """
    legacy_handlers = get_legacy_action_handlers()
    
    if mode not in legacy_handlers:
        raise ValueError(f"Unknown legacy action mode: {mode}")
    
    handler_func = legacy_handlers[mode]
    
    # Call the handler function with the provided parameters
    # Note: This assumes the handler functions accept the correct parameters
    try:
        if mode in ["drop_and_read", "intelligent_archive", "summarize_tag"]:
            # File interaction actions
            file_path = params.get("file_path")
            if not file_path:
                raise ValueError("file_path parameter is required")
            
            if mode == "drop_and_read":
                result = handler_func(file_path)
                return {"status": "ok", "data": result}
            elif mode == "intelligent_archive":
                target_dir = params.get("target_dir", "")
                result = handler_func(file_path, target_dir)
                return {"status": "ok", "data": result}
            elif mode == "summarize_tag":
                tag_count = params.get("tag_count", 3)
                result = handler_func(file_path, tag_count)
                return {"status": "ok", "data": result}
        else:
            # Other actions - call with params as keyword arguments
            result = handler_func(**params)
            return {"status": "ok", "data": result}
            
    except Exception as e:
        return {"status": "error", "message": str(e)}
