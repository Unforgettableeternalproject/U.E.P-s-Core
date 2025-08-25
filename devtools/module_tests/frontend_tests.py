# -*- coding: utf-8 -*-
"""
Frontend 模組測試函數
已重構模組 - 完整功能測試
"""

import asyncio
import time
from utils.debug_helper import debug_log, info_log, error_log

def show_desktop_pet(modules):
    """顯示桌面寵物"""
    frontend = modules.get("frontend")
    if frontend is None:
        print("❌ Frontend 模組未載入")
        return None

    print("🐾 顯示桌面寵物...")
    
    try:
        result = frontend.handle({
            "action": "show_pet",
            "animation": "idle"
        })
        
        if result and result.get("status") == "success":
            print("✅ 桌面寵物已顯示")
            return result
        else:
            print(f"❌ 顯示桌面寵物失敗: {result.get('message', '未知錯誤')}")
            return result
            
    except Exception as e:
        print(f"❌ 顯示桌面寵物時發生錯誤: {str(e)}")
        return {"status": "error", "error": str(e)}

def hide_desktop_pet(modules):
    """隱藏桌面寵物"""
    frontend = modules.get("frontend")
    if frontend is None:
        print("❌ Frontend 模組未載入")
        return None

    print("🙈 隱藏桌面寵物...")
    
    try:
        result = frontend.handle({
            "action": "hide_pet"
        })
        
        if result and result.get("status") == "success":
            print("✅ 桌面寵物已隱藏")
            return result
        else:
            print(f"❌ 隱藏桌面寵物失敗: {result.get('message', '未知錯誤')}")
            return result
            
    except Exception as e:
        print(f"❌ 隱藏桌面寵物時發生錯誤: {str(e)}")
        return {"status": "error", "error": str(e)}

def control_desktop_pet(modules, action="wave", duration=3):
    """控制桌面寵物動作"""
    frontend = modules.get("frontend")
    if frontend is None:
        print("❌ Frontend 模組未載入")
        return None

    print(f"🎭 控制桌面寵物執行動作: {action} (持續 {duration} 秒)")
    
    try:
        result = frontend.handle({
            "action": "control_pet",
            "animation": action,
            "duration": duration
        })
        
        if result and result.get("status") == "success":
            print(f"✅ 桌面寵物正在執行 {action} 動作")
            return result
        else:
            print(f"❌ 控制桌面寵物失敗: {result.get('message', '未知錯誤')}")
            return result
            
    except Exception as e:
        print(f"❌ 控制桌面寵物時發生錯誤: {str(e)}")
        return {"status": "error", "error": str(e)}

def test_mov_ani_integration(modules):
    """測試 MOV-ANI 整合功能 - 第一步藍圖"""
    frontend = modules.get("frontend")
    if frontend is None:
        print("❌ Frontend 模組未載入")
        return None

    print("🔗 測試 MOV-ANI 整合功能...")
    print("   這是藍圖第一步：UEP主程式能夠跟MOV、ANI進行連動")
    
    try:
        # 測試基本連動
        result = frontend.handle({
            "action": "test_integration",
            "components": ["mov", "ani"],
            "test_type": "basic_connection"
        })
        
        if result and result.get("status") == "success":
            print("✅ MOV-ANI 基本連動測試通過")
            print(f"   MOV 狀態: {result.get('mov_status', '未知')}")
            print(f"   ANI 狀態: {result.get('ani_status', '未知')}")
            
            # 測試簡單動作同步
            sync_result = frontend.handle({
                "action": "test_sync",
                "animation": "idle",
                "movement": "float"
            })
            
            if sync_result and sync_result.get("status") == "success":
                print("✅ 動作同步測試通過")
                return {"status": "success", "integration": result, "sync": sync_result}
            else:
                print("⚠️ 動作同步測試失敗")
                return {"status": "partial", "integration": result, "sync": sync_result}
        else:
            print(f"❌ MOV-ANI 連動測試失敗: {result.get('message', '未知錯誤')}")
            return result
            
    except Exception as e:
        print(f"❌ MOV-ANI 整合測試時發生錯誤: {str(e)}")
        return {"status": "error", "error": str(e)}

def test_behavior_modes(modules):
    """測試行為模式 - 第二步藍圖"""
    frontend = modules.get("frontend")
    if frontend is None:
        print("❌ Frontend 模組未載入")
        return None

    print("🎭 測試行為模式功能...")
    print("   這是藍圖第二步：不同行為模式的實現")
    
    behavior_modes = ["idle", "active", "listening", "thinking", "speaking"]
    results = {}
    
    try:
        for mode in behavior_modes:
            print(f"\n   測試行為模式: {mode}")
            
            result = frontend.handle({
                "action": "set_behavior_mode",
                "mode": mode,
                "duration": 2
            })
            
            if result and result.get("status") == "success":
                print(f"   ✅ {mode} 模式測試通過")
                results[mode] = "success"
            else:
                print(f"   ❌ {mode} 模式測試失敗")
                results[mode] = "failed"
            
            time.sleep(1)  # 短暫延遲
        
        success_count = sum(1 for status in results.values() if status == "success")
        total_count = len(behavior_modes)
        
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
    frontend = modules.get("frontend")
    if frontend is None:
        print("❌ Frontend 模組未載入")
        return None

    print("🔄 測試動畫狀態機...")
    print("   這是藍圖第三步：動畫狀態機的實現")
    
    # 定義狀態轉換測試序列
    state_transitions = [
        ("idle", "listening"),
        ("listening", "thinking"),
        ("thinking", "speaking"),
        ("speaking", "idle"),
        ("idle", "active"),
        ("active", "idle")
    ]
    
    results = []
    
    try:
        for from_state, to_state in state_transitions:
            print(f"\n   測試狀態轉換: {from_state} -> {to_state}")
            
            # 設置初始狀態
            init_result = frontend.handle({
                "action": "set_animation_state",
                "state": from_state
            })
            
            if init_result and init_result.get("status") == "success":
                # 執行狀態轉換
                transition_result = frontend.handle({
                    "action": "transition_to_state",
                    "target_state": to_state,
                    "transition_type": "smooth"
                })
                
                if transition_result and transition_result.get("status") == "success":
                    print(f"   ✅ 狀態轉換成功")
                    results.append({
                        "from": from_state,
                        "to": to_state,
                        "status": "success"
                    })
                else:
                    print(f"   ❌ 狀態轉換失敗")
                    results.append({
                        "from": from_state,
                        "to": to_state,
                        "status": "failed"
                    })
            else:
                print(f"   ❌ 初始狀態設置失敗")
                results.append({
                    "from": from_state,
                    "to": to_state,
                    "status": "init_failed"
                })
            
            time.sleep(1)  # 短暫延遲
        
        success_count = sum(1 for r in results if r["status"] == "success")
        total_count = len(state_transitions)
        
        print(f"\n📊 狀態機測試總結: {success_count}/{total_count} 轉換成功")
        
        if success_count == total_count:
            print("✅ 動畫狀態機測試完全通過")
            return {"status": "success", "transitions": results}
        elif success_count > total_count // 2:
            print("⚠️ 動畫狀態機部分功能正常")
            return {"status": "partial", "transitions": results}
        else:
            print("❌ 動畫狀態機測試主要失敗")
            return {"status": "failed", "transitions": results}
            
    except Exception as e:
        print(f"❌ 動畫狀態機測試時發生錯誤: {str(e)}")
        return {"status": "error", "error": str(e)}

def frontend_test_full(modules):
    """Frontend 完整測試 - 第四步藍圖"""
    frontend = modules.get("frontend")
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
        
        # 第四步：Core 整合測試
        print("\n🏗️ 執行 Core 整合測試...")
        core_result = frontend.handle({
            "action": "test_core_integration",
            "test_components": ["stt", "nlp", "llm", "tts"],
            "integration_type": "full"
        })
        
        if core_result and core_result.get("status") == "success":
            print("✅ Core 整合測試通過")
            test_results["core_integration"] = core_result
        else:
            print("❌ Core 整合測試失敗")
            test_results["core_integration"] = core_result
        
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
    frontend = modules.get("frontend")
    if frontend is None:
        print("❌ Frontend 模組未載入")
        return None

    try:
        result = frontend.handle({
            "action": "get_status"
        })
        
        if result and result.get("status") == "success":
            data = result.get("data", {})
            print("📊 Frontend 模組狀態:")
            print(f"   模組狀態: {data.get('module_status', '未知')}")
            print(f"   桌面寵物: {data.get('pet_status', '未知')}")
            print(f"   當前動畫: {data.get('current_animation', '未知')}")
            print(f"   行為模式: {data.get('behavior_mode', '未知')}")
            print(f"   MOV 狀態: {data.get('mov_status', '未知')}")
            print(f"   ANI 狀態: {data.get('ani_status', '未知')}")
            return result
        else:
            print("❌ 無法獲取 Frontend 狀態")
            return result
            
    except Exception as e:
        print(f"❌ 獲取 Frontend 狀態時發生錯誤: {str(e)}")
        return {"status": "error", "error": str(e)}

def frontend_test_animations(modules):
    """測試各種動畫效果"""
    frontend = modules.get("frontend")
    if frontend is None:
        print("❌ Frontend 模組未載入")
        return None

    animations = ["idle", "wave", "dance", "jump", "sleep", "excited", "confused"]
    
    print("🎨 測試動畫效果...")
    
    results = {}
    
    try:
        for animation in animations:
            print(f"\n   測試動畫: {animation}")
            
            result = frontend.handle({
                "action": "play_animation",
                "animation": animation,
                "duration": 2
            })
            
            if result and result.get("status") == "success":
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
    frontend = modules.get("frontend")
    if frontend is None:
        print("❌ Frontend 模組未載入")
        return None

    print("👆 測試用戶交互功能...")
    
    interaction_tests = [
        {"type": "click", "action": "pet_click"},
        {"type": "drag", "action": "pet_drag"},
        {"type": "hover", "action": "pet_hover"},
        {"type": "double_click", "action": "pet_double_click"},
        {"type": "right_click", "action": "pet_context_menu"}
    ]
    
    results = {}
    
    try:
        for test in interaction_tests:
            interaction_type = test["type"]
            action = test["action"]
            
            print(f"\n   測試交互: {interaction_type}")
            
            result = frontend.handle({
                "action": "test_interaction",
                "interaction_type": interaction_type,
                "test_action": action
            })
            
            if result and result.get("status") == "success":
                print(f"   ✅ {interaction_type} 交互測試成功")
                results[interaction_type] = "success"
            else:
                print(f"   ❌ {interaction_type} 交互測試失敗")
                results[interaction_type] = "failed"
        
        success_count = sum(1 for status in results.values() if status == "success")
        total_count = len(interaction_tests)
        
        print(f"\n📊 交互測試總結: {success_count}/{total_count} 成功")
        
        return {
            "status": "success" if success_count == total_count else "partial" if success_count > 0 else "failed",
            "results": results,
            "success_rate": (success_count / total_count) * 100
        }
        
    except Exception as e:
        print(f"❌ 用戶交互測試時發生錯誤: {str(e)}")
        return {"status": "error", "error": str(e)}
