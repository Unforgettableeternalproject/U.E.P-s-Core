import os
import yaml
import sys

# 確保 utils 可以被導入
BASE_DIR = os.path.dirname(os.path.dirname(__file__))  # 專案根目錄
sys.path.append(BASE_DIR)

# 避免循環導入，在函數內部導入 debug_helper
# from utils.debug_helper import debug_log, info_log, error_log

CONFIG_PATH = os.path.join(BASE_DIR, "configs", "config.yaml")

def load_config(path=CONFIG_PATH):
    """載入全域 config.yaml 設定"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        print(f"[!] 找不到全域設定檔：{path}")
        return {}
    except yaml.YAMLError as e:
        print(f"[!] YAML 語法錯誤：{e}")
        return {}

def save_config(config, path=CONFIG_PATH):
    """儲存配置到 config.yaml"""
    try:
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        return True
    except Exception as e:
        print(f"[!] 儲存配置失敗：{e}")
        return False

def load_module_config(module_name: str):
    """
    載入指定模組的 config.yaml 設定
    
    優先使用模組內部的 config.yaml
    如果模組設定不存在或讀取失敗，將返回空設定
    
    注意：不再從全局 config.yaml 中讀取模組設定
    所有模組設定應該只在模組內部的 config.yaml 中配置
    """
    from utils.debug_helper import debug_log, error_log
    
    module_config_path = os.path.join(BASE_DIR, "modules", module_name, "config.yaml")
    try:
        with open(module_config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
            debug_log(3, f"[ConfigLoader] 已載入模組設定：{module_name}")
            return config
    except FileNotFoundError:
        error_log(f"[ConfigLoader] 找不到模組設定：{module_config_path}，將使用空設定")
        return {}
    except yaml.YAMLError as e:
        error_log(f"[ConfigLoader] YAML 語法錯誤：{e}")
        return {}

def get_input_mode():
    """
    獲取系統輸入模式配置
    
    Returns:
        str: "vad" (語音活動檢測) 或 "text" (文字輸入)
    """
    config = load_config()
    return config.get("system", {}).get("input_mode", {}).get("mode", "vad")

def is_text_input_mode():
    """檢查是否為文字輸入模式"""
    return get_input_mode() == "text"

def is_vad_mode():
    """檢查是否為 VAD 模式"""
    return get_input_mode() == "vad"
