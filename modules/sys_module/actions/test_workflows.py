"""
測試用工作流程，用於驗證 session-based 工作流架構的運作
包含單步驟、多步驟以及不同複雜度的工作流程範例
"""
import time
from pathlib import Path
import random
import datetime
from utils.debug_helper import debug_log, debug_log_e, info_log, error_log

def simple_echo_workflow(session_data: dict, llm_module=None) -> dict:
    """
    簡單回顯工作流程 - 單步驟工作流程範例
    接收用戶輸入並回傳相同的內容，用於測試最基本的工作流程機制
    
    Args:
        session_data: 工作流程會話數據
        llm_module: LLM模組實例 (此工作流程不需要)
        
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

def countdown_workflow(session_data: dict, llm_module=None) -> dict:
    """
    倒數工作流程 - 多步驟工作流程範例
    從用戶提供的數字開始倒數到零，每步驟減一
    
    Args:
        session_data: 工作流程會話數據，包含:
            - step: 當前步驟
            - count: 當前倒數值
            - original_count: 初始倒數值
        llm_module: LLM模組實例 (此工作流程不需要)
        
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
        session_data["step"] = 2
        debug_log(2, f"[Test Workflow] 倒數工作流程設置起始值: {count}")
        
        return {
            "status": "processing",
            "message": f"開始從 {count} 倒數",
            "requires_input": False,
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

def data_collector_workflow(session_data: dict, llm_module=None) -> dict:
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
            "status": "processing", 
            "message": f"您好，{name}！",
            "requires_input": False,
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
            "status": "processing",
            "message": f"已記錄年齡: {age}",
            "requires_input": False,
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
            "status": "processing",
            "message": f"已記錄 {len(interests)} 項興趣",
            "requires_input": False,
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

請用輕鬆活潑的語氣，並加入一些與用戶興趣相關的有趣評論。
"""
                response = llm_module.handle({
                    "text": prompt,
                    "intent": "chat",
                    "is_internal": True
                })
                
                if response and response.get("status") == "ok" and "text" in response:
                    enhanced_summary = response["text"]
                    debug_log(2, f"[Test Workflow] LLM成功生成增強摘要")
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

def random_fail_workflow(session_data: dict, llm_module=None) -> dict:
    """
    隨機失敗工作流程 - 測試錯誤處理機制
    在執行過程中有機率隨機失敗，用於測試系統的錯誤恢復能力
    
    Args:
        session_data: 工作流程會話數據，包含:
            - step: 當前步驟
            - fail_chance: 失敗機率 (0-100)
            - retry_count: 重試次數
        llm_module: LLM模組實例 (此工作流程不需要)
        
    Returns:
        工作流程狀態與結果
    """
    # 獲取當前步驟
    step = session_data.get("step", 1)
    debug_log(1, f"[Test Workflow] 執行隨機失敗工作流程，步驟 {step}")
    
    # 步驟1: 設定失敗機率
    if step == 1:
        fail_chance = session_data.get("fail_chance")
        if fail_chance is None:
            debug_log(2, f"[Test Workflow] 隨機失敗工作流程需要設定失敗機率")
            return {
                "status": "awaiting_input",
                "message": "此工作流程將測試系統的錯誤處理能力",
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
        
        # 進入下一步
        session_data["step"] = 2
        session_data["retry_count"] = 0
        debug_log(2, f"[Test Workflow] 隨機失敗工作流程設定失敗機率: {fail_chance}%")
        return {
            "status": "processing",
            "message": f"已設定失敗機率為 {fail_chance}%",
            "requires_input": False,
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
            # 失敗，詢問是否重試
            retry_count += 1
            session_data["retry_count"] = retry_count
            error_log(f"[Test Workflow] 隨機失敗工作流程故意失敗 (擲骰結果: {roll}, 失敗機率: {fail_chance}%, 重試次數: {retry_count})")
            
            return {
                "status": "error",
                "message": f"工作流程故意失敗 (擲骰結果: {roll}, 閾值: {fail_chance})",
                "prompt": f"這是第 {retry_count} 次失敗，是否重試？(是/否)",
                "requires_input": True,
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
        "random_fail": random_fail_workflow
    }
    
    return workflow_map.get(workflow_type)
