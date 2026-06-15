"""
配置驗證腳本 - 檢查環境設定

運行此腳本以驗證 .env 配置是否正確。
"""

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 添加專案路徑
sys.path.insert(0, str(Path(__file__).parent))

from echo_memory.config import get_config


def main():
    """驗證配置"""

    config = get_config()

    print("=" * 80)
    print("Echo Memory System - 配置驗證")
    print("=" * 80)

    # 顯示配置摘要
    print(config.summary())

    # 驗證 LLM 配置
    is_valid, missing = config.validate_llm()

    print("\n配置驗證結果:")
    print("-" * 80)

    if is_valid:
        print("✅ 所有必要配置已設定")
        print("\n可以開始使用 Echo Memory System！")
        print("\n下一步:")
        print(r"  1. 運行測試: ..\U.E.P-s-Core\env\Scripts\python.exe -m pytest -v")
        print(r"  2. 啟動 CLI: .\run.bat")
        return 0
    else:
        print("⚠️ 缺少 LLM API 配置:")
        for key in missing:
            print(f"   - {key}")
        print("\n請編輯 .env 文件，設定缺少的環境變數。")
        print("參考 .env.example 獲取完整的配置選項。")
        print("\n注意: 即使未設定 LLM API，記憶 CLI 的存儲/檢索功能仍可正常使用。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
