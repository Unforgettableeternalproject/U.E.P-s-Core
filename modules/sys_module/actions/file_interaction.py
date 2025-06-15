from pathlib import Path
from utils.debug_helper import info_log, error_log

def drop_and_read(file_path: str) -> str:
    """
    將拖放的檔案內容讀取為文字 - 支援 txt、md、pdf 格式
    
    Args:
        file_path: 要讀取的檔案路徑
        
    Returns:
        檔案的文字內容
        
    Raises:
        ValueError: 當檔案格式不支援時
        Exception: 其他讀取錯誤
    """
    info_log(f"[file] 讀取檔案：{file_path}")
    ext = Path(file_path).suffix.lower()
    try:
        if ext in (".txt", ".md"):
            return Path(file_path).read_text(encoding="utf-8")
        elif ext == ".pdf":
            from pdfminer.high_level import extract_text # type: ignore
            return extract_text(file_path)
        else:
            raise ValueError(f"不支援格式：{ext}")
    except Exception as e:
        error_log(f"[file] 讀取失敗：{e}")
        raise

def intelligent_archive(file_path: str, target_dir: str = "") -> str:
    """
    智能歸檔功能 - 根據檔案類型、使用者指定或歷史記錄智能地將檔案歸檔到適當位置
    
    Args:
        file_path: 要歸檔的檔案路徑
        target_dir: 使用者指定的目標資料夾，若為空則自動判斷
        
    Returns:
        歸檔後的新檔案路徑
    """
    import os
    import shutil
    import json
    import datetime
    from pathlib import Path
    
    info_log(f"[file] 準備歸檔檔案：{file_path}")
    
    # 檢查檔案是否存在
    file_path_obj = Path(file_path)
    if not file_path_obj.exists():
        error_log(f"[file] 歸檔失敗：檔案 {file_path} 不存在")
        raise FileNotFoundError(f"檔案 {file_path} 不存在")
    
    # 取得檔案資訊
    file_name = file_path_obj.name
    file_ext = file_path_obj.suffix.lower()
    
    # 如果使用者有指定目標資料夾，優先使用
    if target_dir and Path(target_dir).exists():
        target_path = Path(target_dir) / file_name
        info_log(f"[file] 使用用戶指定位置：{target_dir}")
    else:
        # 查看過去的歸檔記錄
        archive_history_path = Path("configs") / "archive_history.json"
        similar_target = None
        
        # 檢查歷史記錄
        if archive_history_path.exists():
            try:
                with open(archive_history_path, "r", encoding="utf-8") as f:
                    history = json.load(f)
                
                # 尋找相似檔案的歸檔位置
                for record in history.get("archives", []):
                    if record.get("file_ext") == file_ext:
                        similar_target = record.get("target_dir")
                        break
                        
                if similar_target:
                    info_log(f"[file] 找到相似檔案歷史記錄，使用位置：{similar_target}")
            except Exception as e:
                error_log(f"[file] 讀取歸檔記錄失敗：{e}")
        
        # 如果找到了相似的歸檔記錄
        if similar_target and Path(similar_target).exists():
            target_path = Path(similar_target) / file_name
        else:
            # 根據檔案類型選擇預設位置
            if file_ext in (".txt", ".doc", ".docx", ".pdf", ".md", ".csv", ".xlsx", ".pptx"):
                # 文件類型歸到文件資料夾
                target_path = Path(os.path.expanduser("~/Documents")) / file_name
            elif file_ext in (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff"):
                # 圖片類型歸到圖片資料夾
                target_path = Path(os.path.expanduser("~/Pictures")) / file_name
            elif file_ext in (".mp3", ".wav", ".flac", ".ogg", ".m4a"):
                # 音樂類型歸到音樂資料夾
                target_path = Path(os.path.expanduser("~/Music")) / file_name
            elif file_ext in (".mp4", ".avi", ".mkv", ".mov", ".wmv"):
                # 影片類型歸到影片資料夾
                target_path = Path(os.path.expanduser("~/Videos")) / file_name
            else:
                # 其他類型歸到下載資料夾
                target_path = Path(os.path.expanduser("~/Downloads")) / file_name
            
            info_log(f"[file] 根據檔案類型選擇預設位置：{target_path}")
    
    # 確保目標資料夾存在
    target_path.parent.mkdir(parents=True, exist_ok=True)
    
    # 處理同名檔案
    if target_path.exists():
        base = target_path.stem
        extension = target_path.suffix
        counter = 1
        
        while True:
            new_name = f"{base}_{counter}{extension}"
            new_target = target_path.parent / new_name
            if not new_target.exists():
                target_path = new_target
                break
            counter += 1
    
    # 移動檔案
    try:
        shutil.copy2(file_path, target_path)
        info_log(f"[file] 檔案已複製至：{target_path}")
        
        # 記錄歸檔資訊
        archive_history_path = Path("configs") / "archive_history.json"
        
        history = {"archives": []}
        if archive_history_path.exists():
            try:
                with open(archive_history_path, "r", encoding="utf-8") as f:
                    history = json.load(f)
            except:
                pass
        
        # 添加新記錄
        new_record = {
            "original_path": str(file_path),
            "target_dir": str(target_path.parent),
            "file_name": file_name,
            "file_ext": file_ext,
            "timestamp": datetime.datetime.now().isoformat()
        }
        
        if "archives" not in history:
            history["archives"] = []
        
        history["archives"].insert(0, new_record)  # 最新記錄放在最前面
        
        # 限制記錄數量，避免檔案過大
        history["archives"] = history["archives"][:100]
        
        # 保存記錄
        try:
            with open(archive_history_path, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            error_log(f"[file] 保存歸檔記錄失敗：{e}")
        
        return str(target_path)
    except Exception as e:
        error_log(f"[file] 歸檔失敗：{e}")
        raise

def summarize_tag(file_path: str, tag_count: int = 3) -> dict:
    """
    為檔案生成摘要與標籤 - 解析檔案內容，使用LLM模組生成摘要，輸出至同路徑的summary檔案
    
    Args:
        file_path: 要摘要的檔案路徑
        tag_count: 要生成的標籤數量，默認為3個
        
    Returns:
        包含摘要檔案路徑的字典
    """
    import os
    import json
    import datetime
    import importlib
    from pathlib import Path
    
    info_log(f"[file] 準備摘要並標記檔案：{file_path}")
    
    # 檢查檔案是否存在
    file_path_obj = Path(file_path)
    if not file_path_obj.exists():
        error_log(f"[file] 摘要失敗：檔案 {file_path} 不存在")
        raise FileNotFoundError(f"檔案 {file_path} 不存在")
    
    # 讀取檔案內容
    try:
        content = drop_and_read(file_path)  # 使用已實作的函數讀取檔案
    except Exception as e:
        error_log(f"[file] 摘要失敗，無法讀取檔案：{e}")
        raise
    
    # 準備生成摘要
    summary_content = ""
    tags = []
    
    # 嘗試使用LLM模組生成摘要，如果模組未啟用則使用簡單摘要
    try:
        # 嘗試導入並使用LLM模組
        try:
            # 動態導入LLM模組
            llm_module = None
            try:
                from modules.llm_module.llm_module import LlmModule
                from configs.config_loader import get_config
                
                config = get_config().get("llm_module", {})
                llm_module = LlmModule(config)
                info_log(f"[file] 成功載入LLM模組")
            except ImportError:
                info_log(f"[file] LLM模組未啟用或無法導入，將使用簡單摘要")
                
            if llm_module:
                # 構建摘要請求
                request_data = {
                    "task": "summarize",
                    "content": content[:5000],  # 限制長度避免過長
                    "options": {
                        "summary_length": "short",
                        "generate_tags": True,
                        "tag_count": tag_count
                    }
                }
                
                # 呼叫LLM模組
                response = llm_module.handle(request_data)
                
                if response and "summary" in response:
                    summary_content = response["summary"]
                    tags = response.get("tags", [])
                    info_log(f"[file] LLM模組成功生成摘要和{len(tags)}個標籤")
                else:
                    raise ValueError("LLM模組未返回有效摘要")
        except Exception as e:
            info_log(f"[file] 使用LLM模組摘要失敗：{e}，將使用簡單摘要")
            # 若LLM模組失敗，使用簡單摘要法
            raise
            
        # 如果沒有成功使用LLM模組，使用簡單方法生成摘要
        if not summary_content:
            raise ValueError("需要使用簡單摘要")
            
    except Exception as e:
        # 使用簡單摘要方法
        info_log(f"[file] 使用簡單摘要方法")
        
        # 簡單摘要：取前1000個字符
        summary_preview = content[:1000] + ("..." if len(content) > 1000 else "")
        
        # 簡單標籤：使用檔案類型和大小
        file_ext = file_path_obj.suffix.lower()[1:]  # 移除開頭的點
        file_size_kb = os.path.getsize(file_path) / 1024
        file_size_tag = f"{file_size_kb:.1f}KB" if file_size_kb < 1024 else f"{file_size_kb/1024:.1f}MB"
        
        tags = [file_ext, file_size_tag, file_path_obj.name.split(".")[0]]
        summary_content = f"檔案預覽：\n\n{summary_preview}\n\n(自動生成的簡單摘要，內容為檔案前1000個字符)"
    
    # 生成摘要檔案的內容
    output_content = f"""# {file_path_obj.name} 摘要

## 檔案資訊
- 原始檔案：{file_path_obj.name}
- 建立時間：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## 標籤
{', '.join(f'`{tag}`' for tag in tags)}

## 內容摘要
{summary_content}
"""
    
    # 建立摘要檔案
    summary_file_path = file_path_obj.parent / f"{file_path_obj.stem}-summary.md"
    try:
        with open(summary_file_path, "w", encoding="utf-8") as f:
            f.write(output_content)
        info_log(f"[file] 成功生成摘要檔案：{summary_file_path}")
    except Exception as e:
        error_log(f"[file] 建立摘要檔案失敗：{e}")
        raise
    
    # 返回摘要檔案資訊
    return {
        "summary_file": str(summary_file_path),
        "tags": tags
    }
