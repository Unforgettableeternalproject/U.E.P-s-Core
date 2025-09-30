"""
測試純系統級 Controller 的基本功能
"""

from core.controller import unified_controller

def test_system_level_controller():
    """測試系統級控制器功能"""
    print("🧪 測試純系統級 Controller...")
    
    try:
        # 1. 測試系統初始化
        print("\n1. 測試系統初始化...")
        success = unified_controller.initialize()
        print(f"   初始化結果: {'成功' if success else '失敗'}")
        
        # 2. 測試系統狀態報告
        print("\n2. 測試系統狀態報告...")
        status = unified_controller.get_system_status()
        print(f"   系統狀態: {status.get('system_status')}")
        print(f"   是否已初始化: {status.get('is_initialized')}")
        print(f"   當前狀態: {status.get('current_state')}")
        print(f"   當前GS: {status.get('current_gs', 'None')}")
        
        # 3. 測試用戶輸入觸發（純觸發器功能）
        print("\n3. 測試用戶輸入觸發...")
        result = unified_controller.trigger_user_input("測試輸入", "text")
        print(f"   觸發結果: {result.get('status')}")
        print(f"   會話ID: {result.get('session_id', 'None')}")
        print(f"   訊息: {result.get('message')}")
        
        # 4. 再次檢查系統狀態
        print("\n4. 檢查處理後的系統狀態...")
        status = unified_controller.get_system_status()
        print(f"   總GS會話數: {status.get('total_gs_sessions')}")
        print(f"   錯誤數量: {status.get('error_count')}")
        
        print("\n✅ 系統級 Controller 測試完成")
        return True
        
    except Exception as e:
        print(f"\n❌ 測試失敗: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    test_system_level_controller()