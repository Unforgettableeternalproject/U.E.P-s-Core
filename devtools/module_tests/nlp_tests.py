# -*- coding: utf-8 -*-
"""
NLP 模組測試函數
已重構模組 - 完整功能測試
"""

from utils.debug_helper import debug_log, info_log, error_log

def nlp_test(modules, text: str = "", enable_identity: bool = True, enable_segmentation: bool = True):
    """測試增強版NLP模組 - 包含語者身份和意圖分析"""
    nlp = modules.get("nlp")
    if nlp is None:
        error_log("[Controller] ❌ 無法載入 NLP 模組")
        return

    test_text = text if text else "Hello UEP, please save my work and then play some music"
    
    print(f"\n🧠 測試增強版NLP - 文本: '{test_text}'")
    print("=" * 60)
    
    # 準備測試輸入
    nlp_input = {
        "text": test_text,
        "speaker_id": "test_speaker_001",
        "speaker_confidence": 0.85,
        "speaker_status": "known",
        "enable_identity_processing": enable_identity,
        "enable_segmentation": enable_segmentation,
        "current_system_state": "idle",
        "conversation_history": []
    }
    
    try:
        result = nlp.handle(nlp_input)
        
        print(f"📝 原始文本: {result.get('original_text', 'N/A')}")
        print(f"🎯 主要意圖: {result.get('primary_intent', 'N/A')}")
        print(f"📊 整體信心度: {result.get('overall_confidence', 0):.3f}")
        
        # 語者身份信息
        identity = result.get('identity')
        if identity:
            print(f"👤 語者身份: {identity.get('identity_id', 'N/A')}")
            print(f"🔄 身份動作: {result.get('identity_action', 'N/A')}")
        else:
            print("👤 語者身份: 未識別")
        
        # 意圖分段
        segments = result.get('intent_segments', [])
        print(f"\n📋 意圖分段 ({len(segments)}個):")
        for i, segment in enumerate(segments, 1):
            if hasattr(segment, 'text'):
                print(f"  {i}. '{segment.text}' -> {segment.intent} (信心度: {segment.confidence:.3f})")
            else:
                print(f"  {i}. '{segment.get('text', 'N/A')}' -> {segment.get('intent', 'N/A')}")
        
        # 上下文信息
        context_ids = result.get('context_ids', [])
        if context_ids:
            print(f"\n🔗 創建的上下文: {len(context_ids)}個")
            for ctx_id in context_ids:
                print(f"  - {ctx_id}")
        
        # 執行計劃
        execution_plan = result.get('execution_plan', [])
        if execution_plan:
            print(f"\n📋 執行計劃:")
            for plan_item in execution_plan:
                print(f"  步驟{plan_item.get('step', 'N/A')}: {plan_item.get('description', 'N/A')} (優先級: {plan_item.get('priority', 'N/A')})")
        
        # 狀態轉換
        state_transition = result.get('state_transition')
        if state_transition:
            print(f"\n🔄 狀態轉換: {state_transition}")
        
        # 下一步模組
        next_modules = result.get('next_modules', [])
        if next_modules:
            print(f"➡️ 下一步模組: {', '.join(next_modules)}")
        
        # 處理註記
        processing_notes = result.get('processing_notes', [])
        if processing_notes:
            print(f"\n📝 處理註記:")
            for note in processing_notes:
                print(f"  - {note}")
        
        return result
        
    except Exception as e:
        error_log(f"[NLP] 增強版測試失敗: {e}")
        return None

def nlp_test_state_queue_integration(modules, text: str = ""):
    """測試NLP與狀態佇列的整合"""
    nlp = modules.get("nlp")    
    
    if nlp is None:
        error_log("[Controller] ❌ 無法載入 NLP 模組")
        return

    from core.state_queue import get_state_queue_manager
    state_queue = get_state_queue_manager()

    test_text = text if text else "Hi UEP, how are you? Please save my work and then remind me about the meeting."
    
    print(f"\n🔄 測試NLP與狀態佇列整合")
    print(f"📝 測試文本: '{test_text}'")
    print("=" * 80)
    
    # 清空佇列開始測試
    state_queue.clear_queue()
    print(f"🧹 清空狀態佇列")
    
    # 顯示初始狀態
    initial_status = state_queue.get_queue_status()
    print(f"🏁 初始狀態: {initial_status['current_state']}")
    print(f"📋 初始佇列長度: {initial_status['queue_length']}")
    
    # 執行NLP分析
    result = nlp_test(test_text, enable_segmentation=True)
    
    # 顯示分析後的狀態佇列
    print(f"\n📊 NLP分析後的狀態佇列:")
    final_status = state_queue.get_queue_status()
    print(f"🎯 當前狀態: {final_status['current_state']}")
    print(f"📋 佇列長度: {final_status['queue_length']}")
    
    if final_status['queue_items']:
        print(f"📝 佇列內容:")
        for i, item in enumerate(final_status['queue_items'], 1):
            print(f"  {i}. {item['state']} (優先級: {item['priority']})")
            print(f"     觸發: {item['trigger_content']}")
            print(f"     上下文: {item['context_content']}")
            print()
    
    return result

def nlp_test_multi_intent(modules, text: str = ""):
    """測試多意圖上下文管理"""
    nlp = modules.get("nlp")
    if nlp is None:
        error_log("[Controller] ❌ 無法載入 NLP 模組")
        return

    test_text = text if text else "Hey system, please save my document and then remind me about the meeting tomorrow"
    
    print(f"\n🔄 測試多意圖上下文管理")
    print(f"📝 測試文本: '{test_text}'")
    print("=" * 70)
    
    result = nlp_test(test_text, enable_segmentation=True)
    
    if result and hasattr(nlp, 'intent_analyzer'):
        analyzer = nlp.intent_analyzer
        
        # 獲取上下文摘要
        context_summary = analyzer.get_context_summary()
        print(f"\n📊 上下文管理摘要:")
        print(f"  活躍上下文: {context_summary.get('active_contexts', 0)}")
        print(f"  待執行上下文: {context_summary.get('pending_contexts', 0)}")
        print(f"  已完成上下文: {context_summary.get('completed_contexts', 0)}")
        
        # 獲取下一個可執行的上下文
        next_context = analyzer.get_next_context()
        if next_context:
            state, context = next_context
            print(f"\n➡️ 下一個可執行上下文:")
            print(f"  上下文ID: {context.context_id}")
            print(f"  類型: {context.context_type.value}")
            print(f"  任務描述: {context.task_description or context.conversation_topic}")
            print(f"  優先級: {context.priority}")
        else:
            print(f"\n➡️ 無待執行的上下文")

def nlp_test_identity_management(modules, speaker_id: str = "test_user"):
    """測試語者身份管理"""
    nlp = modules.get("nlp")
    if nlp is None:
        error_log("[Controller] ❌ 無法載入 NLP 模組")
        return

    print(f"\n👤 測試語者身份管理 - 語者ID: {speaker_id}")
    print("=" * 50)
    
    # 多次交互測試身份累積和識別
    test_interactions = [
        "Hello, I'm testing the system",
        "Can you help me organize my files?", 
        "I want to schedule a meeting for tomorrow",
        "Play my favorite music please"
    ]
    
    for i, text in enumerate(test_interactions, 1):
        print(f"\n--- 交互 {i} ---")
        
        nlp_input = {
            "text": text,
            "speaker_id": speaker_id,
            "speaker_confidence": 0.8 + (i * 0.05),  # 逐漸提高信心度
            "speaker_status": "known" if i > 2 else "accumulating",
            "enable_identity_processing": True,
            "enable_segmentation": True
        }
        
        result = nlp.handle(nlp_input)
        
        print(f"文本: '{text}'")
        print(f"身份動作: {result.get('identity_action', 'N/A')}")
        
        identity = result.get('identity')
        if identity:
            print(f"身份ID: {identity.get('identity_id', 'N/A')}")
            print(f"互動次數: {identity.get('interaction_stats', {}).get('total_interactions', 0)}")

def nlp_analyze_context_queue(modules):
    """分析NLP模組的上下文佇列狀態"""
    nlp = modules.get("nlp")
    if nlp is None:
        error_log("[Controller] ❌ 無法載入 NLP 模組")
        return

    if not hasattr(nlp, 'context_manager'):
        print("❌ NLP模組沒有上下文管理器")
        return

    context_manager = nlp.context_manager
    
    print(f"\n📊 多意圖上下文佇列分析")
    print("=" * 40)
    
    # 獲取佇列狀態
    summary = context_manager.get_context_summary()
    
    print(f"總上下文數: {len(context_manager.contexts)}")
    print(f"活躍上下文: {len(context_manager.active_contexts)}")
    print(f"已完成上下文: {len(context_manager.completed_contexts)}")
    print(f"佇列長度: {len(context_manager.state_queue)}")
    
    # 顯示活躍上下文詳情
    if context_manager.active_contexts:
        print(f"\n🔄 活躍上下文:")
        for ctx_id in context_manager.active_contexts:
            if ctx_id in context_manager.contexts:
                ctx = context_manager.contexts[ctx_id]
                print(f"  {ctx_id}: {ctx.context_type.value} - {ctx.task_description or ctx.conversation_topic}")
    
    # 顯示佇列中的條目
    if context_manager.state_queue:
        print(f"\n📋 佇列條目:")
        for i, entry in enumerate(context_manager.state_queue[:5]):  # 只顯示前5個
            ctx = entry.context
            print(f"  {i+1}. {ctx.context_id}: {ctx.context_type.value} (優先級: {ctx.priority})")
        
        if len(context_manager.state_queue) > 5:
            print(f"  ... 還有 {len(context_manager.state_queue) - 5} 個條目")

def nlp_clear_contexts(modules):
    """清空NLP模組的上下文"""
    nlp = modules.get("nlp")
    if nlp is None:
        error_log("[Controller] ❌ 無法載入 NLP 模組")
        return

    if not hasattr(nlp, 'context_manager'):
        print("❌ NLP模組沒有上下文管理器")
        return

    context_manager = nlp.context_manager
    
    # 清空上下文
    context_manager.contexts.clear()
    context_manager.active_contexts.clear()
    context_manager.completed_contexts.clear()
    context_manager.state_queue.clear()
    context_manager.dependency_graph.clear()
    
    print("✅ 已清空所有NLP上下文")