from logging import info
from urllib import response
import core.controller as controller
from utils.debug_helper import debug_log, info_log, error_log
from configs.config_loader import load_config

config = load_config()

module_enabled = config.get("modules_enabled", {})

import re

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


def debug_interactive():
    print("==========================\n\n歡迎來到U.E.P模組測試介面!\n\n==========================\n")
    while True:
        user_input = input("請選擇想要測試的模組:\n\n"+
                          "stt - 語音轉文字模組; " + 
                          "\n\nnlp - 自然語言分析模組; " +
                          "\n\nmem - 記憶存取模組; " +
                          "\n\nllm - 大型語言模型模組; " +
                          "\n\ntts - 文字轉語音模組; " +
                          "\n\nsys - 系統功能模組; "
                          "\n\n也可進行模組交叉測試 (使用+號來連接，例如stt+nlp)" +
                          "\n\n(用 exit 來離開): \n\n> ")
        print("\n==========================\n")
        match user_input.lower():
            case "stt":
                if not module_enabled.get("stt_module", False):
                    info_log("STT 模組未啟用，請檢查配置。", "WARNING")
                    print("\n==========================\n")
                    continue

                debug_log(1, "STT 模組測試")
                print("<STT 模組測試>\n")
                choice = input("請選擇測試模式 (1: 單次測試, 2: 連續測試): \n\n> ")
                if choice == "1":
                    controller.stt_test_single()
                elif choice == "2":
                    controller.stt_test_realtime()
            case "nlp":
                if not module_enabled.get("nlp_module", False):
                    info_log("NLP 模組未啟用，請檢查配置。", "WARNING")
                    print("\n==========================\n")
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
                if not module_enabled.get("mem_module", False):
                    info_log("MEM 模組未啟用，請檢查配置。", "WARNING")
                    print("\n==========================\n")
                    continue

                debug_log(1, "MEM 模組測試")
                print("<MEM 模組測試>\n")
                choice = input("請選擇欲測試之功能 (1: 記憶寫入, 2: 記憶查詢, 3: 記憶刪除): \n\n> ")
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
                else:
                    print("\033[31m無效的選擇，請再試一次。\033[0m")
            case "llm":
                if not module_enabled.get("llm_module", False):
                    info_log("LLM 模組未啟用，請檢查配置。", "WARNING")
                    print("\n==========================\n")
                    continue

                print("\n<LLM 模組測試>\n")
                print("目前還未實作 LLM 模組的測試功能")
            case "tts":
                if not module_enabled.get("tts_module", False):
                    info_log("TTS 模組未啟用，請檢查配置。", "WARNING")
                    print("\n==========================\n")
                    continue

                print("\n<TTS 模組測試>\n")
                print("目前還未實作 TTS 模組的測試功能")
            case "sys":
                if not module_enabled.get("sys_module", False):
                    info_log("SYS 模組未啟用，請檢查配置。", "WARNING")
                    print("\n==========================\n")
                    continue

                print("\n<SYS 模組測試>\n")
                print("目前還未實作 SYS 模組的測試功能")
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