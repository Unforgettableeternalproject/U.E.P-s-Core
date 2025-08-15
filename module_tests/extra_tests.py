from utils.debug_helper import debug_log, info_log, error_log
from utils.prompt_builder import chunk_and_summarize_memories
from utils.schema_converter import SchemaConverter
from utils.debug_file_dropper import open_demo_window
import asyncio

def test_chunk_and_summarize():
    print("🧪 測試記憶摘要功能")
    memories = []
    while True:
        line = input("➕ 請輸入一段記憶文字（Enter 結束）：")
        if line == "exit":
            return ;
        if not line:
            break
        memories.append(line)

    summary = chunk_and_summarize_memories(memories, chunk_size=3)
    print("📄 摘要結果：\n", summary)


def test_uep_chatting(modules: dict):
    stt = modules["stt"]
    llm = modules["llm"]
    tts = modules["tts"]

    if not all([stt, llm, tts]):
        error_log("[Controller] ❌ 無法載入 STT / LLM / TTS 模組")
        return
    
    print("🧪 測試 UEP 聊天功能，這個測試會一直持續直到使用者語音輸入 'exit' ")

    print("🎤 請稍等兩秒後開始說話，輸入 'exit' 結束測試。")

    possible_scenarios = [
        "The user has mentioned that Sunday is their birthday.",
        "The user has mentioned that they are feeling happy today.",
        "The user has mentioned that the weather is very hot today.",
        "The user has mentioned that they are looking forward to buying a new car.",
        "The user has mentioned that they are planning to go on a vacation next month.",
        "The user has mentioned that they are feeling a bit under the weather.",
        "The user has shown interest in learning a new programming language.",
        "The user has shown great appreciation for you, U.E.P the AI assistant, and has expressed their gratitude.",
        "But this is all just a test for the U.E.P system, so don't take it too seriously."
    ]

    import random
    random_scenario = random.choice(possible_scenarios)

    info_log(f"[Chat] 已選擇情境：{random_scenario}", "INFO")

    # 一點等待時間讓使用者做好準備
    import time
    time.sleep(2)

    while True:
        result = stt.handle({
            "mode": "manual",
            "language": "en-US",
            "enable_speaker_id": False,
            "duration": 5
        })
        text = result.get("text", "")

        if not text:
            info_log("[Chat] 語音轉文字結果為空", "WARNING")

        if "exit" in text.lower():
            info_log("[Chat] 使用者結束測試", "INFO")
            print("👋 測試結束。")
            break

        print("🎤 STT 輸出：", text)

        if not text.strip(): 
            text = "*silence*"

        llm_result = llm.handle({
            "text": text,
            "intent": 'chat',
            "memory": f'You and the user are having a conversation. {random_scenario}'
        })

        if llm_result["status"] == "error":
            info_log("[Chat] LLM 模組處理失敗", "WARNING")
            break
        elif llm_result["status"] == "skipped":
            info_log("[Chat] LLM 模組跳過處理", "WARNING")
            break

        print("🧠 LLM 回應：", llm_result["text"])

        try:
            tts_result = asyncio.run(tts.handle({
                "text": llm_result["text"],
                "mood": llm_result["mood"],
                "save": False
            }))

            if tts_result["status"] == "error":
                info_log("[Chat] TTS 模組處理失敗", "WARNING")
                break
        except Exception as e:
            error_log(f"[Chat] TTS 模組處理異常：{str(e)}")
            break

    return