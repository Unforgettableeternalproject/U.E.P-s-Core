import threading
import time
import json
import os
import win32clipboard
import difflib
from tkinter import simpledialog
from dotenv import load_dotenv
from utils.debug_helper import info_log, error_log

load_dotenv()
HISTORY_FILE = os.getenv("CLIPBOARD_HISTORY_FILE", "clipboard_history.json")

# 初始化歷史
if os.path.exists(HISTORY_FILE):
    with open(HISTORY_FILE, "r") as f:
        _history = json.load(f)
else:
    _history = []

_monitoring = True

def _save_history():
    with open(HISTORY_FILE, "w") as f:
        json.dump(_history, f)

def _get_text():
    for _ in range(10):
        try:
            win32clipboard.OpenClipboard()
            data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
            win32clipboard.CloseClipboard()
            return data
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("無法存取剪貼簿")

def _monitor_loop():
    prev = _get_text()
    if prev:
        _history.append(prev); _save_history()
    while _monitoring:
        cur = _get_text()
        if cur and cur != prev and cur not in _history:
            _history.append(cur)
            _save_history()
            info_log(f"[CLIP] 新增剪貼簿記錄: {cur[:30]}...")
            prev = cur
        time.sleep(1)

# 啟動監控
# threading.Thread(target=_monitor_loop, daemon=True).start()

def clipboard_tracker(parent=None):
    """搜尋剪貼簿歷史並複製選定項目"""
    kw = simpledialog.askstring("搜尋剪貼簿", "請輸入關鍵字：", parent=parent)
    if not kw: return None
    matches = difflib.get_close_matches(kw, _history, n=5, cutoff=0.1)
    if not matches:
        info_log("[CLIP] 無相關記錄", "WARNING")
        return None
    # 顯示並讓使用者選擇
    for i, m in enumerate(matches, 1):
        print(f"{i}. {m}")
    idx = simpledialog.askinteger("選擇", "輸入序號：", parent=parent)
    if not idx or idx<1 or idx>len(matches):
        return None
    sel = matches[idx-1]
    try:
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(sel)
        win32clipboard.CloseClipboard()
        info_log(f"[CLIP] 已複製: {sel[:30]}...")
        return sel
    except Exception as e:
        error_log(f"[CLIP] 複製失敗: {e}")
        return None


def quick_phrases(template_name: str = None, parent=None):
    """快速貼上預先定義的文字範本"""
    # 預設範本
    templates = {
        "email": "您好，\n\n\n此致\n敬祥",
        "signature": "-- \nU.E.P 智慧助理",
        "meeting": "會議議程：\n1. \n2. \n3. ",
        "thanks": "感謝您的協助！"
    }
    
    try:
        if not template_name:
            # 如果沒有指定，顯示選單
            from tkinter import Toplevel, Listbox, Button, SINGLE
            if not parent:
                import tkinter as tk
                parent = tk.Tk()
                parent.withdraw()
            
            dialog = Toplevel(parent)
            dialog.title("選擇範本")
            
            listbox = Listbox(dialog, selectmode=SINGLE)
            for name in templates.keys():
                listbox.insert('end', name)
            listbox.pack()
            
            selected = [None]
            def on_select():
                if listbox.curselection():
                    idx = listbox.curselection()[0]
                    selected[0] = listbox.get(idx)
                dialog.destroy()
            
            Button(dialog, text="確定", command=on_select).pack()
            dialog.wait_window()
            
            template_name = selected[0]
        
        if template_name and template_name in templates:
            text = templates[template_name]
            # 複製到剪貼簿
            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text)
            win32clipboard.CloseClipboard()
            info_log(f"[PHRASE] 已複製範本: {template_name}")
            return text
        else:
            error_log(f"[PHRASE] 未知範本: {template_name}")
            return None
    except Exception as e:
        error_log(f"[PHRASE] 快速範本失敗: {e}")
        return None


def ocr_extract(image_path: str, target_num : int = 1):

    """利用OCR辨識傳入檔案內容
        根據傳入值1 or 2決定要回傳文字還是輸出檔案"""

    import pytesseract
    import cv2
    from pathlib import Path

    file_path_obj = Path(image_path)
    image = cv2.imread(image_path)

    if image is None:
        error_log("[OCR] 圖片讀取失敗，請確認路徑與檔案格式！")
        raise ValueError("圖片讀取失敗")

    # 灰階 + 二值化，提升辨識效果
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY)

    if target_num == 1:

        try:
            text = pytesseract.image_to_string(thresh, lang = "chi_tra+eng")
            recognition_context = f"辨識結果：{text}"
            info_log("[OCR] 回傳文字辨識成功")

            return recognition_context
        except Exception as e:
            error_log(f"[OCR] OCR 辨識文字失敗：{e}")
            raise
    elif target_num == 2:
        # 寫入檔案
        try:
            data = pytesseract.image_to_pdf_or_hocr(thresh, lang = "chi_tra+eng", extension = "pdf")
            output_file_path = file_path_obj.parent / f"{file_path_obj.stem}_OCR.pdf"

            with open(output_file_path, "wb") as f:
                f.write(data)
            
            info_log(f"[OCR] OCR PDF 生成完成：{output_file_path}")
            return str(output_file_path)

        except Exception as e:
            error_log(f"[OCR] OCR 生成 PDF 失敗：：{e}")
            raise    

# def clear_history():
#     global _history, _monitoring
#     _monitoring = False
#     _history = []
#     _save_history()
#     info_log("[CLIP] 歷史已清空")
