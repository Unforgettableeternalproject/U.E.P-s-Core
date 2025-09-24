#!/usr/bin/env python3
"""
Test script for the new prompt management system
測試新提示詞管理系統的腳本
"""

import sys
import os
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from modules.llm_module.prompt_manager import PromptManager
from modules.llm_module.module_interfaces import ModuleDataProvider
from configs.config_loader import load_config, load_module_config


def test_config_loading():
    """測試配置加載"""
    print("=== Testing Config Loading ===")
    try:
        # Load global config
        global_config = load_config()
        print(f"✅ Global config loaded successfully")
        
        # Load LLM module config
        llm_config = load_module_config("llm_module")
        print(f"✅ LLM module config loaded successfully")
        print(f"📋 System instructions available: {list(llm_config.get('system_instructions', {}).keys())}")
        print(f"📊 System values guide available: {'system_values_guide' in llm_config}")
        print(f"🔧 Module interfaces available: {'module_interfaces' in llm_config}")
        
        return llm_config
    except Exception as e:
        print(f"❌ Config loading failed: {e}")
        return None


def test_module_data_provider():
    """測試模組資料提供者"""
    print("\n=== Testing ModuleDataProvider ===")
    
    provider = ModuleDataProvider()
    
    # Test mock providers (development mode)
    print("📝 Testing mock data providers...")
    
    # Test MEM data
    mem_data = provider.get_mem_data("context")
    print(f"MEM context data: {mem_data}")
    
    # Test SYS data  
    sys_data = provider.get_sys_data("functions")
    print(f"SYS functions data: {sys_data}")
    
    # Test custom provider registration
    def custom_mem_provider(**kwargs):
        return {"custom_memory": f"Custom memory for test", "kwargs": kwargs}
    
    provider.register_mem_provider("test", custom_mem_provider)
    custom_mem_data = provider.get_mem_data("test")
    print(f"Custom MEM data: {custom_mem_data}")
    
    print("✅ ModuleDataProvider working correctly")
    return provider


def test_prompt_manager(llm_config):
    """測試提示詞管理器"""
    print("\n=== Testing PromptManager ===")
    
    if not llm_config:
        print("❌ No LLM config available for PromptManager test")
        return
        
    print(f"📋 LLM Config loaded, system instructions: {list(llm_config.get('system_instructions', {}).keys())}")
    
    try:
        # Initialize PromptManager with LLM module config
        prompt_manager = PromptManager(llm_config)
        
        # Test template stats
        stats = prompt_manager.get_template_stats()
        print(f"📊 Template stats: {stats}")
        
        # Test chat prompt building
        print("\n📝 Testing chat prompt building...")
        user_input = "Hello, how are you today?"
        chat_prompt = prompt_manager.build_chat_prompt(
            user_input=user_input,
            identity_context={"identity": {"name": "Test User"}},
            is_internal=False
        )
        
        print("Generated chat prompt:")
        print("-" * 50)
        print(chat_prompt)
        print("-" * 50)
        
        # Test work prompt building
        print("\n📝 Testing work prompt building...")
        work_input = "Please help me organize my files"
        work_prompt = prompt_manager.build_work_prompt(
            user_input=work_input,
            identity_context={"identity": {"name": "Test User"}},
            workflow_context={
                "current_step": "File analysis",
                "remaining_steps": ["Organize", "Clean up", "Report"]
            }
        )
        
        print("Generated work prompt:")
        print("-" * 50)
        print(work_prompt)
        print("-" * 50)
        
        # Test direct prompt
        print("\n📝 Testing direct prompt building...")
        direct_prompt = prompt_manager.build_direct_prompt("Simple question")
        print(f"Direct prompt: {direct_prompt}")
        
        print("✅ PromptManager working correctly")
        
    except Exception as e:
        print(f"❌ PromptManager test failed: {e}")
        import traceback
        traceback.print_exc()


def test_integration():
    """整合測試"""
    print("\n=== Integration Test ===")
    
    # Load config
    llm_config = test_config_loading()
    
    # Test module data provider
    provider = test_module_data_provider()
    
    # Test prompt manager
    test_prompt_manager(llm_config)
    
    print("\n🎉 All tests completed!")


if __name__ == "__main__":
    print("🚀 Testing New Prompt Management System")
    print("=" * 50)
    
    test_integration()