"""transformers 第一次 import 的共用鎖。

transformers 的延遲載入（``_LazyModule``）不是執行緒安全的：兩個執行緒同時第一次 import 它（或 sentence-transformers）
會得到 ``ImportError: cannot import name 'is_torch_npu_available' from 'transformers'`` 之類的半初始化錯誤。
組裝期同時有 Laya 載入執行緒、記憶 embedding 暖機（主執行緒）與翻譯器各自第一次 import，
一律在這把鎖內完成「只 import、不載模型」的步驟，模型權重載入留在鎖外以保留並行。
鎖是 ``RLock``，同一執行緒重入安全。
"""
from __future__ import annotations

import threading

transformers_import_lock = threading.RLock()
