import threading
import time
import json
import os
import win32clipboard
import difflib
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

def clipboard_tracker(keyword: str = "", max_results: int = 5, copy_index: int = -1) -> dict:
    """搜尋剪貼簿歷史
    
    Args:
        keyword: 搜尋關鍵字（空字串則返回全部歷史）
        max_results: 最大結果數量
        copy_index: 要複製的項目索引（-1 表示不複製，只返回結果）
        
    Returns:
        dict: {
            "status": "ok" | "error",
            "results": [搜尋結果列表],
            "copied": 已複製的內容（如果有）
        }
    """
    try:
        if not keyword:
            # 返回最近的歷史
            results = _history[-max_results:] if len(_history) > max_results else _history[:]
            info_log(f"[CLIP] 返回 {len(results)} 條歷史記錄")
        else:
            # 模糊搜尋
            results = difflib.get_close_matches(keyword, _history, n=max_results, cutoff=0.1)
            if not results:
                info_log("[CLIP] 無相關記錄")
                return {"status": "ok", "results": [], "message": "無相關記錄"}
            info_log(f"[CLIP] 找到 {len(results)} 條相關記錄")
        
        # 如果指定了複製索引
        if copy_index >= 0 and copy_index < len(results):
            selected = results[copy_index]
            try:
                win32clipboard.OpenClipboard()
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, selected)
                win32clipboard.CloseClipboard()
                info_log(f"[CLIP] 已複製: {selected[:30]}...")
                return {
                    "status": "ok",
                    "results": results,
                    "copied": selected
                }
            except Exception as e:
                error_log(f"[CLIP] 複製失敗: {e}")
                return {
                    "status": "error",
                    "results": results,
                    "message": f"複製失敗: {str(e)}"
                }
        
        return {"status": "ok", "results": results}
        
    except Exception as e:
        error_log(f"[CLIP] 剪貼簿追蹤失敗: {e}")
        return {"status": "error", "message": str(e)}


def quick_phrases(template_name: str = "", copy_to_clipboard: bool = False, custom_prompt: str = "") -> dict:
    """快速取得預先定義的文字範本或使用 LLM 生成自訂範本
    
    Args:
        template_name: 範本名稱（空字串則返回所有範本列表）
        copy_to_clipboard: 是否複製到剪貼簿
        custom_prompt: 自訂提示詞，若提供則使用 LLM 生成範本
        
    Returns:
        dict: {
            "status": "ok" | "error",
            "templates": {所有範本} (當 template_name 為空時),
            "template_name": 範本名稱,
            "content": 範本內容,
            "copied": 是否已複製,
            "generated": 是否為 LLM 生成（僅自訂範本）
        }
    """
    # 預設範本
    templates = {
        "email": "您好，\n\n\n此致\n敬祥",
        "signature": "-- \nU.E.P 智慧助理",
        "meeting": "會議議程：\n1. \n2. \n3. ",
        "thanks": "感謝您的協助！",
        "greeting": "您好，很高興為您服務！",
        "apology": "很抱歉造成您的不便，我們會盡快處理。",
        "followup": "關於之前討論的事項，請問有任何進展嗎？"
    }
    
    try:
        # 如果提供了自訂提示詞，使用 LLM 生成範本
        if custom_prompt:
            info_log(f"[PHRASE] 使用 LLM 生成自訂範本: {custom_prompt[:50]}...")
            try:
                # 導入 LLM 模組
                from modules.llm_module.llm_module import LLMModule
                llm_module = LLMModule()
                
                # 建構 LLM 請求（prompt 必須是英文）
                generation_prompt = f"""Generate a text template based on the following requirements (respond in Traditional Chinese):

{custom_prompt}

Please provide the template content directly without any additional explanation. The template should:
1. Be written in Traditional Chinese (繁體中文)
2. Have clear and readable formatting
3. Meet professional communication standards
4. Be ready to use immediately"""
                
                # 構建符合 LLMInput 格式的請求
                request_data = {
                    "text": generation_prompt,
                    "intent": "chat",
                    "is_internal": True  # 內部調用模式
                }
                
                # 調用 LLM 模組
                result = llm_module.handle(request_data)
                
                if result and result.get("status") == "ok":
                    generated_text = result.get("text", "").strip()
                    info_log(f"[PHRASE] LLM 生成完成，長度: {len(generated_text)}")
                    
                    # 如果需要複製到剪貼簿
                    if copy_to_clipboard:
                        try:
                            win32clipboard.OpenClipboard()
                            win32clipboard.EmptyClipboard()
                            win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, generated_text)
                            win32clipboard.CloseClipboard()
                            info_log(f"[PHRASE] 已複製 LLM 生成的範本")
                            return {
                                "status": "ok",
                                "template_name": "custom",
                                "content": generated_text,
                                "copied": True,
                                "generated": True
                            }
                        except Exception as e:
                            error_log(f"[PHRASE] 複製失敗: {e}")
                            return {
                                "status": "ok",
                                "template_name": "custom",
                                "content": generated_text,
                                "copied": False,
                                "generated": True,
                                "message": f"複製失敗: {str(e)}"
                            }
                    
                    return {
                        "status": "ok",
                        "template_name": "custom",
                        "content": generated_text,
                        "copied": False,
                        "generated": True
                    }
                else:
                    error_log(f"[PHRASE] LLM 生成失敗: {result}")
                    return {
                        "status": "error",
                        "message": "LLM 生成範本失敗",
                        "details": result
                    }
                    
            except Exception as e:
                error_log(f"[PHRASE] LLM 調用失敗: {e}")
                return {
                    "status": "error",
                    "message": f"LLM 調用失敗: {str(e)}"
                }
        
        # 如果沒有指定範本名稱，返回所有範本
        if not template_name:
            info_log(f"[PHRASE] 返回 {len(templates)} 個範本")
            return {
                "status": "ok",
                "templates": templates
            }
        
        # 檢查範本是否存在
        if template_name not in templates:
            error_log(f"[PHRASE] 未知範本: {template_name}")
            available = ", ".join(templates.keys())
            return {
                "status": "error",
                "message": f"未知範本: {template_name}",
                "available_templates": list(templates.keys())
            }
        
        text = templates[template_name]
        
        # 如果需要複製到剪貼簿
        if copy_to_clipboard:
            try:
                win32clipboard.OpenClipboard()
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
                win32clipboard.CloseClipboard()
                info_log(f"[PHRASE] 已複製範本: {template_name}")
                return {
                    "status": "ok",
                    "template_name": template_name,
                    "content": text,
                    "copied": True,
                    "generated": False
                }
            except Exception as e:
                error_log(f"[PHRASE] 複製失敗: {e}")
                return {
                    "status": "error",
                    "template_name": template_name,
                    "content": text,
                    "copied": False,
                    "generated": False,
                    "message": f"複製失敗: {str(e)}"
                }
        
        info_log(f"[PHRASE] 返回範本: {template_name}")
        return {
            "status": "ok",
            "template_name": template_name,
            "content": text,
            "copied": False,
            "generated": False
        }
        
    except Exception as e:
        error_log(f"[PHRASE] 快速範本失敗: {e}")
        return {"status": "error", "message": str(e)}


def ocr_extract(image_path: str, target_num : int = 1):

    """利用OCR辨識傳入檔案內容
        根據傳入值1 or 2決定要回傳文字還是輸出檔案"""

    import pytesseract
    import cv2
    from pathlib import Path
    import os
    
    # 設定 Tesseract 路徑（Windows）
    if os.name == 'nt':
        tesseract_path = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        if os.path.exists(tesseract_path):
            pytesseract.pytesseract.tesseract_cmd = tesseract_path

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
