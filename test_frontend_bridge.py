# 測試 FrontendBridge 初始化
import sys
sys.path.insert(0, '.')

try:
    print("=== 測試 FrontendBridge 初始化 ===")
    
    # 初始化核心管理器
    from core.event_bus import event_bus
    print("✓ EventBus 已載入")
    
    from core.frontend_bridge import FrontendBridge
    from core.framework import core_framework
    print("✓ FrontendBridge 類已導入")
    
    # 創建實例
    fb = FrontendBridge()
    print("✓ FrontendBridge 實例已創建")
    
    # 初始化（協調器模式）
    print("\n正在初始化（coordinator_only=True）...")
    result = fb.initialize(coordinator_only=True)
    print(f"初始化結果: {result}")
    
    if result:
        # 註冊到 framework
        core_framework.frontend_bridge = fb
        print("✓ 已註冊到 core_framework")
        
        # 驗證
        print(f"✓ hasattr(core_framework, 'frontend_bridge'): {hasattr(core_framework, 'frontend_bridge')}")
        print(f"✓ core_framework.frontend_bridge is not None: {core_framework.frontend_bridge is not None}")
    else:
        print("✗ 初始化失敗")
        
except Exception as e:
    print(f"✗ 錯誤: {e}")
    import traceback
    traceback.print_exc()
