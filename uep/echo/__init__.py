"""Echo Stream 轉接層（P3接入方案 §1）：只有這個套件可以 import ``echo_stream``（邊界測試鎖住）。

刻意不在 ``__init__`` 匯入子模組：``uep.echo.services`` 等不依賴 echo_stream 的部分要能在沒有安裝 ES 的
環境被組裝層 import；用到 ES 的是 ``host``、``sink``、``policy``、``outcomes``。
"""
