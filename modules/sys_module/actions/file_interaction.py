from pathlib import Path
from utils.debug_helper import info_log, error_log

def drop_and_read(file_path: str) -> str:
    raise NotImplementedError("drop_and_read 尚未實作")

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

def intelligent_archive(file_path: str, rule: str) -> str:
    raise NotImplementedError("intelligent_archive 尚未實作")

    info_log(f"[file] 歸檔：{file_path}，規則：{rule}")
    # TODO: 實作 date/type/keyword 規則
    return "/path/to/new/location"

def summarize_tag(file_path: str) -> dict:
    raise NotImplementedError("summarize_tag 尚未實作")

    info_log(f"[file] 摘要並標記：{file_path}")
    # TODO: 呼叫 LLMModule 生成摘要、輸出 summary.md
    return {"summary_file": file_path + "-summary.md"}
