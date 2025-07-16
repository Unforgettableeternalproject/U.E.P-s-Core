from utils.debug_helper import debug_log, info_log, error_log
from utils.prompt_builder import chunk_and_summarize_memories
from utils.schema_converter import SchemaConverter
from utils.debug_file_dropper import open_demo_window
import asyncio

def _handle_workflow_interaction(sysmod, session_id, initial_resp):
    """
    處理工作流程的後續互動邏輯，參考 controller.py 中的實現
    
    Args:
        sysmod: SYS 模組實例
        session_id: 工作流程會話ID
        initial_resp: 初始回應
    """
    resp = initial_resp
    
    # 進入互動循環
    while resp.get("requires_input", False) or resp.get("status") == "waiting":
        requires_input = resp.get("requires_input", False)
        prompt = resp.get("prompt", "請輸入")
        
        if requires_input:
            print(f"\n{prompt}")
            
            # 檢查是否需要檔案選擇
            needs_file_selection = (
                any(keyword in prompt.lower() for keyword in [
                    "請輸入要讀取的檔案路徑", 
                    "請選擇要歸檔的檔案路徑",
                    "請輸入要生成摘要的檔案路徑",
                    "請選擇檔案", 
                    "請輸入檔案路徑", 
                    "file path"
                ]) and
                "確認" not in prompt.lower() and
                "是否" not in prompt.lower() and
                "y/n" not in prompt.lower()
            )
            
            if needs_file_selection:
                print("🔍 正在開啟檔案選擇視窗...")
                try:
                    file_path = open_demo_window()
                    if file_path:
                        print(f"✅ 已選擇檔案: {file_path}")
                        user_input = file_path
                    else:
                        print("❌ 未選擇檔案，結束工作流程")
                        return
                except Exception as e:
                    error_log(f"[Integration Test] 檔案選擇出現錯誤: {e}")
                    print("❌ 檔案選擇失敗，結束工作流程")
                    return
            else:
                # 一般文字輸入或確認步驟 - 為了測試自動化，提供預設回應
                if "確認" in prompt.lower() or "是否" in prompt.lower():
                    user_input = "y"  # 自動確認
                    print(f"> {user_input} (自動確認)")
                else:
                    user_input = input("> ")
                    
                    # 如果用戶輸入 exit 或 quit，取消工作流程
                    if user_input.lower() in ["exit", "quit", "取消"]:
                        cancel_resp = sysmod.handle({
                            "mode": "cancel_workflow",
                            "params": {
                                "session_id": session_id,
                                "reason": "用戶取消"
                            }
                        })
                        print(f"\n❌ 工作流程已取消：{cancel_resp.get('message', '已取消')}")
                        return
            
            # 繼續工作流程
            resp = sysmod.handle({
                "mode": "continue_workflow",
                "params": {
                    "session_id": session_id,
                    "user_input": user_input
                }
            })
            
            print(f"\n回應狀態: {resp.get('status', '未知')}")
            print(f"回應訊息: {resp.get('message', '無訊息')}")
            
            # 如果狀態是 waiting，繼續自動推進
            while resp.get("status") == "waiting" and not resp.get("requires_input", False):
                import time
                time.sleep(0.5)  # 短暫延遲
                resp = sysmod.handle({
                    "mode": "continue_workflow", 
                    "params": {
                        "session_id": session_id,
                        "user_input": ""  # 自動推進不需要輸入
                    }
                })
                print(f"自動推進 - 回應狀態: {resp.get('status', '未知')}")
                print(f"自動推進 - 回應訊息: {resp.get('message', '無訊息')}")
        else:
            # 工作流程已完成或失敗
            break
    
    print(f"\n=== 工作流程結束 ===")
    print(f"最終狀態: {resp.get('status', '未知')}")
    print(f"最終訊息: {resp.get('message', '無訊息')}")
    
    # 顯示工作流程結果（如果有）
    if "data" in resp:
        print("\n🎯 工作流程結果:")
        data = resp["data"]
        
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, str) and len(value) > 200:
                    print(f"  {key}: {value[:200]}...")
                elif isinstance(value, list) and len(value) > 5:
                    print(f"  {key}: {value[:5]}... (總共 {len(value)} 項)")
                else:
                    print(f"  {key}: {value}")
        else:
            print(f"  結果: {data}")

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

    print("[SN] 模型整合 2/2 測試完成")

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

    print("[SM] 模型整合 2/2 測試完成")

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

    print("[SL] 模型整合 2/2 測試完成")

# 測試STT到TTS的整合

def itST(modules: dict):
    stt = modules["stt"]
    tts = modules["tts"]

    if not stt or not tts:
        error_log("[Controller] ❌ 無法載入 STT 或 TTS 模組")
        return

    result = stt.handle()
    text = result.get("text", "")

    if not text:
        info_log("[ST] 語音轉文字結果為空", "WARNING")
        return

    print("✨ 回傳語音內容：", text)

    try:
        tts_result = asyncio.run(tts.handle({
            "text": text,
            "mood": "neutral",
            "save": False
        }))

        if tts_result["status"] == "error":
            info_log("[ST] TTS 模組處理失敗", "WARNING")
            return

        print("[ST] 模型整合 2/2 測試完成")
    except Exception as e:
        error_log(f"[ST] TTS 模組處理異常：{str(e)}")
        return

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

    print("[NM] 模型整合 2/2 測試完成")

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

    print("[NL] 模型整合 2/2 測試完成")

# 測試NLP到TTS的整合

def itNT(modules: dict):
    nlp = modules["nlp"]
    tts = modules["tts"]

    if not nlp or not tts:
        error_log("[Controller] ❌ 無法載入 NLP 或 TTS 模組")
        return

    text = input("📝 請輸入文字：")
    nlp_result = nlp.handle({"text": text})

    print(
        f"🧠 NLP 輸出結果：{nlp_result['text']} 對應的是 {nlp_result['label']}，程式決定進行 {nlp_result['intent']}\n")
    
    if nlp_result["intent"] == "chat":
        try:
            tts_result = asyncio.run(tts.handle({
                "text": nlp_result["text"],
                "mood": "neutral",
                "save": False
            }))

            if tts_result["status"] == "error":
                info_log("[NT] TTS 模組處理失敗", "WARNING")
                return

            print("[NT] 模型整合 2/2 測試完成]")
        except Exception as e:
            error_log(f"[NT] TTS 模組處理異常：{str(e)}")
            return
    else:
        info_log("[NT] 非聊天輸入，不進行 TTS 處理", "WARNING")
        return

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

    memory_list = [f"{r['user']} → {r['response']}" for r in mem_result.get("results", [])]
    memory = chunk_and_summarize_memories(memory_list)

    if not memory:
        info_log("[ML] 記憶摘要為空", "WARNING")
        memory = "This is a new beginning of your chat."

    debug_log(2, f"[ML] 查詢到的記憶：{memory}")

    llm_result = llm.handle({
        "text": text,
        "intent": "chat",
        "memory": memory
    })

    if llm_result["status"] == "error":
        info_log("[ML] LLM 模組處理失敗", "WARNING")
        return
    elif llm_result["status"] == "skipped":
        info_log("[ML] LLM 模組跳過處理", "WARNING")
        return

    # 回存到 MEM 模組
    mem.handle({"mode": "store", "entry": {
        "user": text, "response": llm_result["text"]}})

    print("🧠 LLM 回應：", llm_result["text"])
    print("🎭 心情：", llm_result.get("mood"))

    print("[ML] 模型整合 2/2 測試完成")

# 測試LLM到TTS的整合

def itLT(modules: dict):
    llm = modules["llm"]
    tts = modules["tts"]

    if not llm or not tts:
        error_log("[Controller] ❌ 無法載入 LLM 或 TTS 模組")
        return

    text = input("📝 測試輸入（LLM + TTS）：")
    llm_result = llm.handle({
        "text": text,
        "intent": "chat",
        "memory": ""
    })

    if llm_result["status"] == "error":
        info_log("[LT] LLM 模組處理失敗", "WARNING")
        return

    print("🧠 LLM 回應：", llm_result["text"])

    try:
        tts_result = asyncio.run(tts.handle({
            "text": llm_result["text"],
            "mood": llm_result["mood"],
            "save": False
        }))

        if tts_result["status"] == "error":
            info_log("[LT] TTS 模組處理失敗", "WARNING")
            return
    except Exception as e:
        error_log(f"[LT] TTS 模組處理異常：{str(e)}")
        return

    print("[LT] 模型整合 2/2 測試完成")

# 測試LLM到SYS的整合

def itLY(modules: dict):
    llm = modules["llm"]
    sysmod = modules["sysmod"]

    if not llm or not sysmod:
        error_log("[Controller] ❌ 無法載入 LLM 或 SYS 模組")
        return

    text = input("📝 測試輸入（LLM + SYS，請輸入指令）：")
    
    # 獲取可用的系統功能列表
    available_functions = sysmod.handle({"mode": "list_functions"})
    debug_log(2, f"[LY] 可用系統功能: {available_functions}")

    llm_result = llm.handle({
        "text": text,
        "intent": "command",
        "memory": "",
        "available_functions": available_functions.get("functions", [])
    })

    if llm_result["status"] == "error":
        info_log("[LY] LLM 模組處理失敗", "WARNING")
        return

    print("🧠 LLM 回應：", llm_result["text"])
    print("🎭 心情：", llm_result.get("mood"))
    
    sys_action = llm_result.get("sys_action")
    if sys_action:
        print("⚙️ 系統動作：", sys_action)
        
        # 執行系統動作 - 優先以工作流形式
        if isinstance(sys_action, dict):
            # 將 LLM sys_action 格式轉換為 SYS 模組格式
            sys_input = SchemaConverter.convert_and_validate(sys_action)
            
            if not sys_input:
                print("⚠️ 無法轉換系統動作格式")
                return
            
            # 確保以工作流形式執行
            if sys_input.get("mode") != "start_workflow":
                print("📝 轉換為工作流形式執行")
                workflow_input = SchemaConverter.sys_action_to_workflow_input(sys_action)
                sys_result = sysmod.handle(workflow_input)
            else:
                sys_result = sysmod.handle(sys_input)
                
            print("🔧 SYS 執行結果：", sys_result.get("status", "未知"))
            if sys_result.get("status") == "completed":
                print("✅ 系統功能執行成功")
                if "result" in sys_result:
                    print("📊 執行結果：", sys_result["result"])
            elif sys_result.get("status") == "error":
                print("❌ 系統功能執行失敗：", sys_result.get("message", "未知錯誤"))
            elif sys_result.get("status") == "success":
                print("🔄 工作流程已啟動")
                session_id = sys_result.get("session_id")
                if session_id:
                    print(f"會話ID：{session_id}")
                    # 處理工作流程互動
                    _handle_workflow_interaction(sysmod, session_id, sys_result)
                else:
                    print("⚠️ 無法獲取會話ID")
            else:
                print(f"ℹ️ 系統狀態：{sys_result.get('status', '未知')}")
                if sys_result.get("message"):
                    print(f"📝 訊息：{sys_result.get('message')}")
        else:
            print("⚠️ 系統動作格式不正確")
    else:
        print("ℹ️ 沒有需要執行的系統動作")

    print("[LY] 模型整合 2/2 測試完成")

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

    print("[SNM] 模型整合 3/3 測試完成")

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

    print("[SNL] 模型整合 3/3 測試完成")

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

    if nlp_result["intent"] != "chat":
        info_log("[NML] 非聊天輸入，不進行 MEM 查詢，此測試結束", "WARNING")
        return

    mem_result = mem.handle({"mode": "fetch", "text": text})

    if mem_result["status"] == "empty":
        info_log("[NML] 查無相關記憶", "WARNING")

    memory_list = [f"{r['user']} → {r['response']}" for r in mem_result.get("results", [])]
    memory = chunk_and_summarize_memories(memory_list)

    if not memory:
        info_log("[NML] 記憶摘要為空", "WARNING")
        memory = "This is a new beginning of your chat."

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

    # 回存到 MEM 模組
    mem.handle({"mode": "store", "entry": {"user": text, "response": llm_result["text"]}})

    print("🧠 LLM 回應：", llm_result["text"])
    print("🎭 心情：", llm_result.get("mood"))
    print("⚙️ 系統指令：", llm_result.get("sys_action"))

    print("[NML] 模型整合 3/3 測試完成")

# NLP + LLM + SYS 整合測試

def itNLY(modules: dict):
    nlp = modules["nlp"]
    llm = modules["llm"]
    sysmod = modules["sysmod"]

    if not nlp or not llm or not sysmod:
        error_log("[Controller] ❌ 無法載入 NLP / LLM / SYS 模組")
        return

    text = input("📝 測試輸入（NLP → LLM → SYS）：")
    nlp_result = nlp.handle({"text": text})
    print(f"🧠 NLP 輸出結果：{nlp_result['text']} 對應的是 {nlp_result['label']}，程式決定進行 {nlp_result['intent']}\n")

    if nlp_result["intent"] != "command":
        info_log("[NLY] 非指令輸入，不進行 SYS 處理，此測試結束", "WARNING")
        return

    # 獲取可用的系統功能列表
    available_functions = sysmod.handle({"mode": "list_functions"})
    debug_log(2, f"[NLY] 可用系統功能: {available_functions}")

    llm_result = llm.handle({
        "text": text,
        "intent": nlp_result["intent"],
        "memory": "",
        "available_functions": available_functions.get("functions", [])
    })

    if llm_result["status"] == "error":
        info_log("[NLY] LLM 模組處理失敗", "WARNING")
        return
    elif llm_result["status"] == "skipped":
        info_log("[NLY] LLM 模組跳過處理", "WARNING")
        return

    print("🧠 LLM 回應：", llm_result["text"])
    print("🎭 心情：", llm_result.get("mood"))
    
    sys_action = llm_result.get("sys_action")
    if sys_action:
        print("⚙️ 系統動作：", sys_action)
        
        # 執行系統動作 - 優先以工作流形式
        if isinstance(sys_action, dict):
            # 將 LLM sys_action 格式轉換為 SYS 模組格式
            sys_input = SchemaConverter.convert_and_validate(sys_action)
            
            if not sys_input:
                print("⚠️ 無法轉換系統動作格式")
                return
            
            # 確保以工作流形式執行
            if sys_input.get("mode") != "start_workflow":
                print("📝 轉換為工作流形式執行")
                workflow_input = SchemaConverter.sys_action_to_workflow_input(sys_action)
                sys_result = sysmod.handle(workflow_input)
            else:
                sys_result = sysmod.handle(sys_input)
                
            print("🔧 SYS 執行結果：", sys_result.get("status", "未知"))
            if sys_result.get("status") == "completed":
                print("✅ 系統功能執行成功")
                if "result" in sys_result:
                    print("📊 執行結果：", sys_result["result"])
            elif sys_result.get("status") == "error":
                print("❌ 系統功能執行失敗：", sys_result.get("message", "未知錯誤"))
            elif sys_result.get("status") == "success":
                print("🔄 工作流程已啟動")
                session_id = sys_result.get("session_id")
                if session_id:
                    print(f"會話ID：{session_id}")
                    # 處理工作流程互動
                    _handle_workflow_interaction(sysmod, session_id, sys_result)
                else:
                    print("⚠️ 無法獲取會話ID")
            else:
                print(f"ℹ️ 系統狀態：{sys_result.get('status', '未知')}")
                if sys_result.get("message"):
                    print(f"📝 訊息：{sys_result.get('message')}")
        else:
            print("⚠️ 系統動作格式不正確")
    else:
        print("ℹ️ 沒有需要執行的系統動作")

    print("[NLY] 模型整合 3/3 測試完成")

# STT + NLP + MEM + LLM 整合測試

def itSNML(modules: dict):
    stt = modules["stt"]
    nlp = modules["nlp"]
    mem = modules["mem"]
    llm = modules["llm"]

    if not all([stt, nlp, mem, llm]):
        error_log("[Controller] ❌ 無法載入 STT / NLP / MEM / LLM 模組")
        return

    result = stt.handle()
    text = result.get("text", "")
    if not text:
        info_log("[SNML] 語音轉文字結果為空", "WARNING")
        return

    print("🎤 STT 輸出：", text)

    nlp_result = nlp.handle({"text": text})
    print(f"🧠 NLP 輸出結果：{nlp_result['text']} 對應的是 {nlp_result['label']}，程式決定進行 {nlp_result['intent']}\n")
    
    if nlp_result["intent"] != "chat":
        info_log("[NML] 非聊天輸入，不進行 MEM 查詢，此測試結束", "WARNING")
        return

    mem_result = mem.handle({"mode": "fetch", "text": text})

    if mem_result["status"] == "empty":
        info_log("[SNML] 查無相關記憶", "WARNING")

    memory_list = [
        f"{r['user']} → {r['response']}" for r in mem_result.get("results", [])]
    memory = chunk_and_summarize_memories(memory_list)

    if not memory:
        info_log("[SNML] 記憶摘要為空", "WARNING")
        memory = "This is a new beginning of your chat."
        # 回存到 MEM 模組

    debug_log(2, f"[SNML] 查詢到的記憶：{memory}")

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

    # 回存到 MEM 模組
    mem.handle({"mode": "store", "entry": {
               "user": text, "response": llm_result["text"]}})

    print("🧠 LLM 回應：", llm_result["text"])
    print("🎭 心情：", llm_result.get("mood"))
    print("⚙️ 系統指令：", llm_result.get("sys_action"))

    print("[SNML] 模型整合 4/4 測試完成")

# NLP+MEM+LLM+TTS 整合測試


def itNMLT(modules: dict):
    nlp = modules["nlp"]
    mem = modules["mem"]
    llm = modules["llm"]
    tts = modules["tts"]

    if not all([nlp, mem, llm, tts]):
        error_log("[Controller] ❌ 無法載入 NLP / MEM / LLM / TTS 模組")
        return

    text = input("📝 測試輸入（NLP → MEM → LLM → TTS）：")
    nlp_result = nlp.handle({"text": text})
    print(
        f"🧠 NLP 輸出結果：{nlp_result['text']} 對應的是 {nlp_result['label']}，程式決定進行 {nlp_result['intent']}\n")
    
    if nlp_result["intent"] != "chat":
        info_log("[NML] 非聊天輸入，不進行 MEM 查詢，此測試結束", "WARNING")
        return
    
    mem_result = mem.handle({"mode": "fetch", "text": text})

    if mem_result["status"] == "empty":
        info_log("[NMLT] 查無相關記憶", "WARNING")

    memory_list = [
        f"{r['user']} → {r['response']}" for r in mem_result.get("results", [])]
    memory = chunk_and_summarize_memories(memory_list)

    if not memory:
        info_log("[NMLT] 記憶摘要為空", "WARNING")
        memory = "This is a new beginning of your chat."

    debug_log(2, f"[NMLT] 查詢到的記憶：{memory}")

    llm_result = llm.handle({
        "text": text,
        "intent": nlp_result["intent"],
        "memory": memory
    })

    if llm_result["status"] == "error":
        info_log("[NMLT] LLM 模組處理失敗", "WARNING")
        return
    elif llm_result["status"] == "skipped":
        info_log("[NMLT] LLM 模組跳過處理", "WARNING")
        return

    # 回存到 MEM 模組
    mem.handle({"mode": "store", "entry": {
        "user": text, "response": llm_result["text"]}})

    print("🧠 LLM 回應：", llm_result["text"])

    try:
        tts_result = asyncio.run(tts.handle({
            "text": llm_result["text"],
            "mood": llm_result["mood"],
            "save": False
        }))

        if tts_result["status"] == "error":
            info_log("[NMLT] TTS 模組處理失敗", "WARNING")
            return
    except Exception as e:
        error_log(f"[NMLT] TTS 模組處理異常：{str(e)}")
        return

    print("[NMLT] 模型整合 4/4 測試完成")

# STT+NLP+MEM+LLM+TTS 整合測試

def itSNMLT(modules: dict):
    stt = modules["stt"]
    nlp = modules["nlp"]
    mem = modules["mem"]
    llm = modules["llm"]
    tts = modules["tts"]

    if not all([stt, nlp, mem, llm, tts]):
        error_log("[Controller] ❌ 無法載入 STT / NLP / MEM / LLM / TTS 模組")
        return

    result = stt.handle()
    text = result.get("text", "")

    if not text:
        info_log("[SNMLT] 語音轉文字結果為空", "WARNING")
        return

    print("🎤 STT 輸出：", text)

    nlp_result = nlp.handle({"text": text})
    print(
        f"🧠 NLP 輸出結果：{nlp_result['text']} 對應的是 {nlp_result['label']}，程式決定進行 {nlp_result['intent']}\n")
    
    if nlp_result["intent"] != "chat":
        info_log("[NML] 非聊天輸入，不進行 MEM 查詢，此測試結束", "WARNING")
        return

    mem_result = mem.handle({"mode": "fetch", "text": text})

    if mem_result["status"] == "empty":
        info_log("[SNMLT] 查無相關記憶", "WARNING")

    memory_list = [
        f"{r['user']} → {r['response']}" for r in mem_result.get("results", [])]
    memory = chunk_and_summarize_memories(memory_list)

    if not memory:
        info_log("[SNMLT] 記憶摘要為空", "WARNING")
        memory = "This is a new beginning of your chat."

    debug_log(2, f"[SNMLT] 查詢到的記憶：{memory}")

    llm_result = llm.handle({
        "text": text,
        "intent": nlp_result["intent"],
        "memory": memory
    })

    if llm_result["status"] == "error":
        info_log("[SNMLT] LLM 模組處理失敗", "WARNING")
        return
    elif llm_result["status"] == "skipped":
        info_log("[SNMLT] LLM 模組跳過處理", "WARNING")
        return

    # 回存到 MEM 模組
    mem.handle({"mode": "store", "entry": {
        "user": text, "response": llm_result["text"]}})

    print("🧠 LLM 回應：", llm_result["text"])

    try:
        tts_result = asyncio.run(tts.handle({
            "text": llm_result["text"],
            "mood": llm_result["mood"],
            "save": False
        }))

        if tts_result["status"] == "error":
            info_log("[SNMLT] TTS 模組處理失敗", "WARNING")
            return
    except Exception as e:
        error_log(f"[SNMLT] TTS 模組處理異常：{str(e)}")
        return

    print("[SNMLT] 模型整合 5/5 測試完成")

# STT + NLP + MEM + LLM + TTS + SYS 完整管線測試

def itSNMLTY(modules: dict):
    stt = modules["stt"]
    nlp = modules["nlp"]
    mem = modules["mem"]
    llm = modules["llm"]
    tts = modules["tts"]
    sysmod = modules["sysmod"]

    if not all([stt, nlp, mem, llm, tts, sysmod]):
        error_log("[Controller] ❌ 無法載入所有模組，請檢查模組註冊狀態")
        return

    print("🎙️ 開始完整管線測試：STT → NLP → MEM/SYS → LLM → TTS → SYS")

    # Step 1: 取得語音輸入並轉為文字
    result = stt.handle()
    audio_text = result.get("text", "")
    
    if not audio_text:
        info_log("[SNMLTSY] 語音轉文字結果為空", "WARNING")
        return

    print("🎤 STT 輸出：", audio_text)

    # Step 2: NLP 模組判斷 intent
    nlp_result = nlp.handle({"text": audio_text})
    intent = nlp_result.get("intent")
    print(f"🧠 NLP 輸出結果：{nlp_result['text']} 對應的是 {nlp_result['label']}，程式決定進行 {nlp_result['intent']}\n")

    # Step 3: 分流處理（聊天或指令）
    if intent == "chat":
        # 聊天模式：查詢記憶
        mem_result = mem.handle({"mode": "fetch", "text": audio_text})
        
        if mem_result["status"] == "empty":
            info_log("[SNMLTSY] 查無相關記憶", "WARNING")
            memory = "This is a new beginning of your chat."
        else:
            memory_list = [f"{r['user']} → {r['response']}" for r in mem_result.get("results", [])]
            memory = chunk_and_summarize_memories(memory_list)
            debug_log(2, f"[SNMLTSY] 查詢到的記憶：{memory}")

        llm_result = llm.handle({
            "text": audio_text,
            "intent": "chat",
            "memory": memory
        })
        
        if llm_result["status"] == "ok":
            # 回存到 MEM 模組
            mem.handle({"mode": "store", "entry": {
                "user": audio_text, 
                "response": llm_result["text"]
            }})

    elif intent == "command":
        # 指令模式：執行系統功能
        available_functions = sysmod.handle({"mode": "list_functions"})
        debug_log(2, f"[SNMLTSY] 可用系統功能: {available_functions}")
        
        llm_result = llm.handle({
            "text": audio_text,
            "intent": "command",
            "memory": "",
            "available_functions": available_functions.get("functions", [])
        })
        
    else:
        llm_result = {"text": "我不太明白你的意思...", "mood": "neutral"}

    if llm_result.get("status") == "error":
        info_log("[SNMLTSY] LLM 模組處理失敗", "WARNING")
        return
    elif llm_result.get("status") == "skipped":
        info_log("[SNMLTSY] LLM 模組跳過處理", "WARNING")
        return

    print("🧠 LLM 回應：", llm_result["text"])
    print("🎭 心情：", llm_result.get("mood"))

    # Step 4: 立即輸出給 TTS（告知用戶正在執行的操作）
    try:
        tts_result = asyncio.run(tts.handle({
            "text": llm_result["text"],
            "mood": llm_result.get("mood", "neutral"),
            "save": False
        }))

        if tts_result["status"] == "error":
            info_log("[SNMLTSY] TTS 模組處理失敗", "WARNING")
        else:
            print("🔊 TTS 處理完成")
    except Exception as e:
        error_log(f"[SNMLTSY] TTS 模組處理異常：{str(e)}")

    # Step 5: 如果是指令模式，執行系統動作（與 TTS 並行）
    if intent == "command":
        sys_action = llm_result.get("sys_action")
        if sys_action:
            print("⚙️ 系統動作：", sys_action)
            if isinstance(sys_action, dict):
                # 將 LLM sys_action 格式轉換為 SYS 模組格式
                sys_input = SchemaConverter.convert_and_validate(sys_action)
                
                if not sys_input:
                    print("⚠️ 無法轉換系統動作格式")
                    return
                
                # 確保系統動作以工作流形式啟動
                if sys_input.get("mode") != "start_workflow":
                    print("📝 轉換為工作流形式執行")
                    workflow_input = SchemaConverter.sys_action_to_workflow_input(sys_action)
                    sys_result = sysmod.handle(workflow_input)
                else:
                    sys_result = sysmod.handle(sys_input)
                
                print("🔧 SYS 執行結果：", sys_result.get("status", "未知"))
                if sys_result.get("status") == "completed":
                    print("✅ 系統功能執行成功")
                    if "result" in sys_result:
                        print("📊 執行結果：", sys_result["result"])
                elif sys_result.get("status") == "error":
                    print("❌ 系統功能執行失敗：", sys_result.get("message", "未知錯誤"))
                elif sys_result.get("status") == "success":
                    print("🔄 工作流程已啟動")
                    session_id = sys_result.get("session_id")
                    if session_id:
                        print(f"會話ID：{session_id}")
                        # 處理工作流程互動
                        _handle_workflow_interaction(sysmod, session_id, sys_result)
                    else:
                        print("⚠️ 無法獲取會話ID")
                else:
                    print(f"ℹ️ 系統狀態：{sys_result.get('status', '未知')}")
                    if sys_result.get("message"):
                        print(f"📝 訊息：{sys_result.get('message')}")
            else:
                print("⚠️ 系統動作格式不正確")
        else:
            print("ℹ️ 沒有需要執行的系統動作")

    print("[SNMLTSY] 完整管線測試 6/6 完成！🎉")
