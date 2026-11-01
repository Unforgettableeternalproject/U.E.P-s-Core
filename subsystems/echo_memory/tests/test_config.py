"""
配置診斷腳本 - 檢查 .env 文件是否正確讀取
"""

import os
from pathlib import Path

# 手動載入 .env
try:
    from dotenv import load_dotenv
    env_file = Path(__file__).parent / ".env"
    print(f"🔍 檢查 .env 文件: {env_file}")
    print(f"   文件存在: {env_file.exists()}")
    if env_file.exists():
        print(f"   文件大小: {env_file.stat().st_size} bytes")
        load_dotenv(env_file, override=True)
        print("   ✓ dotenv 載入完成\n")
    else:
        print("   ✗ .env 文件不存在！\n")
except ImportError:
    print("✗ python-dotenv 未安裝！\n")
    print("請執行: pip install python-dotenv\n")
    exit(1)

# 檢查環境變數
print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
print("環境變數檢查")
print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")

env_vars = {
    "GCP_PROJECT_ID": os.getenv("GCP_PROJECT_ID"),
    "GCP_LOCATION": os.getenv("GCP_LOCATION"),
    "GOOGLE_API_KEY": os.getenv("GOOGLE_API_KEY"),
    "EMBEDDING_MODEL": os.getenv("EMBEDDING_MODEL"),
    "MEMORY_STORAGE_PATH": os.getenv("MEMORY_STORAGE_PATH"),
}

for key, value in env_vars.items():
    if value:
        # 隱藏 API key
        if "KEY" in key or "API" in key:
            display_value = f"{value[:10]}..." if len(value) > 10 else "***"
        else:
            display_value = value
        print(f"✓ {key:25} = {display_value}")
    else:
        print(f"✗ {key:25} = (未設定)")

print("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
print("Config 類別檢查")
print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")

from echo_memory.config import get_config

config = get_config()

print(f"GCP_PROJECT_ID:     {config.gcp_project_id or '(未設定)'}")
print(f"GCP_LOCATION:       {config.gcp_location}")
print(f"GOOGLE_API_KEY:     {'✓ 已設定' if config.google_api_key else '✗ 未設定'}")
print(f"EMBEDDING_MODEL:    {config.embedding_model}")
print(f"STORAGE_PATH:       {config.memory_storage_path}")

print("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
print("配置驗證")
print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")

is_valid, missing = config.validate()

if is_valid:
    print("✓ 配置驗證通過！")
else:
    print(f"✗ 配置驗證失敗，缺少: {', '.join(missing)}")
    print("\n診斷資訊：")
    print(f"  - gcp_project_id 為空: {not config.gcp_project_id}")
    print(f"  - google_api_key 為空: {not config.google_api_key}")
    print(f"  - 兩者都為空: {not config.gcp_project_id and not config.google_api_key}")

print("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
