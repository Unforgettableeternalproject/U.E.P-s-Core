"""
Pytest conftest — 全域 fixture 與清理

已知問題：所有測試檔一起跑時，pytest 退出可能 hang。
原因是 torch / pyannote import 建立的 non-daemon thread 在 test session
之間累積，pytest 退出時等待這些 thread 而卡住。

個別測試檔案 (`pytest tests/test_xxx.py`) 全部正常通過。

Workaround: 使用 test.bat 的 per-file 模式，
或在 CI 中用 `pytest --forked`（需要 pytest-forked）。
"""
