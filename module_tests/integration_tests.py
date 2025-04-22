from utils.debug_helper import debug_log, info_log, error_log
from utils.prompt_builder import chunk_and_summarize_memories


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

# 統合測試

# 測試STT到NLP的整合
def itSN(modules : dict):
    stt = modules["stt"]
    nlp = modules["nlp"]

    if stt is None or nlp is None:
        error_log("[Controller] ❌ 無法載入 STT 或 NLP 模組")
        return
    
    result = stt.handle()
    if not result.get("text"):
        info_log("[SN] 語音轉文字結果為空", "WARNING")
        return

    print("✨ 回傳語音內容：", result["text"])

    nlp_result = nlp.handle({"text": result["text"]})
    print(f"🧠 NLP 輸出結果：{nlp_result['text']} 對應的是 {nlp_result['label']}，程式決定進行 {nlp_result['intent']}\n")

# 測試STT到MEM的整合
def itSM(modules : dict):
    stt = modules["stt"]
    mem = modules["mem"]

    if not all([stt, mem]):
        error_log("[Controller] ❌ 無法載入 STT 或 MEM 模組")
        return

    result = stt.handle()
    text = result.get("text", "")
    if not text:
        info_log("[SM] 語音轉文字結果為空", "WARNING")
        return
    
    print("✨ 回傳語音內容：", text)

    mem_result = mem.handle({
        "mode": "fetch",
        "text": text
    })

    if mem_result["status"] == "empty":
        info_log("[SM] 查無相關記憶", "WARNING")
        return

    print(f"🧠 記憶查詢結果：\n\n使用者: {mem_result['results'][0]['user']} \n回應: {mem_result['results'][0]['response']}")

# 測試STT到LLM的整合

def itSL(modules: dict):
    stt = modules["stt"]
    llm = modules["llm"]

    if not stt or not llm:
        error_log("[Controller] ❌ 無法載入 STT 或 LLM 模組")
        return

    result = stt.handle()
    text = result.get("text", "")
    if not text:
        info_log("[SL] 語音轉文字結果為空", "WARNING")
        return

    print("✨ 回傳語音內容：", text)

    llm_result = llm.handle({
        "text": text,
        "intent": "chat",
        "memory": ""
    })

    if llm_result["status"] == "error":
        info_log("[SL] LLM 模組處理失敗", "WARNING")
        return

    print("🧠 LLM 回應：", llm_result["text"])
    print("🎭 心情：", llm_result.get("mood"))

# 測試NLP到MEM的整合

def itNM(modules : dict):
    nlp = modules["nlp"]
    mem = modules["mem"]

    if not all([nlp, mem]):
        error_log("[Controller] ❌ 無法載入 NLP 或 MEM 模組")
        return

    text = input("📝 手動輸入測試句：")
    nlp_result = nlp.handle({"text": text})
    print(f"🧠 NLP 輸出結果：{nlp_result['text']} 對應的是 {nlp_result['label']}，程式決定進行 {nlp_result['intent']}\n")

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
        info_log("[NM] 查無相關記憶", "WARNING")
        return

    # 刪除不必要的記憶
    mem.handle({"mode": "clear_by_text", "text": text, "top_k": 1})

    print(f"🧠 記憶查詢結果：\n\n使用者: {mem_result['results'][0]['user']} \n回應: {mem_result['results'][0]['response']}")

# 測試NLP到LLM的整合

def itNL(modules: dict):
    nlp = modules["nlp"]
    llm = modules["llm"]

    if not nlp or not llm:
        error_log("[Controller] ❌ 無法載入 NLP 或 LLM 模組")
        return

    text = input("📝 請輸入文字：")
    nlp_result = nlp.handle({"text": text})
    print(f"🧠 NLP 輸出結果：{nlp_result['text']} 對應的是 {nlp_result['label']}，程式決定進行 {nlp_result['intent']}\n")

    llm_result = llm.handle({
        "text": text,
        "intent": nlp_result["intent"],
        "memory": ""
    })

    if llm_result["status"] == "error":
        info_log("[NL] LLM 模組處理失敗", "WARNING")
        return
    elif llm_result["status"] == "skipped":
        info_log("[NL] LLM 模組跳過處理", "WARNING")
        return

    print("🧠 LLM 回應：", llm_result["text"])
    print("🎭 心情：", llm_result.get("mood"))

# 測試MEM到LLM的整合

def itML(modules: dict):
    mem = modules["mem"]
    llm = modules["llm"]

    if not mem or not llm:
        error_log("[Controller] ❌ 無法載入 MEM 或 LLM 模組")
        return

    text = input("🔎 測試輸入（MEM + LLM）：")
    mem_result = mem.handle({"mode": "fetch", "text": text})

    if mem_result["status"] == "empty":
        info_log("[ML] 查無相關記憶", "WARNING")
        return

    memory_list = [f"{r['user']} → {r['response']}" for r in mem_result.get("results", [])]
    memory = chunk_and_summarize_memories(memory_list)

    if not memory:
        info_log("[ML] 記憶摘要為空", "WARNING")
        return

    debug_log(2, f"[ML] 查詢到的記憶：{memory}")

    llm_result = llm.handle({
        "text": text,
        "intent": "chat",
        "memory": memory
    })
    print("🧠 LLM 回應：", llm_result["text"])
    print("🎭 心情：", llm_result.get("mood"))

# STT + NLP + MEM 整合測試

def itSNM(modules : dict):
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
        info_log("[SNM] 語音轉文字結果為空", "WARNING")
        return

    print("🎤 STT 輸出：", text)

    # Step 2: NLP 判斷
    nlp_result = nlp.handle({"text": text})
    print(f"🧠 NLP 輸出結果：{nlp_result['text']} 對應的是 {nlp_result['label']}，程式決定進行 {nlp_result['intent']}\n")

    # Step 3: 判斷是否為聊天，若是就進行MEM查詢
    if nlp_result["intent"] == "chat":
        mem_result = mem.handle({"mode": "fetch", "text": text})
        if mem_result["status"] == "empty":
            info_log("[SNM] 查無相關記憶", "WARNING")
            return
    else:
        info_log("[SNM] 非聊天輸入，不查詢記憶", "WARNING")
        return

    print(
        f"🧠 記憶查詢結果：\n\n使用者: {mem_result['results'][0]['user']} \n回應: {mem_result['results'][0]['response']}")

# STT + NLP + LLM 整合測試

def itSNL(modules: dict):
    stt = modules["stt"]
    nlp = modules["nlp"]
    llm = modules["llm"]

    if not stt or not nlp or not llm:
        error_log("[Controller] ❌ 無法載入 STT / NLP / LLM 模組")
        return

    result = stt.handle()
    text = result.get("text", "")
    if not text:
        info_log("[SNL] 語音轉文字結果為空", "WARNING")
        return

    print("🎤 STT 輸出：", text)

    nlp_result = nlp.handle({"text": text})
    print(f"🧠 NLP 輸出結果：{nlp_result['text']} 對應的是 {nlp_result['label']}，程式決定進行 {nlp_result['intent']}\n")

    llm_result = llm.handle({
        "text": text,
        "intent": nlp_result["intent"],
        "memory": ""
    })

    if llm_result["status"] == "error":
        info_log("[SNL] LLM 模組處理失敗", "WARNING")
        return
    elif llm_result["status"] == "skipped":
        info_log("[SNL] LLM 模組跳過處理", "WARNING")
        return

    print("🧠 LLM 回應：", llm_result["text"])
    print("🎭 心情：", llm_result.get("mood"))

# NLP + MEM + LLM 整合測試

def itNML(modules: dict):
    nlp = modules["nlp"]
    mem = modules["mem"]
    llm = modules["llm"]

    if not nlp or not mem or not llm:
        error_log("[Controller] ❌ 無法載入 NLP / MEM / LLM 模組")
        return

    text = input("📝 測試輸入（NLP → MEM → LLM）：")
    nlp_result = nlp.handle({"text": text})
    print(f"🧠 NLP 輸出結果：{nlp_result['text']} 對應的是 {nlp_result['label']}，程式決定進行 {nlp_result['intent']}\n")

    mem_result = mem.handle({"mode": "fetch", "text": text})

    if mem_result["status"] == "empty":
        info_log("[NML] 查無相關記憶", "WARNING")
        return

    memory_list = [f"{r['user']} → {r['response']}" for r in mem_result.get("results", [])]
    memory = chunk_and_summarize_memories(memory_list)

    if not memory:
        info_log("[NML] 記憶摘要為空", "WARNING")
        return

    debug_log(2, f"[NML] 查詢到的記憶：{memory}")

    llm_result = llm.handle({
        "text": text,
        "intent": nlp_result["intent"],
        "memory": memory
    })

    if llm_result["status"] == "error":
        info_log("[NML] LLM 模組處理失敗", "WARNING")
        return
    elif llm_result["status"] == "skipped":
        info_log("[NML] LLM 模組跳過處理", "WARNING")
        return

    print("🧠 LLM 回應：", llm_result["text"])
    print("🎭 心情：", llm_result.get("mood"))
    print("⚙️ 系統指令：", llm_result.get("sys_action"))

# STT + NLP + MEM + LLM 整合測試

def itSNML(modules: dict):
    stt = modules["stt"]
    nlp = modules["nlp"]
    mem = modules["mem"]
    llm = modules["llm"]

    if not all([stt, nlp, mem, llm]):
        error_log("[Controller] ❌ 無法載入 STT / NLP / MEM / LLM 模組")
        return

    # Step 1: STT 語音輸入
    result = stt.handle()
    text = result.get("text", "")
    if not text:
        info_log("[SNML] 語音轉文字結果為空", "WARNING")
        return

    print("🎤 STT 輸出：", text)

    # Step 2: NLP 判斷
    nlp_result = nlp.handle({"text": text})
    print(f"🧠 NLP 輸出結果：{nlp_result['text']} 對應的是 {nlp_result['label']}，程式決定進行 {nlp_result['intent']}\n")
    
    # Step 3: MEM 查詢
    mem_result = mem.handle({"mode": "fetch", "text": text})

    if mem_result["status"] == "empty":
        info_log("[SNML] 查無相關記憶", "WARNING")
        return

    memory_list = [
        f"{r['user']} → {r['response']}" for r in mem_result.get("results", [])]
    memory = chunk_and_summarize_memories(memory_list)

    if not memory:
        info_log("[SNML] 記憶摘要為空", "WARNING")
        return

    debug_log(2, f"[SNML] 查詢到的記憶：{memory}")

    # Step 4: LLM 處理

    llm_result = llm.handle({
        "text": text,
        "intent": nlp_result["intent"],
        "memory": memory
    })

    if llm_result["status"] == "error":
        info_log("[SNML] LLM 模組處理失敗", "WARNING")
        return
    elif llm_result["status"] == "skipped":
        info_log("[SNML] LLM 模組跳過處理", "WARNING")
        return

    print("🧠 LLM 回應：", llm_result["text"])
    print("🎭 心情：", llm_result.get("mood"))
    print("⚙️ 系統指令：", llm_result.get("sys_action"))

def handle_pipeline(modules : dict):
    stt = modules["stt"]
    nlp = modules["nlp"]
    llm = modules["llm"]
    mem = modules["mem"]
    tts = modules["tts"]
    sysmod = modules["sysmod"]

    if any(mod is None for mod in modules.values()):
        error_log("[Controller] ❌ 無法載入所有模組，請檢查模組註冊狀態")
        return "模組載入失敗"

    # Step 1: 取得語音輸入並轉為文字
    audio_text = stt.handle({})["text"]

    # Step 2: NLP 模組判斷 intent
    nlp_result = nlp.handle({"text": audio_text})
    intent = nlp_result.get("intent")
    detail = nlp_result.get("detail")

    # Step 3: 分流處理（聊天或指令）
    if intent == "chat":
        snapshot = mem.handle({"mode": "fetch", "text": audio_text})
        llm_result = llm.handle({
            "text": audio_text,
            "intent": "chat",
            "snapshot": snapshot
        })
        mem.handle({"mode": "store", "entry": llm_result["log"]})
    elif intent == "command":
        available_sys = sysmod.handle({"mode": "list_functions"})
        llm_result = llm.handle({
            "text": audio_text,
            "intent": "command",
            "available": available_sys,
            "detail": detail
        })
        sysmod.handle(llm_result.get("sys_action", {}))
    else:
        llm_result = {"text": "我不太明白你的意思..."}

    # Step 4: 輸出給 TTS 和 UI
    tts.handle({
        "text": llm_result.get("text"),
        "emotion": llm_result.get("emotion", "neutral")
    })

    return llm_result.get("text")