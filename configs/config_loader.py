import os
import yaml

BASE_DIR = os.path.dirname(os.path.dirname(__file__))  # �M�׮ڥؿ�
CONFIG_PATH = os.path.join(BASE_DIR, "configs", "config.yaml")

def load_config(path=CONFIG_PATH):
    """���J���� config.yaml �]�w"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        print(f"[!] �䤣�����]�w�ɡG{path}")
        return {}
    except yaml.YAMLError as e:
        print(f"[!] YAML �y�k���~�G{e}")
        return {}

def load_module_config(module_name: str):
    """���J���w�Ҳժ� config.yaml �]�w"""
    module_config_path = os.path.join(BASE_DIR, "modules", module_name, "config.yaml")
    try:
        with open(module_config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        print(f"[!] �䤣��Ҳճ]�w�G{module_config_path}�A�N�ϥΪų]�w")
        return {}
    except yaml.YAMLError as e:
        print(f"[!] YAML �y�k���~�G{e}")
        return {}
