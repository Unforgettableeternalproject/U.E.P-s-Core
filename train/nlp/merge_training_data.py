"""
NLP 訓練資料合併工具

功能：
1. 比對新舊訓練資料
2. 移除重複項目（找出反交集）
3. 合併新資料到舊資料集
4. 生成統計報告
"""

import json
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple
from datetime import datetime

def load_jsonl(filepath: Path) -> List[Dict]:
    """載入 JSONL 文件"""
    data = []
    with open(filepath, 'r', encoding='utf-8') as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if line:
                try:
                    data.append(json.loads(line))
                except json.JSONDecodeError as e:
                    print(f"⚠️  警告：第 {line_num} 行解析失敗: {e}")
    return data

def save_jsonl(data: List[Dict], filepath: Path):
    """儲存為 JSONL 文件"""
    with open(filepath, 'w', encoding='utf-8') as f:
        for item in data:
            f.write(json.dumps(item, ensure_ascii=False) + '\n')

def get_item_key(item: Dict) -> str:
    """
    生成項目的唯一鍵值
    
    使用 text 和 segments 的組合作為唯一識別
    因為相同文字可能有不同的標註（這種情況很少但可能存在）
    """
    text = item.get('text', '')
    segments = item.get('segments', [])
    
    # 處理 segments 可能不是列表的情況
    if not isinstance(segments, list):
        segments = []
    
    # 建立 segments 的規範化表示
    segments_repr = tuple(
        (seg.get('text', ''), seg.get('label', ''), seg.get('start', 0), seg.get('end', 0))
        for seg in segments
        if isinstance(seg, dict)  # 確保 seg 是字典
    )
    
    return f"{text}|||{segments_repr}"

def compare_datasets(old_data: List[Dict], new_data: List[Dict]) -> Tuple[Set[str], Set[str], Set[str]]:
    """
    比對兩個資料集
    
    Returns:
        (共同項, 僅在舊資料, 僅在新資料)
    """
    old_keys = {get_item_key(item): item for item in old_data}
    new_keys = {get_item_key(item): item for item in new_data}
    
    old_key_set = set(old_keys.keys())
    new_key_set = set(new_keys.keys())
    
    common = old_key_set & new_key_set
    only_old = old_key_set - new_key_set
    only_new = new_key_set - old_key_set
    
    return common, only_old, only_new

def print_statistics(old_data: List[Dict], new_data: List[Dict], 
                    common: Set[str], only_old: Set[str], only_new: Set[str]):
    """列印統計資訊"""
    print("\n" + "="*60)
    print("📊 訓練資料比對統計")
    print("="*60)
    print(f"\n舊資料集 (nlp_training_data.jsonl):")
    print(f"  總項目數: {len(old_data)}")
    print(f"  唯一項目: {len(old_data)}")
    
    print(f"\n新資料集 (nlp_training_data2.jsonl):")
    print(f"  總項目數: {len(new_data)}")
    print(f"  唯一項目: {len(new_data)}")
    
    print(f"\n比對結果:")
    print(f"  共同項目（重複）: {len(common)} ({len(common)/max(len(old_data), len(new_data))*100:.1f}%)")
    print(f"  僅在舊資料: {len(only_old)}")
    print(f"  僅在新資料: {len(only_new)}")
    
    print(f"\n合併後預期（聯集）:")
    total_union = len(only_old) + len(only_new) + len(common)
    print(f"  總項目數: {total_union}")
    print(f"  = 僅舊資料 ({len(only_old)}) + 僅新資料 ({len(only_new)}) + 共同項 ({len(common)})")
    print(f"  將移除重複: {len(common)} 項")
    print("="*60 + "\n")

def sample_items(items: List[Dict], keys: Set[str], num_samples: int = 3) -> List[Dict]:
    """抽樣顯示項目"""
    key_to_item = {get_item_key(item): item for item in items}
    sampled_keys = list(keys)[:num_samples]
    return [key_to_item[key] for key in sampled_keys if key in key_to_item]

def print_samples(title: str, items: List[Dict]):
    """列印樣本"""
    print(f"\n{title}:")
    for i, item in enumerate(items, 1):
        text = item.get('text', '')[:50]
        segments_raw = item.get('segments', [])
        # 處理 segments 可能不是列表或包含非字典元素的情況
        if isinstance(segments_raw, list):
            segments = [seg.get('label', '') for seg in segments_raw if isinstance(seg, dict)]
        else:
            segments = []
        print(f"  {i}. \"{text}...\" -> {segments}")

def main():
    """主函數"""
    # 設定路徑
    script_dir = Path(__file__).parent
    old_file = script_dir / "nlp_training_data.jsonl"
    new_file = script_dir / "nlp_training_data_additional.jsonl"
    
    # 檢查文件是否存在
    if not old_file.exists():
        print(f"❌ 錯誤：找不到舊資料集: {old_file}")
        sys.exit(1)
    
    if not new_file.exists():
        print(f"❌ 錯誤：找不到新資料集: {new_file}")
        sys.exit(1)
    
    print("🔍 正在載入訓練資料...")
    
    # 載入資料
    old_data = load_jsonl(old_file)
    new_data = load_jsonl(new_file)
    
    print(f"✅ 已載入舊資料: {len(old_data)} 項")
    print(f"✅ 已載入新資料: {len(new_data)} 項")
    
    # 比對資料
    print("\n🔄 正在比對資料...")
    common, only_old, only_new = compare_datasets(old_data, new_data)
    
    # 列印統計
    print_statistics(old_data, new_data, common, only_old, only_new)
    
    # 顯示樣本
    if common:
        samples = sample_items(old_data, common, 3)
        print_samples("📝 重複項目樣本 (將被移除)", samples)
    
    if only_new:
        samples = sample_items(new_data, only_new, 3)
        print_samples("✨ 新增項目樣本", samples)
    
    # 詢問是否合併
    print("\n" + "="*60)
    response = input("是否要合併資料？(y/n): ").strip().lower()
    
    if response != 'y':
        print("❌ 已取消合併")
        return
    
    # 建立聯集（保留所有唯一項目，重複的只保留一個）
    print("\n🔨 正在建立合併資料集（聯集）...")
    
    old_key_to_item = {get_item_key(item): item for item in old_data}
    new_key_to_item = {get_item_key(item): item for item in new_data}
    
    # 聯集：所有唯一項目（重複的從新資料取）
    merged_dict = {}
    
    # 先添加所有舊資料
    for key in old_key_to_item:
        merged_dict[key] = old_key_to_item[key]
    
    # 再添加新資料（會覆蓋重複的，使用新版本）
    for key in new_key_to_item:
        merged_dict[key] = new_key_to_item[key]
    
    merged_items = list(merged_dict.values())
    
    print(f"✅ 合併完成，共 {len(merged_items)} 項 (移除 {len(common)} 個重複項)")
    
    # 儲存合併結果
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # 1. 備份原始舊資料
    backup_file = script_dir / f"nlp_training_data.jsonl.backup_{timestamp}"
    print(f"\n💾 正在備份原始資料到: {backup_file.name}")
    old_file.rename(backup_file)
    
    # 2. 儲存合併後的資料為新的 nlp_training_data.jsonl
    print(f"💾 正在儲存合併資料到: {old_file.name}")
    save_jsonl(merged_items, old_file)
    
    # 3. 產生合併後檔案副本（僅供參考）
    merged_file = script_dir / f"nlp_training_data_merged_{timestamp}.jsonl"
    print(f"💾 正在儲存合併資料副本到: {merged_file.name}")
    save_jsonl(merged_items, merged_file)
    
    # 4. 產生僅新增項目檔案（僅供參考）
    new_only_file = None
    if only_new:
        new_only_file = script_dir / f"nlp_training_data_new_only_{timestamp}.jsonl"
        new_only_items = [new_key_to_item[key] for key in only_new]
        print(f"💾 正在儲存新增項目到: {new_only_file.name}")
        save_jsonl(new_only_items, new_only_file)
    
    print("\n" + "="*60)
    print("✅ 合併完成！")
    print("="*60)
    print(f"\n產生的檔案:")
    print(f"  1. {old_file.name} - 合併後的訓練資料 ({len(merged_items)} 項)")
    print(f"  2. {backup_file.name} - 原始資料備份 ({len(old_data)} 項)")
    print(f"  3. {merged_file.name} - 合併資料副本 ({len(merged_items)} 項)")
    if new_only_file:
        print(f"  4. {new_only_file.name} - 僅新增項目 ({len(only_new)} 項)")
    
    print(f"\n統計摘要:")
    print(f"  原始舊資料: {len(old_data)}")
    print(f"  原始新資料: {len(new_data)}")
    print(f"  移除重複項: {len(common)}")
    print(f"  最終總數: {len(merged_items)} = {len(old_data)} + {len(new_data)} - {len(common)}")
    
    print("\n💡 提示：")
    print("  - 原始舊資料已備份")
    print("  - nlp_training_data.jsonl 現在包含合併後的資料")
    print("  - 可以使用新資料訓練模型測試準確度")
    print("  - 如需還原，重新命名備份檔案即可")
    print("="*60 + "\n")

if __name__ == "__main__":
    main()
