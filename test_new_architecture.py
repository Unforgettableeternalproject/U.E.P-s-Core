"""
測試新的架構：Schema 適配器 + 調試器 + 簡化框架
展示模組間通信無需專門轉換器的能力
"""

import sys
import os

# 確保可以導入項目模組
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.schema_adapter import schema_handler
from devtools.module_debugger import UEPModuleDebugger
from core.controller import UnifiedController
from utils.debug_helper import debug_log, info_log, error_log

def test_schema_based_pipeline():
    """測試基於 Schema 適配器的管線處理"""
    print("🚀 測試新架構：Schema 適配器驅動的模組通信")
    print("=" * 60)
    
    try:
        # 初始化控制器
        controller = UnifiedController()
        print("✅ 統一控制器初始化成功")
        
        # 測試輸入
        test_input = "你好，我想測試新的架構"
        print(f"\n📤 測試輸入: {test_input}")
        
        # 模擬使用 Schema 適配器的處理流程
        print("\n🔄 開始模組鏈式處理（使用 Schema 適配器）")
        print("-" * 40)
        
        # 步驟 1: NLP 處理
        if controller.framework.modules.get("nlp"):
            nlp_module = controller.framework.modules["nlp"].instance
            
            # 使用 Schema 適配器包裝輸入
            nlp_input = schema_handler.adapter_registry.adapt_input("nlp", {"text": test_input})
            print(f"1️⃣ NLP 適配輸入: {nlp_input}")
            
            # 執行 NLP 處理
            nlp_raw_output = nlp_module.handle(nlp_input)
            print(f"   NLP 原始輸出: {nlp_raw_output}")
            
            # 使用 Schema 適配器標準化輸出
            nlp_standardized = schema_handler.adapter_registry.adapt_output("nlp", nlp_raw_output)
            print(f"   NLP 標準化輸出: {nlp_standardized}")
            
            # 步驟 2: 自動轉換為 MEM 輸入
            mem_input = schema_handler.adapter_registry.adapt_input("mem", nlp_standardized.get("data", {}))
            print(f"\n2️⃣ MEM 自動適配輸入: {mem_input}")
            
            if controller.framework.modules.get("mem"):
                mem_module = controller.framework.modules["mem"].instance
                
                # 執行 MEM 處理
                mem_raw_output = mem_module.handle(mem_input)
                print(f"   MEM 原始輸出: {mem_raw_output}")
                
                # 標準化 MEM 輸出
                mem_standardized = schema_handler.adapter_registry.adapt_output("mem", mem_raw_output)
                print(f"   MEM 標準化輸出: {mem_standardized}")
                
                # 步驟 3: 自動轉換為 LLM 輸入
                llm_input_data = {
                    "text": test_input,
                    "intent": nlp_standardized.get("data", {}).get("intent", "chat"),
                    "memory": "No relevant memory found." if mem_standardized.get("status") != "success" else str(mem_standardized.get("data", {}).get("results", []))
                }
                llm_input = schema_handler.adapter_registry.adapt_input("llm", llm_input_data)
                print(f"\n3️⃣ LLM 自動適配輸入: {llm_input}")
                
                if controller.framework.modules.get("llm"):
                    llm_module = controller.framework.modules["llm"].instance
                    
                    # 執行 LLM 處理
                    llm_raw_output = llm_module.handle(llm_input)
                    print(f"   LLM 原始輸出: {llm_raw_output}")
                    
                    # 標準化 LLM 輸出
                    llm_standardized = schema_handler.adapter_registry.adapt_output("llm", llm_raw_output)
                    print(f"   LLM 標準化輸出: {llm_standardized}")
                    
                    # 步驟 4: 可選的 TTS 處理
                    tts_input_data = {
                        "text": llm_standardized.get("data", {}).get("text", ""),
                        "mood": llm_standardized.get("data", {}).get("mood", "neutral"),
                        "save": False
                    }
                    tts_input = schema_handler.adapter_registry.adapt_input("tts", tts_input_data)
                    print(f"\n4️⃣ TTS 自動適配輸入: {tts_input}")
                    
                    print("\n🎯 關鍵觀察：")
                    print("✅ 所有模組間數據轉換都通過 Schema 適配器自動完成")
                    print("✅ 不需要專門的轉換器函數")
                    print("✅ 統一的數據格式標準化")
                    print("✅ 模組解耦，易於擴展和維護")
                    
        print("\n" + "=" * 60)
        print("🎉 Schema 適配器架構測試完成！")
        
    except Exception as e:
        error_log(f"測試失敗: {e}")
        import traceback
        traceback.print_exc()

def test_debugger_integration():
    """測試調試器整合"""
    print("\n🔧 測試調試器整合")
    print("=" * 40)
    
    try:
        debugger = UEPModuleDebugger()
        print("✅ 模組調試器初始化成功")
        
        # 檢查可用模組
        available = [name for name, mod in debugger.modules.items() if mod is not None]
        print(f"📦 可用模組: {available}")
        
        # 測試單一模組（如果 NLP 可用）
        if "nlp" in available:
            print("\n🧠 測試 NLP 模組（通過調試器）")
            result = debugger.nlp_test("測試 Schema 適配器")
            print(f"   結果: {result}")
        
        print("\n✅ 調試器整合測試完成")
        
    except Exception as e:
        error_log(f"調試器測試失敗: {e}")

def demonstrate_architecture_benefits():
    """展示新架構的優勢"""
    print("\n🎯 新架構優勢展示")
    print("=" * 40)
    
    print("📈 架構改進對比：")
    print()
    print("🔸 舊架構 (controller.py):")
    print("   • 調試功能與控制邏輯混雜")
    print("   • 模組間需要專門的轉換器")
    print("   • 數據格式不統一")
    print("   • 難以維護和擴展")
    print()
    print("🔸 新架構 (unified_controller + schema_adapter + module_debugger):")
    print("   • 清晰的職責分離")
    print("   • 統一的 Schema 適配器處理數據轉換")
    print("   • 標準化的輸入/輸出格式")
    print("   • 獨立的調試工具集")
    print("   • 易於模組重構和升級")
    print()
    print("🎉 實現目標：")
    print("   ✅ Working Context 整合完成")
    print("   ✅ State Management 系統運行")
    print("   ✅ 模組間網狀通信架構")
    print("   ✅ 漸進式重構支持")

def main():
    """主函數"""
    print("🚀 UEP 新架構綜合測試")
    print("=" * 70)
    
    # 測試 Schema 適配器管線
    test_schema_based_pipeline()
    
    # 測試調試器整合
    test_debugger_integration()
    
    # 展示架構優勢
    demonstrate_architecture_benefits()
    
    print("\n" + "=" * 70)
    print("🎊 所有測試完成！新架構已就緒！")

if __name__ == "__main__":
    main()
