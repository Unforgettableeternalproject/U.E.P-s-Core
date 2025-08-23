#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修復 debug_api.py 中的模組調用"""

import re

def fix_debug_api():
    file_path = "devtools/debug_api.py"
    
    # 讀取檔案
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 替換模組調用
    replacements = [
        (r'(\w+) = modules\["stt"\]', r'\1 = get_or_load_module("stt")'),
        (r'(\w+) = modules\["nlp"\]', r'\1 = get_or_load_module("nlp")'),
        (r'(\w+) = modules\["mem"\]', r'\1 = get_or_load_module("mem")'),
        (r'(\w+) = modules\["llm"\]', r'\1 = get_or_load_module("llm")'),
        (r'(\w+) = modules\["tts"\]', r'\1 = get_or_load_module("tts")'),
        (r'(\w+) = modules\["sysmod"\]', r'\1 = get_or_load_module("sysmod")'),
        (r'(\w+) = modules\["ui"\]', r'\1 = get_or_load_module("ui")'),
        (r'(\w+) = modules\["ani"\]', r'\1 = get_or_load_module("ani")'),
        (r'(\w+) = modules\["mov"\]', r'\1 = get_or_load_module("mov")'),
    ]
    
    # 執行替換
    for pattern, replacement in replacements:
        content = re.sub(pattern, replacement, content)
    
    # 寫回檔案
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(content)
    
    print("修復完成！")

if __name__ == "__main__":
    fix_debug_api()
