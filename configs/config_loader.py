import os
import yaml

BASE_DIR = os.path.dirname(os.path.dirname(__file__))  # 專案根目錄
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

def load_module_config(module_name: str):
    """載入指定模組的 config.yaml 設定"""
    module_config_path = os.path.join(BASE_DIR, "modules", module_name, "config.yaml")
    try:
        with open(module_config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        print(f"[!] 找不到模組設定：{module_config_path}，將使用空設定")
        return {}
    except yaml.YAMLError as e:
        print(f"[!] YAML 語法錯誤：{e}")
        return {}
