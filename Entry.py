"""
Session Control — Rich CLI 互動式測試介面

向後相容入口。實際實作已移至 echo_thought_core/cli.py。

Usage:
    python Entry.py                    # 預設使用 llm_config.yaml 設定
    python Entry.py --model gemini-2.5-pro  # 覆蓋模型
"""

from echo_thought_core.cli import main

if __name__ == "__main__":
    main()
