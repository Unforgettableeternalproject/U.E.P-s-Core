#!/usr/bin/env python3
"""
批量生成 Stage 4 訓練資料

自動生成包含以下標籤的訓練數據：
- DIRECT_WORK: 緊急工作指令
- BACKGROUND_WORK: 背景任務
- UNKNOWN: 模糊/無法識別的輸入
"""

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any


# DIRECT_WORK 範例模板 (緊急、需要立即執行)
DIRECT_WORK_TEMPLATES = [
    # 文件操作
    "Open the document",
    "Delete this file now",
    "Save the current file",
    "Close all windows",
    "Print this page",
    "Show me the file",
    "Edit the document",
    "Rename this folder",
    "Move the file to desktop",
    "Copy this to clipboard",
    "Cut this text",
    "Paste the content",
    "Undo the last action",
    "Redo that change",
    "Select all text",
    
    # 系統控制
    "Turn on the lights",
    "Increase the volume",
    "Decrease brightness",
    "Mute the sound",
    "Unmute the audio",
    "Lock the screen",
    "Unlock the device",
    "Restart the system",
    "Shut down now",
    "Switch to dark mode",
    "Enable notifications",
    "Disable auto-update",
    "Pause the video",
    "Resume playback",
    "Stop the music",
    
    # 時間敏感任務
    "Set alarm for 7 AM",
    "Set a timer for 5 minutes",
    "Cancel the alarm",
    "Show me my schedule",
    "What's my next appointment",
    "Add event to calendar",
    "Remove this reminder",
    "Snooze the notification",
    "Mark this as urgent",
    "Flag this message",
    
    # 搜尋與查詢
    "Search for restaurants",
    "Find my notes",
    "Look up the definition",
    "Show me the weather",
    "Check the time",
    "What's the date today",
    "Display my tasks",
    "Show recent files",
    "Find this contact",
    "Search my emails",
    
    # 通訊相關
    "Call John",
    "Send a message",
    "Reply to that email",
    "Forward this message",
    "Block this contact",
    "Unblock the user",
    "Start a video call",
    "End the call",
    "Answer the phone",
    "Decline the call",
]

# BACKGROUND_WORK 範例模板 (可排隊、背景執行)
BACKGROUND_WORK_TEMPLATES = [
    # 同步與備份
    "Sync my devices",
    "Backup my data",
    "Upload to cloud",
    "Download the updates",
    "Sync calendar events",
    "Backup photos to cloud",
    "Export my contacts",
    "Import the data",
    "Sync email accounts",
    "Update contact list",
    
    # 系統維護
    "Clear the cache",
    "Empty trash",
    "Optimize performance",
    "Run diagnostics",
    "Check for updates",
    "Scan for viruses",
    "Defragment the disk",
    "Clean up storage",
    "Compress old files",
    "Archive old emails",
    
    # 數據處理
    "Generate the report",
    "Process the images",
    "Convert file format",
    "Merge documents",
    "Split the PDF",
    "Compress this folder",
    "Extract the archive",
    "Index the files",
    "Analyze the data",
    "Calculate statistics",
    
    # 長時間任務
    "Install the software",
    "Uninstall the app",
    "Update all apps",
    "Download the file",
    "Upload the video",
    "Render the video",
    "Transcode the audio",
    "Export in high quality",
    "Batch process images",
    "Train the model",
]

# UNKNOWN 範例模板 (模糊、歧義、無法識別)
UNKNOWN_TEMPLATES = [
    # 模糊請求
    "Do something",
    "Help me with that thing",
    "You know what I mean",
    "Fix it",
    "Make it work",
    "Handle this",
    "Deal with it",
    "Sort this out",
    "Figure it out",
    "Just do it",
    
    # 不完整句子
    "I want to",
    "Can you maybe",
    "How about we",
    "What if I",
    "Should I or",
    "Either this or",
    "Not sure if",
    "Wondering whether",
    "Thinking about",
    "Considering to",
    
    # 語義不清
    "The thing is broken",
    "It's not working",
    "Something happened",
    "There's an issue",
    "Problem with stuff",
    "Error somewhere",
    "Can't do the task",
    "Won't let me proceed",
    "Stuck at some point",
    "Blocked by something",
    
    # 隨機文字
    "asdf jkl",
    "test 123",
    "hello world test",
    "random text here",
    "blah blah blah",
    "etc etc",
    "and so on",
    "you know",
    "whatever",
    "stuff and things",
]

# 複合意圖組合
COMPOUND_COMBINATIONS = [
    # CALL + CHAT
    [("Hello UEP", "CALL"), ("How are you doing today", "CHAT")],
    [("Hey assistant", "CALL"), ("The weather is nice", "CHAT")],
    [("Wake up system", "CALL"), ("I had a great day", "CHAT")],
    [("Good morning UEP", "CALL"), ("I'm feeling excited", "CHAT")],
    [("Attention please", "CALL"), ("Just wanted to chat", "CHAT")],
    
    # CALL + DIRECT_WORK
    [("Hello UEP", "CALL"), ("Open my calendar", "DIRECT_WORK")],
    [("Hey there", "CALL"), ("Delete this file", "DIRECT_WORK")],
    [("System wake up", "CALL"), ("Show me the weather", "DIRECT_WORK")],
    [("Good evening", "CALL"), ("Set alarm for 6 AM", "DIRECT_WORK")],
    [("Are you there", "CALL"), ("Turn on the lights", "DIRECT_WORK")],
    
    # CHAT + DIRECT_WORK
    [("I had a productive day", "CHAT"), ("Save this file", "DIRECT_WORK")],
    [("The meeting went well", "CHAT"), ("Send the report", "DIRECT_WORK")],
    [("I'm feeling tired", "CHAT"), ("Set a timer", "DIRECT_WORK")],
    [("That was interesting", "CHAT"), ("Search for more info", "DIRECT_WORK")],
    [("I love this music", "CHAT"), ("Increase volume", "DIRECT_WORK")],
    
    # CALL + BACKGROUND_WORK
    [("Hello assistant", "CALL"), ("Sync my devices", "BACKGROUND_WORK")],
    [("Hey UEP", "CALL"), ("Backup my data", "BACKGROUND_WORK")],
    [("Wake up", "CALL"), ("Download updates", "BACKGROUND_WORK")],
    [("System online", "CALL"), ("Clear the cache", "BACKGROUND_WORK")],
    [("Are you ready", "CALL"), ("Optimize storage", "BACKGROUND_WORK")],
    
    # DIRECT_WORK + BACKGROUND_WORK
    [("Open the file", "DIRECT_WORK"), ("Then sync to cloud", "BACKGROUND_WORK")],
    [("Show my schedule", "DIRECT_WORK"), ("And backup calendar", "BACKGROUND_WORK")],
    [("Send this email", "DIRECT_WORK"), ("Then archive old ones", "BACKGROUND_WORK")],
    
    # 三段組合
    [("Hello UEP", "CALL"), ("Nice weather today", "CHAT"), ("Open calendar", "DIRECT_WORK")],
    [("Hey there", "CALL"), ("I'm feeling good", "CHAT"), ("Backup my files", "BACKGROUND_WORK")],
    [("System wake", "CALL"), ("Show weather", "DIRECT_WORK"), ("Sync devices", "BACKGROUND_WORK")],
]


def tokenize_simple(text: str) -> List[str]:
    """簡單英文分詞"""
    tokens = []
    current = ""
    
    for char in text:
        if char.isspace():
            if current:
                tokens.append(current)
                current = ""
        elif char.isalnum() or char in "'-":
            current += char
        else:
            if current:
                tokens.append(current)
                current = ""
            tokens.append(char)
    
    if current:
        tokens.append(current)
    
    return tokens


def create_single_intent_data(text: str, label: str, scenario: str) -> Dict[str, Any]:
    """創建單一意圖數據"""
    tokens = tokenize_simple(text)
    bio_labels = [f"B-{label}"] + [f"I-{label}"] * (len(tokens) - 1)
    
    return {
        "id": f"{scenario}_{uuid.uuid4().hex[:8]}",
        "text": text,
        "tokens": tokens,
        "bio_labels": bio_labels,
        "segments": [{
            "text": text,
            "label": label,
            "start": 0,
            "end": len(text),
            "confidence": 1.0,
            "annotator_notes": ""
        }],
        "metadata": {
            "source": "batch_generator_stage4",
            "scenario": scenario,
            "created_date": datetime.now().isoformat(),
            "annotated": True,
            "quality_checked": False,
            "annotator": "auto_batch_generator",
            "annotation_date": datetime.now().isoformat()
        }
    }


def create_compound_intent_data(segments: List[tuple]) -> Dict[str, Any]:
    """創建複合意圖數據"""
    # 構建完整文本
    parts = []
    segment_data = []
    char_pos = 0
    
    for i, (text, label) in enumerate(segments):
        if i > 0:
            parts.append(". ")
            char_pos += 2
        
        parts.append(text)
        segment_data.append({
            "text": text,
            "label": label,
            "start": char_pos,
            "end": char_pos + len(text),
            "confidence": 1.0,
            "annotator_notes": ""
        })
        char_pos += len(text)
    
    full_text = "".join(parts)
    tokens = tokenize_simple(full_text)
    
    # 生成 BIO 標籤
    bio_labels = ["O"] * len(tokens)
    token_text = " ".join(tokens)
    
    for seg in segment_data:
        label = seg["label"]
        seg_start = seg["start"]
        seg_end = seg["end"]
        
        # 簡化處理：基於字符位置估算 token 位置
        char_count = 0
        first_token = True
        
        for i, token in enumerate(tokens):
            token_start = char_count
            token_end = char_count + len(token)
            
            if token_start >= seg_start and token_end <= seg_end:
                if first_token:
                    bio_labels[i] = f"B-{label}"
                    first_token = False
                else:
                    bio_labels[i] = f"I-{label}"
            
            char_count = token_end + 1  # +1 for space
    
    return {
        "id": f"compound_stage4_{uuid.uuid4().hex[:8]}",
        "text": full_text,
        "tokens": tokens,
        "bio_labels": bio_labels,
        "segments": segment_data,
        "metadata": {
            "source": "batch_generator_stage4",
            "scenario": "compound_interaction",
            "created_date": datetime.now().isoformat(),
            "annotated": True,
            "quality_checked": False,
            "annotator": "auto_batch_generator",
            "annotation_date": datetime.now().isoformat()
        }
    }


def generate_training_data(count: int = 150) -> List[Dict[str, Any]]:
    """
    生成訓練數據
    
    Args:
        count: 要生成的數據條數
        
    Returns:
        生成的數據列表
    """
    data = []
    
    # 分配比例
    direct_work_count = int(count * 0.30)  # 30% DIRECT_WORK
    background_work_count = int(count * 0.25)  # 25% BACKGROUND_WORK
    unknown_count = int(count * 0.15)  # 15% UNKNOWN
    compound_count = count - direct_work_count - background_work_count - unknown_count  # 30% 複合
    
    print(f"生成計劃:")
    print(f"  DIRECT_WORK: {direct_work_count} 條")
    print(f"  BACKGROUND_WORK: {background_work_count} 條")
    print(f"  UNKNOWN: {unknown_count} 條")
    print(f"  複合意圖: {compound_count} 條")
    print(f"  總計: {count} 條\n")
    
    # 生成 DIRECT_WORK
    print("生成 DIRECT_WORK 數據...")
    for i in range(direct_work_count):
        template = DIRECT_WORK_TEMPLATES[i % len(DIRECT_WORK_TEMPLATES)]
        data.append(create_single_intent_data(template, "DIRECT_WORK", "direct_work"))
    
    # 生成 BACKGROUND_WORK
    print("生成 BACKGROUND_WORK 數據...")
    for i in range(background_work_count):
        template = BACKGROUND_WORK_TEMPLATES[i % len(BACKGROUND_WORK_TEMPLATES)]
        data.append(create_single_intent_data(template, "BACKGROUND_WORK", "background_work"))
    
    # 生成 UNKNOWN
    print("生成 UNKNOWN 數據...")
    for i in range(unknown_count):
        template = UNKNOWN_TEMPLATES[i % len(UNKNOWN_TEMPLATES)]
        data.append(create_single_intent_data(template, "UNKNOWN", "unknown_intent"))
    
    # 生成複合意圖
    print("生成複合意圖數據...")
    for i in range(compound_count):
        segments = COMPOUND_COMBINATIONS[i % len(COMPOUND_COMBINATIONS)]
        data.append(create_compound_intent_data(segments))
    
    return data


def main():
    """主函數"""
    print("=" * 60)
    print("批量生成 Stage 4 訓練資料")
    print("=" * 60)
    print()
    
    # 設定輸出路徑
    output_file = Path(__file__).parent / "nlp_training_data.jsonl"
    
    # 詢問生成數量
    try:
        count_input = input("要生成多少條數據? (預設 150): ").strip()
        count = int(count_input) if count_input else 150
    except ValueError:
        count = 150
    
    print(f"\n將生成 {count} 條新數據...\n")
    
    # 生成數據
    data_list = generate_training_data(count)
    
    # 保存到文件
    print(f"\n保存數據到 {output_file}...")
    with open(output_file, 'a', encoding='utf-8') as f:
        for data in data_list:
            f.write(json.dumps(data, ensure_ascii=False) + '\n')
    
    print(f"\n✅ 成功生成並保存 {len(data_list)} 條數據！")
    
    # 統計總數
    with open(output_file, 'r', encoding='utf-8') as f:
        total = sum(1 for _ in f)
    
    print(f"📊 當前總數據量: {total} 條")
    print("\n標籤分佈 (新增):")
    print(f"  DIRECT_WORK: {int(count * 0.30)} 條")
    print(f"  BACKGROUND_WORK: {int(count * 0.25)} 條")
    print(f"  UNKNOWN: {int(count * 0.15)} 條")
    print(f"  複合意圖: {count - int(count * 0.30) - int(count * 0.25) - int(count * 0.15)} 條")


if __name__ == "__main__":
    main()
