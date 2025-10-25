#!/usr/bin/env python3
"""
生成 UNKNOWN 意圖訓練資料（針對 STT 錯誤和語法問題）

包含：
- STT 辨識錯誤的句子
- 文法錯誤
- 不合邏輯的字詞組合
- 片段句子
- 無意義的音節組合
"""

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any


# UNKNOWN 範例（STT 錯誤、語法錯誤、無意義句子）
UNKNOWN_EXAMPLES = [
    # STT 常見錯誤（同音字、近音字）
    "open the file write now",  # right now -> write now
    "I knead to save this",  # need -> knead
    "please weight a moment",  # wait -> weight
    "turn on the lights hear",  # here -> hear
    "could you here me",  # hear -> here
    "send the massage",  # message -> massage
    "accept my apology",  # accept 但語境錯誤
    "I want to right a letter",  # write -> right
    "the whether is nice",  # weather -> whether
    "too much to bare",  # bear -> bare
    
    # 破碎的 STT 輸出
    "ope the fil plea",
    "ca yo hel me wi thi",
    "I wa to se the do",
    "pleas sav thi fi",
    "tur on ligh",
    "se alar for se",
    "che my em",
    "sta the mu",
    
    # 文法錯誤
    "me want open file",
    "you be can help me",
    "I is need to go",
    "they was going",
    "he don't knows",
    "she have went",
    "we was there",
    "it are working",
    "does you understand",
    "I has finished",
    
    # 字詞順序錯誤
    "file the open please",
    "light turn on the",
    "alarm set for tomorrow",
    "music play some",
    "email my check",
    "weather the show me",
    "this save file",
    "screen lock the",
    
    # 重複或卡住
    "open open open the",
    "file file save save",
    "turn turn turn on",
    "the the the the",
    "please please help help",
    "can can you you",
    
    # 無意義組合
    "the green ideas sleep",
    "chair running water cloud",
    "music telephone yesterday orange",
    "happy delete the morning",
    "computer singing fast slow",
    "window clock eating digital",
    "keyboard tomorrow blue quick",
    
    # 片段句子（STT 截斷）
    "I want to",
    "could you please",
    "how about",
    "maybe we should",
    "if you can",
    "what if I",
    "in case of",
    "depending on the",
    
    # 混亂的音節
    "klop frin desh",
    "brin stor felm",
    "yesh kren floop",
    "trem vosh plin",
    "snep grel vunk",
    
    # 數字/符號錯誤
    "open file 1 2 3 4 5",
    "the ### system @@@",
    "save $$ document %%",
    "turn && lights ||",
    
    # 語言混雜（STT 識別錯誤）
    "open de file por favor",  # 英文+其他語言
    "please helfen mich",
    "turn on la lumiere",
    "save das dokument",
    
    # 超長無意義串
    "and then but also maybe if when where how why because",
    "the of to and a in is it you that",
    "if if if but but but when when then then",
    
    # 不完整命令
    "could you maybe possibly perhaps",
    "I think I need to maybe",
    "if you dont mind possibly",
    "perhaps we could just",
    
    # 語境不明
    "that thing over there",
    "the stuff from before",
    "you know what I mean",
    "it happened again",
    "same as last time",
    "like we discussed",
    
    # 純雜訊
    "umm uhh err hmm",
    "ah oh uh eh",
    "mmm hmm uh-huh",
    "er erm umm uhh",
    
    # 錯誤的命令格式
    "open close open close",
    "yes no maybe yes no",
    "start stop start stop",
    "on off on off on",
    
    # 奇怪的時間表達
    "tomorrow yesterday next last",
    "before after during while",
    "always never sometimes often",
    
    # 單字重複變形
    "opening opened opens opening",
    "saving saved saves saving",
    "turning turned turns turning",
    
    # 無關聯的專有名詞
    "john mary apple microsoft",
    "paris london tokyo berlin",
    "monday tuesday january march",
    
    # 標點符號問題
    "...???!!!...",
    ",,,,;;;;",
    "----____====",
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


def create_unknown_data(text: str) -> Dict[str, Any]:
    """創建 UNKNOWN 意圖數據"""
    tokens = tokenize_simple(text)
    bio_labels = ["B-UNKNOWN"] + ["I-UNKNOWN"] * (len(tokens) - 1)
    
    return {
        "id": f"unknown_stt_{uuid.uuid4().hex[:8]}",
        "text": text,
        "tokens": tokens,
        "bio_labels": bio_labels,
        "segments": [{
            "text": text,
            "label": "UNKNOWN",
            "start": 0,
            "end": len(text),
            "confidence": 1.0,
            "annotator_notes": "STT error or grammatical issue"
        }],
        "metadata": {
            "source": "unknown_generator_stt_errors",
            "scenario": "unknown_intent",
            "created_date": datetime.now().isoformat(),
            "annotated": True,
            "quality_checked": False,
            "annotator": "auto_unknown_generator",
            "annotation_date": datetime.now().isoformat()
        }
    }


def main():
    """主函數"""
    print("=" * 60)
    print("生成 UNKNOWN 意圖訓練資料（STT 錯誤處理）")
    print("=" * 60)
    print()
    
    output_file = Path(__file__).parent / "nlp_training_data.jsonl"
    
    print(f"將添加 {len(UNKNOWN_EXAMPLES)} 條 UNKNOWN 數據...")
    print()
    
    # 生成數據
    data_list = [create_unknown_data(text) for text in UNKNOWN_EXAMPLES]
    
    # 保存到文件
    print(f"保存數據到 {output_file}...")
    with open(output_file, 'a', encoding='utf-8') as f:
        for data in data_list:
            f.write(json.dumps(data, ensure_ascii=False) + '\n')
    
    print(f"\n✅ 成功添加 {len(data_list)} 條 UNKNOWN 數據！")
    
    # 統計總數
    with open(output_file, 'r', encoding='utf-8') as f:
        total = sum(1 for _ in f)
    
    print(f"📊 當前總數據量: {total} 條")
    
    # 統計 UNKNOWN 總數
    with open(output_file, 'r', encoding='utf-8') as f:
        unknown_count = sum(1 for line in f 
                           if any(seg['label'] == 'UNKNOWN' 
                                 for seg in json.loads(line)['segments']))
    
    print(f"📊 UNKNOWN 標籤總數: {unknown_count} 條")


if __name__ == "__main__":
    main()
