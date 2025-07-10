"""
測試用工作流程，用於驗證 session-based 工作流架構的運作
包含單步驟、多步驟以及不同複雜度的工作流程範例
"""
import time
from pathlib import Path
import random
import datetime
from utils.debug_helper import debug_log, debug_log_e, info_log, error_log

def simple_echo_workflow(session_data: dict, llm_module=None, tts_module=None) -> dict:
    """
    簡單回顯工作流程 - 單步驟工作流程範例
    接收用戶輸入並回傳相同的內容，用於測試最基本的工作流程機制
    
    Args:
        session_data: 工作流程會話數據
        llm_module: LLM模組實例 (此工作流程不需要)
        tts_module: TTS模組實例 (此工作流程不需要)
        
    Returns:
        工作流程狀態與結果
    """
    debug_log(1, f"[Test Workflow] 執行簡單回顯工作流程，步驟 {session_data.get('step', 1)}")
    
    message = session_data.get("message", "")
    if not message:
        # 需要用戶輸入
        debug_log(2, f"[Test Workflow] 簡單回顯工作流程需要用戶輸入")
        return {
            "status": "awaiting_input",
            "message": "請輸入任意訊息",
            "prompt": "請輸入要回顯的訊息:",
            "requires_input": True,
            "session_data": session_data
        }
    
    # 已經有用戶輸入，直接完成工作流程
    debug_log(1, f"[Test Workflow] 簡單回顯工作流程完成，回顯訊息: {message}")
    return {
        "status": "completed",
        "message": f"已完成訊息回顯: {message}",
        "requires_input": False,
        "session_data": session_data,
        "result": {
            "echo_message": message,
            "timestamp": datetime.datetime.now().isoformat()
        }
    }

def countdown_workflow(session_data: dict, llm_module=None, tts_module=None) -> dict:
    """
    倒數工作流程 - 多步驟工作流程範例
    從用戶提供的數字開始倒數到零，每步驟減一
    
    Args:
        session_data: 工作流程會話數據，包含:
            - step: 當前步驟
            - count: 當前倒數值
            - original_count: 初始倒數值
        llm_module: LLM模組實例 (此工作流程不需要)
        tts_module: TTS模組實例 (此工作流程不需要)
        
    Returns:
        工作流程狀態與結果
    """
    # 獲取當前步驟
    step = session_data.get("step", 1)
    debug_log(1, f"[Test Workflow] 執行倒數工作流程，步驟 {step}")
    
    # 步驟1: 獲取起始數字
    if step == 1:
        count = session_data.get("count")
        if count is None:
            debug_log(2, f"[Test Workflow] 倒數工作流程需要用戶輸入起始數字")
            return {
                "status": "awaiting_input",
                "message": "請提供一個起始數字進行倒數",
                "prompt": "請輸入一個正整數:",
                "requires_input": True,
                "session_data": session_data
            }
        
        # 嘗試轉換為整數
        try:
            count = int(count)
            if count <= 0:
                raise ValueError("數字必須大於零")
        except ValueError as e:
            error_log(f"[Test Workflow] 無效的倒數起始值: {e}")
            return {
                "status": "error",
                "message": f"無效的數字: {count}，請提供大於零的整數",
                "prompt": "請輸入一個正整數:",
                "requires_input": True,
                "session_data": session_data
            }
        
        # 保存初始值
        session_data["original_count"] = count
        session_data["count"] = count  # 確保 count 被保存到 session_data
        session_data["step"] = 2
        debug_log(2, f"[Test Workflow] 倒數工作流程設置起始值: {count}")
        
        # 直接進入倒數模式，且要求用戶輸入
        return {
            "status": "awaiting_input",
            "message": f"開始從 {count} 倒數",
            "prompt": "按Enter開始倒數，或輸入'跳過'直接結束:",
            "requires_input": True,
            "session_data": session_data
        }
    
    # 步驟2: 倒數過程
    elif step == 2:
        count = session_data.get("count", 0)
        original_count = session_data.get("original_count", 0)
        
        # 檢查是否完成倒數
        if count <= 0:
            debug_log(1, f"[Test Workflow] 倒數工作流程完成")
            return {
                "status": "completed",
                "message": f"倒數完成！從 {original_count} 到 0",
                "requires_input": False,
                "session_data": session_data,
                "result": {
                    "original_count": original_count,
                    "countdown_completed": True,
                    "completion_time": datetime.datetime.now().isoformat()
                }
            }
        
        # 更新倒數值
        session_data["count"] = count - 1
        next_count = session_data["count"]
        
        # 每次倒數都詢問用戶是否繼續
        debug_log(2, f"[Test Workflow] 倒數工作流程: {count} -> {next_count}")
        return {
            "status": "awaiting_input",
            "message": f"當前值: {count}, 下一個值將是 {next_count}",
            "prompt": f"按Enter繼續倒數，或輸入'跳過'直接結束:",
            "requires_input": True,
            "session_data": session_data
        }
        
    # 未知步驟
    else:
        error_log(f"[Test Workflow] 倒數工作流程中的未知步驟: {step}")
        return {
            "status": "error",
            "message": f"工作流程錯誤: 未知步驟 {step}",
            "requires_input": False,
            "session_data": session_data
        }

def data_collector_workflow(session_data: dict, llm_module=None, tts_module=None) -> dict:
    """
    資料收集工作流程 - 複雜多步驟工作流程範例
    逐步收集不同類型的用戶資料，並在最後生成摘要報告
    
    Args:
        session_data: 工作流程會話數據，包含:
            - step: 當前步驟
            - name: 用戶姓名
            - age: 用戶年齡
            - interests: 用戶興趣列表
            - feedback: 用戶反饋
        llm_module: LLM模組實例 (用於生成摘要)
        tts_module: TTS模組實例 (此工作流程不需要)
        
    Returns:
        工作流程狀態與結果
    """
    # 獲取當前步驟
    step = session_data.get("step", 1)
    debug_log(1, f"[Test Workflow] 執行資料收集工作流程，步驟 {step}")
    
    # 步驟1: 收集姓名
    if step == 1:
        name = session_data.get("name")
        if not name:
            debug_log(2, f"[Test Workflow] 資料收集工作流程需要用戶輸入姓名")
            return {
                "status": "awaiting_input",
                "message": "歡迎參與資料收集測試",
                "prompt": "請輸入您的姓名:",
                "requires_input": True,
                "session_data": session_data
            }
        
        # 已獲得姓名，進入下一步
        session_data["step"] = 2
        debug_log(2, f"[Test Workflow] 資料收集工作流程已獲得姓名: {name}")
        return {
            "status": "awaiting_input", 
            "message": f"您好，{name}！\n\n接下來請提供年齡資訊",
            "prompt": "請輸入您的年齡:",
            "requires_input": True,
            "session_data": session_data
        }
    
    # 步驟2: 收集年齡
    elif step == 2:
        age = session_data.get("age")
        if age is None:
            debug_log(2, f"[Test Workflow] 資料收集工作流程需要用戶輸入年齡")
            return {
                "status": "awaiting_input",
                "message": "接下來請提供年齡資訊",
                "prompt": "請輸入您的年齡:",
                "requires_input": True,
                "session_data": session_data
            }
        
        # 嘗試轉換為整數
        try:
            age = int(age)
            if age <= 0 or age > 120:
                raise ValueError("年齡必須在1-120之間")
        except ValueError as e:
            error_log(f"[Test Workflow] 無效的年齡值: {e}")
            # 清除無效的年齡值
            session_data.pop("age", None)
            return {
                "status": "error",
                "message": "無效的年齡，請提供1-120之間的數字",
                "prompt": "請輸入您的年齡:",
                "requires_input": True,
                "session_data": session_data
            }
        
        # 進入下一步
        session_data["step"] = 3
        debug_log(2, f"[Test Workflow] 資料收集工作流程已獲得年齡: {age}")
        return {
            "status": "awaiting_input",
            "message": f"已記錄年齡: {age}",
            "prompt": "請輸入您的興趣，以逗號分隔多個興趣:",
            "requires_input": True,
            "session_data": session_data
        }
    
    # 步驟3: 收集興趣
    elif step == 3:
        interests = session_data.get("interests", [])
        if not interests:
            debug_log(2, f"[Test Workflow] 資料收集工作流程需要用戶輸入興趣")
            return {
                "status": "awaiting_input",
                "message": "接下來請提供您的興趣愛好",
                "prompt": "請輸入您的興趣，以逗號分隔多個興趣:",
                "requires_input": True,
                "session_data": session_data
            }
        
        # 進入下一步
        session_data["step"] = 4
        # 如果輸入是字符串，分割成列表
        if isinstance(interests, str):
            interests = [i.strip() for i in interests.split(",") if i.strip()]
            session_data["interests"] = interests
            
        debug_log(2, f"[Test Workflow] 資料收集工作流程已獲得興趣: {interests}")
        return {
            "status": "awaiting_input",
            "message": f"已記錄 {len(interests)} 項興趣",
            "prompt": "請分享您對此測試的看法:",
            "requires_input": True,
            "session_data": session_data
        }
    
    # 步驟4: 收集反饋
    elif step == 4:
        feedback = session_data.get("feedback")
        if feedback is None:
            debug_log(2, f"[Test Workflow] 資料收集工作流程需要用戶輸入反饋")
            return {
                "status": "awaiting_input",
                "message": "最後，請提供一些反饋",
                "prompt": "請分享您對此測試的看法:",
                "requires_input": True,
                "session_data": session_data
            }
        
        # 進入最後的總結步驟
        session_data["step"] = 5
        debug_log(2, f"[Test Workflow] 資料收集工作流程已獲得反饋")
        return {
            "status": "processing",
            "message": "感謝您的反饋，正在生成摘要...",
            "requires_input": False,
            "session_data": session_data
        }
    
    # 步驟5: 生成摘要
    elif step == 5:
        name = session_data.get("name", "未提供")
        age = session_data.get("age", "未提供")
        interests = session_data.get("interests", [])
        feedback = session_data.get("feedback", "未提供")
        
        # 生成摘要報告
        interests_text = "、".join(interests) if interests else "無"
        
        summary = f"""
資料收集摘要報告
------------------
姓名: {name}
年齡: {age}
興趣: {interests_text}
反饋: {feedback}
------------------
收集時間: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
        
        # 如果有LLM模組可用，可以讓LLM生成更友好的摘要
        enhanced_summary = ""
        if llm_module:
            try:
                debug_log(2, f"[Test Workflow] 嘗試使用LLM生成增強摘要")
                prompt = f"""
請根據以下收集到的用戶資料，生成一個友好、有個性的摘要報告：

姓名: {name}
年齡: {age}
興趣: {interests_text}
反饋: "{feedback}"

請用輕鬆活潑的語氣，並加入一些與用戶興趣相關的有趣評論。格式可以自由發揮，但請確保內容豐富且有個性。
"""
                # 確保使用is_internal=True避免加入系統指示詞
                # 首先確保使用 intent="direct" 而非 "chat"，防止套用聊天相關提示詞
                response = llm_module.handle({
                    "text": prompt,
                    "intent": "direct",  # 使用直接模式代替chat，避免使用聊天特定提示詞
                    "is_internal": True  # 使用內部調用模式，避免加入系統指示詞
                })
                
                if response and response.get("status") == "ok" and "text" in response:
                    enhanced_summary = response["text"]
                    debug_log(2, f"[Test Workflow] LLM成功生成增強摘要")
                    info_log(f"[Test Workflow] 增強摘要內容: \n{enhanced_summary}")
            except Exception as e:
                error_log(f"[Test Workflow] LLM生成增強摘要失敗: {e}")
        
        # 完成工作流程
        debug_log(1, f"[Test Workflow] 資料收集工作流程完成")
        return {
            "status": "completed",
            "message": "資料收集完成，已生成摘要報告",
            "requires_input": False,
            "session_data": session_data,
            "result": {
                "name": name,
                "age": age,
                "interests": interests,
                "feedback": feedback,
                "basic_summary": summary,
                "enhanced_summary": enhanced_summary
            }
        }
    
    # 未知步驟
    else:
        error_log(f"[Test Workflow] 資料收集工作流程中的未知步驟: {step}")
        return {
            "status": "error",
            "message": f"工作流程錯誤: 未知步驟 {step}",
            "requires_input": False,
            "session_data": session_data
        }

def random_fail_workflow(session_data: dict, llm_module=None, tts_module=None) -> dict:
    """
    隨機失敗工作流程 - 測試錯誤處理機制
    在執行過程中有機率隨機失敗，用於測試系統的錯誤恢復能力
    
    Args:
        session_data: 工作流程會話數據，包含:
            - step: 當前步驟
            - fail_chance: 失敗機率 (0-100)
            - retry_count: 重試次數
        llm_module: LLM模組實例 (此工作流程不需要)
        tts_module: TTS模組實例 (此工作流程不需要)
        
    Returns:
        工作流程狀態與結果
    """
    # 獲取當前步驟
    step = session_data.get("step", 1)
    debug_log(1, f"[Test Workflow] 執行隨機失敗工作流程，步驟 {step}")
    
    # 步驟1: 設定失敗機率與最大重試次數
    if step == 1:
        fail_chance = session_data.get("fail_chance")
        max_retries = session_data.get("max_retries")
        
        if fail_chance is None:
            debug_log(2, f"[Test Workflow] 隨機失敗工作流程需要設定失敗機率")
            return {
                "status": "awaiting_input",
                "message": "此工作流程將測試系統的錯誤處理與自動重試能力",
                "prompt": "請設定失敗機率 (0-100):",
                "requires_input": True,
                "session_data": session_data
            }
        
        # 嘗試轉換為整數
        try:
            fail_chance = int(fail_chance)
            if fail_chance < 0 or fail_chance > 100:
                raise ValueError("機率必須在0-100之間")
        except ValueError as e:
            error_log(f"[Test Workflow] 無效的失敗機率: {e}")
            # 清除無效值
            session_data.pop("fail_chance", None)
            return {
                "status": "error",
                "message": "無效的機率值，請提供0-100之間的數字",
                "prompt": "請設定失敗機率 (0-100):",
                "requires_input": True,
                "session_data": session_data
            }
            
        # 儲存失敗機率
        session_data["fail_chance"] = fail_chance
        
        # 第二階段：詢問最大重試次數
        if "max_retries_stage" not in session_data:
            session_data["max_retries_stage"] = True
            return {
                "status": "awaiting_input",
                "message": f"已設定失敗機率為 {fail_chance}%",
                "prompt": "請設定最大重試次數 (預設5次，直接按Enter可使用預設值):",
                "requires_input": True,
                "session_data": session_data
            }
        
        # 處理最大重試次數輸入
        if "max_retries" not in session_data:
            max_retries = 5  # 默認值
            
            # 如果用戶輸入了值，嘗試解析
            if session_data.get("max_retries_input", "").strip():
                try:
                    input_value = session_data.get("max_retries_input", "")
                    max_retries = int(input_value)
                    if max_retries <= 0:
                        raise ValueError("重試次數必須大於零")
                except ValueError as e:
                    error_log(f"[Test Workflow] 無效的重試次數: {e}")
                    return {
                        "status": "awaiting_input",
                        "message": "無效的重試次數，請提供大於零的整數",
                        "prompt": "請設定最大重試次數:",
                        "requires_input": True,
                        "session_data": session_data
                    }
            
            session_data["max_retries"] = max_retries
        
        # 進入下一步
        session_data["step"] = 2
        session_data["retry_count"] = 0
        max_retries = session_data.get("max_retries", 5)
        debug_log(2, f"[Test Workflow] 隨機失敗工作流程設定 - 失敗機率: {fail_chance}%, 最大重試次數: {max_retries}")
        return {
            "status": "awaiting_input",  # 改為 awaiting_input，這樣讓用戶可以按 Enter 開始擲骰
            "message": f"已設定失敗機率為 {fail_chance}%, 最大重試次數為 {max_retries}",
            "prompt": "按 Enter 開始擲骰測試，或輸入 '取消' 結束工作流程:",
            "requires_input": True,
            "session_data": session_data
        }
    
    # 步驟2: 執行並可能隨機失敗
    elif step == 2:
        fail_chance = session_data.get("fail_chance", 50)
        retry_count = session_data.get("retry_count", 0)
        
        # 隨機決定是否失敗
        roll = random.randint(1, 100)
        will_fail = roll <= fail_chance
        
        if will_fail:
            # 失敗，自動重試
            retry_count += 1
            session_data["retry_count"] = retry_count
            error_log(f"[Test Workflow] 隨機失敗工作流程故意失敗 (擲骰結果: {roll}, 失敗機率: {fail_chance}%, 重試次數: {retry_count})")
            
            # 如果設定了最大重試次數且達到上限，則終止工作流程
            max_retries = session_data.get("max_retries", 5)  # 預設最大重試5次
            if retry_count >= max_retries:
                error_log(f"[Test Workflow] 隨機失敗工作流程達到最大重試次數 ({max_retries})，停止重試")
                return {
                    "status": "error",
                    "message": f"工作流程已達到最大重試次數 ({max_retries})，放棄執行",
                    "requires_input": False,
                    "session_data": session_data,
                    "result": {
                        "fail_chance": fail_chance,
                        "retry_count": retry_count,
                        "last_roll": roll,
                        "max_retries": max_retries,
                        "completion_time": datetime.datetime.now().isoformat()
                    }
                }
            
            # 未達到最大重試次數，自動繼續嘗試
            return {
                "status": "processing",  # 使用 processing 狀態來觸發自動繼續
                "message": f"工作流程故意失敗 (擲骰結果: {roll}, 閾值: {fail_chance})，自動重試中 (第 {retry_count} 次嘗試)...",
                "requires_input": False,
                "session_data": session_data
            }
        else:
            # 成功，完成工作流程
            debug_log(1, f"[Test Workflow] 隨機失敗工作流程成功 (擲骰結果: {roll}, 失敗機率: {fail_chance}%, 重試次數: {retry_count})")
            return {
                "status": "completed",
                "message": f"工作流程成功！(擲骰結果: {roll}, 閾值: {fail_chance})",
                "requires_input": False,
                "session_data": session_data,
                "result": {
                    "fail_chance": fail_chance,
                    "retry_count": retry_count,
                    "last_roll": roll,
                    "completion_time": datetime.datetime.now().isoformat()
                }
            }
    
    # 未知步驟
    else:
        error_log(f"[Test Workflow] 隨機失敗工作流程中的未知步驟: {step}")
        return {
            "status": "error",
            "message": f"工作流程錯誤: 未知步驟 {step}",
            "requires_input": False,
            "session_data": session_data
        }
    
def tts_test_workflow(session_data: dict, llm_module=None, tts_module=None) -> dict:
    """
    TTS測試工作流程 - 用於測試與TTS模組的整合
    讓用戶輸入文字，調用TTS模組轉換為語音，並播放結果
    
    Args:
        session_data: 工作流程會話數據，包含:
            - step: 當前步驟
            - text: 要轉換為語音的文字
            - mood: 語音情緒
            - save: 是否保存音檔
        llm_module: LLM模組實例 (此工作流程不需要)
        tts_module: TTS模組實例 (用於生成語音)
        
    Returns:
        工作流程狀態與結果
    """
    # 獲取當前步驟
    step = session_data.get("step", 1)
    debug_log(1, f"[Test Workflow] 執行TTS測試工作流程，步驟 {step}")
    
    # 檢查TTS模組是否可用
    if not tts_module:
        error_log("[Test Workflow] TTS測試工作流程需要TTS模組支持，但模組不可用")
        return {
            "status": "error",
            "message": "TTS模組不可用，無法執行此工作流程",
            "requires_input": False,
            "session_data": session_data
        }
    
    # 步驟1: 獲取要轉換為語音的文字
    if step == 1:
        text = session_data.get("text")
        if not text:
            debug_log(2, f"[Test Workflow] TTS測試工作流程需要用戶輸入文字")
            return {
                "status": "awaiting_input",
                "message": "請輸入要轉換為語音的文字",
                "prompt": "請輸入文字:",
                "requires_input": True,
                "session_data": session_data
            }
        
        # 已獲得文字，進入下一步
        session_data["step"] = 2
        debug_log(2, f"[Test Workflow] TTS測試工作流程已獲得文字: {text}")
        return {
            "status": "awaiting_input", 
            "message": f"您輸入的文字為: {text}\n\n接下來請選擇語音情緒",
            "prompt": "請輸入語音情緒 (neutral, happy, sad, angry, excited, calm):",
            "requires_input": True,
            "session_data": session_data
        }
    
    # 步驟2: 獲取語音情緒設置
    elif step == 2:
        text = session_data.get("text", "")
        
        # 有效的情緒選項
        valid_moods = ["neutral", "happy", "sad", "angry", "excited", "calm"]
        mood = session_data.get("user_input", "neutral").lower()
        
        # 驗證情緒輸入是否有效
        if mood not in valid_moods:
            mood = "neutral"  # 默認使用neutral
            debug_log(2, f"[Test Workflow] 無效的語音情緒: {mood}, 使用預設值: neutral")
        
        session_data["mood"] = mood
        
        # 進入下一步
        session_data["step"] = 3
        debug_log(2, f"[Test Workflow] TTS測試工作流程已設置情緒: {mood}")
        return {
            "status": "awaiting_input", 
            "message": f"您選擇的語音情緒為: {mood}\n\n是否要保存語音檔案?",
            "prompt": "是否保存? (y/n):",
            "requires_input": True,
            "session_data": session_data
        }
    
    # 步驟3: 獲取是否保存設置並生成語音
    elif step == 3:
        text = session_data.get("text", "")
        mood = session_data.get("mood", "neutral")
        
        # 處理是否保存的選擇
        save_response = session_data.get("user_input", "").lower()
        save = save_response in ["是", "yes", "y", "保存", "save", "true"]
        session_data["save"] = save
        
        # 進入處理步驟
        session_data["step"] = 4
        debug_log(1, f"[Test Workflow] 準備呼叫TTS模組處理文字")
        
        return {
            "status": "processing",
            "message": f"正在處理文字轉語音...\n文字: {text}\n情緒: {mood}\n{'保存檔案' if save else '不保存檔案'}",
            "requires_input": False,
            "session_data": session_data
        }
    
    # 步驟4: 執行TTS處理
    elif step == 4:
        text = session_data.get("text", "")
        mood = session_data.get("mood", "neutral")
        save = session_data.get("save", False)
        
        # 構建TTS輸入 (TTS模組為非同步調用)
        tts_input = {
            "text": text,
            "mood": mood,
            "save": save
        }
        
        debug_log(1, f"[Test Workflow] 呼叫TTS模組處理文字: {text}, 情緒: {mood}, 保存: {save}")
        
        try:
            # 執行TTS處理 (使用asyncio.run來運行異步函數)
            import asyncio
            tts_output = asyncio.run(tts_module.handle(tts_input))
            
            # 保存結果到會話數據
            session_data["tts_output"] = tts_output
            
            debug_log(1, f"[Test Workflow] TTS處理結果: {tts_output}")
            
            # 檢查TTS處理是否成功
            if tts_output.get("status") == "success":
                output_path = tts_output.get("output_path", "")
                if save and output_path:
                    message = f"TTS處理成功!\n\n語音檔案已保存至: {output_path}"
                else:
                    message = "TTS處理成功! 語音已播放。"
                
                # 完成工作流程
                return {
                    "status": "completed",
                    "message": message,
                    "requires_input": False,
                    "session_data": session_data,
                    "result": {
                        "text": text,
                        "mood": mood,
                        "save": save,
                        "output_path": output_path,
                        "completion_time": datetime.datetime.now().isoformat()
                    }
                }
            else:
                # TTS處理失敗
                error_message = tts_output.get("message", "未知錯誤")
                error_log(f"[Test Workflow] TTS處理失敗: {error_message}")
                return {
                    "status": "error",
                    "message": f"TTS處理失敗: {error_message}",
                    "requires_input": False,
                    "session_data": session_data
                }
                
        except Exception as e:
            error_log(f"[Test Workflow] TTS測試工作流程異常: {str(e)}")
            return {
                "status": "error",
                "message": f"TTS處理錯誤: {str(e)}",
                "requires_input": False,
                "session_data": session_data
            }
    
    # 未知步驟
    else:
        error_log(f"[Test Workflow] TTS測試工作流程中的未知步驟: {step}")
        return {
            "status": "error",
            "message": f"工作流程錯誤: 未知步驟 {step}",
            "requires_input": False,
            "session_data": session_data
        }

# 工作流程工廠函數，用於根據類型返回對應的工作流程函數
def get_test_workflow(workflow_type: str):
    """
    獲取指定類型的測試工作流程函數
    
    Args:
        workflow_type: 工作流程類型
        
    Returns:
        工作流程處理函數或None
    """
    workflow_map = {
        "echo": simple_echo_workflow,
        "countdown": countdown_workflow,
        "data_collector": data_collector_workflow,
        "random_fail": random_fail_workflow,
        "tts_test": tts_test_workflow
    }
    
    return workflow_map.get(workflow_type)
