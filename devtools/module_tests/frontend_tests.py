# -*- coding: utf-8 -*-
"""
Frontend 模組測試函數
已重構模組 - 完整功能測試
修正為使用 handle_frontend_request() 方法
"""

import asyncio
import time
from utils.debug_helper import debug_log, info_log, error_log

def show_desktop_pet(modules):
    """顯示桌面寵物"""
    frontend = modules.get("ui")
    if frontend is None:
        print("❌ Frontend 模組未載入")
        return None

    print("🐾 顯示桌面寵物...")
    
    try:
        # 使用正確的前端模組命令格式
        result = frontend.handle_frontend_request({
            "command": "show_interface",
            "interface": "main_desktop_pet"
        })
        
        if result and result.get("success"):
            print("✅ 桌面寵物已顯示")
            return {"status": "success", "result": result}
        else:
            print(f"❌ 顯示桌面寵物失敗: {result.get('error', '未知錯誤')}")
            return {"status": "error", "result": result}
            
    except Exception as e:
        print(f"❌ 顯示桌面寵物時發生錯誤: {str(e)}")
        return {"status": "error", "error": str(e)}

def hide_desktop_pet(modules):
    """隱藏桌面寵物"""
    frontend = modules.get("ui")
    if frontend is None:
        print("❌ Frontend 模組未載入")
        return None

    print("🙈 隱藏桌面寵物...")
    
    try:
        result = frontend.handle_frontend_request({
            "command": "hide_interface",
            "interface": "main_desktop_pet"
        })
        
        if result and result.get("success"):
            print("✅ 桌面寵物已隱藏")
            return {"status": "success", "result": result}
        else:
            print(f"❌ 隱藏桌面寵物失敗: {result.get('error', '未知錯誤')}")
            return {"status": "error", "result": result}
            
    except Exception as e:
        print(f"❌ 隱藏桌面寵物時發生錯誤: {str(e)}")
        return {"status": "error", "error": str(e)}

def control_desktop_pet(modules, action="wave", duration=3, x=None, y=None):
    """控制桌面寵物動作"""
    frontend = modules.get("ui")
    if frontend is None:
        print("❌ Frontend 模組未載入")
        return None

    print(f"🎭 控制桌面寵物執行動作: {action}")
    
    try:
        # 先確保桌面寵物已顯示
        show_result = frontend.handle_frontend_request({
            "command": "show_interface",
            "interface": "main_desktop_pet"
        })
        
        # 處理不同的動作類型
        if action == "move_window" and x is not None and y is not None:
            # 移動窗口到指定位置
            print(f"📍 移動桌面寵物到位置 ({x}, {y})")
            
            # 獲取桌面寵物實例並直接移動
            desktop_pet = frontend.interfaces.get(frontend.UIInterfaceType.MAIN_DESKTOP_PET) if hasattr(frontend, 'interfaces') else None
            
            if desktop_pet:
                desktop_pet.set_position(x, y)
                print(f"✅ 桌面寵物已移動到 ({x}, {y})")
                return {"status": "success", "action": action, "position": {"x": x, "y": y}}
            else:
                print("❌ 無法獲取桌面寵物實例")
                return {"status": "error", "error": "無法獲取桌面寵物實例"}
        
        else:
            # 其他動作：設置圖像或動畫
            result = frontend.handle_frontend_request({
                "command": "set_image",
                "image_path": f"resources/animations/{action}"
            })
            
            if result and not result.get("error"):
                print(f"✅ 桌面寵物正在執行 {action} 動作")
                return {"status": "success", "result": result}
            else:
                print(f"❌ 控制桌面寵物失敗: {result.get('error', '未知錯誤')}")
                return {"status": "error", "result": result}
            
    except Exception as e:
        print(f"❌ 控制桌面寵物時發生錯誤: {str(e)}")
        return {"status": "error", "error": str(e)}

def test_mov_ani_integration(modules):
    """測試 MOV-ANI 整合功能 - 第一步藍圖"""
    frontend = modules.get("ui")
    if frontend is None:
        print("❌ Frontend 模組未載入")
        return None

    print("🔗 測試 MOV-ANI 整合功能...")
    print("   這是藍圖第一步：UEP主程式能夠跟MOV、ANI進行連動")
    
    try:
        # 先顯示桌面寵物
        show_result = frontend.handle_frontend_request({
            "command": "show_interface",
            "interface": "main_desktop_pet"
        })
        
        if show_result and show_result.get("success"):
            print("✅ UEP 主程式已顯示")
            
            # 透過UI模組檢查ANI和MOV模組狀態（避免重複載入）
            ui_module = modules.get("ui")
            if ui_module:
                # 檢查UI模組是否已經初始化了ANI和MOV模組
                ani_available = hasattr(ui_module, 'ani_module') and ui_module.ani_module is not None
                mov_available = hasattr(ui_module, 'mov_module') and ui_module.mov_module is not None
                
                ani_status = "可用" if ani_available else "不可用"
                mov_status = "可用" if mov_available else "不可用"
                
                print(f"   ANI 模組狀態: {ani_status} (透過UI模組)")
                print(f"   MOV 模組狀態: {mov_status} (透過UI模組)")
                
                if ani_available and mov_available:
                    print("✅ MOV-ANI 基本連動測試通過")
                    return {"status": "success", "mov_status": mov_status, "ani_status": ani_status}
                else:
                    print("⚠️ 部分模組不可用")
                    return {"status": "partial", "mov_status": mov_status, "ani_status": ani_status}
            else:
                print("❌ UI模組不可用")
                return {"status": "error", "message": "UI模組不可用"}
        else:
            print(f"❌ UEP 主程式顯示失敗: {show_result.get('error', '未知錯誤')}")
            return {"status": "error", "result": show_result}
            
    except Exception as e:
        print(f"❌ MOV-ANI 整合測試時發生錯誤: {str(e)}")
        return {"status": "error", "error": str(e)}

def test_behavior_modes(modules):
    """測試行為模式 - 第二步藍圖"""
    frontend = modules.get("ui")
    if frontend is None:
        print("❌ Frontend 模組未載入")
        return None

    print("🎭 測試行為模式功能...")
    print("   這是藍圖第二步：不同行為模式的實現")
    
    # 先確保桌面寵物已顯示
    show_result = frontend.handle_frontend_request({
        "command": "show_interface",
        "interface": "main_desktop_pet"
    })
    
    # 測試不同的界面狀態
    interface_types = ["main_desktop_pet", "user_access_widget", "user_main_window"]
    results = {}
    
    try:
        for interface_type in interface_types:
            print(f"\n   測試界面: {interface_type}")
            
            # 嘗試顯示界面
            result = frontend.handle_frontend_request({
                "command": "show_interface",
                "interface": interface_type
            })
            
            if result and result.get("success"):
                print(f"   ✅ {interface_type} 顯示成功")
                results[interface_type] = "success"
                
                # 嘗試隱藏界面（除了主桌寵）
                if interface_type != "main_desktop_pet":
                    hide_result = frontend.handle_frontend_request({
                        "command": "hide_interface",
                        "interface": interface_type
                    })
                    if hide_result and hide_result.get("success"):
                        print(f"   ✅ {interface_type} 隱藏成功")
            else:
                print(f"   ❌ {interface_type} 顯示失敗")
                results[interface_type] = "failed"
            
            time.sleep(1)  # 短暫延遲
        
        success_count = sum(1 for status in results.values() if status == "success")
        total_count = len(interface_types)
        
        print(f"\n📊 行為模式測試總結: {success_count}/{total_count} 通過")
        
        if success_count == total_count:
            print("✅ 所有行為模式測試通過")
            return {"status": "success", "results": results}
        elif success_count > 0:
            print("⚠️ 部分行為模式測試通過")
            return {"status": "partial", "results": results}
        else:
            print("❌ 所有行為模式測試失敗")
            return {"status": "failed", "results": results}
            
    except Exception as e:
        print(f"❌ 行為模式測試時發生錯誤: {str(e)}")
        return {"status": "error", "error": str(e)}

def test_animation_state_machine(modules):
    """測試動畫狀態機 - 第三步藍圖"""
    frontend = modules.get("ui")
    
    if frontend is None:
        print("❌ Frontend 模組未載入")
        return None

    print("🔄 測試動畫狀態機...")
    print("   這是藍圖第三步：動畫狀態機的實現")
    
    try:
        # 確保桌面寵物已顯示
        show_result = frontend.handle_frontend_request({
            "command": "show_interface",
            "interface": "main_desktop_pet"
        })
        
        # 透過UI模組取得ANI模組實例
        ani_module = None
        if hasattr(frontend, 'ani_module'):
            ani_module = frontend.ani_module
        
        # 如果有 ANI 模組，測試動畫功能
        if ani_module:
            print("✅ ANI 模組可用，測試動畫狀態轉換...")
            
            # 測試播放一個動畫
            ani_result = ani_module.handle_frontend_request({
                "command": "play_animation",
                "animation_type": "smile_idle_f"
            })
            
            if ani_result and ani_result.get("success"):
                print("✅ 動畫播放測試成功")
                return {"status": "success", "animation_test": ani_result}
            else:
                print("⚠️ 動畫播放測試失敗，但 ANI 模組可用")
                return {"status": "partial", "animation_test": ani_result}
        else:
            print("⚠️ ANI 模組不可用，跳過動畫測試")
            return {"status": "partial", "message": "ANI 模組不可用"}
            
    except Exception as e:
        print(f"❌ 動畫狀態機測試時發生錯誤: {str(e)}")
        return {"status": "error", "error": str(e)}

def frontend_test_full(modules):
    """Frontend 完整測試 - 第四步藍圖"""
    frontend = modules.get("ui")
    if frontend is None:
        print("❌ Frontend 模組未載入")
        return None

    print("🚀 Frontend 完整測試...")
    print("   這是藍圖第四步：與Core的完整整合")
    
    test_results = {}
    
    try:
        # 第一步：MOV-ANI 整合測試
        print("\n🔗 執行 MOV-ANI 整合測試...")
        integration_result = test_mov_ani_integration(modules)
        test_results["integration"] = integration_result
        
        # 第二步：行為模式測試
        print("\n🎭 執行行為模式測試...")
        behavior_result = test_behavior_modes(modules)
        test_results["behavior_modes"] = behavior_result
        
        # 第三步：動畫狀態機測試
        print("\n🔄 執行動畫狀態機測試...")
        state_machine_result = test_animation_state_machine(modules)
        test_results["state_machine"] = state_machine_result
        
        # 第四步：介面狀態檢查
        print("\n🏗️ 執行介面狀態檢查...")
        status_result = frontend.handle_frontend_request({
            "command": "get_interface_status"
        })
        
        if status_result and not status_result.get("error"):
            print("✅ 介面狀態檢查通過")
            test_results["interface_status"] = {"status": "success", "result": status_result}
        else:
            print("❌ 介面狀態檢查失敗")
            test_results["interface_status"] = {"status": "error", "result": status_result}
        
        # 計算總體成功率
        success_count = 0
        total_tests = 0
        
        for test_name, result in test_results.items():
            total_tests += 1
            if result and result.get("status") in ["success", "partial"]:
                success_count += 1
        
        success_rate = (success_count / total_tests) * 100 if total_tests > 0 else 0
        
        print(f"\n📊 Frontend 完整測試總結:")
        print(f"   成功率: {success_rate:.1f}% ({success_count}/{total_tests})")
        
        for test_name, result in test_results.items():
            status = result.get("status", "unknown") if result else "failed"
            status_icon = "✅" if status == "success" else "⚠️" if status == "partial" else "❌"
            print(f"   {status_icon} {test_name}: {status}")
        
        overall_status = "success" if success_rate >= 80 else "partial" if success_rate >= 50 else "failed"
        
        return {
            "status": overall_status,
            "success_rate": success_rate,
            "test_results": test_results
        }
        
    except Exception as e:
        print(f"❌ Frontend 完整測試時發生錯誤: {str(e)}")
        return {"status": "error", "error": str(e)}

def frontend_get_status(modules):
    """獲取 Frontend 模組狀態"""
    frontend = modules.get("ui")
    if frontend is None:
        print("❌ Frontend 模組未載入")
        return None

    try:
        result = frontend.handle_frontend_request({
            "command": "get_interface_status"
        })
        
        if result and not result.get("error"):
            print("📊 Frontend 模組狀態:")
            
            # 顯示各介面狀態
            for interface_name, status in result.items():
                exists = status.get("exists", False)
                active = status.get("active", False)
                visible = status.get("visible", False)
                
                status_icon = "✅" if visible else "⚠️" if exists else "❌"
                print(f"   {status_icon} {interface_name}: 存在={exists}, 活躍={active}, 可見={visible}")
            
            return {"status": "success", "result": result}
        else:
            print("❌ 無法獲取 Frontend 狀態")
            return {"status": "error", "result": result}
            
    except Exception as e:
        print(f"❌ 獲取 Frontend 狀態時發生錯誤: {str(e)}")
        return {"status": "error", "error": str(e)}

def frontend_test_animations(modules):
    """測試各種動畫效果"""
    frontend = modules.get("ui")
    if frontend is None:
        print("❌ UI 模組未載入")
        return None
        
    # 透過UI模組取得ANI模組實例
    ani_module = None
    if hasattr(frontend, 'ani_module'):
        ani_module = frontend.ani_module
    
    if ani_module is None:
        print("❌ ANI 模組未在UI模組中初始化")
        return None

    animations = ["smile_idle_f", "angry_idle_f", "curious_idle_f", "dance_f", "laugh_f"]
    
    print("🎨 測試動畫效果...")
    
    results = {}
    
    try:
        for animation in animations:
            print(f"\n   測試動畫: {animation}")
            
            result = ani_module.handle_frontend_request({
                "command": "play_animation",
                "animation_type": animation
            })
            
            if result and result.get("success"):
                print(f"   ✅ {animation} 動畫播放成功")
                results[animation] = "success"
            else:
                print(f"   ❌ {animation} 動畫播放失敗")
                results[animation] = "failed"
            
            time.sleep(1)
        
        success_count = sum(1 for status in results.values() if status == "success")
        total_count = len(animations)
        
        print(f"\n📊 動畫測試總結: {success_count}/{total_count} 成功")
        
        return {
            "status": "success" if success_count == total_count else "partial" if success_count > 0 else "failed",
            "results": results,
            "success_rate": (success_count / total_count) * 100
        }
        
    except Exception as e:
        print(f"❌ 動畫測試時發生錯誤: {str(e)}")
        return {"status": "error", "error": str(e)}

def frontend_test_user_interaction(modules):
    """測試用戶交互功能"""
    frontend = modules.get("ui")
    if frontend is None:
        print("❌ Frontend 模組未載入")
        return None

    print("👆 測試用戶交互功能...")
    
    try:
        # 確保桌面寵物已顯示
        show_result = frontend.handle_frontend_request({
            "command": "show_interface",
            "interface": "main_desktop_pet"
        })
        
        # 測試視窗操作
        operations = [
            {"command": "get_window_info", "name": "視窗資訊"},
            {"command": "set_always_on_top", "enabled": True, "name": "設定置頂"},
            {"command": "set_opacity", "opacity": 0.8, "name": "設定透明度"}
        ]
        
        results = {}
        
        for operation in operations:
            op_name = operation.pop("name")
            print(f"\n   測試操作: {op_name}")
            
            result = frontend.handle_frontend_request(operation)
            
            if result and not result.get("error"):
                print(f"   ✅ {op_name} 測試成功")
                results[op_name] = "success"
            else:
                print(f"   ❌ {op_name} 測試失敗")
                results[op_name] = "failed"
        
        success_count = sum(1 for status in results.values() if status == "success")
        total_count = len(operations)
        
        print(f"\n📊 交互測試總結: {success_count}/{total_count} 成功")
        
        return {
            "status": "success" if success_count == total_count else "partial" if success_count > 0 else "failed",
            "results": results,
            "success_rate": (success_count / total_count) * 100
        }
        
    except Exception as e:
        print(f"❌ 用戶交互測試時發生錯誤: {str(e)}")
        return {"status": "error", "error": str(e)}
