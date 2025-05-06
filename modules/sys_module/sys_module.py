import os
from core.module_base import BaseModule
from configs.config_loader import load_module_config
from utils.debug_helper import info_log, error_log
from .schemas import SYSInput, SYSOutput

from .actions.file_interaction import drop_and_read, intelligent_archive, summarize_tag
from .actions.window_control   import push_window, fold_window, switch_workspace, screenshot_and_annotate
from .actions.text_processing  import clipboard_tracker, quick_phrases, ocr_extract
from .actions.automation_helper import set_reminder, generate_backup_script, monitor_folder
from .actions.integrations import news_summary, get_weather, get_world_time, code_analysis, media_control

# TTS Debug log 的部分還沒弄，SYS 也是

class SYSModule(BaseModule):
    def __init__(self, config=None):
        self.config = config or load_module_config("sys_module")
        self.enabled_modes = set(self.config.get("modes", []))

    def initialize(self):
        info_log("[SYS] 初始化完成，啟用模式：" + ", ".join(self.enabled_modes))
        return True

    def handle(self, data: dict) -> dict:
        try:
            inp = SYSInput(**data)
        except Exception as e:
            return SYSOutput(status="error", message=f"輸入錯誤：{e}").dict()

        mode = inp.mode
        params = inp.params or {}

        if mode not in self.enabled_modes:
            return SYSOutput(status="error", message=f"未啟用或未知模式：{mode}").dict()

        try:
            dispatch = {
                "drop_and_read":          drop_and_read,
                "intelligent_archive":    intelligent_archive,
                "summarize_tag":          summarize_tag,
                "push_window":            push_window,
                "fold_window":            fold_window,
                "switch_workspace":       switch_workspace,
                "screenshot_and_annotate":screenshot_and_annotate,
                "clipboard_tracker":      clipboard_tracker,
                "quick_phrases":          quick_phrases,
                "ocr_extract":            ocr_extract,
                "set_reminder":           set_reminder,
                "generate_backup_script": generate_backup_script,
                "monitor_folder":         monitor_folder,
                "news_summary":           news_summary,
                "get_weather":            get_weather,
                "get_world_time":         get_world_time,
                "code_analysis":          code_analysis,
                "media_control":          media_control,
            }

            func = dispatch.get(mode)
            result = func(**params)
            info_log(f"[SYS][{mode}] 執行完成")
            return SYSOutput(status="success", data=result).dict()

        except Exception as e:
            error_log(f"[SYS][{mode}] 執行失敗：{e}")
            return SYSOutput(status="error", message=str(e)).dict()