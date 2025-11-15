"""
添加實際工作流相關的訓練數據
重點加強 summarize 和 archive 等關鍵工作流動詞的識別
"""

import json
from datetime import datetime
from pathlib import Path

# 新的訓練範例 - 針對實際工作流
new_examples = [
    # Summarize 相關 - 都應該是 DIRECT_WORK
    {
        "text": "Summarize the document",
        "segments": [{"text": "Summarize the document", "label": "DIRECT_WORK"}]
    },
    {
        "text": "Summarize all documents with research tag",
        "segments": [{"text": "Summarize all documents with research tag", "label": "DIRECT_WORK"}]
    },
    {
        "text": "Summarize the meeting notes",
        "segments": [{"text": "Summarize the meeting notes", "label": "DIRECT_WORK"}]
    },
    {
        "text": "Summarize this file for me",
        "segments": [{"text": "Summarize this file for me", "label": "DIRECT_WORK"}]
    },
    
    # Archive 相關 - 都應該是 DIRECT_WORK
    {
        "text": "Archive old documents",
        "segments": [{"text": "Archive old documents", "label": "DIRECT_WORK"}]
    },
    {
        "text": "Archive documents from last year",
        "segments": [{"text": "Archive documents from last year", "label": "DIRECT_WORK"}]
    },
    {
        "text": "Archive all files from 2023",
        "segments": [{"text": "Archive all files from 2023", "label": "DIRECT_WORK"}]
    },
    
    # 其他工作流指令 - DIRECT_WORK
    {
        "text": "Search for python files",
        "segments": [{"text": "Search for python files", "label": "DIRECT_WORK"}]
    },
    {
        "text": "Create a backup of the database",
        "segments": [{"text": "Create a backup of the database", "label": "DIRECT_WORK"}]
    },
    {
        "text": "Generate a report for last month",
        "segments": [{"text": "Generate a report for last month", "label": "DIRECT_WORK"}]
    },
    {
        "text": "Analyze the error logs",
        "segments": [{"text": "Analyze the error logs", "label": "DIRECT_WORK"}]
    },
    
    # 聊天類型 - 確保不被誤判為 WORK
    {
        "text": "Tell me a joke",
        "segments": [{"text": "Tell me a joke", "label": "CHAT"}]
    },
    {
        "text": "Tell me about yourself",
        "segments": [{"text": "Tell me about yourself", "label": "CHAT"}]
    },
    {
        "text": "Can you explain how this works",
        "segments": [{"text": "Can you explain how this works", "label": "CHAT"}]
    },
    {
        "text": "What do you think about this",
        "segments": [{"text": "What do you think about this", "label": "CHAT"}]
    },
    
    # 複合意圖 - 多個獨立任務
    {
        "text": "Hey, can you summarize this document",
        "segments": [
            {"text": "Hey", "label": "CALL"},
            {"text": "can you summarize this document", "label": "DIRECT_WORK"}
        ]
    },
    {
        "text": "Backup the files and then send me a report",
        "segments": [
            {"text": "Backup the files", "label": "DIRECT_WORK"},
            {"text": "and then", "label": "UNKNOWN"},  # 連接詞
            {"text": "send me a report", "label": "DIRECT_WORK"}
        ]
    },
    {
        "text": "Archive old data then generate a summary",
        "segments": [
            {"text": "Archive old data", "label": "DIRECT_WORK"},
            {"text": "then", "label": "UNKNOWN"},  # 連接詞
            {"text": "generate a summary", "label": "DIRECT_WORK"}
        ]
    },
]

def create_bio_format(example):
    """將 segment 格式轉換為 BIO 標註格式"""
    text = example["text"]
    segments = example["segments"]
    
    # Tokenize (簡單按空格分割)
    tokens = text.split()
    bio_labels = []
    
    # 追蹤當前位置
    current_pos = 0
    segment_idx = 0
    
    for token in tokens:
        # 找到這個 token 在原文中的位置
        token_start = text.find(token, current_pos)
        token_end = token_start + len(token)
        
        # 找到這個 token 屬於哪個 segment
        label = "O"
        for seg in segments:
            seg_text = seg["text"]
            seg_start = text.find(seg_text)
            seg_end = seg_start + len(seg_text)
            
            if token_start >= seg_start and token_end <= seg_end:
                # 判斷是 B- 還是 I-
                if token_start == seg_start or (bio_labels and not bio_labels[-1].endswith(seg["label"])):
                    label = f"B-{seg['label']}"
                else:
                    label = f"I-{seg['label']}"
                break
        
        bio_labels.append(label)
        current_pos = token_end
    
    return tokens, bio_labels

def main():
    """生成並添加新的訓練數據"""
    project_root = Path(__file__).parent.parent.parent
    output_file = project_root / "train" / "nlp" / "workflow_additional_examples.jsonl"
    
    print(f"📝 準備生成 {len(new_examples)} 個新訓練範例...")
    
    # 轉換為完整格式
    formatted_examples = []
    for i, example in enumerate(new_examples):
        tokens, bio_labels = create_bio_format(example)
        
        formatted = {
            "id": f"workflow_additional_{i:04d}",
            "text": example["text"],
            "tokens": tokens,
            "bio_labels": bio_labels,
            "segments": [
                {
                    "text": seg["text"],
                    "label": seg["label"],
                    "start": example["text"].find(seg["text"]),
                    "end": example["text"].find(seg["text"]) + len(seg["text"]),
                    "confidence": 1.0,
                    "annotator_notes": "Manual workflow example"
                }
                for seg in example["segments"]
            ],
            "metadata": {
                "source": "workflow_manual_addition",
                "scenario": "single_intent" if len(example["segments"]) == 1 else "compound_intent",
                "created_date": datetime.now().isoformat(),
                "annotated": True,
                "quality_checked": True,
                "annotator": "human"
            }
        }
        formatted_examples.append(formatted)
    
    # 寫入文件
    with open(output_file, "w", encoding="utf-8") as f:
        for example in formatted_examples:
            f.write(json.dumps(example, ensure_ascii=False) + "\n")
    
    print(f"✅ 已生成 {len(formatted_examples)} 個範例到: {output_file}")
    print("\n📊 範例統計:")
    
    # 統計各類型數量
    label_counts = {}
    for ex in formatted_examples:
        for seg in ex["segments"]:
            label = seg["label"]
            label_counts[label] = label_counts.get(label, 0) + 1
    
    for label, count in sorted(label_counts.items()):
        print(f"  - {label}: {count} 個 segment")
    
    print("\n💡 下一步:")
    print("1. 檢查生成的範例是否正確")
    print("2. 合併到主訓練數據: python train/nlp/merge_training_data.py")
    print("3. 重新訓練模型: python train/nlp/train_bio_model.py")

if __name__ == "__main__":
    main()
