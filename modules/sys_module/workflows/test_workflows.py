"""
modules/sys_module/workflows/test_workflows.py
Test and demo workflow definitions for the SYS module

包含各種測試和演示工作流程的定義，用於驗證工作流程引擎的功能。
這些工作流程都使用標準的工作流程引擎架構。
"""

from typing import Dict, Any, List, Optional, Tuple, Callable, Union
import datetime
import random

from core.sessions.session_manager import WorkflowSession
from utils.debug_helper import info_log, error_log, debug_log

# Import the workflow engine components
# We need to import directly from the module file to avoid circular imports
import sys
import os
import importlib.util

# Load workflows.py directly
workflows_path = os.path.join(os.path.dirname(__file__), '..', 'workflows.py')
spec = importlib.util.spec_from_file_location("workflows", workflows_path)
workflows_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflows_module)

# Get the classes we need
WorkflowDefinition = workflows_module.WorkflowDefinition
WorkflowEngine = workflows_module.WorkflowEngine
WorkflowStep = workflows_module.WorkflowStep
StepResult = workflows_module.StepResult
StepTemplate = workflows_module.StepTemplate
WorkflowType = workflows_module.WorkflowType
WorkflowMode = workflows_module.WorkflowMode


def create_echo_workflow(session: WorkflowSession) -> WorkflowEngine:
    """創建回顯測試工作流程引擎"""
    workflow_def = WorkflowDefinition(
        workflow_type="echo",
        name="回顯測試工作流程",
        description="簡單的回顯測試，用於驗證工作流程基本功能",
        workflow_mode=WorkflowMode.DIRECT,  # 測試工作流為直接模式
        requires_llm_review=False  # 測試工作流不需要 LLM 審核
    )
    
    # 創建輸入步驟
    input_step = StepTemplate.create_input_step(
        session, 
        "echo_input", 
        "請輸入要回顯的訊息:"
    )
    
    # 創建處理步驟
    def echo_processor(session):
        message = session.get_data("echo_input", "")
        return StepResult.complete_workflow(
            f"已完成訊息回顯: {message}",
            {
                "echo_message": message,
                "timestamp": datetime.datetime.now().isoformat()
            }
        )
    
    process_step = StepTemplate.create_processing_step(
        session,
        "echo_process",
        echo_processor,
        ["echo_input"],
        True
    )
    
    # 添加步驟和轉換
    workflow_def.add_step(input_step)
    workflow_def.add_step(process_step)
    workflow_def.set_entry_point("echo_input")
    workflow_def.add_transition("echo_input", "echo_process")
    workflow_def.add_transition("echo_process", "END")
    
    # 創建引擎
    engine = WorkflowEngine(workflow_def, session)
    engine.auto_advance = True
    
    return engine


def create_countdown_workflow(session: WorkflowSession) -> WorkflowEngine:
    """創建倒數測試工作流程引擎"""
    workflow_def = WorkflowDefinition(
        workflow_type="countdown",
        name="倒數測試工作流程",
        description="倒數測試，用於驗證多步驟工作流程",
        workflow_mode=WorkflowMode.DIRECT,
        requires_llm_review=False
    )
    
    # 步驟 1: 輸入起始數字
    def validate_count(value):
        try:
            count = int(value)
            return count > 0
        except ValueError:
            return False
            
    input_step = StepTemplate.create_input_step(
        session,
        "countdown_input",
        "請輸入一個正整數作為倒數起始值:",
        lambda x: (validate_count(x), "請輸入一個大於零的整數")
    )
    
    # 步驟 2: 初始化倒數
    def initialize_countdown(session):
        import time
        start_count = int(session.get_data("countdown_input", "0"))
        session.add_data("current_count", start_count)
        session.add_data("original_count", start_count)
        print(f"🚀 開始倒數，從 {start_count} 開始...")
        return StepResult.success(f"倒數初始化完成，起始值: {start_count}")
    
    init_step = StepTemplate.create_auto_step(
        session,
        "countdown_init",
        initialize_countdown,
        ["countdown_input"],
        "初始化倒數..."
    )
    
    # 步驟 3: 倒數循環
    def countdown_processor(session):
        import time
        current_count = session.get_data("current_count", 0)
        
        if current_count <= 0:
            # 倒數完成，退出循環進入下一步驟
            original_count = session.get_data("original_count", 0)
            print("🎉 倒數完成！")
            session.add_data("countdown_completed", True)
            return StepResult.success(
                f"倒數完成！從 {original_count} 到 0",
                {
                    "countdown_completed": True
                }
            )
        
        # 顯示當前倒數值
        print(f"⏰ 倒數: {current_count}")
        
        # 添加延遲以模擬真實倒數
        time.sleep(1)
        
        # 繼續倒數
        new_count = current_count - 1
        session.add_data("current_count", new_count)
        
        return StepResult.success(
            f"倒數: {current_count} -> {new_count}",
            continue_current_step=True  # 繼續在當前步驟
        )
    
    def should_continue_countdown(session):
        current_count = session.get_data("current_count", 0)
        return current_count > 0
    
    countdown_step = StepTemplate.create_loop_step(
        session,
        "countdown_loop",
        countdown_processor,
        should_continue_countdown,
        ["current_count"],
        "倒數進行中..."
    )
    
    # 步驟 4: 生成倒數完成報告
    def generate_countdown_report(session):
        original_count = session.get_data("original_count", 0)
        print(f"\n📊 倒數測試完成報告")
        print(f"🚀 起始值: {original_count}")
        print(f"🏁 結束值: 0")
        print(f"⏱️ 總耗時: 約 {original_count} 秒")
        
        return StepResult.complete_workflow(
            f"倒數測試完成！從 {original_count} 倒數到 0",
            {
                "original_count": original_count,
                "countdown_completed": True,
                "total_duration": original_count,
                "completion_time": datetime.datetime.now().isoformat()
            }
        )
    
    countdown_report_step = StepTemplate.create_auto_step(
        session,
        "countdown_report",
        generate_countdown_report,
        ["original_count", "countdown_completed"],
        "正在生成倒數報告..."
    )
    
    # 添加步驟和轉換
    workflow_def.add_step(input_step)
    workflow_def.add_step(init_step)
    workflow_def.add_step(countdown_step)
    workflow_def.add_step(countdown_report_step)
    workflow_def.set_entry_point("countdown_input")
    workflow_def.add_transition("countdown_input", "countdown_init")
    workflow_def.add_transition("countdown_init", "countdown_loop")
    workflow_def.add_transition("countdown_loop", "countdown_report")
    workflow_def.add_transition("countdown_report", "END")
    
    # 創建引擎
    engine = WorkflowEngine(workflow_def, session)
    engine.auto_advance = True
    
    return engine


def create_data_collector_workflow(session: WorkflowSession, llm_module=None) -> WorkflowEngine:
    """創建資料收集測試工作流程引擎"""
    workflow_def = WorkflowDefinition(
        workflow_type="data_collector",
        name="資料收集測試工作流程",
        description="收集用戶資料並生成摘要報告，用於測試多步驟工作流程和LLM整合",
        workflow_mode=WorkflowMode.DIRECT,
        requires_llm_review=False
    )
    
    # 步驟 1: 收集姓名
    name_step = StepTemplate.create_input_step(
        session,
        "name_input",
        "【步驟 1/4】請輸入您的姓名（例如：張三）"
    )
    
    # 步驟 2: 收集年齡
    def validate_age(value):
        try:
            age = int(value)
            return 1 <= age <= 120
        except ValueError:
            return False
            
    age_step = StepTemplate.create_input_step(
        session,
        "age_input",
        "【步驟 2/4】請輸入您的年齡（1-120之間的數字）",
        lambda x: (validate_age(x), "❌ 年齡必須是 1-120 之間的數字，請重新輸入"),
        ["name_input"]
    )
    
    # 步驟 3: 收集興趣
    interests_step = StepTemplate.create_input_step(
        session,
        "interests_input",
        "【步驟 3/4】請輸入您的興趣（多個興趣請用逗號分隔，例如：閱讀, 旅遊, 音樂）",
        required_data=["name_input", "age_input"]
    )
    
    # 步驟 4: 收集反饋
    feedback_step = StepTemplate.create_input_step(
        session,
        "feedback_input",
        "【步驟 4/4】請分享您對此工作流程測試的想法或建議",
        required_data=["name_input", "age_input", "interests_input"]
    )
    
    # 步驟 5: 生成摘要
    def generate_summary(session):
        name = session.get_data("name_input", "未提供")
        age = session.get_data("age_input", "未提供")
        interests_raw = session.get_data("interests_input", "")
        feedback = session.get_data("feedback_input", "未提供")
        
        # 處理興趣列表
        if isinstance(interests_raw, str):
            interests = [i.strip() for i in interests_raw.split(",") if i.strip()]
        else:
            interests = interests_raw if isinstance(interests_raw, list) else []
            
        interests_text = "、".join(interests) if interests else "無"
        
        # 生成基本摘要
        basic_summary = f"""
資料收集摘要報告
------------------
姓名: {name}
年齡: {age}
興趣: {interests_text}
反饋: {feedback}
------------------
收集時間: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
        
        enhanced_summary = ""
        
        # 如果有LLM模組，生成增強摘要
        if llm_module:
            try:
                debug_log(2, f"[Workflow] 使用LLM生成增強摘要")
                prompt = f"""請根據以下收集到的用戶資料，生成一個友好、有個性的摘要報告：

姓名: {name}
年齡: {age}
興趣: {interests_text}
反饋: "{feedback}"

請用輕鬆活潑的語氣，並加入一些與用戶興趣相關的有趣評論。格式可以自由發揮，但請確保內容豐富且有個性。"""
                
                # 使用直接模式避免系統提示詞
                response = llm_module.handle({
                    "text": prompt,
                    "intent": "direct",
                    "is_internal": True
                })
                
                if response and response.get("status") == "ok" and "text" in response:
                    enhanced_summary = response["text"]
                    debug_log(2, f"[Workflow] LLM成功生成增強摘要")

            except Exception as e:
                error_log(f"[Workflow] LLM生成增強摘要失敗: {e}")
        
        return StepResult.complete_workflow(
            "資料收集完成，已生成摘要報告",
            {
                "name": name,
                "age": int(age) if str(age).isdigit() else age,
                "interests": interests,
                "feedback": feedback,
                "basic_summary": basic_summary,
                "enhanced_summary": enhanced_summary
            }
        )
    
    summary_step = StepTemplate.create_processing_step(
        session,
        "generate_summary",
        generate_summary,
        ["name_input", "age_input", "interests_input", "feedback_input"],
        True
    )
    
    # 添加步驟和轉換
    workflow_def.add_step(name_step)
    workflow_def.add_step(age_step)
    workflow_def.add_step(interests_step)
    workflow_def.add_step(feedback_step)
    workflow_def.add_step(summary_step)
    
    workflow_def.set_entry_point("name_input")
    workflow_def.add_transition("name_input", "age_input")
    workflow_def.add_transition("age_input", "interests_input")
    workflow_def.add_transition("interests_input", "feedback_input")
    workflow_def.add_transition("feedback_input", "generate_summary")
    workflow_def.add_transition("generate_summary", "END")
    
    # 創建引擎
    engine = WorkflowEngine(workflow_def, session)
    engine.auto_advance = True
    
    return engine


def create_random_fail_workflow(session: WorkflowSession) -> WorkflowEngine:
    """創建隨機失敗測試工作流程引擎"""
    workflow_def = WorkflowDefinition(
        workflow_type="random_fail",
        name="隨機失敗測試工作流程",
        description="測試系統錯誤處理與自動重試能力的工作流程",
        workflow_mode=WorkflowMode.DIRECT,
        requires_llm_review=False
    )
    
    # 步驟 1: 設定失敗機率
    def validate_chance(value):
        try:
            chance = int(value)
            return 0 <= chance <= 100
        except ValueError:
            return False
            
    chance_step = StepTemplate.create_input_step(
        session,
        "fail_chance_input",
        "此工作流程將測試系統的錯誤處理與自動重試能力\n請設定失敗機率 (0-100):",
        lambda x: (validate_chance(x), "請輸入0-100之間的數字")
    )
    
    # 步驟 2: 設定最大重試次數
    def validate_retries(value):
        if not value.strip():  # 允許空值，使用默認值
            return True
        try:
            retries = int(value)
            return retries > 0
        except ValueError:
            return False
            
    retries_step = StepTemplate.create_input_step(
        session,
        "max_retries_input",
        "請設定最大重試次數 (預設5次，直接按Enter可使用預設值):",
        lambda x: (validate_retries(x), "請輸入大於零的整數或留空使用預設值"),
        ["fail_chance_input"],
        optional=True
    )
    
    # 步驟 3: 確認開始測試
    confirmation_step = StepTemplate.create_confirmation_step(
        session,
        "start_confirmation",
        lambda: f"已設定失敗機率為 {session.get_data('fail_chance_input', 'unknown')}%, "
                f"最大重試次數為 {session.get_data('max_retries_input', '5') or '5'}。"
                f"按 Enter 開始擲骰測試，或輸入 '取消' 結束工作流程:",
        "開始執行隨機失敗測試...",
        "測試已取消",
        ["fail_chance_input", "max_retries_input"]
    )
    
    # 步驟 4: 初始化測試
    def initialize_test(session):
        max_retries_input = session.get_data("max_retries_input", "5")
        max_retries = int(max_retries_input) if max_retries_input.strip() else 5
        fail_chance = int(session.get_data("fail_chance_input", "50"))
        
        session.add_data("retry_count", 0)
        session.add_data("max_retries", max_retries)
        session.add_data("test_completed", False)
        
        print(f"🎲 開始隨機失敗測試，失敗機率: {fail_chance}%, 最大重試次數: {max_retries}")
        return StepResult.success(f"測試初始化完成")
    
    init_test_step = StepTemplate.create_auto_step(
        session,
        "init_test",
        initialize_test,
        ["fail_chance_input", "max_retries_input"],
        "初始化測試..."
    )
    
    # 步驟 5: 擲骰循環測試
    def dice_roll_processor(session):
        import time
        fail_chance = int(session.get_data("fail_chance_input", "50"))
        max_retries = session.get_data("max_retries", 5)
        retry_count = session.get_data("retry_count", 0)
        
        # 檢查是否已達到最大重試次數
        if retry_count >= max_retries:
            # 達到最大重試次數，退出循環並進入下一步驟
            session.add_data("test_result", "max_retries_reached")
            session.add_data("final_roll", None)
            return StepResult.success(
                f"已達到最大重試次數 ({max_retries})，進入結果處理階段",
                {
                    "fail_chance": fail_chance,
                    "retry_count": retry_count,
                    "max_retries": max_retries,
                    "test_result": "max_retries_reached"
                }
            )
        
        # 擲骰子
        roll = random.randint(1, 100)
        will_fail = roll <= fail_chance
        
        # 增加重試計數
        retry_count += 1
        session.add_data("retry_count", retry_count)
        session.add_data("final_roll", roll)
        
        print(f"🎲 第 {retry_count} 次嘗試 - 擲骰結果: {roll} (失敗閾值: {fail_chance})")
        
        if will_fail:
            # 失敗，記錄並繼續循環
            error_log(f"[Workflow] 第 {retry_count} 次嘗試失敗 (擲骰: {roll} <= {fail_chance})")
            print(f"❌ 測試失敗，準備重試... (第 {retry_count}/{max_retries} 次)")
            
            # 添加短暫延遲
            time.sleep(0.5)
            
            return StepResult.success(
                f"第 {retry_count} 次嘗試失敗，繼續重試",
                continue_current_step=True
            )
        else:
            # 成功，退出循環並進入下一步驟
            debug_log(1, f"[Workflow] 第 {retry_count} 次嘗試成功 (擲骰: {roll} > {fail_chance})")
            print(f"🎉 測試成功！")
            
            session.add_data("test_result", "success")
            return StepResult.success(
                f"測試成功！(擲骰結果: {roll}, 閾值: {fail_chance}, 嘗試次數: {retry_count})",
                {
                    "fail_chance": fail_chance,
                    "retry_count": retry_count,
                    "final_roll": roll,
                    "max_retries": max_retries,
                    "test_result": "success"
                }
            )
    
    def should_continue_test(session):
        retry_count = session.get_data("retry_count", 0)
        max_retries = session.get_data("max_retries", 5)
        test_result = session.get_data("test_result", None)
        # 只要還沒達到最大重試次數且沒有成功就繼續
        return retry_count < max_retries and test_result is None
    
    dice_roll_step = StepTemplate.create_loop_step(
        session,
        "dice_roll_loop",
        dice_roll_processor,
        should_continue_test,
        ["retry_count", "max_retries"],
        "正在執行擲骰測試..."
    )
    
    # 步驟 6: 生成最終結果報告
    def generate_final_report(session):
        fail_chance = int(session.get_data("fail_chance_input", "50"))
        retry_count = session.get_data("retry_count", 0)
        max_retries = session.get_data("max_retries", 5)
        test_result = session.get_data("test_result", "unknown")
        final_roll = session.get_data("final_roll", None)
        
        print(f"\n📊 生成測試報告...")
        
        if test_result == "success":
            print(f"✅ 測試結果: 成功")
            print(f"🎲 最終擲骰: {final_roll}")
            print(f"🔢 嘗試次數: {retry_count}/{max_retries}")
            print(f"📈 成功率: {(1/retry_count)*100:.1f}% (理論: {100-fail_chance}%)")
            
            return StepResult.complete_workflow(
                f"隨機失敗測試成功完成！共嘗試 {retry_count} 次",
                {
                    "test_result": "success",
                    "fail_chance": fail_chance,
                    "retry_count": retry_count,
                    "final_roll": final_roll,
                    "max_retries": max_retries,
                    "actual_success_rate": (1/retry_count)*100,
                    "theoretical_success_rate": 100-fail_chance,
                    "completion_time": datetime.datetime.now().isoformat()
                }
            )
        else:
            print(f"❌ 測試結果: 失敗 (達到最大重試次數)")
            print(f"🔢 嘗試次數: {retry_count}/{max_retries}")
            print(f"📉 失敗率: 100% (理論: {fail_chance}%)")
            
            return StepResult.complete_workflow(
                f"隨機失敗測試失敗，已達到最大重試次數 {max_retries}",
                {
                    "test_result": "max_retries_reached",
                    "fail_chance": fail_chance,
                    "retry_count": retry_count,
                    "max_retries": max_retries,
                    "theoretical_fail_rate": fail_chance,
                    "completion_time": datetime.datetime.now().isoformat()
                }
            )
    
    final_report_step = StepTemplate.create_auto_step(
        session,
        "final_report",
        generate_final_report,
        ["test_result", "retry_count", "max_retries"],
        "正在生成最終報告..."
    )
    
    # 添加步驟和轉換
    workflow_def.add_step(chance_step)
    workflow_def.add_step(retries_step)
    workflow_def.add_step(confirmation_step)
    workflow_def.add_step(init_test_step)
    workflow_def.add_step(dice_roll_step)
    workflow_def.add_step(final_report_step)
    
    workflow_def.set_entry_point("fail_chance_input")
    workflow_def.add_transition("fail_chance_input", "max_retries_input")
    workflow_def.add_transition("max_retries_input", "start_confirmation")
    workflow_def.add_transition("start_confirmation", "init_test")
    workflow_def.add_transition("init_test", "dice_roll_loop")
    workflow_def.add_transition("dice_roll_loop", "final_report")
    workflow_def.add_transition("final_report", "END")
    
    # 創建引擎
    engine = WorkflowEngine(workflow_def, session)
    engine.auto_advance = True
    
    return engine


def create_tts_test_workflow(session: WorkflowSession, tts_module=None) -> WorkflowEngine:
    """創建TTS測試工作流程引擎"""
    workflow_def = WorkflowDefinition(
        workflow_type="tts_test",
        name="TTS測試工作流程",
        description="測試與TTS模組整合的工作流程，包含文字輸入、情緒選擇和語音生成",
        workflow_mode=WorkflowMode.DIRECT,
        requires_llm_review=False
    )
    
    # 檢查TTS模組是否可用
    if not tts_module:
        # 創建錯誤步驟
        class TTSErrorStep(WorkflowStep):
            def __init__(self, session):
                super().__init__(session)
                self.set_id("tts_error")
                self.set_step_type(self.STEP_TYPE_PROCESSING)
                
            def get_prompt(self) -> str:
                return "TTS模組檢查中..."
                
            def execute(self, user_input: Any = None) -> StepResult:
                return StepResult.failure("TTS模組不可用，無法執行此工作流程")
                
            def should_auto_advance(self) -> bool:
                return True
        
        error_step = TTSErrorStep(session)
        workflow_def.add_step(error_step)
        workflow_def.set_entry_point("tts_error")
        workflow_def.add_transition("tts_error", "END")
        
        engine = WorkflowEngine(workflow_def, session)
        engine.auto_advance = True
        return engine
    
    # 步驟 1: 輸入文字
    text_step = StepTemplate.create_input_step(
        session,
        "text_input",
        "請輸入要轉換為語音的文字:"
    )
    
    # 步驟 2: 選擇情緒
    emotion_step = StepTemplate.create_selection_step(
        session,
        "emotion_input",
        "請選擇情緒:",
        ["neutral", "happy", "sad", "angry", "surprised"],
        ["中性", "開心", "悲傷", "憤怒", "驚訝"],
        ["text_input"]
    )
    
    # 步驟 3: 生成語音並播放
    def generate_and_play_tts(session):
        text = session.get_data("text_input", "")
        emotion = session.get_data("emotion_input", "neutral")
        
        if not text.strip():
            return StepResult.failure("Text content cannot be empty")
        
        try:
            import asyncio
            
            debug_log(2, f"[Workflow] 呼叫TTS模組生成語音，文字: {text}, 情緒: {emotion}")
            
            # TTS 模組的 handle 方法是異步的，需要正確調用
            try:
                # 構建TTS請求數據
                tts_data = {
                    "text": text,
                    "mood": emotion,  # 注意：TTS模組使用 'mood' 而不是 'emotion'
                    "save": False,    # 不保存文件，直接播放
                    "force_chunking": False
                }
                
                # 使用 asyncio.run 調用異步方法
                result = asyncio.run(tts_module.handle(tts_data))
                
            except Exception as e:
                error_log(f"[Workflow] TTS調用失敗: {e}")
                return StepResult.failure(f"TTS調用失敗: {e}")
            
            if result and result.get("status") == "success":
                debug_log(2, f"[Workflow] TTS生成成功")
                return StepResult.complete_workflow(
                    f"語音生成完成！文字: {text}, 情緒: {emotion}",
                    {
                        "text": text,
                        "emotion": emotion,
                        "tts_result": result,
                        "completion_time": datetime.datetime.now().isoformat()
                    }
                )
            else:
                error_msg = result.get("message", "未知錯誤") if result else "TTS模組無回應"
                return StepResult.failure(f"TTS生成失敗: {error_msg}")
                
        except Exception as e:
            error_log(f"[Workflow] TTS生成過程中發生錯誤: {e}")
            return StepResult.failure(f"TTS生成過程中發生錯誤: {e}")
    
    tts_step = StepTemplate.create_processing_step(
        session,
        "generate_tts",
        generate_and_play_tts,
        ["text_input", "emotion_input"],
        True
    )
    
    # 添加步驟和轉換
    workflow_def.add_step(text_step)
    workflow_def.add_step(emotion_step)
    workflow_def.add_step(tts_step)
    
    workflow_def.set_entry_point("text_input")
    workflow_def.add_transition("text_input", "emotion_input")
    workflow_def.add_transition("emotion_input", "generate_tts")
    workflow_def.add_transition("generate_tts", "END")
    
    # 創建引擎
    engine = WorkflowEngine(workflow_def, session)
    engine.auto_advance = True
    
    return engine


# 測試工作流程註冊表
TEST_WORKFLOWS = {
    "echo": create_echo_workflow,
    "countdown": create_countdown_workflow,
    "data_collector": create_data_collector_workflow,
    "random_fail": create_random_fail_workflow,
    # tts_test 已移除，TTS 模組已重構，應在 TTS 模組測試中直接測試
}


def create_test_workflow(workflow_type: str, session: WorkflowSession, **kwargs) -> WorkflowEngine:
    """
    創建測試工作流程的統一入口
    
    Args:
        workflow_type: 工作流程類型
        session: 工作流程會話
        **kwargs: 其他參數，如 llm_module, tts_module 等
    
    Returns:
        WorkflowEngine: 配置完成的工作流程引擎
    """
    if workflow_type not in TEST_WORKFLOWS:
        raise ValueError(f"未支援的測試工作流程類型: {workflow_type}")
    
    workflow_factory = TEST_WORKFLOWS[workflow_type]
    
    # 檢查工作流程是否需要特定參數
    if workflow_type == "data_collector":
        return workflow_factory(session, kwargs.get("llm_module"))
    else:
        return workflow_factory(session)


def get_available_test_workflows() -> List[str]:
    """取得可用的測試工作流程列表"""
    return list(TEST_WORKFLOWS.keys())
