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
    raise NotImplementedError("控制桌面寵物動作尚未實作")

def test_mov_ani_integration(modules):
    """測試 MOV-ANI 整合功能 - 第一步藍圖"""
    raise NotImplementedError("MOV-ANI 整合功能尚未實作")

def test_behavior_modes(modules):
    """測試行為模式 - 第二步藍圖"""
    raise NotImplementedError("行為模式測試尚未實作")

def test_animation_state_machine(modules):
    """測試動畫狀態機 - 第三步藍圖"""
    raise NotImplementedError("動畫狀態機測試尚未實作")

def frontend_test_full(modules):
    """Frontend 完整測試 - 第四步藍圖"""
    raise NotImplementedError("Frontend 完整測試尚未實作")

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

    animations = ["angry_idle_f"]
    
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
