import tkinter as tk
from tkinterdnd2 import TkinterDnD, DND_FILES

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

if __name__ == "__main__":
    file_path = open_demo_window()
    if file_path:
        print(f"拖曳的檔案路徑：{file_path}")
    else:
        print("沒有拖曳任何檔案")