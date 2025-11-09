#!/usr/bin/env python3
"""
Quick Training Data Addition Tool

Usage:
    python train/nlp/quick_add_data.py

Features:
- Interactive command-line interface
- Auto-generate BIO labels
- Auto-calculate character positions
- Validate data format
"""

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any


# BIOS Label Mapping (Stage 4: 5 labels)
INTENT_LABELS = {
    "1": ("CALL", "System call (Hello UEP, Wake up assistant)"),
    "2": ("CHAT", "Casual conversation (How are you, Nice weather today)"),
    "3": ("DIRECT_WORK", "Urgent work command (Open file, Set alarm now)"),
    "4": ("BACKGROUND_WORK", "Background task (Sync devices, Download updates)"),
    "5": ("UNKNOWN", "Unrecognized input")
}


def tokenize_english(text: str) -> List[str]:
    """
    Simple English tokenization (split by whitespace and punctuation)
    
    Note: This is a simplified version. For production, consider using
    spaCy or NLTK for more robust tokenization.
    """
    tokens = []
    current_token = ""
    
    for char in text:
        if char.isspace():
            if current_token:
                tokens.append(current_token)
                current_token = ""
        elif char.isalnum() or char in "'-":
            current_token += char
        else:
            if current_token:
                tokens.append(current_token)
                current_token = ""
            tokens.append(char)
    
    if current_token:
        tokens.append(current_token)
    
    return tokens


def generate_bio_labels(tokens: List[str], segments: List[Dict[str, Any]]) -> List[str]:
    """
    Generate BIO labels from segments
    
    Args:
        tokens: List of tokens
        segments: List of segment information
        
    Returns:
        List of BIO labels
    """
    bio_labels = ["O"] * len(tokens)
    
    # 重建文本以確定 token 位置
    text = " ".join(tokens)  # 簡化版本
    
    for segment in segments:
        seg_start = segment["start"]
        seg_end = segment["end"]
        label = segment["label"]
        
        # 找到對應的 token 範圍
        char_pos = 0
        first_token = True
        
        for i, token in enumerate(tokens):
            token_start = char_pos
            token_end = char_pos + len(token)
            
            # 檢查 token 是否在分段範圍內
            if token_start >= seg_start and token_end <= seg_end:
                if first_token:
                    bio_labels[i] = f"B-{label}"
                    first_token = False
                else:
                    bio_labels[i] = f"I-{label}"
            
            char_pos = token_end + 1  # +1 for space
    
    return bio_labels


def add_single_intent_data():
    """Add single intent data"""
    print("\n=== Add Single Intent Data ===\n")
    
    text = input("Enter text: ").strip()
    if not text:
        print("❌ Text cannot be empty")
        return None
    
    print("\nSelect intent type:")
    for key, (label, desc) in INTENT_LABELS.items():
        print(f"  {key}. {label:20} - {desc}")
    
    choice = input("\nEnter option (1-5): ").strip()
    if choice not in INTENT_LABELS:
        print("❌ Invalid option")
        return None
    
    label, _ = INTENT_LABELS[choice]
    
    # Generate tokens
    tokens = tokenize_english(text)
    
    # Generate segment info
    segments = [{
        "text": text,
        "label": label,
        "start": 0,
        "end": len(text),
        "confidence": 1.0,
        "annotator_notes": ""
    }]
    
    # Generate BIO labels
    bio_labels = [f"B-{label}"] + [f"I-{label}"] * (len(tokens) - 1)
    
    # Build data object
    data = {
        "id": f"manual_{uuid.uuid4().hex[:8]}",
        "text": text,
        "tokens": tokens,
        "bio_labels": bio_labels,
        "segments": segments,
        "metadata": {
            "source": "quick_add_tool",
            "scenario": "single_intent",
            "created_date": datetime.now().isoformat(),
            "annotated": True,
            "quality_checked": False,
            "annotator": "manual_user"
        }
    }
    
    return data


def add_compound_intent_data():
    """Add compound intent data"""
    print("\n=== Add Compound Intent Data ===\n")
    
    text = input("Enter full text: ").strip()
    if not text:
        print("❌ Text cannot be empty")
        return None
    
    segments = []
    print("\nNow annotate each intent segment (empty line to finish)")
    print("Tip: Copy part of the text, then select intent type\n")
    
    while True:
        print(f"\nCurrent text: {text}")
        print(f"Annotated segments: {len(segments)}")
        
        seg_text = input("\nEnter segment text (empty to finish): ").strip()
        if not seg_text:
            break
        
        # Find segment position in text
        start = text.find(seg_text)
        if start == -1:
            print(f"❌ Cannot find '{seg_text}' in text")
            continue
        
        end = start + len(seg_text)
        
        print("\nSelect intent type:")
        for key, (label, desc) in INTENT_LABELS.items():
            print(f"  {key}. {label:20} - {desc}")
        
        choice = input("\nEnter option (1-5): ").strip()
        if choice not in INTENT_LABELS:
            print("❌ Invalid option")
            continue
        
        label, _ = INTENT_LABELS[choice]
        
        segments.append({
            "text": seg_text,
            "label": label,
            "start": start,
            "end": end,
            "confidence": 1.0,
            "annotator_notes": ""
        })
        
        print(f"✅ Added: '{seg_text}' -> {label}")
    
    if not segments:
        print("❌ Need at least one segment")
        return None
    
    # Generate tokens
    tokens = tokenize_english(text)
    
    # Generate BIO labels
    bio_labels = generate_bio_labels(tokens, segments)
    
    # Build data object
    data = {
        "id": f"manual_compound_{uuid.uuid4().hex[:8]}",
        "text": text,
        "tokens": tokens,
        "bio_labels": bio_labels,
        "segments": segments,
        "metadata": {
            "source": "quick_add_tool",
            "scenario": "compound_intent",
            "created_date": datetime.now().isoformat(),
            "annotated": True,
            "quality_checked": False,
            "annotator": "manual_user"
        }
    }
    
    return data


def save_to_jsonl(data: Dict[str, Any], filepath: Path):
    """Save data to JSONL file"""
    with open(filepath, 'a', encoding='utf-8') as f:
        f.write(json.dumps(data, ensure_ascii=False) + '\n')


def main():
    """Main function"""
    print("=" * 60)
    print("Quick Training Data Addition Tool")
    print("=" * 60)
    
    # Set output path
    output_file = Path(__file__).parent / "nlp_training_data.jsonl"
    
    if not output_file.exists():
        print(f"⚠️  Warning: {output_file} does not exist")
        create = input("Create new file? (y/n): ").strip().lower()
        if create != 'y':
            print("❌ Cancelled")
            return
    
    while True:
        print("\n" + "=" * 60)
        print("Select operation:")
        print("  1. Add single intent data")
        print("  2. Add compound intent data")
        print("  3. View current data count")
        print("  4. Exit")
        print("=" * 60)
        
        choice = input("\nEnter option (1-4): ").strip()
        
        if choice == "1":
            data = add_single_intent_data()
            if data:
                save_to_jsonl(data, output_file)
                print(f"\n✅ Saved to: {output_file}")
                print(f"   ID: {data['id']}")
                print(f"   Text: {data['text']}")
                print(f"   Label: {data['segments'][0]['label']}")
        
        elif choice == "2":
            data = add_compound_intent_data()
            if data:
                save_to_jsonl(data, output_file)
                print(f"\n✅ Saved to: {output_file}")
                print(f"   ID: {data['id']}")
                print(f"   Text: {data['text']}")
                print(f"   Segments: {len(data['segments'])}")
        
        elif choice == "3":
            if output_file.exists():
                with open(output_file, 'r', encoding='utf-8') as f:
                    count = sum(1 for _ in f)
                print(f"\n📊 Current data count: {count} entries")
            else:
                print("\n⚠️  File does not exist")
        
        elif choice == "4":
            print("\n👋 Goodbye!")
            break
        
        else:
            print("❌ Invalid option")


if __name__ == "__main__":
    main()
