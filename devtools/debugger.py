import core.controller as controller
from utils.debug_helper import debug_log

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
        match user_input:
            case "stt":
                debug_log(1, "STT 模組測試")
                print("<STT 模組測試>\n")
                choice = input("請選擇測試模式 (1: 單次測試, 2: 連續測試): \n\n> ")
                if choice == "1":
                    controller.stt_test_single()
                elif choice == "2":
                    controller.stt_test_realtime()
            case "nlp":
                debug_log(1, "NLP 模組測試")
                print("<NLP 模組測試>\n")
                print("請輸入測試文本 (或輸入 'exit' 來結束):")
                while True:
                    text = input("\n> ")
                    if text.lower() == "exit":
                        break
                    print()
                    controller.nlp_test(text)
            case "stt+nlp":
                debug_log(1, "TTS + NLP 模組整合測試")
                print("<TTS + NLP 模組測試>\n")
                controller.integration_test_StN()
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
                print("\n離開測試介面")
                break
            case _:
                print("無效的選擇，請再試一次。")
        print("\n==========================\n")