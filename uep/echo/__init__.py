"""Echo Stream 轉接層（P3接入方案 §1）：ES 已併入 Core 的 ``uep.stream``（N1），本套件是 Host 與 Core 的轉接層；子系統只允許 ``*_wiring.py`` 與 ``uep/stream/adapters/`` import（邊界測試鎖住）。

刻意不在 ``__init__`` 匯入子模組：``uep.echo.services`` 等不依賴 echo_stream 的部分要能在沒有安裝 ES 的
環境被組裝層 import；用到 ES 的是 ``host``、``sink``、``policy``、``outcomes``。
"""
