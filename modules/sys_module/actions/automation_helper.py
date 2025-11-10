import sqlite3
import threading
import time
import os
from datetime import datetime
from pathlib import Path
from utils.debug_helper import info_log, error_log

# 將資料庫放在 memory 目錄中
_DB_DIR = Path(__file__).parent.parent.parent.parent / "memory"
_DB_DIR.mkdir(exist_ok=True)
_DB = str(_DB_DIR / "uep_tasks.db")

def _init_db():
    conn = sqlite3.connect(_DB)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS reminders (
      id INTEGER PRIMARY KEY,
      time TEXT NOT NULL,
      message TEXT NOT NULL
    )""")
    conn.commit()
    conn.close()

_init_db()

def set_reminder(dt: datetime, message: str):
    """新增提醒"""
    try:
        conn = sqlite3.connect(_DB)
        conn.execute("INSERT INTO reminders (time, message) VALUES (?, ?)",
                     (dt.isoformat(), message))
        conn.commit()
        conn.close()
        info_log(f"[AUTO] 設定提醒：{dt} -> {message}")
    except Exception as e:
        error_log(f"[AUTO] 設定提醒失敗: {e}")

def _checker_loop():
    while True:
        now = datetime.utcnow().isoformat()
        conn = sqlite3.connect(_DB)
        c = conn.cursor()
        for row in c.execute("SELECT id, time, message FROM reminders WHERE time<=?", (now,)):
            _, t, msg = row
            info_log(f"[AUTO] 提醒觸發：{msg}")
            c.execute("DELETE FROM reminders WHERE id=?", (row[0],))
        conn.commit()
        conn.close()
        time.sleep(30)

# 啟動背景檢查
threading.Thread(target=_checker_loop, daemon=True).start()

def generate_backup_script(target_folder: str, dest_folder: str, output_path: str):
    """產生備份腳本 (.bat / .sh)"""
    try:
        if os.name == "nt":
            content = f'xcopy /E /I "{target_folder}" "{dest_folder}"'
            suffix = ".bat"
        else:
            content = f'#!/bin/bash\nrsync -a "{target_folder}/" "{dest_folder}/"'
            suffix = ".sh"
        p = os.path.splitext(output_path)[0] + suffix
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        info_log(f"[AUTO] 備份腳本已生成：{p}")
        return p
    except Exception as e:
        error_log(f"[AUTO] 生成腳本失敗: {e}")
        return None

def monitor_folder(path: str, callback, interval: int = 10):
    """監控資料夾變更"""
    def _loop():
        prev = set(os.listdir(path))
        while True:
            curr = set(os.listdir(path))
            added = curr - prev
            if added:
                for f in added:
                    info_log(f"[AUTO] 資料夾新增：{f}")
                    callback(f)
            prev = curr
            time.sleep(interval)
    threading.Thread(target=_loop, daemon=True).start()
