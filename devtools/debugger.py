import devtools.debug_api as controller
from utils.debug_helper import debug_log, debug_log_e, info_log, error_log
from configs.config_loader import load_config
import asyncio
import time

config = load_config()

module_enabled = config.get("modules_enabled", {})
module_refactored = config.get("modules_refactored", {})

mod_list = {"stt": (module_enabled.get("stt_module", False), module_refactored.get("stt_module", False)),
            "nlp": (module_enabled.get("nlp_module", False), module_refactored.get("nlp_module", False)),
            "mem": (module_enabled.get("mem_module", False), module_refactored.get("mem_module", False)),
            "llm": (module_enabled.get("llm_module", False), module_refactored.get("llm_module", False)),
            "tts": (module_enabled.get("tts_module", False), module_refactored.get("tts_module", False)),
            "sys": (module_enabled.get("sys_module", False), module_refactored.get("sys_module", False)),
            # 前端模組
            "ui": (module_enabled.get("ui_module", False), module_refactored.get("ui_module", False)),
            "ani": (module_enabled.get("ani_module", False), module_refactored.get("ani_module", False)),
            "mov": (module_enabled.get("mov_module", False), module_refactored.get("mov_module", False))}

def handle_module_integration(user_input):
    """
    處理模組整合測試輸入
    支援新版架構的整合測試
    
    輸入格式:
    - stt+nlp: STT + NLP 整合測試
    - nlp+mem: NLP + MEM 整合測試  
    - pipeline 或 all: 完整管道測試
    - debug: 使用除錯模式
    - production: 使用生產模式
    """
    
    # 處理模式選擇
    use_production = False
    if "production" in user_input:
        use_production = True
        user_input = user_input.replace("production", "").strip()
    elif "debug" in user_input:
        use_production = False
        user_input = user_input.replace("debug", "").strip()
    
    # 處理特殊關鍵字
    if user_input in ["pipeline", "all"]:
        debug_log(1, f"執行完整管道測試 ({'生產模式' if use_production else '除錯模式'})")
        if hasattr(controller, "test_full_pipeline_production" if use_production else "test_full_pipeline_debug"):
            func = getattr(controller, "test_full_pipeline_production" if use_production else "test_full_pipeline_debug")
            func()
        else:
            print("\033[31m完整管道測試功能不可用\033[0m")
        return
    
    # 解析模組組合
    if "+" not in user_input:
        print("\033[31m請使用 + 號連接模組 (例如: stt+nlp)，或使用 'pipeline'/'all' 進行完整測試\033[0m")
        return
    
    modules = [m.strip() for m in user_input.split("+")]
    
    # 驗證模組名稱
    valid_modules = ["stt", "nlp", "mem", "llm", "tts", "sys"]
    invalid_modules = [m for m in modules if m not in valid_modules]
    if invalid_modules:
        print(f"\033[31m無效的模組名稱：{', '.join(invalid_modules)}，請確認拼字。\033[0m")
        print(f"有效的模組名稱：{', '.join(valid_modules)}")
        return
    
    # 生成測試函數名稱 (使用新版命名規則)
    code_map = {
        "stt": "S",
        "nlp": "N", 
        "mem": "M",
        "llm": "L",
        "tts": "T",
        "sys": "Y"
    }
    
    execution_order = ["stt", "nlp", "mem", "llm", "tts", "sys"]
    
    try:
        # 排序以保證一致性
        normalized = sorted(modules, key=lambda m: execution_order.index(m))
        code = "".join(code_map[m] for m in normalized)
        
        # 先嘗試使用新版整合測試
        new_func_name = None
        
        # 特殊映射：直接對應到新版測試函數
        # 注意：目前只有 STT+NLP 整合測試可用，其他將在模組重構後添加
        direct_mappings = {
            "SN": "integration_test_SN"
        }
        
        if code in direct_mappings:
            new_func_name = direct_mappings[code]
            if hasattr(controller, new_func_name):
                debug_log(1, f"執行新版整合測試：{'+'.join(normalized)} ({'生產模式' if use_production else '除錯模式'})")
                func = getattr(controller, new_func_name)
                func(production_mode=use_production)
                return
        
        # 這裡不再嘗試使用舊版整合測試函數
        print(f"\033[33m模組整合測試 '{'+'.join(normalized)}' 尚未實作或尚未重構。\033[0m")
        print("目前僅有 STT+NLP 整合測試已經完成重構")
        
        # 提供可用的替代方案
        if len(available_tests := ["stt+nlp"]) > 0:
            print(f"可用的整合測試: {', '.join(available_tests)}")
        
        if available_tests:
            print(f"\033[32m可用的整合測試：{', '.join(available_tests)}\033[0m")
        
        # 建議分別測試
        print(f"\033[36m建議分別測試各模組，或使用 'pipeline' 進行完整測試\033[0m")
        
    except (KeyError, ValueError) as e:
        print(f"\033[31m處理模組組合時發生錯誤：{e}\033[0m")

def colorful_text(text : str, enabled : tuple=(False, False)):
    return '\033[32m' + text + '\033[0m' if enabled[1] and enabled[0] else '\033[33m' + text + ' (待重構)\033[0m' if enabled[0] else '\033[31m' + text + '\033[0m'

def debug_interactive():
    print("==========================\n\n歡迎來到U.E.P模組測試介面!\n\n==========================\n")
    while True:
        # 組織模組選單（避免連續使用字符串拼接可能導致的格式問題）
        menu_items = [
            f"{colorful_text('stt - 語音轉文字模組;', mod_list['stt'])}",
            f"{colorful_text('nlp - 自然語言分析模組;', mod_list['nlp'])}",
            f"{colorful_text('mem - 記憶存取模組;', mod_list['mem'])}",
            f"{colorful_text('llm - 大型語言模型模組;', mod_list['llm'])}",
            f"{colorful_text('tts - 文字轉語音模組;', mod_list['tts'])}",
            f"{colorful_text('sys - 系統功能模組;', mod_list['sys'])}",
            f"{colorful_text('int - 整合測試套件;', (True, True))}",
            f"{colorful_text('ex - 額外功能測試;', (True, True))}"
        ]
        
        menu_text = "請選擇想要測試的模組 (綠色: 已重構、黃色: 已啟用、紅色: 未啟用):\n\n"
        menu_text += "\n\n".join(menu_items)
        menu_text += "\n\n(用 exit 來離開，用 gui 切換到圖形介面): \n\n> "
        
        user_input = input(menu_text)
        print("\n==========================\n")
        match user_input.lower().strip():
            case "stt":
                if not mod_list['stt']:
                    info_log("STT 模組未啟用，請檢查配置。", "WARNING")
                    print("==========================\n")
                    continue

                debug_log(1, "STT 模組測試")
                print("<STT 模組測試>\n")
                
                choice = input("請選擇測試模式:\n" +
                             "1: 基本測試\n" + 
                             "2: 持續背景監聽\n" +
                             "3: 說話人管理\n" +
                             "4: 統計信息\n" +
                             "exit: 離開\n\n> ")
                
                if choice == "1":
                    print("開始 STT 基本測試...")
                    controller.stt_test_single()
                
                elif choice == "2":
                    print("開始持續背景監聽測試...")
                    controller.stt_test_continuous_listening()
                
                elif choice == "3":
                    # 說話人管理子菜單
                    while True:
                        speaker_choice = input("\n說話人管理:\n" +
                                             "1: 列出所有說話人\n" +
                                             "2: 重新命名說話人\n" +
                                             "3: 刪除說話人\n" +
                                             "4: 清空所有說話人\n" +
                                             "5: 備份說話人數據\n" +
                                             "6: 恢復說話人數據\n" +
                                             "7: 資料庫詳細信息\n" +
                                             "8: 調整相似度閾值\n" +
                                             "back: 返回上級\n\n> ")
                        
                        if speaker_choice == "1":
                            controller.stt_speaker_list()
                        
                        elif speaker_choice == "2":
                            old_id = input("輸入要重新命名的說話人 ID: ")
                            new_id = input("輸入新的說話人 ID: ")
                            controller.stt_speaker_rename(old_id, new_id)
                        
                        elif speaker_choice == "3":
                            speaker_id = input("輸入要刪除的說話人 ID: ")
                            controller.stt_speaker_delete(speaker_id)
                        
                        elif speaker_choice == "4":
                            controller.stt_speaker_clear_all()
                        
                        elif speaker_choice == "5":
                            controller.stt_speaker_backup()
                        
                        elif speaker_choice == "6":
                            controller.stt_speaker_restore()
                        
                        elif speaker_choice == "7":
                            controller.stt_speaker_info()
                        
                        elif speaker_choice == "8":
                            controller.stt_speaker_adjust_threshold()
                        
                        elif speaker_choice.lower() in ["exit", "e", "back", "b", "quit", "q"]:
                            break
                        else:
                            print("\033[31m無效的選擇，請再試一次。\033[0m")
                
                elif choice == "4":
                    print("📊 獲取 STT 統計信息...")
                    controller.stt_get_stats()
                
                elif choice in ["exit", "e", "back", "b", "quit", "q"]:
                    pass
                else:
                    print("\033[31m無效的選擇，請再試一次。\033[0m")
            case "nlp":
                if not mod_list['nlp']:
                    info_log("NLP 模組未啟用，請檢查配置。", "WARNING")
                    print("==========================\n")
                    continue

                debug_log(1, "NLP 模組測試")
                print("<NLP 模組測試>\n")
                
                # NLP子選單
                while True:
                    nlp_choice = input("\n選擇測試功能:\n" +
                                     "1: 增強版意圖分析 (包含語者身份)\n" +
                                     "2: 多意圖上下文管理測試\n" +
                                     "3: 語者身份管理測試\n" +
                                     "4: 上下文佇列分析\n" +
                                     "5: 清空所有上下文\n" +
                                     "back: 返回上級\n\n> ")
                    
                    if nlp_choice == "1":

                        enable_identity = input("啟用語者身份處理? (y/n, 默認y): ").lower() != 'n'
                        enable_segmentation = input("啟用意圖分段? (y/n, 默認y): ").lower() != 'n'
                        print("請輸入測試文本 (留空使用默認) (或輸入 'exit' 來結束):")

                        while True:
                            text = input("\n> ")
                            if text.lower() in ["exit", "e", "quit", "q", "back", "b"]:
                                break
                            print()
                            controller.nlp_test(text, enable_identity, enable_segmentation)
                    
                    elif nlp_choice == "2":
                        print("輸入多意圖測試文本 (留空使用默認): ")
                        
                        while True:
                            text = input("\n> ")
                            if text.lower() in ["exit", "e", "quit", "q", "back", "b"]:
                                break
                            print()
                            controller.nlp_test_multi_intent(text)
                    
                    elif nlp_choice == "3":
                        speaker_id = input("輸入語者ID (留空使用默認): ") or "test_user"
                        controller.nlp_test_identity_management(speaker_id)
                    
                    elif nlp_choice == "4":
                        controller.nlp_analyze_context_queue()
                    
                    elif nlp_choice == "5":
                        controller.nlp_clear_contexts()
                    
                    elif nlp_choice.lower() in ["exit", "e", "back", "b", "quit", "q"]:
                        break
                    else:
                        print("\033[31m無效的選擇，請再試一次。\033[0m")
            case "mem":
                if not mod_list['mem']:
                    info_log("MEM 模組未啟用，請檢查配置。", "WARNING")
                    print("==========================\n")
                    continue

                debug_log(1, "MEM 模組測試")
                print("<MEM 模組測試>\n")
                choice = input("請選擇欲測試之功能 (1: 記憶查詢, 2: 對話快照查詢, 3: 記憶庫列表, 4: 記憶統計, exit: 離開): \n\n> ")
                if choice == "1":
                    print("記憶查詢測試 (使用 Mock 身份) (或輸入 'exit' 來結束):")
                    while True:
                        query_text = input("\n查詢文字 (預設 天氣): \n> ") or "天氣"
                        if query_text.lower() in ["exit", "e", "quit", "q", "back", "b"]:
                            info_log("使用者中斷測試")
                            break

                        print()
                        controller.mem_test_memory_query("test_user", query_text)
                elif choice == "2":
                    print("對話快照查詢測試 (使用 Mock 身份) (或輸入 'exit' 來結束):")
                    while True:
                        conversation = input("\n對話內容 (預設 你好，今天天氣如何？): \n> ") or "你好，今天天氣如何？"
                        if conversation.lower() in ["exit", "e", "quit", "q", "back", "b"]:
                            info_log("使用者中斷測試")
                            break
                        
                        print()
                        controller.mem_test_conversation_snapshot("test_user", conversation)
                elif choice == "3":
                    print("記憶庫列表測試 (使用預設記憶令牌):")
                    print()
                    controller.mem_test_memory_access_control("mem_token_debug_2024")
                elif choice == "4":
                    print("記憶統計測試 (使用 Mock 身份):")
                    print()
                    controller.mem_test_identity_manager_stats("test_user")
                elif choice in ["exit", "e", "quit", "q", "back", "b"]:
                    pass
                else:
                    print("\033[31m無效的選擇，請再試一次。\033[0m")
            case "llm":
                if not mod_list['llm']:
                    info_log("LLM 模組未啟用，請檢查配置。", "WARNING")
                    print("==========================\n")
                    continue
                debug_log(1, "LLM 模組測試")
                print("<LLM 模組測試>\n")

                # LLM 子選單
                while True:
                    llm_choice = input("\n選擇測試功能:\n" +
                                     "1: 聊天對話測試 (CHAT 模式)\n" +
                                     "2: 指令分析測試 (WORK 模式)\n" +
                                     "3: 快取功能測試 (暫時停用)\n" +
                                     "4: 學習引擎測試\n" +
                                     "5: 狀況變動測試\n" +
                                     "back: 返回上級\n\n> ")
                    
                    if llm_choice == "1":
                        print("🗣️ 聊天對話測試 (或輸入 'exit' 來結束):")
                        while True:
                            text = input("\n> ")
                            if text.lower() in ["exit", "e", "quit", "q", "back", "b"]:
                                info_log("使用者中斷測試")
                                break
                            print()
                            controller.llm_test_chat(text)
                    
                    elif llm_choice == "2":
                        print("🔧 指令分析測試 (或輸入 'exit' 來結束):")
                        while True:
                            text = input("\n> ")
                            if text.lower() in ["exit", "e", "quit", "q", "back", "b"]:
                                info_log("使用者中斷測試")
                                break
                            print()
                            controller.llm_test_command(text)
                    
                    elif llm_choice == "3":
                        print("⚠️ 快取功能測試暫時停用")
                        pass
                        print("🗄️ 執行快取功能測試...")
                        controller.llm_test_cache_functionality()
                    
                    elif llm_choice == "4":
                        print("🧠 執行學習引擎測試...")
                        controller.llm_test_learning_engine()
                        
                    elif llm_choice == "5":
                        print("🔄 執行狀況變動測試...")
                        controller.llm_test_system_status_monitoring()
                    
                    elif llm_choice.lower() in ["exit", "e", "back", "b", "quit", "q"]:
                        break
                    else:
                        print("\033[31m無效的選擇，請再試一次。\033[0m")
            case "tts":
                if not mod_list['tts']:
                    info_log("TTS 模組未啟用，請檢查配置。", "WARNING")
                    print("==========================\n")
                    continue

                print("<TTS 模組測試>\n")
                
                # TTS 測試子選單
                while True:
                    tts_choice = input("\n選擇測試功能:\n" +
                                     "1: TTS 即時合成測試 (連續輸入文本和情緒)\n" +
                                     "2: 情感變化測試 (同一文本,不同情緒)\n" +
                                     "3: 串流測試 (長文本分段)\n" +
                                     "back: 返回上級\n\n> ")
                    
                    if tts_choice == "1":
                        print("\n🎙️  開始 TTS 即時合成測試...")
                        controller.tts_interactive_synthesis()
                    
                    elif tts_choice == "2":
                        print("\n🎭 開始情感變化測試...")
                        controller.tts_emotion_variation_test()
                    
                    elif tts_choice == "3":
                        print("\n📡 開始串流測試...")
                        controller.tts_streaming_test()
                    
                    elif tts_choice.lower() in ["exit", "e", "back", "b", "quit", "q"]:
                        break
                    else:
                        print("\033[31m無效的選擇，請再試一次。\033[0m")
                else:
                    print("\033[31m無效的選擇，請再試一次。\033[0m")
            case "sys":
                if not mod_list['sys']:
                    info_log("SYS 模組未啟用，請檢查配置。", "WARNING")
                    print("==========================\n")
                    continue                
                
                print("<SYS 模組測試>\n")
                choice = input("請選擇欲測試之功能 (1: 檔案互動功能, 2: 測試工作流程, help: 列出所有功能以及其參數, exit: 離開): \n\n> ")
                
                match choice:
                    case "1":
                        sub = input("請選擇欲測試之子功能 (1-3: 工作流程模式, exit: 離開):\n1: 檔案讀取工作流程, 2: 智慧歸檔工作流程, 3: 摘要標籤工作流程\n\n> ")
                        # Test if sub is not a number or "exit"
                        if sub in ["1", "2", "3"]:
                            controller.sys_test_functions(mode=1, sub=int(sub))
                        elif sub.lower() in ["exit", "e", "quit", "q", "back", "b"]:
                            break
                        else:
                            print("\033[31m無效的選擇，請再試一次。\033[0m")
                    case "2":
                        sub = input("請選擇欲測試之工作流程 (1: 簡單回顯, 2: 倒數計時, 3: 資料收集, 4: 隨機失敗, 5: TTS工作流測試, exit: 離開): \n\n> ")
                        if sub in ["1", "2", "3", "4", "5"]:
                            controller.sys_test_workflows(workflow_type=int(sub))
                        elif sub.lower() in ["exit", "e", "quit", "q", "back", "b"]:
                            break
                        else:
                            print("\033[31m無效的選擇，請再試一次。\033[0m")
                    case "help" | "h":
                        controller.sys_list_functions()
                        print("\n=== 測試工作流程選項 ===")
                        controller.sys_list_test_workflows()
                    case "exit" | "e" | "quit" | "q":
                        pass
                    case _:
                        print("\033[31m無效的選擇，請再試一次。\033[0m")
            case "int":
                debug_log(1, "整合測試套件")
                print("<整合測試套件>\n")
                
                # 整合測試子選單
                while True:
                    integration_choice = input("請選擇整合測試:\n" +
                                            "1: STT + NLP 整合測試 (可用)\n" +
                                            "2: NLP + MEM 整合測試 (未重構)\n" +
                                            "3: NLP + LLM 整合測試 (未重構)\n" +
                                            "4: LLM + TTS 整合測試 (未重構)\n" +
                                            "5: 完整管道測試 (未重構)\n" +
                                            "back: 返回上級\n\n> ")
                    
                    if integration_choice == "1":
                        print("\n[測試] STT + NLP 整合")
                        handle_module_integration("stt+nlp")
                    elif integration_choice == "2":
                        print("\n[⚠️] NLP + MEM 整合測試尚未重構")
                        print("僅有 STT 和 NLP 模組已完成重構")
                    elif integration_choice == "3":
                        print("\n[⚠️] NLP + LLM 整合測試尚未重構")
                        print("僅有 STT 和 NLP 模組已完成重構")
                    elif integration_choice == "4":
                        print("\n[⚠️] LLM + TTS 整合測試尚未重構")
                        print("僅有 STT 和 NLP 模組已完成重構")
                    elif integration_choice == "5":
                        print("\n[⚠️] 完整管道測試尚未重構")
                        print("僅有 STT 和 NLP 模組已完成重構")
                    elif integration_choice.lower() in ["exit", "e", "back", "b", "quit", "q"]:
                        break
                    else:
                        print("\033[31m無效的選擇，請再試一次。\033[0m")
                    
                    # 詢問測試模式
                    def get_test_mode():
                        mode_choice = input("選擇測試模式 (1: 除錯模式, 2: 生產模式, 預設: 除錯): ")
                        return mode_choice == "2"
                    
                    if integration_choice == "1":
                        use_production = get_test_mode()
                        print(f"🧪 執行 STT + NLP 整合測試 ({'生產模式' if use_production else '除錯模式'})")
                        controller.test_stt_nlp_v2(production_mode=use_production)
                    
                    elif integration_choice == "2":
                        print("\n[⚠️] NLP + MEM 整合測試尚未重構")
                        print("僅有 STT 和 NLP 模組已完成重構")

                    elif integration_choice == "3":
                        print("\n[⚠️] NLP + LLM 整合測試尚未重構")
                        print("僅有 STT 和 NLP 模組已完成重構")
                    
                    elif integration_choice == "4":
                        print("\n[⚠️] LLM + TTS 整合測試尚未重構")
                        print("僅有 STT 和 NLP 模組已完成重構")
                    
                    elif integration_choice == "5":
                        print("\n[⚠️] 完整管道測試尚未重構")
                        print("僅有 STT 和 NLP 模組已完成重構")
                    
                    elif integration_choice == "6":
                        print("\n[⚠️] 所有整合測試尚未重構")
                        print("僅有 STT 和 NLP 模組已完成重構")
                        print("請使用 STT+NLP 整合測試")
                    
                    elif integration_choice == "7":
                        print("輸入模組組合 (例如: stt+nlp, nlp+mem+llm):")
                        print("可用模組: stt, nlp, mem, llm, tts, sys")
                        custom_input = input("\n模組組合> ")
                        if custom_input and custom_input.lower() not in ["exit", "e", "quit", "q", "back", "b"]:
                            use_production = get_test_mode()
                            mode_suffix = " production" if use_production else " debug"
                            handle_module_integration(custom_input + mode_suffix)
                    
                    elif integration_choice.lower() == "mode":
                        print("當前版本支援兩種測試模式:")
                        print("🔧 除錯模式 - 使用 UnifiedController，適合開發測試")
                        print("🚀 生產模式 - 使用 SystemInitializer + SystemLoop，模擬真實環境")
                        print("\n選擇測試項目時會提示選擇模式")
                    
                    elif integration_choice.lower() in ["exit", "e", "back", "b", "quit", "q"]:
                        break
                    else:
                        print("\033[31m無效的選擇，請再試一次。\033[0m")
            case "frontend":
                debug_log(1, "前端整合測試")
                print("<前端整合測試>\n")
                
                # 檢查是否在終端模式（預先載入模式），如果是則提示切換到GUI模式
                import devtools.debug_api as debug_api
                if hasattr(debug_api, 'PRELOAD_MODULES') and debug_api.PRELOAD_MODULES is True:
                    print("⚠️  注意：您目前在終端測試模式中")
                    print("🖥️  前端模組(UI/ANI/MOV)測試建議在圖形除錯介面中進行")
                    print("💡 使用 'gui' 命令切換到圖形介面，或重新啟動程式時使用 'python Entry.py --debug-gui'\n")
            case "ex":
                debug_log(1, "額外功能測試")
                print("<額外功能測試>\n")
                choice = input("請選擇欲進行測試 (1: 重點整理測試 (LLM), 2: 聊天測試 (STT+LLM+TTS), exit: 離開): \n\n> ")
                if choice == "1":
                    controller.test_summarize()
                elif choice == "2":
                    controller.test_chat()
                elif choice in ["exit", "e", "quit", "q", "back", "b"]:
                    break
                else:
                    print("\033[31m無效的選擇，請再試一次。\033[0m")
            case "exit" | "e" | "quit" | "q":
                debug_log(1, "離開測試介面")
                print("\n離開測試介面")
                break
            case "gui":
                debug_log(1, "切換到圖形除錯介面")
                print("\n🖥️ 正在啟動圖形除錯介面...")
                try:
                    # 設定為按需載入模式（GUI模式）
                    import devtools.debug_api as debug_api
                    debug_api.switch_to_gui_mode()
                    print("✅ 已切換為GUI模式（按需載入）")
                    
                    from modules.ui_module.debug import launch_debug_interface
                    print("圖形介面啟動中，請稍候...")
                    launch_debug_interface(prefer_gui=True, blocking=True)
                except KeyboardInterrupt:
                    print("\n⌨️ 圖形介面被用戶中斷")
                except ImportError as e:
                    print(f"❌ 無法載入圖形介面模組: {e}")
                    print("💡 提示：請確認 PyQt5 已正確安裝")
                except Exception as e:
                    print(f"❌ 圖形介面啟動失敗: {e}")
                
                # 返回終端時重新設定為預先載入模式並清理前端模組
                print("\n🔄 返回命令行介面...")
                print("🧹 正在清理前端模組實例...")
                try:
                    debug_api.switch_to_terminal_mode()
                    print("✅ 前端模組已清理")
                    print("✅ 已重新設定為終端模式（預先載入非UI模組）")
                except Exception as e:
                    print(f"⚠️  模式切換警告: {e}")
                
                # 提示用戶等待一下讓清理完成
                print("⏳ 請稍候，確保所有前端進程已完全關閉...")
                import time
                time.sleep(1)  # 給清理過程一點時間
            case _:
                n_input = user_input.lower()
                if "+" in n_input or n_input in ["pipeline", "all"]:
                    handle_module_integration(n_input)
                else:
                    print("\033[31m無效的選擇，請再試一次。\033[0m")
        print("\n==========================\n")