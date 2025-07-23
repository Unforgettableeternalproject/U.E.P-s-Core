import tkinter as tk
from tkinterdnd2 import TkinterDnD, DND_FILES
from tkinter import filedialog
import threading
import time

file_path = None

def handle_drop(event):
    """處理檔案拖曳事件，使用延遲銷毀避免檔案管理員當掉"""
    global file_path
    try:
        # 解析拖曳的檔案路徑
        dropped_files = event.data.strip('{}').split('} {')
        if dropped_files:
            file_path = dropped_files[0].strip('{}')
            
        # 更新 UI 顯示已接收檔案
        widget = event.widget
        widget.config(text=f"已接收檔案: {file_path.split('/')[-1] if file_path else 'None'}")
        widget.update()
        
        # 使用延遲銷毀，讓 Windows 有時間完成拖曳操作
        def delayed_destroy():
            time.sleep(0.5)  # 給 Windows 時間完成拖曳操作
            try:
                root = widget.winfo_toplevel()
                if root and root.winfo_exists():
                    root.quit()  # 使用 quit() 而不是 destroy()
            except:
                pass  # 忽略任何銷毀過程中的錯誤
        
        # 在背景執行延遲銷毀
        threading.Thread(target=delayed_destroy, daemon=True).start()
        
    except Exception as e:
        print(f"拖曳處理錯誤: {e}")
        # 即使出錯也要嘗試關閉視窗
        try:
            event.widget.winfo_toplevel().quit()
        except:
            pass


def open_demo_window():
    """開啟檔案拖曳視窗，改進了拖曳處理以避免檔案管理員當掉"""
    global file_path
    file_path = None  # 重置檔案路徑
    
    try:
        root = TkinterDnD.Tk()
        root.title("檔案拖曳視窗")
        root.geometry("200x100")
        root.resizable(False, False)
        
        # 設置視窗關閉事件
        def on_closing():
            try:
                root.quit()
            except:
                pass
        
        root.protocol("WM_DELETE_WINDOW", on_closing)
        
        # 創建拖曳區域
        label = tk.Label(
            root, 
            text="請拖曳檔案到這裡\n(拖曳後會自動關閉)",
            bg="lightblue",
            relief="raised",
            bd=2
        )
        label.pack(fill=tk.BOTH, expand=1, padx=5, pady=5)
        
        # 註冊拖曳事件
        label.drop_target_register(DND_FILES)
        label.dnd_bind('<<Drop>>', handle_drop)
        
        # 設置超時自動關閉 (30秒)
        def timeout_close():
            time.sleep(30)
            try:
                if root and root.winfo_exists():
                    root.quit()
            except:
                pass
        
        threading.Thread(target=timeout_close, daemon=True).start()
        
        # 執行主循環
        root.mainloop()
        
        # 清理資源
        try:
            root.destroy()
        except:
            pass
            
    except Exception as e:
        print(f"視窗創建錯誤: {e}")
        file_path = None
    
    return file_path

def open_folder_dialog():
    """
    開啟資料夾選擇對話框，讓使用者選擇一個目標資料夾
    
    Returns:
        選擇的資料夾路徑 (如果使用者沒有選擇則返回None)
    """
    root = tk.Tk()
    root.withdraw()  # 隱藏主窗口
    folder_path = filedialog.askdirectory(title="請選擇目標資料夾")
    
    # 如果使用者取消選擇，返回None
    if not folder_path:
        return None
    
    return folder_path

if __name__ == "__main__":
    file_path = open_demo_window()
    if file_path:
        print(f"拖曳的檔案路徑：{file_path}")
    else:
        print("沒有拖曳任何檔案")