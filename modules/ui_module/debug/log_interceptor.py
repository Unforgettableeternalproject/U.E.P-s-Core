# debug/log_interceptor.py
"""
Log Interceptor Module

日誌截取器
用於將系統日誌重定向到除錯介面
"""

import os
import sys
import logging
import datetime
from typing import Dict, Any, Optional, List, Callable
import queue
import threading
import time

# 添加項目根目錄到 Python 路徑
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(script_dir, '..', '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from utils.debug_helper import debug_log, info_log, error_log


class LogInterceptor(logging.Handler):
    """
    日誌截取處理器
    截取系統日誌並將其重定向到日誌查看器
    """
    
    def __init__(self, max_queue_size=1000):
        """初始化截取器"""
        super().__init__()
        # 設置日誌格式
        self.formatter = logging.Formatter('[%(asctime)s] %(levelname)s - %(message)s')
        
        # 日誌緩衝區
        self.log_queue = queue.Queue(maxsize=max_queue_size)
        self.callbacks = []
        self.running = True
        
        # 啟動處理線程
        self.process_thread = threading.Thread(target=self._process_logs, daemon=True)
        self.process_thread.start()
        
        debug_log(1, "[LogInterceptor] 初始化日誌截取器完成")
    
    def emit(self, record):
        """處理日誌記錄"""
        try:
            # 格式化日誌消息
            formatted = self.formatter.format(record)
            
            # 添加到隊列
            try:
                # 創建日誌條目
                timestamp = datetime.datetime.fromtimestamp(record.created)
                timestamp_str = timestamp.strftime('%Y-%m-%d %H:%M:%S')
                
                # 確保換行符被正確處理
                message = record.getMessage()
                
                log_entry = {
                    'timestamp': timestamp,
                    'timestamp_str': timestamp_str,
                    'level': record.levelname,
                    'message': message,
                    'formatted': formatted,
                    'logger': record.name,
                    'pathname': record.pathname,
                    'lineno': record.lineno
                }
                
                # 如果隊列已滿，移除最舊的
                if self.log_queue.full():
                    try:
                        self.log_queue.get_nowait()
                    except queue.Empty:
                        pass
                
                # 添加新的日誌
                self.log_queue.put_nowait(log_entry)
            except queue.Full:
                pass  # 隊列滿了，丟棄此日誌
                
        except Exception as e:
            # 在標準錯誤輸出中報告此處理器的問題
            print(f"LogInterceptor 處理日誌時出錯: {e}", file=sys.stderr)
    
    def _process_logs(self):
        """處理日誌隊列"""
        while self.running:
            try:
                # 如果隊列不為空
                if not self.log_queue.empty():
                    # 獲取所有當前日誌
                    logs = []
                    while not self.log_queue.empty():
                        try:
                            logs.append(self.log_queue.get_nowait())
                            self.log_queue.task_done()
                        except queue.Empty:
                            break
                    
                    # 通知所有回調
                    if logs:
                        for callback in list(self.callbacks):
                            try:
                                callback(logs)
                            except Exception as e:
                                print(f"日誌回調執行錯誤: {e}", file=sys.stderr)
            
            except Exception as e:
                print(f"處理日誌隊列時出錯: {e}", file=sys.stderr)
            
            # 避免CPU占用過高
            time.sleep(0.1)
    
    def add_callback(self, callback: Callable):
        """添加日誌回調函數"""
        if callback not in self.callbacks:
            self.callbacks.append(callback)
    
    def remove_callback(self, callback: Callable):
        """移除日誌回調函數"""
        if callback in self.callbacks:
            self.callbacks.remove(callback)
    
    def stop(self):
        """停止處理線程"""
        self.running = False
        if hasattr(self, 'process_thread') and self.process_thread.is_alive():
            self.process_thread.join(1.0)  # 等待最多1秒
    
    def __del__(self):
        """析構函數"""
        self.stop()


# 單例模式
_log_interceptor = None

def get_log_interceptor():
    """獲取日誌截取器單例"""
    global _log_interceptor
    if _log_interceptor is None:
        _log_interceptor = LogInterceptor()
    return _log_interceptor


def install_interceptor():
    """安裝日誌截取器到全局日誌系統"""
    try:
        # 獲取日誌截取器
        interceptor = get_log_interceptor()
        
        # 獲取 U.E.P 主日誌器
        logger = logging.getLogger("UEP")
        
        # 檢查是否已經安裝
        for handler in logger.handlers:
            if isinstance(handler, LogInterceptor):
                debug_log(1, "[LogInterceptor] 日誌截取器已經安裝，跳過")
                return True
        
        # 添加截取器
        logger.addHandler(interceptor)
        
        debug_log(1, "[LogInterceptor] 日誌截取器已安裝到主日誌系統")
        return True
    except Exception as e:
        print(f"安裝日誌截取器時出錯: {e}", file=sys.stderr)
        return False


def uninstall_interceptor():
    """從全局日誌系統移除日誌截取器"""
    try:
        # 獲取日誌截取器
        interceptor = get_log_interceptor()
        
        # 獲取 U.E.P 主日誌器
        logger = logging.getLogger("UEP")
        
        # 移除截取器
        if interceptor in logger.handlers:
            logger.removeHandler(interceptor)
        
        # 停止截取器
        interceptor.stop()
        
        debug_log(1, "[LogInterceptor] 日誌截取器已從主日誌系統移除")
        return True
    except Exception as e:
        print(f"移除日誌截取器時出錯: {e}", file=sys.stderr)
        return False
