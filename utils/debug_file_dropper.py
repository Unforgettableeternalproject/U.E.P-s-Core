import tkinter as tk
from tkinterdnd2 import TkinterDnD, DND_FILES
from tkinter import filedialog

file_path = None

def handle_drop(event):
    global file_path
    file_path = event.data.strip("{}")
    event.widget.winfo_toplevel().destroy()


def open_demo_window():
    global file_path
    root = TkinterDnD.Tk()
    root.title("檔案拖曳測試")
    root.geometry("100x100")
    label = tk.Label(root, text="請拖曳檔案到這裡")
    label.pack(fill=tk.BOTH, expand=1)
    label.drop_target_register(DND_FILES)
    label.dnd_bind('<<Drop>>', handle_drop)
    root.mainloop()
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