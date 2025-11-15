"""
測試 BIO 模型對工作流指令的意圖識別能力
使用實際的工作流指令來驗證 WORK vs CHAT 的區分
"""

import sys
from pathlib import Path

# 添加專案根目錄到 sys.path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from modules.nlp_module.intent_segmenter import BIOTagger

def print_separator():
    print("=" * 80)

def test_intent_recognition():
    """測試工作流相關的意圖識別"""
    
    # 初始化模型
    print("🔧 載入 BIO 模型...")
    model_path = project_root / "models" / "nlp" / "bio_tagger"
    tagger = BIOTagger()
    tagger.load_model(str(model_path))
    print("✅ 模型載入完成\n")
    
    # 測試案例 - 分為幾類
    test_cases = {
        "🔨 明確的工作指令 (應該是 WORK)": [
            "Summarize the document with research tag",
            "Create a backup of the database",
            "Generate a report for last month",
            "Search for files containing error logs",
            "Archive old documents from 2023",
            "Convert all images to PNG format",
            "Analyze the system performance metrics",
        ],
        
        "💬 明確的聊天內容 (應該是 CHAT)": [
            "How are you doing today",
            "What do you think about this",
            "Tell me a joke",
            "I'm feeling tired",
            "That's interesting",
            "Thanks for your help",
            "Can you explain how this works",
        ],
        
        "🤔 模糊案例 (可能混淆)": [
            "Can you help me with something",  # 可能是 CALL 或 CHAT
            "What can you do",  # 可能是 CHAT 或 CALL
            "Show me the time",  # 可能是 WORK 或 CHAT
            "I need some information",  # CHAT 或 WORK
            "Let me know when it's done",  # 可能是指令的一部分
        ],
        
        "🔀 複合意圖 (多個意圖)": [
            "Hey, can you summarize this document",  # CALL + WORK
            "Backup the files and then send me a report",  # WORK + WORK (兩個任務)
            "Thanks, now please analyze the data",  # CHAT + WORK
            "What time is it and create a backup",  # CHAT + WORK
            "Hi there, how are you doing",  # CALL + CHAT
        ],
        
        "🎯 實際工作流指令": [
            "Summarize all documents tagged as research",
            "Archive documents from last year",
            "Search for python files in the project",
            "Generate a summary of meeting notes",
            "Create backup of configuration files",
            "Analyze error logs from yesterday",
        ]
    }
    
    # 執行測試
    for category, test_texts in test_cases.items():
        print_separator()
        print(f"\n{category}\n")
        
        for text in test_texts:
            print(f"\n📝 輸入: \"{text}\"")
            
            # 預測
            segments = tagger.predict(text)
            
            # 顯示結果
            if not segments:
                print("   ❌ 無預測結果")
                continue
            
            # 顯示每個段落
            for i, seg in enumerate(segments, 1):
                intent_emoji = {
                    'CALL': '📞',
                    'CHAT': '💬', 
                    'WORK': '🔨',
                    'UNKNOWN': '❓'
                }.get(seg['intent'], '❔')
                
                print(f"   {intent_emoji} 段落 {i}: \"{seg['text']}\"")
                print(f"      意圖: {seg['intent']} (信心度: {seg['confidence']:.3f})")
            
            # 判斷主要意圖
            if len(segments) == 1:
                primary = segments[0]['intent']
                print(f"   ✨ 單一意圖: {primary}")
            else:
                # 找最高優先級的意圖
                priority_map = {'WORK': 3, 'CHAT': 2, 'CALL': 1, 'UNKNOWN': 0}
                primary = max(segments, key=lambda s: priority_map.get(s['intent'], 0))['intent']
                print(f"   ✨ 複合意圖,主要意圖: {primary} ({len(segments)} 個段落)")

    print_separator()
    print("\n✅ 測試完成!")
    
    # 統計建議
    print("\n📊 觀察要點:")
    print("1. 工作流指令是否正確識別為 WORK")
    print("2. 聊天內容是否正確識別為 CHAT")
    print("3. 複合意圖是否正確分段")
    print("4. 模糊案例的處理是否合理")
    print("5. 連接詞 (and, then) 是否正確分隔不同任務")

if __name__ == "__main__":
    test_intent_recognition()
