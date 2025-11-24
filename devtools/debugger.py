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
                choice = input("請選擇欲測試之功能 (1: 記憶查詢, 2: 對話快照查詢, 3: 身份統計, 4: 寫入並查詢測試, exit: 離開): \n\n> ")
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
                    print("身份統計測試 (使用 Mock 身份):")
                    print()
                    controller.mem_test_identity_stats("test_user")
                elif choice == "4":
                    print("寫入並查詢測試 (使用 Mock 身份):")
                    print()
                    controller.mem_test_write_then_query("test_user")
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
                                     "3: 學習引擎測試\n" +
                                     "4: 狀況變動測試\n" +
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
                        print("🧠 執行學習引擎測試...")
                        controller.llm_test_learning_engine()
                        
                    elif llm_choice == "4":
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
            case "sys":
                if not mod_list['sys']:
                    info_log("SYS 模組未啟用，請檢查配置。", "WARNING")
                    print("==========================\n")
                    continue                
                
                print("<SYS 模組工作流測試>\n")
                print("=== 工作流測試分類 ===")
                print("1. 測試工作流 (4)")
                print("2. 工作流管理 (4)")
                print("3. 列出所有可用工作流")
                print("\nhelp: 顯示詳細說明")
                print("exit: 離開\n")
                
                choice = input("> ")
                
                match choice:
                    case "1":  # 測試工作流
                        print("\n<測試工作流>")
                        sub = input("請選擇:\n1: Echo (簡單回顯)\n2: Countdown (倒數計時)\n3: Data Collector (資料收集)\n4: Random Fail (隨機失敗)\nexit: 返回\n\n> ")
                        match sub:
                            case "1":
                                controller.sys_test_echo_wrapper()
                            case "2":
                                controller.sys_test_countdown_wrapper()
                            case "3":
                                controller.sys_test_data_collector_wrapper()
                            case "4":
                                controller.sys_test_random_fail_wrapper()
                            case s if s.lower() in ["exit", "e", "quit", "q", "back", "b"]:
                                pass
                            case _:
                                print("\033[31m無效的選擇，請再試一次。\033[0m")

                    case "help" | "h":
                        print("\n=== SYS 工作流測試說明 ===")
                        print("\n【測試工作流】- 基礎功能測試")
                        print("  Echo: 簡單的輸入輸出回顯測試")
                        print("  Countdown: 倒數計時器測試")
                        print("  Data Collector: 多步驟資料收集測試")
                        print("  Random Fail: 錯誤處理與重試機制測試")
                    
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
                                            "1: 所有檔案工作流測試 (處理層 WORK 狀態)\n" +
                                            "2: drop_and_read 工作流\n" +
                                            "3: intelligent_archive 工作流\n" +
                                            "4: summarize_tag 工作流\n" +
                                            "back: 返回上級\n\n> ")
                    
                    if integration_choice == "1":
                        print("\n[測試] 所有檔案工作流整合測試")
                        from devtools.debug_api import integration_test_all
                        integration_test_all()
                    elif integration_choice == "2":
                        print("\n[測試] drop_and_read 工作流")
                        from devtools.debug_api import integration_test_file1
                        integration_test_file1()
                    elif integration_choice == "3":
                        print("\n[測試] intelligent_archive 工作流")
                        from devtools.debug_api import integration_test_file2
                        integration_test_file2()
                    elif integration_choice == "4":
                        print("\n[測試] summarize_tag 工作流")
                        from devtools.debug_api import integration_test_file3
                        integration_test_file3()
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
                print("⚠️  額外功能測試已移除，相關測試請使用整合測試選單")
                print("💡 使用 'int' 命令進入整合測試套件\n")
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