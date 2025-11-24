"""
快速測試 BIO 模型的分辨能力
"""

import sys
from pathlib import Path

# 添加專案根目錄到 Python 路徑
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from modules.nlp_module.bio_tagger import BIOTagger


def test_model():
    """測試 BIO 模型對各種輸入的識別能力"""
    
    # 初始化模型
    print("載入 BIO 模型...")
    model_path = project_root / "models" / "nlp" / "bio_tagger"
    tagger = BIOTagger(model_name="distilbert-base-uncased")
    tagger.load_model(str(model_path))
    print("✓ 模型載入完成\n")
    
    # 測試案例
    test_cases = [
        # 1. 簡單的單一意圖
        "Hello there",
        "How are you doing today?",
        "Open the file manager",
        "Search for documents from last week",
        "Once the download finishes",
        
        # 2. 複合意圖 (chat + work)
        "Hey, can you help me organize my files?",
        "Good morning! Please start the backup",
        "I was wondering if you could search my emails",
        
        # 3. 複合意圖 (background + direct work)
        "After the update completes, restart the system",
        "When the server is ready, deploy the application",
        "If the connection is stable, sync the database",
        
        # 4. 多重複合意圖
        "Hi there, after finishing the report, create a backup and send it to my manager",
        "Good afternoon, when the meeting ends, remind me to review the documents",
        
        # 5. 邊緣案例
        "Hmm...",
        "Uh, I don't know",
        "Maybe? Let me think about it",
        
        # 6. 中英混合 (測試健壯性)
        "Hello 你好",
        "Open file 檔案",
    ]
    
    print("=" * 80)
    print("開始測試 BIO 模型...")
    print("=" * 80)
    print()
    
    for i, text in enumerate(test_cases, 1):
        print(f"[測試 {i}] {text}")
        print("-" * 80)
        
        try:
            # 進行預測
            segments = tagger.predict(text)
            
            if not segments:
                print("  ⚠️  未識別出任何意圖")
            else:
                for seg in segments:
                    intent = seg['intent']
                    segment_text = seg['text']
                    confidence = seg.get('confidence', 0.0)
                    
                    # 用不同顏色表示不同意圖
                    intent_symbol = {
                        'call': '📞',
                        'chat': '💬',
                        'direct_work': '⚡',
                        'background_work': '⏳',
                        'unknown': '❓'
                    }.get(intent, '?')
                    
                    print(f"  {intent_symbol} [{intent.upper()}] \"{segment_text}\" (信心度: {confidence:.3f})")
            
        except Exception as e:
            print(f"  ❌ 錯誤: {e}")
        
        print()
    
    print("=" * 80)
    print("測試完成!")
    print("=" * 80)


def test_detailed_analysis():
    """詳細分析特定測試案例"""
    
    print("\n" + "=" * 80)
    print("詳細分析模式")
    print("=" * 80)
    print()
    
    # 初始化模型
    model_path = project_root / "models" / "nlp" / "bio_tagger"
    tagger = BIOTagger(model_name="distilbert-base-uncased")
    tagger.load_model(str(model_path))
    
    # 選擇幾個複雜案例進行詳細分析
    detailed_cases = [
        "Hey there, after the backup finishes, can you send me the report?",
        "Good morning! When the server is free, please organize my emails and remind me about the meeting",
        "Hmm, I'm not sure if I should open the settings or just search for help",
    ]
    
    for text in detailed_cases:
        print(f"分析: {text}")
        print("-" * 80)
        
        segments = tagger.predict(text)
        
        # 顯示整體結構
        print(f"  識別出 {len(segments)} 個語意段落:")
        print()
        
        for i, seg in enumerate(segments, 1):
            intent = seg['intent']
            segment_text = seg['text']
            confidence = seg.get('confidence', 0.0)
            start = seg.get('start_pos', 0)
            end = seg.get('end_pos', 0)
            
            print(f"  段落 {i}:")
            print(f"    文本: \"{segment_text}\"")
            print(f"    意圖: {intent}")
            print(f"    位置: {start}-{end}")
            print(f"    信心度: {confidence:.4f}")
            print()
        
        print()


if __name__ == "__main__":
    # 基本測試
    test_model()
    
    # 詳細分析 (可選)
    try:
        response = input("\n是否執行詳細分析? (y/n): ").strip().lower()
        if response == 'y':
            test_detailed_analysis()
    except:
        pass
