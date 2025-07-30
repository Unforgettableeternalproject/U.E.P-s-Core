#!/usr/bin/env python3
"""
簡化版 TTS Dataset 生成器
直接批量生成所有音檔
"""

import os
import csv
import asyncio
import sys
from pathlib import Path

# 添加專案根目錄到 sys.path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from core.registry import get_module
from configs.config_loader import load_config


async def generate_all_audio():
    """生成所有音檔"""
    
    print("🚀 開始 TTS Dataset 生成...")
    
    # 1. 載入 TTS 模組
    print("📦 載入 TTS 模組...")
    tts_module = get_module("tts_module")
    if tts_module is None:
        print("❌ 無法載入 TTS 模組")
        return
    
    # 2. 初始化 TTS 模組
    print("⚙️ 初始化 TTS 模組...")
    if not tts_module.initialize():
        print("❌ TTS 模組初始化失敗")
        return
    
    print("✅ TTS 模組準備完成")
    
    # 3. 讀取 dataset
    dataset_file = Path("train/tts/dataset.csv")
    if not dataset_file.exists():
        print(f"❌ 找不到 dataset 檔案: {dataset_file}")
        return
    
    dataset = []
    # 嘗試多種編碼格式
    encodings = ['utf-8', 'utf-8-sig', 'cp1252', 'iso-8859-1']
    
    for encoding in encodings:
        try:
            with open(dataset_file, 'r', encoding=encoding) as f:
                reader = csv.DictReader(f)
                dataset = []  # 重置列表
                for idx, row in enumerate(reader):
                    # Handle BOM characters in column names
                    prompt_key = None
                    for key in row.keys():
                        if 'Prompts' in key:
                            prompt_key = key
                            break
                    
                    prompts = row.get(prompt_key, '').strip() if prompt_key else ''
                    if prompts:
                        dataset.append({
                            'index': idx,
                            'text': prompts
                        })
            print(f"✅ 使用 {encoding} 編碼成功讀取檔案")
            break  # 成功讀取，跳出迴圈
        except UnicodeDecodeError:
            print(f"⚠️ {encoding} 編碼失敗，嘗試下一個...")
            continue
        except Exception as e:
            print(f"❌ 讀取檔案時發生錯誤: {str(e)}")
            return
    else:
        print("❌ 無法以任何編碼格式讀取檔案")
        return
    
    print(f"📄 讀取到 {len(dataset)} 筆資料")
    
    # 4. 創建輸出目錄
    output_dir = Path("outputs/data")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # 5. 批量生成
    success_count = 0
    total = len(dataset)
    
    for idx, item in enumerate(dataset):
        text = item['text']
        filename = f"uep-{idx:03d}.wav"
        output_path = output_dir / filename
        
        print(f"[{idx+1:3d}/{total}] 正在生成: {filename}")
        print(f"           文字: {text[:50]}{'...' if len(text) > 50 else ''}")
        
        # 檢查檔案是否已存在
        if output_path.exists():
            print(f"           ⏭️ 檔案已存在，跳過")
            success_count += 1
            continue
        
        try:
            # 生成音檔
            tts_input = {
                "text": text,
                "mood": "neutral",
                "save": True,
                "force_chunking": False
            }
            
            result = await tts_module.handle(tts_input)
            
            if result.get("status") == "success" and result.get("output_path"):
                # 移動並重新命名檔案
                generated_file = Path(result["output_path"])
                if generated_file.exists():
                    generated_file.rename(output_path)
                    print(f"           ✅ 生成成功")
                    success_count += 1
                else:
                    print(f"           ❌ 生成檔案不存在")
            else:
                print(f"           ❌ 生成失敗: {result.get('message', '未知錯誤')}")
                
        except Exception as e:
            print(f"           ❌ 發生錯誤: {str(e)}")
        
        # 簡單的進度顯示
        if (idx + 1) % 10 == 0:
            print(f"\n📊 進度: {idx+1}/{total} ({success_count} 成功)\n")
    
    # 總結
    print("\n" + "="*60)
    print(f"🎉 生成完成!")
    print(f"📊 總計: {total} 個檔案")
    print(f"✅ 成功: {success_count} 個檔案")
    print(f"❌ 失敗: {total - success_count} 個檔案")
    print(f"📁 輸出目錄: {output_dir.absolute()}")
    print("="*60)


if __name__ == "__main__":
    try:
        asyncio.run(generate_all_audio())
    except KeyboardInterrupt:
        print("\n\n❌ 使用者中斷操作")
    except Exception as e:
        print(f"\n❌ 發生未預期錯誤: {str(e)}")
