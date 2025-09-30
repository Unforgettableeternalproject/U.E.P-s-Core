#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模組重構狀態管理工具
用於查看和更新模組的重構狀態
"""

import sys
import os

# 添加專案根目錄到 Python 路徑
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from configs.config_loader import load_config, save_config


def display_module_status():
    """顯示模組重構狀態"""
    config = load_config()
    enabled_modules = config.get("modules_enabled", {})
    refactored_modules = config.get("modules_refactored", {})
    
    print("📋 UEP 模組狀態概覽")
    print("=" * 50)
    
    modules = ["stt_module", "nlp_module", "mem_module", "llm_module", "tts_module", "sys_module"]
    
    for module in modules:
        enabled = enabled_modules.get(module, False)
        refactored = refactored_modules.get(module, False)
        
        # 狀態圖示
        enabled_icon = "✅" if enabled else "❌"
        refactored_icon = "🔧" if refactored else "⏳"
        
        # 狀態文字
        status_text = []
        if enabled:
            status_text.append("已啟用")
        else:
            status_text.append("未啟用")
            
        if refactored:
            status_text.append("已重構")
        else:
            status_text.append("待重構")
        
        print(f"{enabled_icon} {refactored_icon} {module:12} - {' | '.join(status_text)}")
    
    print("\n📊 統計資訊:")
    enabled_count = sum(1 for status in enabled_modules.values() if status)
    refactored_count = sum(1 for status in refactored_modules.values() if status)
    
    print(f"   已啟用模組: {enabled_count}/{len(modules)}")
    print(f"   已重構模組: {refactored_count}/{len(modules)}")
    
    debug_mode = config.get("debug", {}).get("enabled", False)
    mode_text = "除錯模式" if debug_mode else "生產模式"
    print(f"   當前模式: {mode_text}")
    
    if not debug_mode:
        production_modules = [m for m in modules if enabled_modules.get(m, False) and refactored_modules.get(m, False)]
        print(f"   生產模式可用模組: {len(production_modules)}")
        if production_modules:
            print(f"   可用模組列表: {', '.join(production_modules)}")


def mark_module_refactored(module_name: str):
    """將模組標記為已重構"""
    config = load_config()
    
    if "modules_refactored" not in config:
        config["modules_refactored"] = {}
    
    if module_name not in config["modules_refactored"]:
        print(f"❌ 未知的模組名稱: {module_name}")
        return False
    
    config["modules_refactored"][module_name] = True
    
    if save_config(config):
        print(f"✅ 已將 {module_name} 標記為已重構")
        return True
    else:
        print(f"❌ 更新配置失敗")
        return False


def mark_module_not_refactored(module_name: str):
    """將模組標記為未重構"""
    config = load_config()
    
    if "modules_refactored" not in config:
        config["modules_refactored"] = {}
    
    if module_name not in config["modules_refactored"]:
        print(f"❌ 未知的模組名稱: {module_name}")
        return False
    
    config["modules_refactored"][module_name] = False
    
    if save_config(config):
        print(f"✅ 已將 {module_name} 標記為未重構")
        return True
    else:
        print(f"❌ 更新配置失敗")
        return False


def main():
    if len(sys.argv) == 1:
        display_module_status()
        return
    
    command = sys.argv[1].lower()
    
    if command == "status":
        display_module_status()
    elif command == "mark-refactored" and len(sys.argv) == 3:
        module_name = sys.argv[2]
        mark_module_refactored(module_name)
    elif command == "mark-not-refactored" and len(sys.argv) == 3:
        module_name = sys.argv[2]
        mark_module_not_refactored(module_name)
    elif command == "help":
        print("🛠️  模組重構狀態管理工具")
        print()
        print("用法:")
        print("  python utils/module_status.py                    - 顯示模組狀態")
        print("  python utils/module_status.py status             - 顯示模組狀態")
        print("  python utils/module_status.py mark-refactored <模組名>    - 標記模組為已重構")
        print("  python utils/module_status.py mark-not-refactored <模組名> - 標記模組為未重構")
        print("  python utils/module_status.py help               - 顯示幫助")
        print()
        print("可用的模組名稱:")
        print("  stt_module, nlp_module, mem_module, llm_module, tts_module, sys_module")
    else:
        print("❌ 無效的命令。使用 'help' 查看可用命令。")


if __name__ == "__main__":
    main()
