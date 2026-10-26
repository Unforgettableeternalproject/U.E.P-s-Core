"""U.E.P 新 Core。

本套件是回聲系統改造後的新 Core 實作，位於頂層 ``uep/``。
Legacy 程式（``core/``、``modules/``、``utils/``、``devtools/``、``Entry.py``）
原地保留，快照 tag 為 ``legacy/v0.9.4-stable``，待新 Core 穩定後再移除。

邊界規則：``uep`` 不得 import 任何 legacy 頂層名稱，
由 ``tests/uep/test_import_boundary.py`` 強制檢查。
"""

__version__ = "0.0.0"
