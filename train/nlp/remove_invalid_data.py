"""
移除格式有問題的訓練資料行
保留 tokens 和 bio_labels 是正確列表格式的資料
"""

import json
from pathlib import Path
from datetime import datetime

def is_valid_entry(item: dict) -> bool:
    """
    檢查資料項目是否有效
    
    Args:
        item: JSONL 資料項目
        
    Returns:
        True if valid, False otherwise
    """
    # 檢查必須欄位
    if 'tokens' not in item or 'bio_labels' not in item:
        return False
    
    # tokens 必須是列表
    if not isinstance(item['tokens'], list):
        return False
    
    # bio_labels 必須是列表
    if not isinstance(item['bio_labels'], list):
        return False
    
    # 長度必須匹配
    if len(item['tokens']) != len(item['bio_labels']):
        return False
    
    return True


def main():
    # 檔案路徑
    script_dir = Path(__file__).parent
    input_file = script_dir / "nlp_training_data.jsonl"
    output_file = script_dir / "nlp_training_data_clean.jsonl"
    removed_file = script_dir / "nlp_training_data_removed.jsonl"
    
    print(f"讀取檔案: {input_file}")
    print()
    
    # 讀取並過濾資料
    valid_items = []
    invalid_items = []
    
    with open(input_file, 'r', encoding='utf-8') as infile:
        for line_num, line in enumerate(infile, 1):
            try:
                # 解析 JSON
                item = json.loads(line.strip())
                
                if is_valid_entry(item):
                    valid_items.append(item)
                else:
                    invalid_items.append((line_num, item))
                    if len(invalid_items) <= 5:
                        print(f"Line {line_num}: 移除無效項目")
                        print(f"  text: {item.get('text', '')[:50]}...")
                        print(f"  tokens type: {type(item.get('tokens'))}")
                        print()
                    
            except Exception as e:
                invalid_items.append((line_num, {"error": str(e)}))
                print(f"Line {line_num}: JSON 解析錯誤 - {e}")
    
    # 寫入乾淨的資料
    with open(output_file, 'w', encoding='utf-8') as outfile:
        for item in valid_items:
            outfile.write(json.dumps(item, ensure_ascii=False) + '\n')
    
    # 寫入被移除的資料(用於檢查)
    with open(removed_file, 'w', encoding='utf-8') as removed:
        for line_num, item in invalid_items:
            removed.write(f"# Line {line_num}\n")
            removed.write(json.dumps(item, ensure_ascii=False) + '\n')
    
    print("=" * 60)
    print(f"處理完成!")
    print(f"原始資料: {len(valid_items) + len(invalid_items)} 行")
    print(f"有效資料: {len(valid_items)} 行")
    print(f"移除資料: {len(invalid_items)} 行 ({len(invalid_items)/(len(valid_items) + len(invalid_items))*100:.1f}%)")
    print()
    print(f"乾淨資料已儲存到: {output_file}")
    print(f"移除資料已儲存到: {removed_file}")
    print()
    
    # 替換原檔案
    if len(invalid_items) > 0:
        print("替換原始檔案...")
        output_file.replace(input_file)
        print(f"✓ 已更新 {input_file}")


if __name__ == "__main__":
    main()
