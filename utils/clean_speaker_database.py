#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用於清理語者資料庫的腳本
移除不符合最低樣本數閾值的語者
"""

import os
import pickle
import sys
import time
from typing import Dict, Any

def load_speaker_database(db_path="memory/speaker_models.pkl"):
    """載入說話人資料庫"""
    try:
        if os.path.exists(db_path):
            with open(db_path, 'rb') as f:
                data = pickle.load(f)
                speaker_database = data.get('database', {})
                speaker_counter = data.get('counter', 0)
            print(f"載入說話人資料庫: {len(speaker_database)} 位說話人")
            return speaker_database, speaker_counter
        else:
            print("找不到說話人資料庫文件")
            return {}, 0
    except Exception as e:
        print(f"載入資料庫失敗: {e}")
        return {}, 0

def save_speaker_database(speaker_database, speaker_counter, db_path="memory/speaker_models.pkl"):
    """儲存說話人資料庫"""
    try:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        data = {
            'database': speaker_database,
            'counter': speaker_counter
        }
        with open(db_path, 'wb') as f:
            pickle.dump(data, f)
        print(f"儲存說話人資料庫: {len(speaker_database)} 位說話人")
        return True
    except Exception as e:
        print(f"儲存資料庫失敗: {e}")
        return False

def clean_speaker_database(min_samples=15):
    """清理不符合最低樣本數閾值的語者"""
    speaker_database, speaker_counter = load_speaker_database()
    
    if not speaker_database:
        print("資料庫為空或無法載入")
        return
    
    print(f"清理前語者資料庫狀態:")
    print(f"- 總語者數: {len(speaker_database)}")
    
    # 統計每個語者的樣本數
    speaker_stats = {}
    for speaker_id, data in speaker_database.items():
        sample_count = len(data.get('embeddings', []))
        speaker_stats[speaker_id] = sample_count
        print(f"  {speaker_id}: {sample_count} 個語音樣本")
    
    # 標記不符合閾值的語者
    speakers_to_remove = []
    for speaker_id, sample_count in speaker_stats.items():
        if sample_count < min_samples:
            speakers_to_remove.append(speaker_id)
    
    if not speakers_to_remove:
        print(f"沒有找到樣本數低於 {min_samples} 的語者")
        return
    
    print(f"\n將移除以下 {len(speakers_to_remove)} 位語者:")
    for speaker_id in speakers_to_remove:
        print(f"  {speaker_id}: {speaker_stats[speaker_id]} 個語音樣本")
    
    # 詢問確認
    confirm = input("\n確認移除這些語者? (y/n): ")
    if confirm.lower() != 'y':
        print("操作已取消")
        return
    
    # 創建備份
    backup_path = f"memory/speaker_models_backup_{int(time.time())}.pkl"
    save_speaker_database(speaker_database, speaker_counter, backup_path)
    print(f"已創建備份: {backup_path}")
    
    # 移除語者
    for speaker_id in speakers_to_remove:
        del speaker_database[speaker_id]
    
    # 保存更新後的資料庫
    save_speaker_database(speaker_database, speaker_counter)
    print(f"\n清理完成。剩餘 {len(speaker_database)} 位語者")

if __name__ == "__main__":
    min_samples = 15
    if len(sys.argv) > 1:
        try:
            min_samples = int(sys.argv[1])
        except ValueError:
            print(f"無效的樣本數閾值: {sys.argv[1]}, 使用預設值 {min_samples}")
    
    print(f"使用最低樣本數閾值: {min_samples}")
    clean_speaker_database(min_samples)
