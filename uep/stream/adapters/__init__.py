"""真模組的 adapter（原 ES ``adapters/``，未重寫版）。

**這是 ``uep/stream/`` 內唯一允許 import 子系統的地方**（``tests/uep/test_import_boundary.py`` 鎖住）。
``config.py`` 是自 ES 搬入的過渡設定解析（環境變數與 ``.env``），F1 併入 ``UepSettings`` 後移除。
"""
