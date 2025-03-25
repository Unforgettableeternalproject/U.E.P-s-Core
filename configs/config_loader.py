import os
import yaml

BASE_DIR = os.path.dirname(os.path.dirname(__file__))  # 盡ヘ魁
CONFIG_PATH = os.path.join(BASE_DIR, "configs", "config.yaml")

def load_config(path=CONFIG_PATH):
    """更办 config.yaml 砞﹚"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        print(f"[!] тぃ办砞﹚郎{path}")
        return {}
    except yaml.YAMLError as e:
        print(f"[!] YAML 粂猭岿粇{e}")
        return {}

def load_module_config(module_name: str):
    """更﹚家舱 config.yaml 砞﹚"""
    module_config_path = os.path.join(BASE_DIR, "modules", module_name, "config.yaml")
    try:
        with open(module_config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        print(f"[!] тぃ家舱砞﹚{module_config_path}盢ㄏノ砞﹚")
        return {}
    except yaml.YAMLError as e:
        print(f"[!] YAML 粂猭岿粇{e}")
        return {}
