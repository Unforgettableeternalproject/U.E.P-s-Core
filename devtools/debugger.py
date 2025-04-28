import core.controller as controller
from utils.debug_helper import debug_log, info_log, error_log
from configs.config_loader import load_config

config = load_config()

module_enabled = config.get("modules_enabled", {})

mod_list = {"stt": module_enabled.get("stt_module", False)
            , "nlp": module_enabled.get("nlp_module", False)
            , "mem": module_enabled.get("mem_module", False)
            , "llm": module_enabled.get("llm_module", False)
            , "tts": module_enabled.get("tts_module", False)
            , "sys": module_enabled.get("sys_module", False)}

def handle_module_integration(user_input):
    if user_input == "pipeline":
        if hasattr(controller, "pipeline_test"):
            controller.pipeline_test()
        else:
            print("\033[31m尚未實作完整流程 pipeline_test()\033[0m")
        return

    modules = user_input.split("+")

    code_map = {
        "stt": "S",
        "nlp": "N",
        "mem": "M",
        "llm": "L",
        "tts": "T",
        "sys": "Y"
    }

    execution_order = ["stt", "nlp", "mem", "llm", "tts", "sys", "mov"]

    try:
        # 排序以保證一致性
        normalized = sorted(modules, key=lambda m: execution_order.index(m))
        code = "".join(code_map[m] for m in normalized)
        func_name = f"integration_test_{code}"

        if hasattr(controller, func_name):
            debug_log(1, f"執行整合測試函式：{func_name}")
            getattr(controller, func_name)()
        else:
            print(f"\033[31m模組整合測試 {func_name} 尚未實作。\033[0m")
    except KeyError as e:
        print(f"\033[31m無效的模組名稱：{e.args[0]}，請確認拼字。\033[0m")

def colorful_text(text : str, enabled : bool = True):
    return '\033[32m' + text + '\033[0m' if enabled else '\033[31m' + text + '\033[0m'

def debug_interactive():
    print("==========================\n\n歡迎來到U.E.P模組測試介面!\n\n==========================\n")
    while True:
        user_input = input("請選擇想要測試的模組 (紅色標示表示未啟用):\n\n"+
                          f"{colorful_text('stt - 語音轉文字模組;', mod_list['stt'])}" + 
                          f"\n\n{colorful_text('nlp - 自然語言分析模組;', mod_list['nlp'])}" +
                          f"\n\n{colorful_text('mem - 記憶存取模組;', mod_list['mem'])}" +
                          f"\n\n{colorful_text('llm - 大型語言模型模組;', mod_list['llm'])}" +
                          f"\n\n{colorful_text('tts - 文字轉語音模組;', mod_list['tts'])}" + 
                          f"\n\n{colorful_text('sys - 系統功能模組;', mod_list['sys'])}" +
                          f"\n\n{colorful_text('ex - 額外功能測試;')}" +
                          "\n\n也可進行模組交叉測試 (使用+號來連接，例如stt+nlp)" +
                          "\n\n(用 exit 來離開): \n\n> ")
        print("\n==========================\n")
        match user_input.lower().strip():
            case "stt":
                if not mod_list['stt']:
                    info_log("STT 模組未啟用，請檢查配置。", "WARNING")
                    print("==========================\n")
                    continue

                debug_log(1, "STT 模組測試")
                print("<STT 模組測試>\n")
                choice = input("請選擇測試模式 (1: 單次測試, 2: 連續測試, exit: 離開): \n\n> ")
                if choice == "1":
                    controller.stt_test_single()
                elif choice == "2":
                    controller.stt_test_realtime()
                elif choice == "exit" or choice == "e":
                    break
                else:
                    print("\033[31m無效的選擇，請再試一次。\033[0m")
            case "nlp":
                if not mod_list['nlp']:
                    info_log("NLP 模組未啟用，請檢查配置。", "WARNING")
                    print("==========================\n")
                    continue

                debug_log(1, "NLP 模組測試")
                print("<NLP 模組測試>\n")
                print("請輸入測試文本 (或輸入 'exit' 來結束):")
                while True:
                    text = input("\n> ")
                    if text.lower() == "exit" or text.lower() == "e":
                        info_log("使用者中斷測試")
                        break
                    print()
                    controller.nlp_test(text)
            case "mem":
                if not mod_list['mem']:
                    info_log("MEM 模組未啟用，請檢查配置。", "WARNING")
                    print("==========================\n")
                    continue

                debug_log(1, "MEM 模組測試")
                print("<MEM 模組測試>\n")
                choice = input("請選擇欲測試之功能 (1: 記憶寫入, 2: 記憶查詢, 3: 記憶刪除, 4: 列出所有記憶, exit: 離開): \n\n> ")
                if choice == "1":
                    print("請輸入要寫入的記憶內容 (或輸入 'exit' 來結束):")
                    while True:
                        user_text = input("\n輸入使用者對話: \n> ")
                        if user_text.lower() == "exit" or user_text.lower() == "e":
                            info_log("使用者中斷測試")
                            break

                        response_text = input("\n輸入系統回應: \n> ")
                        if response_text.lower() == "exit" or response_text.lower() == "e":
                            info_log("使用者中斷測試")
                            break

                        print()
                        controller.mem_store_test(user_text, response_text)
                elif choice == "2":
                    print("請輸入查詢的記憶內容 (或輸入 'exit' 來結束):")
                    while True:
                        text = input("\n> ")
                        if text.lower() == "exit" or text.lower() == "e":
                            info_log("使用者中斷測試")
                            break
                        print()
                        controller.mem_fetch_test(text)
                elif choice == "3":
                    print("請輸入要刪除的記憶內容 (或輸入 'exit' 來結束):")
                    while True:
                        text = input("記憶關鍵語句:\n> ")
                        if text.lower() == "exit" or text.lower() == "e":
                            info_log("使用者中斷測試")
                            break

                        topk = input("要刪除的相似記憶數量 (預設為 1):\n> ")
                        if topk.lower() == "exit" or topk.lower() == "e":
                            info_log("使用者中斷測試")
                            break
                        controller.mem_clear_test(text, topk)
                elif choice == "4":
                    print("列出所有記憶 (選擇查詢頁面，或輸入 'exit' 來結束):")
                    while True:
                        page = input("\n頁面 (預設為 1):\n> ")
                        if page == "":
                            page = 1
                        elif page.lower() == "exit" or page.lower() == "e":
                            info_log("使用者中斷測試")
                            break
                        else:
                            try:
                                page = int(page)
                            except ValueError:
                                print("\033[31m請輸入有效的頁碼。\033[0m")
                                continue
                        controller.mem_list_all_test(page)
                elif choice == "exit" or choice == "e":
                    break
                else:
                    print("\033[31m無效的選擇，請再試一次。\033[0m")
            case "llm":
                if not mod_list['llm']:
                    info_log("LLM 模組未啟用，請檢查配置。", "WARNING")
                    print("==========================\n")
                    continue
                debug_log(1, "LLM 模組測試")
                print("<LLM 模組測試>\n")

                choice = input("請選擇測試模式 (1: 聊天測試, 2: 指令測試, exit: 離開): \n\n> ")
                if choice == "1":
                    print("🗣️ 請輸入一段對話文字 (必須用英文) (或輸入 'exit' 來結束):")
                    while True:
                        text = input("\n> ")
                        if text.lower() == "exit" or text.lower() == "e":
                            info_log("使用者中斷測試")
                            break
                        print()
                        controller.llm_test_chat(text)
                elif choice == "2":
                    info_log("指令測試尚未實作", "WARNING")
                elif choice == "exit" or choice == "e":
                    break
                else:
                    print("\033[31m無效的選擇，請再試一次。\033[0m")
            case "tts":
                if not mod_list['tts']:
                    info_log("TTS 模組未啟用，請檢查配置。", "WARNING")
                    print("==========================\n")
                    continue

                print("<TTS 模組測試>\n")
                
                while True:
                    text = input("\n請輸入要轉換的文字 (或輸入 'exit' 來結束):\n\n> ")
                    if text.lower() == "exit" or text.lower() == "e":
                        info_log("使用者中斷測試")
                        break
                    mood = input("\n請輸入情緒 (預設為 neutral):\n\n> ")
                    if mood.lower() == "exit" or mood.lower() == "e":
                        info_log("\n使用者中斷測試")
                        break
                    elif mood == "":
                        mood = None
                    else:
                        mood = mood.strip()

                    save = input("\n是否儲存音檔 (y/n)? (預設為 n):\n\n> ")
                    if save.lower() == "exit" or save.lower() == "e":
                        info_log("使用者中斷測試")
                        break
                    else:
                        save = True if save.lower() == "y" else False

                    controller.tts_test(text, mood, save)
            case "sys":
                if not mod_list['sys']:
                    info_log("SYS 模組未啟用，請檢查配置。", "WARNING")
                    print("==========================\n")
                    continue

                print("<SYS 模組測試>\n")
                print("目前還未實作 SYS 模組的測試功能")
            case "ex":
                debug_log(1, "額外功能測試")
                print("<額外功能測試>\n")
                choice = input("請選擇欲進行測試 (1: 重點整理測試 (LLM), exit: 離開): \n\n> ")
                if choice == "1":
                    controller.test_summrize()
                elif choice == "exit" or choice == "e":
                    break
                else:
                    print("\033[31m無效的選擇，請再試一次。\033[0m")
            case "exit" | "e":
                debug_log(1, "離開測試介面")
                print("\n離開測試介面")
                break
            case _:
                n_input = user_input.lower()
                if "+" in n_input or n_input == "pipeline":
                    handle_module_integration(n_input)
                else:
                    print("\033[31m無效的選擇，請再試一次。\033[0m")
        print("\n==========================\n")