"""
安全的檔案選擇對話框工具
使用 queue 在主線程中執行 tkinter GUI 操作，避免線程衝突
"""

import threading
import queue
from typing import Optional
from utils.debug_helper import debug_log, error_log

# 全局隊列用於主線程 GUI 操作
_gui_queue = None
_gui_queue_lock = threading.Lock()


def init_gui_queue():
    """初始化 GUI 隊列（應在主線程啟動時調用）"""
    global _gui_queue
    with _gui_queue_lock:
        if _gui_queue is None:
            _gui_queue = queue.Queue()
            debug_log(2, "[SafeFileDialog] GUI 隊列已初始化")


def open_file_dialog_sync(title: str = "請選擇檔案", 
                          filetypes: Optional[list] = None) -> Optional[str]:
    """
    在背景線程中安全地開啟檔案選擇對話框
    
    ⚠️ 注意：此函數會阻塞當前線程直到用戶完成選擇
    
    Args:
        title: 對話框標題
        filetypes: 檔案類型過濾器列表，格式: [("描述", "*.ext"), ...]
        
    Returns:
        選擇的檔案路徑，如果取消則返回 None
    """
    if filetypes is None:
        filetypes = [
            ("所有檔案", "*.*"),
            ("文字檔案", "*.txt"),
            ("Markdown", "*.md"),
            ("Python", "*.py"),
            ("JSON", "*.json"),
        ]
    
    try:
        # 🔧 使用 threading 在當前線程中創建 tkinter 對話框
        # 這會阻塞，但避免了跨線程問題
        import tkinter as tk
        from tkinter import filedialog
        
        debug_log(2, f"[SafeFileDialog] 在線程 {threading.current_thread().name} 中開啟對話框")
        
        # 創建隱藏的根窗口
        root = tk.Tk()
        root.withdraw()  # 隱藏主窗口
        
        # 設置窗口屬性避免出現在任務欄
        root.attributes('-alpha', 0.0)  # 完全透明
        root.attributes('-topmost', True)  # 置頂
        
        # 開啟檔案選擇對話框（這會阻塞直到用戶完成選擇）
        file_path = filedialog.askopenfilename(
            parent=root,
            title=title,
            filetypes=filetypes
        )
        
        # 清理
        try:
            root.quit()
            root.destroy()
        except Exception as e:
            debug_log(1, f"[SafeFileDialog] 清理窗口時出錯: {e}")
        
        if file_path:
            debug_log(2, f"[SafeFileDialog] 用戶選擇了檔案: {file_path}")
            return file_path
        else:
            debug_log(2, "[SafeFileDialog] 用戶取消了選擇")
            return None
            
    except Exception as e:
        error_log(f"[SafeFileDialog] 開啟檔案對話框失敗: {e}")
        return None


def open_folder_dialog_sync(title: str = "請選擇資料夾") -> Optional[str]:
    """
    在背景線程中安全地開啟資料夾選擇對話框
    
    ⚠️ 注意：此函數會阻塞當前線程直到用戶完成選擇
    
    Args:
        title: 對話框標題
        
    Returns:
        選擇的資料夾路徑，如果取消則返回 None
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
        
        debug_log(2, f"[SafeFileDialog] 在線程 {threading.current_thread().name} 中開啟資料夾對話框")
        
        # 創建隱藏的根窗口
        root = tk.Tk()
        root.withdraw()
        root.attributes('-alpha', 0.0)
        root.attributes('-topmost', True)
        
        # 開啟資料夾選擇對話框
        folder_path = filedialog.askdirectory(
            parent=root,
            title=title
        )
        
        # 清理
        try:
            root.quit()
            root.destroy()
        except Exception as e:
            debug_log(1, f"[SafeFileDialog] 清理窗口時出錯: {e}")
        
        if folder_path:
            debug_log(2, f"[SafeFileDialog] 用戶選擇了資料夾: {folder_path}")
            return folder_path
        else:
            debug_log(2, "[SafeFileDialog] 用戶取消了選擇")
            return None
            
    except Exception as e:
        error_log(f"[SafeFileDialog] 開啟資料夾對話框失敗: {e}")
        return None
