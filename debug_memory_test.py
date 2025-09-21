#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from datetime import datetime
from modules.mem_module.mem_module import MEMModule
from modules.mem_module.schemas import MEMInput, MEMOutput

def test_debug():
    """調試記憶存儲問題"""
    print("=== 開始調試記憶存儲 ===")
    
    # 初始化模組
    mem_module = MEMModule()
    mem_module.initialize()
    
    memory_token = f"test_debug_{int(datetime.now().timestamp())}"
    print(f"使用記憶令牌: {memory_token}")
    
    # 存儲短期記憶
    mem_input = MEMInput(
        operation_type="store_memory",
        memory_token=memory_token,
        memory_entry={
            "content": "用戶詢問了關於Python學習的問題",
            "memory_type": "snapshot",
            "topic": "程式學習",
            "importance": "medium"
        }
    )
    
    print(f"輸入: {mem_input}")
    
    result = mem_module.handle(mem_input)
    
    print(f"=== 結果詳情 ===")
    print(f"Success: {result.success}")
    print(f"Message: {result.message}")
    print(f"Operation Type: {result.operation_type}")
    
    if hasattr(result, 'operation_result') and result.operation_result:
        print(f"Operation Result: {result.operation_result}")
        
    if hasattr(result, 'errors') and result.errors:
        print(f"Errors: {result.errors}")
    
    if hasattr(result, 'warnings') and result.warnings:
        print(f"Warnings: {result.warnings}")
    
    print("=== 調試完成 ===")
    
    mem_module.cleanup()
    
    return result.success

if __name__ == "__main__":
    success = test_debug()
    print(f"測試結果: {'成功' if success else '失敗'}")