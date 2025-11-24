"""快速驗證訓練資料格式"""
import json
from pathlib import Path

data_path = Path(__file__).parent / "nlp_training_data.jsonl"

print(f"檢查文件: {data_path}")
print("="*60)

errors = []
valid_count = 0

with open(data_path, 'r', encoding='utf-8') as f:
    for line_num, line in enumerate(f, 1):
        line = line.strip()
        if not line:
            continue
            
        try:
            data = json.loads(line)
            
            # 檢查必要欄位
            if 'tokens' not in data:
                errors.append(f"Line {line_num}: 缺少 'tokens'")
                continue
            if 'bio_labels' not in data:
                errors.append(f"Line {line_num}: 缺少 'bio_labels'")
                continue
            
            # 檢查 tokens 是否為列表
            if not isinstance(data['tokens'], list):
                errors.append(f"Line {line_num}: 'tokens' 不是列表，而是 {type(data['tokens']).__name__}")
                print(f"  錯誤行內容: {data.get('text', '')[:50]}...")
                print(f"  tokens: {str(data['tokens'])[:100]}")
                continue
            
            # 檢查 bio_labels 是否為列表
            if not isinstance(data['bio_labels'], list):
                errors.append(f"Line {line_num}: 'bio_labels' 不是列表，而是 {type(data['bio_labels']).__name__}")
                print(f"  錯誤行內容: {data.get('text', '')[:50]}...")
                print(f"  bio_labels: {str(data['bio_labels'])[:100]}")
                continue
            
            # 檢查長度是否匹配
            if len(data['tokens']) != len(data['bio_labels']):
                errors.append(f"Line {line_num}: tokens 長度 ({len(data['tokens'])}) 與 bio_labels 長度 ({len(data['bio_labels'])}) 不匹配")
                continue
            
            valid_count += 1
            
        except json.JSONDecodeError as e:
            errors.append(f"Line {line_num}: JSON 解析錯誤 - {e}")

print(f"\n檢查完成!")
print(f"有效行: {valid_count}")
print(f"錯誤數: {len(errors)}")

if errors:
    print(f"\n前 10 個錯誤:")
    for error in errors[:10]:
        print(f"  {error}")
else:
    print("\n✅ 所有資料格式正確!")
