import os
import re

def reverse_frames(folder):
    # 抓取資料夾中的檔案
    files = sorted(os.listdir(folder))
    
    # 過濾出有數字編號的檔案
    pattern = re.compile(r"(.*?)(\d+)(\.\w+)$")  # 匹配 "xxx_00.png"
    matches = [f for f in files if pattern.match(f)]
    
    if not matches:
        print("❌ 找不到符合編號格式的檔案")
        return
    
    # 解析編號
    parsed = []
    for f in matches:
        m = pattern.match(f)
        name, num, ext = m.groups()
        parsed.append((f, name, int(num), ext))
    
    # 照編號排序
    parsed.sort(key=lambda x: x[2])
    
    # 取得最大編號
    max_index = parsed[-1][2]
    
    # 開始倒序命名
    for i, (old, name, num, ext) in enumerate(parsed):
        new_index = i
        new_name = f"diamond_girl_turn_right_{new_index:0{len(str(max_index))}d}{ext}"
        old_path = os.path.join(folder, old)
        new_path = os.path.join(folder, new_name)
        os.rename(old_path, new_path)
        print(f"{old} -> {new_name}")

# 使用範例
if __name__ == "__main__":
    reverse_frames("./frames")  # 把 ./frames 改成你的資料夾路徑
