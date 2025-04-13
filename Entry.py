import sys, os, time
import core.controller as controller

if __name__ == "__main__":
    # controller.stt_test()  # 測試 STT 模組
    # controller.nlp_test()  # 測試 NLP 模組
    controller.integration_test_StN()  # 整合測試 (STT和NLP模組)
    sys.exit(0)

# This will be the entry file for the program.