#!/usr/bin/env python3
"""
將 COMMAND 標籤轉換為 DIRECT_WORK 或 BACKGROUND_WORK

轉換規則：
- BACKGROUND_WORK: 播放音樂、鬧鐘、行事曆、監控、同步、備份、下載、上傳、安裝等背景執行緒任務
- DIRECT_WORK: 檔案處理、資料查詢、搜尋、顯示等需要立即執行的任務
"""

import json
import shutil
from pathlib import Path
from datetime import datetime


# BACKGROUND_WORK 關鍵字（背景執行緒運作）
BACKGROUND_KEYWORDS = [
    # 音樂/媒體播放
    'play', 'pause', 'resume', 'skip', 'music', 'song', 'video', 'audio',
    # 時間相關
    'alarm', 'timer', 'reminder', 'schedule', 'calendar', 'appointment',
    # 同步與備份
    'sync', 'backup', 'upload', 'download', 'install', 'update',
    # 監控相關
    'monitor', 'watch', 'track', 'observe',
    # 系統維護
    'optimize', 'clean', 'clear cache', 'defragment', 'scan',
    # 長時間任務
    'compress', 'archive', 'export', 'import', 'convert', 'process',
    'generate', 'render', 'transcode', 'merge', 'split',
]

# DIRECT_WORK 關鍵字（直接工作）
DIRECT_KEYWORDS = [
    # 檔案操作
    'open', 'close', 'save', 'delete', 'remove', 'rename', 'move', 'copy',
    'cut', 'paste', 'edit', 'create', 'new',
    # 搜尋與查詢
    'search', 'find', 'look', 'query', 'check', 'show', 'display', 'view',
    'list', 'get',
    # 系統控制（即時）
    'turn on', 'turn off', 'enable', 'disable', 'set', 'adjust',
    'increase', 'decrease', 'mute', 'unmute', 'lock', 'unlock',
    'start', 'stop', 'restart', 'shutdown',
    # 通訊
    'call', 'send', 'reply', 'forward', 'block', 'unblock', 'answer',
    # 資料操作
    'rotate', 'zoom', 'crop', 'resize', 'sort', 'filter', 'select',
]


def classify_command(text: str) -> str:
    """
    根據文本內容判斷應該是 DIRECT_WORK 還是 BACKGROUND_WORK
    
    Args:
        text: 指令文本
        
    Returns:
        'DIRECT_WORK' 或 'BACKGROUND_WORK'
    """
    text_lower = text.lower()
    
    # 計算匹配分數
    background_score = sum(1 for keyword in BACKGROUND_KEYWORDS if keyword in text_lower)
    direct_score = sum(1 for keyword in DIRECT_KEYWORDS if keyword in text_lower)
    
    # 如果都沒匹配，預設為 DIRECT_WORK（更保守）
    if background_score == 0 and direct_score == 0:
        return 'DIRECT_WORK'
    
    # 根據分數決定
    return 'BACKGROUND_WORK' if background_score > direct_score else 'DIRECT_WORK'


def convert_file(input_file: Path, output_file: Path) -> dict:
    """
    轉換檔案中的 COMMAND 標籤
    
    Returns:
        統計資訊字典
    """
    stats = {
        'total_lines': 0,
        'converted_lines': 0,
        'direct_work_count': 0,
        'background_work_count': 0,
        'unchanged_count': 0,
    }
    
    with open(input_file, 'r', encoding='utf-8') as f_in, \
         open(output_file, 'w', encoding='utf-8') as f_out:
        
        for line in f_in:
            stats['total_lines'] += 1
            data = json.loads(line)
            
            # 檢查是否有 COMMAND 標籤
            has_command = False
            for segment in data['segments']:
                if segment['label'] == 'COMMAND':
                    has_command = True
                    # 根據文本判斷新標籤
                    new_label = classify_command(segment['text'])
                    segment['label'] = new_label
                    
                    if new_label == 'DIRECT_WORK':
                        stats['direct_work_count'] += 1
                    else:
                        stats['background_work_count'] += 1
            
            # 更新 bio_labels
            if has_command:
                stats['converted_lines'] += 1
                for i, label in enumerate(data['bio_labels']):
                    if label.endswith('-COMMAND'):
                        prefix = label.split('-')[0]  # B 或 I
                        # 找到對應的 segment
                        for segment in data['segments']:
                            if segment['label'] in ['DIRECT_WORK', 'BACKGROUND_WORK']:
                                # 簡化處理：根據 segment 的標籤更新
                                data['bio_labels'][i] = f"{prefix}-{segment['label']}"
                                break
            else:
                stats['unchanged_count'] += 1
            
            # 寫入轉換後的數據
            f_out.write(json.dumps(data, ensure_ascii=False) + '\n')
    
    return stats


def main():
    """主函數"""
    print("=" * 60)
    print("COMMAND 標籤轉換工具")
    print("=" * 60)
    print()
    
    input_file = Path(__file__).parent / "nlp_training_data.jsonl"
    backup_file = Path(__file__).parent / "nlp_training_data.jsonl.backup"
    output_file = Path(__file__).parent / "nlp_training_data.jsonl.converted"
    
    # 備份原始檔案
    print(f"備份原始檔案到: {backup_file}")
    shutil.copy2(input_file, backup_file)
    
    # 轉換
    print(f"開始轉換...")
    stats = convert_file(input_file, output_file)
    
    # 顯示統計
    print()
    print("=" * 60)
    print("轉換完成！")
    print("=" * 60)
    print(f"總行數: {stats['total_lines']}")
    print(f"轉換行數: {stats['converted_lines']}")
    print(f"未變更行數: {stats['unchanged_count']}")
    print()
    print("轉換結果:")
    print(f"  COMMAND → DIRECT_WORK: {stats['direct_work_count']} 個片段")
    print(f"  COMMAND → BACKGROUND_WORK: {stats['background_work_count']} 個片段")
    print()
    print(f"轉換後的檔案: {output_file}")
    print(f"備份檔案: {backup_file}")
    print()
    print("請檢查轉換後的檔案，確認無誤後手動替換原始檔案。")
    print(f"指令: mv {output_file} {input_file}")


if __name__ == "__main__":
    main()
