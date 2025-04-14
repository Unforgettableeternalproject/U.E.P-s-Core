import core.controller as controller
from utils.debug_helper import debug_log

def debug_interactive():
    print("\n==========================\n\n歡迎來到U.E.P模組測試介面!\n\n==========================\n")
    while True:
        user_input = input("請選擇想要測試的模組 (stt, nlp, mem, llm, tts, sys) (exit 來離開): \n> ")
        print("\n==========================")
        match user_input:
            case "stt":
                debug_log(1, "STT 模組測試")
                print("\n<STT 模組測試>\n")
                choice = input("請選擇測試模式 (1: 單次測試, 2: 連續測試): \n> ")
                if choice == "1":
                    controller.stt_test_single()
                elif choice == "2":
                    controller.stt_test_realtime()
            case "nlp":
                debug_log(1, "NLP 模組測試")
                print("\n<NLP 模組測試>\n")
                print("請輸入測試文本 (或輸入 'exit' 來結束):")
                while True:
                    text = input("> ")
                    if text.lower() == "exit":
                        break
                    print()
                    controller.nlp_test(text)
            case "mem":
                print("\n<MEM 模組測試>\n")
                print("目前還未實作 MEM 模組的測試功能")
            case "llm":
                print("\n<LLM 模組測試>\n")
                print("目前還未實作 LLM 模組的測試功能")
            case "tts":
                print("\n<TTS 模組測試>\n")
                print("目前還未實作 TTS 模組的測試功能")
            case "sys":
                print("\n<SYS 模組測試>\n")
                print("目前還未實作 SYS 模組的測試功能")
            case "exit":
                print("離開測試介面")
                break
            case _:
                print("無效的選擇，請再試一次。")
        print("\n==========================\n")