import os

import yaml
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
        self._function_specs = None

    def initialize(self):
        info_log("[SYS] 初始化完成，啟用模式：" + ", ".join(self.enabled_modes))
        return True

    def _load_function_specs(self):
        if self._function_specs is None:
            path = os.path.join(os.path.dirname(__file__), "functions.yaml")
            with open(path, "r", encoding="utf-8") as f:
                self._function_specs = yaml.safe_load(f)
        return self._function_specs

    def _validate_params(self, mode, params):
        specs = self._load_function_specs()
        if mode not in specs:
            return False, f"找不到 mode: {mode} 的規範"
        param_specs = specs[mode].get("params", {})
        # 檢查必填欄位
        for key, rule in param_specs.items():
            if rule.get("required", False) and key not in params:
                return False, f"缺少必要參數: {key}"
            if key in params:
                expected_type = rule.get("type")
                value = params[key]
                # 型別檢查
                if expected_type == "str" and not isinstance(value, str):
                    return False, f"參數 {key} 應為字串"
                if expected_type == "int" and not isinstance(value, int):
                    return False, f"參數 {key} 應為整數"
                if expected_type == "dict" and not isinstance(value, dict):
                    return False, f"參數 {key} 應為字典"
        return True, ""

    def handle(self, data: dict) -> dict:
        try:
            inp = SYSInput(**data)
        except Exception as e:
            return SYSOutput(status="error", message=f"輸入錯誤：{e}").dict()

        mode, params = inp.mode, inp.params or {}

        # list_functions 為特殊 mode，不受 enabled 篩選
        if mode == "list_functions":
            return SYSOutput(status="success", data=self._list_functions()).dict()

        if mode not in self.enabled_modes:
            return SYSOutput(status="error", message=f"未知或未啟用模式：{mode}").dict()

        vaild, msg = self._validate_params(mode, params)
        if not vaild:
            return SYSOutput(status="error", message=f"參數驗證失敗：{msg}").dict()

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
            info_log(f"[SYS] [{mode}] 執行完成")
            return SYSOutput(status="success", data=result).dict()
        except Exception as e:
            error_log(f"[SYS] [{mode}] 執行失敗：{e}")
            return SYSOutput(status="error", message=str(e)).dict()
    
    def _list_functions(self) -> dict:
        """
        讀取 functions.yaml 並回傳所有 mode 定義
        """
        try:
            path = os.path.join(os.path.dirname(__file__), "functions.yaml")
            with open(path, "r", encoding="utf-8") as f:
                funcs = yaml.safe_load(f)
            return funcs
        except Exception as e:
            error_log(f"[SYS] 列出功能失敗：{e}")
            return {}