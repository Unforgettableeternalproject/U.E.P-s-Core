from utils.debug_helper import debug_log, info_log, error_log

# 統合測試

# 測試STT到NLP的整合
def integration_test_SN(modules : dict):
    stt = modules["stt"]
    nlp = modules["nlp"]

    if stt is None or nlp is None:
        error_log("[Controller] ❌ 無法載入 STT 或 NLP 模組")
        return
    
    result = stt.handle()
    if not result.get("text"):
        info_log("[StN] 語音轉文字結果為空", "WARNING")
        return

    print("✨ 回傳語音內容：", result["text"])

    nlp_result = nlp.handle({"text": result["text"]})
    print(f"🧠 NLP 輸出結果：{nlp_result.text} 對應的是 {nlp_result.label}，程式決定進行 {nlp_result.intent}\n")

# 測試STT到MEM的整合
def integration_test_SM(modules : dict):
    stt = modules["stt"]
    mem = modules["mem"]

    if not all([stt, mem]):
        error_log("[Controller] ❌ 無法載入 STT 或 MEM 模組")
        return

    result = stt.handle()
    text = result.get("text", "")
    if not text:
        info_log("[StM] 語音轉文字結果為空", "WARNING")
        return
    
    print("✨ 回傳語音內容：", text)

    mem_result = mem.handle({
        "mode": "fetch",
        "text": text
    })

    if mem_result["status"] == "empty":
        info_log("[StM] 查無相關記憶", "WARNING")
        return

    print(f"🧠 記憶查詢結果：\n\n使用者: {mem_result['results'][0]['user']} \n回應: {mem_result['results'][0]['response']}")

# 測試NLP到MEM的整合

def integration_test_NM(modules : dict):
    nlp = modules["nlp"]
    mem = modules["mem"]

    if not all([nlp, mem]):
        error_log("[Controller] ❌ 無法載入 NLP 或 MEM 模組")
        return

    text = input("📝 手動輸入測試句：")
    nlp_result = nlp.handle({"text": text})
    print(f"🧠 NLP 輸出結果：{nlp_result.text} 對應的是 {nlp_result.label}，程式決定進行 {nlp_result.intent}\n")

    if nlp_result["intent"] == "chat":
        mem.handle({
            "mode": "store",
            "entry": {
                "user": text,
                "response": "Example response."
            }
        })
        print("✅ 記憶儲存成功\n")
    else:
        print("⚠️ 非聊天輸入，不儲存進記憶")

    mem_result = mem.handle({"mode": "fetch", "text": text})
    if mem_result["status"] == "empty":
        info_log("[NtM] 查無相關記憶", "WARNING")
        return

    # 刪除不必要的記憶
    mem.handle({"mode": "clear_by_text", "text": text, "top_k": 1})

    print(f"🧠 記憶查詢結果：\n\n使用者: {mem_result['results'][0]['user']} \n回應: {mem_result['results'][0]['response']}")

# STT + NLP + MEM 整合測試

def integration_test_SNM(modules : dict):
    stt = modules["stt"]
    nlp = modules["nlp"]
    mem = modules["mem"]

    if not all([stt, nlp, mem]):
        error_log("[Controller] ❌ 無法載入 STT / NLP / MEM 模組")
        return

    # Step 1: STT 語音輸入
    result = stt.handle()
    text = result.get("text", "")
    if not text:
        info_log("[SttM] 語音轉文字結果為空", "WARNING")
        return

    print("🎤 STT 輸出：", text)

    # Step 2: NLP 判斷
    nlp_result = nlp.handle({"text": text})
    print(f"🧠 NLP 輸出結果：{nlp_result.text} 對應的是 {nlp_result.label}，程式決定進行 {nlp_result.intent}\n")

    # Step 3: 判斷是否為聊天，若是就進行MEM查詢
    if nlp_result["intent"] == "chat":
        mem_result = mem.handle({"mode": "fetch", "text": text})
        if mem_result["status"] == "empty":
            info_log("[SttM] 查無相關記憶", "WARNING")
            return
    else:
        info_log("[SttM] 非聊天輸入，不查詢記憶", "WARNING")
        return

    print(
        f"🧠 記憶查詢結果：\n\n使用者: {result['results'][0]['user']} \n回應: {result['results'][0]['response']}")
