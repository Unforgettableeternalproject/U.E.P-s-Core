"""
修復 JSONL 訓練資料格式問題
將字串化的列表轉換為真正的 JSON 列表
"""

import json
import ast
from pathlib import Path
from datetime import datetime

def fix_stringified_lists(item: dict) -> dict:
    """
    修復字串化的列表欄位
    
    Args:
        item: JSONL 資料項目
        
    Returns:
        修復後的項目
    """
    # 修復 tokens
    if isinstance(item.get('tokens'), str):
        try:
            # 嘗試用 ast.literal_eval 解析 Python 字串表示
            item['tokens'] = ast.literal_eval(item['tokens'])
        except Exception as e:
            print(f"  警告: 無法解析 tokens: {e}")
            print(f"    內容: {item['tokens'][:100]}")
    
    # 修復 bio_labels
    if isinstance(item.get('bio_labels'), str):
        try:
            item['bio_labels'] = ast.literal_eval(item['bio_labels'])
        except Exception as e:
            print(f"  警告: 無法解析 bio_labels: {e}")
            print(f"    內容: {item['bio_labels'][:100]}")
    
    # 修復 segments (如果是字串)
    if isinstance(item.get('segments'), str):
        try:
            item['segments'] = ast.literal_eval(item['segments'])
        except Exception as e:
            print(f"  警告: 無法解析 segments: {e}")
            print(f"    內容: {item['segments'][:100]}")
    
    # 修復 metadata (如果是字串)
    if isinstance(item.get('metadata'), str):
        try:
            item['metadata'] = ast.literal_eval(item['metadata'])
        except Exception as e:
            print(f"  警告: 無法解析 metadata: {e}")
            print(f"    內容: {item['metadata'][:100]}")
    
    return item


def main():
    # 檔案路徑
    script_dir = Path(__file__).parent
    input_file = script_dir / "nlp_training_data.jsonl"
    output_file = script_dir / "nlp_training_data_fixed.jsonl"
    
    # 備份原始檔案
    backup_file = script_dir / f"nlp_training_data_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    
    print(f"讀取檔案: {input_file}")
    print(f"輸出檔案: {output_file}")
    print(f"備份檔案: {backup_file}")
    print()
    
    # 創建備份
    with open(input_file, 'r', encoding='utf-8') as f:
        with open(backup_file, 'w', encoding='utf-8') as backup:
            backup.write(f.read())
    
    print("✓ 已創建備份")
    print()
    
    # 讀取並修復資料
    fixed_count = 0
    total_count = 0
    errors = []
    
    with open(input_file, 'r', encoding='utf-8') as infile:
        with open(output_file, 'w', encoding='utf-8') as outfile:
            for line_num, line in enumerate(infile, 1):
                total_count += 1
                
                try:
                    # 解析 JSON
                    item = json.loads(line.strip())
                    
                    # 檢查是否需要修復
                    needs_fix = (
                        isinstance(item.get('tokens'), str) or
                        isinstance(item.get('bio_labels'), str) or
                        isinstance(item.get('segments'), str) or
                        isinstance(item.get('metadata'), str)
                    )
                    
                    if needs_fix:
                        fixed_count += 1
                        if fixed_count <= 3:  # 只顯示前3個
                            print(f"Line {line_num}: 修復字串化欄位")
                        item = fix_stringified_lists(item)
                    
                    # 寫入修復後的資料
                    outfile.write(json.dumps(item, ensure_ascii=False) + '\n')
                    
                except Exception as e:
                    error_msg = f"Line {line_num}: {e}"
                    errors.append(error_msg)
                    if len(errors) <= 5:
                        print(f"錯誤 - {error_msg}")
    
    print()
    print("=" * 60)
    print(f"處理完成!")
    print(f"總行數: {total_count}")
    print(f"修復行數: {fixed_count}")
    print(f"錯誤數: {len(errors)}")
    print()
    
    if errors:
        print("前 10 個錯誤:")
        for error in errors[:10]:
            print(f"  {error}")
        print()
    
    # 替換原檔案
    if len(errors) == 0:
        print("無錯誤,替換原始檔案...")
        output_file.replace(input_file)
        print(f"✓ 已更新 {input_file}")
    else:
        print(f"有 {len(errors)} 個錯誤,請檢查 {output_file}")
        print("如果確認無誤,請手動替換原檔案")


if __name__ == "__main__":
    main()
